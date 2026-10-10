"""Research schema normalization, RAM-only filters, and publication snapshots."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import polars as pl

from comm_research.dashboard.server import CaisoMemoryLake


def write_research_fixture(root: Path, count=2300):
    start = datetime(2023, 1, 1, tzinfo=timezone.utc)
    times = [start + timedelta(minutes=5 * i) for i in range(count)]
    cr = [
        {
            "interval_start_time_gmt": t,
            "apnode_id": node,
            "apnode_id_price": float(i + offset),
            "market_name": "AUCTION",
            "market_term": term,
            "time_of_use": tou,
            "xml_data_item": "ON_PRC" if tou == "ON" else "OFF_PRC",
            "as_of_date": "2023-01-15",
        }
        for i, t in enumerate(times)
        for node in ("NP15", "SP15")
        for term in ("Monthly", "Seasonal")
        for tou, offset in (("ON", 0), ("OFF", 10))
    ]
    tr = [
        {
            "interval_start_time_gmt": t,
            "ti_id": "PATH",
            "ti_direction": direction,
            "market_run_id": market,
            "ti_constraint_id": "CONSTRAINT",
            "xml_data_item": item,
            "tr_type": kind,
            "mw": float(value),
        }
        for i, t in enumerate(times)
        for market in ("RTPD", "HASP")
        for direction in ("E", "W")
        for item, kind, value in [
            ("USEAGE_MW", "TRNS_TR_USEAGE", i % 100),
            ("ATC_MW", "RATING_ATC", 200),
        ]
    ]
    ancillary = [
        {
            "interval_start_time_gmt": t,
            "market_run_id": market,
            "anc_region": region,
            "anc_type": kind,
            "xml_data_item": f"{kind}_REQ_{measure}_MW",
            "mw": float(value),
        }
        for t in times
        for market in ("DAM", "RTM")
        for region in ("AS_CAISO", "AS_SP26_EXP")
        for kind, value in [("NR", 10), ("RD", 20), ("RU", 30), ("SR", 40)]
        for measure in ("MIN", "MAX")
    ]
    for name, rows in [
        ("crr_bids", cr),
        ("transmission_usage", tr),
        ("as_req", ancillary),
    ]:
        pl.DataFrame(rows).write_parquet(root / (name + ".parquet"))


class ResearchLakeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        write_research_fixture(self.root)
        self.lake = CaisoMemoryLake(self.root)

    def tearDown(self):
        self.lake.close()
        self.temporary.cleanup()

    def test_exact_schemas_flat_files_and_registered_tabs(self):
        research = self.lake.research
        for feed, entity in [
            ("crr", "apnode_id"),
            ("transmission", "ti_id"),
            ("ancillary", "anc_region"),
        ]:
            self.assertEqual(research.metadata[feed]["entity"], entity)
            self.assertGreater(research.metadata[feed]["observations"], 0)
        self.assertEqual(
            sum(
                item.get("transport") == "research-history"
                for item in self.lake.datasets
            ),
            3,
        )
        indexes = self.lake.con.execute(
            "SELECT index_name FROM duckdb_indexes()"
        ).fetchall()
        self.assertEqual(sum("research_" in item[0] for item in indexes), 6)

    def test_date_and_dimension_filters_and_point_cap(self):
        result = self.lake.research.series(
            "transmission",
            "PATH",
            "2023-01-01T00:00:00Z",
            "2023-01-01T01:00:00Z",
            {
                "market_run_id": ["HASP"],
                "ti_direction": ["W"],
                "measure": ["USEAGE_MW"],
            },
        )
        self.assertEqual(len(result["series"]), 1)
        trace = result["series"][0]
        self.assertEqual(len(trace["timestamps"]), 12)
        self.assertEqual(trace["values"], list(range(12)))
        complete = self.lake.research.series("crr", "NP15", max_points=0)
        self.assertEqual(len(complete["series"]), 4)
        self.assertEqual(len(complete["series"][0]["values"]), 2300)
        bounded = self.lake.research.series("crr", "NP15", max_points=100)
        for trace in bounded["series"]:
            self.assertLessEqual(len(trace["values"]), 100)
            self.assertEqual(trace["sourceCount"], 2300)
            self.assertEqual(
                trace["timestamps"][0], complete["series"][0]["timestamps"][0]
            )
            self.assertEqual(
                trace["timestamps"][-1], complete["series"][0]["timestamps"][-1]
            )

    def test_as_excludes_rtm_and_keeps_minimum_maximum_separate(self):
        meta = self.lake.research.metadata["ancillary"]
        self.assertEqual(meta["options"]["measure"], ["MAXIMUM", "MINIMUM"])
        result = self.lake.research.series(
            "ancillary", "AS_CAISO", filters={"measure": ["MINIMUM"]}, max_points=0
        )
        self.assertEqual(len(result["series"]), 4)
        self.assertEqual(sum(trace["values"][0] for trace in result["series"]), 100)
        self.assertEqual(meta["observations"], 2300 * 2 * 4 * 2)

    def test_ram_queries_survive_source_deletion_and_reject_injection(self):
        for path in self.root.glob("*.parquet"):
            path.unlink()
        with patch.object(Path, "rglob", side_effect=AssertionError("disk scan")):
            self.assertEqual(self.lake.research.series("crr", "NP15")["status"], "ok")
            self.assertEqual(
                self.lake.research.series("crr", "NP15'; DROP TABLE research_crr;--")[
                    "status"
                ],
                "empty",
            )
        for args in [
            {"key": "bad"},
            {"key": "crr", "filters": {"bad;drop": ["x"]}},
            {"key": "crr", "filters": {"market_term": "Monthly"}},
            {"key": "crr", "start": "bad"},
            {"key": "crr", "start": "2024-01-02", "end": "2024-01-01"},
            {"key": "crr", "max_points": 1501},
        ]:
            with self.assertRaises(ValueError):
                self.lake.research.series(**args)
        self.assertEqual(self.lake.research.series("crr", "missing")["status"], "empty")
        self.assertEqual(
            self.lake.research.series("crr", "NP15", filters={"market_term": []})[
                "status"
            ],
            "empty",
        )

    def test_failed_research_rescan_preserves_previous_tables_and_metadata(self):
        previous = self.lake.research
        pl.DataFrame({"wrong": [1]}).write_parquet(self.root / "as_req.parquet")
        with self.assertRaises(ValueError):
            self.lake.preload()
        self.assertIs(self.lake.research, previous)
        self.assertEqual(previous.series("ancillary", "AS_CAISO")["status"], "ok")

    def test_crr_latest_snapshot_is_not_summed(self):
        path = self.root / "crr_bids.parquet"
        frame = pl.read_parquet(path)
        correction = frame.head(1).with_columns(
            pl.lit(123.0).alias("apnode_id_price"),
            pl.lit("2023-01-16").alias("as_of_date"),
        )
        pl.concat([frame, correction]).write_parquet(path)
        self.lake.preload()
        result = self.lake.research.series(
            "crr",
            "NP15",
            filters={"market_term": ["Monthly"], "time_of_use": ["ON"]},
            max_points=0,
        )
        self.assertEqual(result["series"][0]["values"][0], 123)
        self.assertEqual(len(result["series"][0]["values"]), 2300)
