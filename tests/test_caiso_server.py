"""Startup snapshot correctness: no filesystem access after preloading."""

from __future__ import annotations

import gzip
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import duckdb
import polars as pl

from comm_research.dashboard.server import CaisoMemoryLake, resolve_market


class CaisoMemoryLakeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.start = datetime(2023, 7, 8, 7, tzinfo=timezone.utc)
        for feed, column, market in (
            ("dam_lmp", "mw", "DAM"),
            ("rtm_lmp", "value", "RTM"),
        ):
            path = self.root / f"power_gas/napg/caiso/{feed}/year=2023/month=07/day=08"
            path.mkdir(parents=True)
            rows = [
                {
                    "interval_start_time_gmt": self.start + timedelta(minutes=i * 5),
                    "node": node,
                    "lmp_type": component,
                    column: float(i + offset),
                    "market_run_id": market,
                }
                for i in range(2000)
                for node in ("TH_NP15_GEN-APND", "TH_SP15_GEN-APND")
                for component, offset in (("LMP", 0), ("MCE", -2), ("MCC", 3))
            ]
            pl.DataFrame(rows).write_parquet(path / "data.parquet")
        self.lake = CaisoMemoryLake(self.root)

    def tearDown(self):
        self.lake.close()
        self.temporary.cleanup()

    def test_complete_long_format_history_and_missing_components(self):
        for feed in ("DAM", "RTM", "power_gas/napg/caiso/rtm_lmp"):
            raw, compressed = self.lake.series(feed, "TH_SP15_GEN-APND")
            result = json.loads(raw)
            self.assertEqual(result["timestamps"][0], "2023-07-08T07:00:00Z")
            self.assertEqual(
                len(result["timestamps"]), 2000
            )  # No old 1,500-row API truncation.
            self.assertEqual(result["energy"][0], -2)
            self.assertEqual(result["congestion"][0], 3)
            self.assertEqual(result["loss"], [None] * 2000)
            self.assertEqual(gzip.decompress(compressed), raw)

    def test_missing_node_fallback_and_sql_injection_is_only_a_value(self):
        for node in (None, "MISSING", "'; DROP TABLE caiso_rtm;--"):
            result = json.loads(self.lake.series("RTM", node)[0])
            self.assertEqual(result["node"], "TH_NP15_GEN-APND")
        self.assertEqual(
            self.lake.con.execute("SELECT count(*) FROM caiso_rtm").fetchone()[0], 12000
        )
        for feed in (
            "some-dam",
            "caiso_dam;DROP TABLE caiso_rtm",
            "oil/rtm_lmp",
            "FMM",
        ):
            with self.assertRaises(ValueError):
                resolve_market(feed)

    def test_queries_survive_deleted_sources_with_no_disk_access(self):
        for file in self.root.rglob("*.parquet"):
            file.unlink()
        with patch(
            "comm_research.dashboard.server.discover_lake",
            side_effect=AssertionError("disk scan"),
        ):
            with ThreadPoolExecutor(max_workers=8) as pool:
                responses = list(
                    pool.map(
                        lambda _: self.lake.series("RTM", "TH_SP15_GEN-APND"), range(16)
                    )
                )
            self.assertTrue(
                all(response[0] is responses[0][0] for response in responses)
            )
            self.assertEqual(len(json.loads(responses[0][0])["timestamps"]), 2000)

    def test_empty_snapshot_and_explicit_rescan(self):
        for file in self.root.rglob("*.parquet"):
            file.unlink()
        self.lake.preload()
        result = json.loads(self.lake.series("DAM", None)[0])
        self.assertEqual(result["status"], "empty")
        self.assertEqual(result["timestamps"], [])
        self.assertEqual(self.lake.nodes["DAM"], [])

    def test_failed_rescan_keeps_the_previous_snapshot(self):
        path = self.root / "power_gas/napg/caiso/rtm_lmp/broken.parquet"
        pl.DataFrame({"bad": [1]}).write_parquet(path)
        # A schema union cannot fail on a sparse file; use a corrupt parquet.
        path.write_bytes(b"broken")
        with (
            self.assertLogs("caiso.workstation", level="ERROR"),
            self.assertRaises(duckdb.Error),
        ):
            self.lake.preload()
        self.assertEqual(
            len(json.loads(self.lake.series("DAM", None)[0])["timestamps"]), 2000
        )

    def test_market_filter_and_mixed_schema_files(self):
        path = self.root / "power_gas/napg/caiso/dam_lmp/legacy.parquet"
        pl.DataFrame(
            {
                "INTERVAL_START_TIME_GMT": [self.start + timedelta(days=30)] * 2,
                "NODE": ["TH_NP15_GEN-APND"] * 2,
                "LMP_TYPE": ["LMP"] * 2,
                "value": [123.0, 999.0],
                "MARKET_RUN_ID": ["DAM", "RTM"],
            }
        ).write_parquet(path)
        self.lake.preload()
        result = json.loads(self.lake.series("DAM", "TH_NP15_GEN-APND")[0])
        self.assertEqual(len(result["timestamps"]), 2001)
        self.assertEqual(result["lmp"][-1], 123.0)

    def test_legacy_rows_without_market_column_survive_a_schema_union(self):
        path = self.root / "power_gas/napg/caiso/dam_lmp/legacy.parquet"
        pl.DataFrame(
            {
                "interval_start_time_gmt": [self.start + timedelta(days=30)],
                "node": ["TH_NP15_GEN-APND"],
                "lmp_type": ["LMP"],
                "value": [123.0],
            }
        ).write_parquet(path)
        self.lake.preload()
        result = json.loads(self.lake.series("DAM", None)[0])
        self.assertEqual(result["lmp"][-1], 123.0)
        self.assertEqual(self.lake.datasets[0]["preview"]["sourceCount"], 2001)

    def test_failed_preview_generation_rolls_back_tables_and_response_cache(self):
        previous = self.lake.series("DAM", None)
        with (
            patch.object(
                self.lake,
                "series",
                side_effect=duckdb.InvalidInputException("preview failed"),
            ),
            self.assertLogs("caiso.workstation", level="ERROR"),
            self.assertRaises(duckdb.Error),
        ):
            self.lake.preload()
        self.assertIs(self.lake.series("DAM", None)[0], previous[0])
        self.assertEqual(
            self.lake.con.execute("SELECT count(*) FROM caiso_dam").fetchone()[0], 12000
        )


if __name__ == "__main__":
    unittest.main()
