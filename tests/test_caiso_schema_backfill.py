"""Legacy schemas, RTM fallbacks, and direct CLI behavior."""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

import polars as pl

from comm_research.dashboard.data import loader
from comm_research.infra.tools.caiso_schema import normalize_schema
from comm_research.infra.tools.lake_methods.writer_lock import writer_lock

SPEC = importlib.util.spec_from_file_location(
    "fetch_caiso_cli", Path(__file__).resolve().parents[1] / "scripts/fetch_caiso.py"
)
cli = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = cli
SPEC.loader.exec_module(cli)


class SchemaTests(unittest.TestCase):
    def test_operating_hour_and_subhour_fallbacks_are_utc(self):
        for feed, interval, expected in (
            ("dam_lmp", 0, 23 * 60),
            ("rtm_lmp", 12, 23 * 60 + 55),
            ("fmm_lmp", 4, 23 * 60 + 45),
        ):
            raw = pl.DataFrame(
                {
                    "OPR_DT": ["2026-09-01"],
                    "OPR_HR": [24],
                    "OPR_INTERVAL": [interval],
                    "XML_DATA": ["LMP_CONG_PRC"],
                    "VALUE": ["2.5"],
                    "PNODE": ["SP15"],
                }
            )
            rows = normalize_schema(raw.lazy(), feed).collect()
            self.assertEqual(
                rows["timestamp"][0],
                datetime(2026, 9, 1, 7, tzinfo=timezone.utc)
                + timedelta(minutes=expected),
            )
            self.assertEqual(rows["lmp_type"][0], "CONG")
            self.assertEqual(rows["value"].dtype, pl.Float64)
            self.assertEqual(rows["node"][0], "SP15")

    def test_market_selects_fmm_resolution_and_rejects_invalid_intervals(self):
        raw = pl.DataFrame(
            {
                "OPR_DT": ["2026-09-01"],
                "OPR_HR": [1],
                "OPR_INTERVAL": [4],
                "MARKET_RUN_ID": ["RTPD"],
                "VALUE": [1.0],
            }
        )
        rows = normalize_schema(raw.lazy(), "rtm_lmp").collect()
        self.assertEqual(
            rows["timestamp"][0], datetime(2026, 9, 1, 7, 45, tzinfo=timezone.utc)
        )
        for changes in ({"OPR_HR": 25}, {"OPR_INTERVAL": 5}):
            invalid = raw.with_columns(
                pl.lit(value).alias(key) for key, value in changes.items()
            )
            with self.assertRaises(pl.exceptions.InvalidOperationError):
                normalize_schema(invalid.lazy(), "rtm_lmp").collect()

    def test_explicit_gmt_overrides_counters_and_handles_offset_strings(self):
        raw = pl.DataFrame(
            {
                "INTERVALSTARTTIME_GMT": ["2026-09-01T00:05:00-07:00"],
                "OPR_DT": ["2026-09-01"],
                "OPR_HR": [24],
                "OPR_INTERVAL": [12],
                "LMP_TYPE": ["MCE"],
                "VALUE": [1.0],
            }
        )
        rows = normalize_schema(raw.lazy(), "rtm_lmp").collect()
        self.assertEqual(
            rows["timestamp"][0], datetime(2026, 9, 1, 7, 5, tzinfo=timezone.utc)
        )
        self.assertEqual(rows["lmp_type"][0], "ENERGY")

    def test_interval_index_from_pacific_midnight_and_dst_ambiguity(self):
        raw = pl.DataFrame(
            {
                "OPR_DT": ["2026-09-01"],
                "INTERVAL_NUM": [288],
                "VALUE": [1.0],
                "LMP_TYPE": ["MCL"],
            }
        )
        rows = normalize_schema(raw.lazy(), "rtm_lmp").collect()
        self.assertEqual(
            rows["timestamp"][0], datetime(2026, 9, 2, 6, 55, tzinfo=timezone.utc)
        )
        ambiguous = pl.DataFrame(
            {
                "OPR_DT": ["2025-11-02"],
                "OPR_HR": [2],
                "OPR_INTERVAL": [1],
                "VALUE": [1.0],
            }
        )
        with self.assertRaises(pl.exceptions.ComputeError):
            normalize_schema(ambiguous.lazy(), "rtm_lmp").collect()

    def test_mixed_case_files_wide_prices_and_empty_default_horizon(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            feed = root / "power_gas/napg/caiso/rtm_lmp"
            feed.mkdir(parents=True)
            raw = pl.DataFrame(
                {
                    "INTERVALSTARTTIME_GMT": ["2023-07-08T07:00:00Z"],
                    "NODE": ["SP15"],
                    "LMP": ["25.0"],
                    "ENERGY": ["23.0"],
                    "CONGESTION": ["3.0"],
                    "LOSSES": ["-1.0"],
                }
            )
            raw.write_ipc(feed / "old.arrow")
            raw.rename({c: c.lower() for c in raw.columns}).write_parquet(
                feed / "old.parquet"
            )
            loader.clear_caches()
            dataset = loader.discover_lake(str(root))[0]
            metadata = loader.inspect_dataset(dataset)
            self.assertEqual(
                {signal.component for signal in metadata.signals},
                {"LMP", "ENERGY", "CONG", "LOSS"},
            )
            bundle = loader.load_series(
                dataset,
                date(2026, 9, 1),
                date(2026, 9, 30),
                "UTC",
                ("LMP",),
                (("node", ("SP15",)),),
                fallback_to_latest=True,
            )
            self.assertEqual(bundle.points, 1)
            self.assertEqual(
                bundle.fallback_horizon, (date(2023, 7, 8), date(2023, 7, 8))
            )
            manual = loader.load_series(
                dataset, date(2026, 9, 1), date(2026, 9, 30), "UTC", ("LMP",)
            )
            self.assertEqual(manual.points, 0)
            empty = loader.load_series(
                dataset,
                date(2026, 9, 1),
                date(2026, 9, 30),
                "UTC",
                ("LMP",),
                (("node", ()),),
                fallback_to_latest=True,
            )
            self.assertEqual(empty.points, 0)

    def test_latest_fallback_survives_misdated_hive_partitions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            feed = root / "power_gas/napg/caiso/rtm_lmp"
            for day, timestamp, node in (
                (7, "2026-10-07T07:00:00Z", "NP15"),
                (8, "2023-07-08T07:00:00Z", "SP15"),
            ):
                path = feed / f"year=2026/month=10/day={day:02d}/data.parquet"
                path.parent.mkdir(parents=True)
                pl.DataFrame(
                    {
                        "INTERVALSTARTTIME_GMT": [timestamp],
                        "NODE": [node],
                        "LMP_TYPE": ["LMP"],
                        "VALUE": [25.0],
                    }
                ).write_parquet(path)
            loader.clear_caches()
            dataset = loader.discover_lake(str(root))[0]
            bundle = loader.load_series(
                dataset,
                date(2026, 9, 8),
                date(2026, 10, 7),
                "UTC",
                ("value:LMP",),
                (("node", ("SP15",)),),
                fallback_to_latest=True,
            )
            self.assertEqual(bundle.points, 1)
            self.assertEqual(bundle.plot["timestamp"][0].date(), date(2023, 7, 8))

    def test_all_pnodes_are_searchable_beyond_old_option_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            feed = root / "caiso/rtm_lmp"
            feed.mkdir(parents=True)
            pl.DataFrame(
                {
                    "timestamp": [datetime(2026, 9, 1, tzinfo=timezone.utc)] * 6001,
                    "node": [f"N{index:05d}" for index in range(6001)],
                    "lmp_type": ["LMP"] * 6001,
                    "value": [1.0] * 6001,
                }
            ).write_parquet(feed / "data.parquet")
            loader.clear_caches()
            dataset = loader.discover_lake(str(root))[0]
            self.assertEqual(len(loader.dimension_options(dataset, "node")), 6001)


class FakeClient:
    def __init__(self, root: Path):
        self.raw_root = root
        self.calls = []
        self.price = 25.0
        self.bad = False
        self.block = False

    def download(self, report, lower, upper, *, node):
        self.calls.append((report.queryname, lower, upper, node))
        if self.block:
            raise HTTPError("http://oasis.caiso.com", 403, "blocked", {}, None)
        self.raw_root.mkdir(parents=True, exist_ok=True)
        path = self.raw_root / f"{len(self.calls)}.csv"
        if self.bad:
            path.write_text("NODE,VALUE\nSP15,bad\n")
        else:
            rows = []
            current = lower
            while current < upper:
                for component in report.required_components:
                    rows.append(
                        {
                            "INTERVALSTARTTIME_GMT": current.isoformat(),
                            "INTERVALENDTIME_GMT": (
                                current + timedelta(minutes=report.interval_minutes)
                            ).isoformat(),
                            "NODE": node,
                            "MARKET_RUN_ID": report.market_run_id,
                            "LMP_TYPE": component,
                            "VALUE": self.price,
                        }
                    )
                current += timedelta(minutes=report.interval_minutes)
            pl.DataFrame(rows).write_csv(path)
        return [path]


class BackfillTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.lake = self.root / "lake"
        self.raw = self.root / "raw"
        self.client = FakeClient(self.raw)
        self.day = date(2026, 9, 1)

    def fetch(self, **kwargs):
        return cli.fetch(
            market="DAM",
            nodes=("SP15", "NP15"),
            start=self.day,
            end=self.day,
            lake=self.lake,
            raw=self.raw,
            client=self.client,
            **kwargs,
        )

    def test_inclusive_dates_skip_verified_days_and_force_replaces_matching_prices(
        self,
    ):
        summary = self.fetch()
        self.assertEqual((summary.downloaded, summary.incomplete), (2, 0))
        self.assertEqual(self.fetch().skipped, 2)
        self.assertEqual(len(self.client.calls), 2)
        self.assertEqual(list(self.raw.glob("*.csv")), [])
        self.client.price = 80.0
        cli.fetch(
            market="DAM",
            nodes=("SP15",),
            start=self.day,
            end=self.day,
            lake=self.lake,
            raw=self.raw,
            client=self.client,
            force=True,
        )
        data = pl.concat(
            [pl.read_parquet(path) for path in self.lake.rglob("*.parquet")]
        )
        self.assertEqual(
            data.filter(pl.col("node") == "SP15")["value"].unique().to_list(), [80.0]
        )
        self.assertEqual(
            data.filter(pl.col("node") == "NP15")["value"].unique().to_list(), [25.0]
        )
        self.assertEqual(data.height, 24 * 4 * 2)
        self.assertIn("timestamp", data.columns)
        self.assertIn("component", data.columns)

    def test_missing_component_is_refetched_and_failed_raw_is_retained(self):
        self.fetch()
        path = next(self.lake.rglob("*.parquet"))
        frame = pl.read_parquet(path).filter(
            ~((pl.col("node") == "SP15") & (pl.col("lmp_type") == "MCL"))
        )
        frame.write_parquet(path)
        self.assertEqual(self.fetch().downloaded, 1)
        self.client.bad = True
        result = self.fetch(force=True)
        self.assertEqual(result.failed, 2)
        self.assertEqual(len(list(self.raw.glob("*.csv"))), 2)

    def test_access_denial_stops_remaining_days_and_writer_lock_is_shared(self):
        self.client.block = True
        result = cli.fetch(
            market="RTM",
            nodes=("SP15", "NP15"),
            start=self.day,
            end=self.day + timedelta(days=3),
            lake=self.lake,
            raw=self.raw,
            client=self.client,
        )
        self.assertEqual((result.failed, len(self.client.calls)), (1, 1))
        with (
            writer_lock(self.lake),
            self.assertRaisesRegex(RuntimeError, "Another CAISO writer"),
        ):
            self.fetch()

    def test_rtm_and_fmm_dst_days_have_expected_coverage(self):
        for market in ("RTM", "FMM"):
            day = date(2025, 11, 2)
            summary = cli.fetch(
                market=market,
                nodes=("SP15",),
                start=day,
                end=day,
                lake=self.lake,
                raw=self.raw,
                client=self.client,
            )
            self.assertEqual(summary.incomplete, 0)
            self.assertEqual(
                self.client.calls[-1][2] - self.client.calls[-1][1], timedelta(hours=25)
            )

    def test_component_aliases_do_not_leave_stale_prices_after_force(self):
        self.fetch()
        for path in self.lake.rglob("*.parquet"):
            frame = pl.read_parquet(path).with_columns(
                pl.col("lmp_type").replace(
                    {"MCE": "ENERGY", "MCC": "CONG", "MCL": "LOSS"}
                )
            )
            frame.write_parquet(path)
        self.client.price = 90.0
        self.fetch(force=True)
        rows = pl.concat(
            [pl.read_parquet(path) for path in self.lake.rglob("*.parquet")]
        )
        self.assertEqual(rows.height, 24 * 4 * 2)
        self.assertEqual(rows["value"].unique().to_list(), [90.0])

    def test_main_exit_status_reports_partial_and_failure(self):
        args = [
            "--market",
            "RTM",
            "--nodes",
            "SP15",
            "--start",
            "2026-09-01",
            "--end",
            "2026-09-01",
        ]
        for summary, status in (
            (cli.FetchSummary(), 0),
            (cli.FetchSummary(no_data=1), 2),
            (cli.FetchSummary(incomplete=1), 2),
            (cli.FetchSummary(failed=1), 1),
        ):
            with patch.object(cli, "fetch", return_value=summary):
                self.assertEqual(cli.main(args), status)


if __name__ == "__main__":
    unittest.main()
