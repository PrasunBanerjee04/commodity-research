"""Analytics and HTTP dashboard regressions. Install the optional dashboard extra."""

from __future__ import annotations

import importlib.util
import json
import tempfile
import threading
import unittest
from datetime import date, datetime, timedelta, timezone
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import polars as pl

HAS_DASHBOARD = importlib.util.find_spec("pyarrow") is not None

from comm_research.dashboard.app import DashboardHandler
from comm_research.dashboard.config.taxonomy import display_name
from comm_research.dashboard.data import loader


def price_data(
    column: str = "value", hours: int = 48, nodes: tuple[str, ...] = ("SP15",)
) -> pl.DataFrame:
    rows = []
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    for hour in range(hours):
        for index, node in enumerate(nodes):
            for component, offset in (
                ("LMP", 10),
                ("MCE", 9),
                ("MCC", 2),
                ("MCL", -1),
                ("MGHG", 0),
            ):
                rows.append(
                    {
                        "interval_start_time_gmt": start + timedelta(hours=hour),
                        "interval_end_time_gmt": start + timedelta(hours=hour + 1),
                        "node": node,
                        "market_run_id": "DAM" if column == "mw" else "RTM",
                        "lmp_type": component,
                        column: float(hour + offset + index * 100),
                        "opr_hr": hour % 24 + 1,
                        "opr_interval": 1,
                    }
                )
    return pl.DataFrame(rows)


@unittest.skipUnless(HAS_DASHBOARD, "Install .[dashboard] to run dashboard checks")
class LoaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def write(self, key: str, table: pl.DataFrame, name: str = "data.parquet") -> Path:
        path = self.root / key / "year=2024/month=01/day=01" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix == ".parquet":
            table.write_parquet(path)
        else:
            table.write_ipc(path)
        loader.clear_caches()
        return path

    def dataset(self, key: str):
        return next(
            dataset
            for dataset in loader.discover_lake(str(self.root))
            if dataset.key == key
        )

    def test_hive_discovery_arrow_and_taxonomy(self):
        self.write("power_gas/napg/caiso/rtm_lmp", price_data())
        self.write(
            "oil/wti/settlements",
            pl.DataFrame({"date": [date(2024, 1, 1)], "close": [75.0]}),
            "data.arrow",
        )
        datasets = loader.discover_lake(str(self.root))
        self.assertEqual(len(datasets), 2)
        self.assertEqual(
            self.dataset("power_gas/napg/caiso/rtm_lmp").title, "CAISO / Real-Time LMP"
        )
        self.assertEqual(display_name("power_gas"), "Power & Natural Gas")
        self.assertEqual(display_name("aeso"), "AESO")
        arrow = self.dataset("oil/wti/settlements")
        metadata = loader.inspect_dataset(arrow)
        self.assertEqual(metadata.signals[0].key, "close")
        self.assertEqual(metadata.latest.date(), date(2024, 1, 1))

    def test_dam_mw_and_rtm_value_map_to_same_components_without_hour_counters(self):
        for feed, column in (("dam_lmp", "mw"), ("rtm_lmp", "value")):
            self.write(f"power_gas/napg/caiso/{feed}", price_data(column))
            dataset = self.dataset(f"power_gas/napg/caiso/{feed}")
            metadata = loader.inspect_dataset(dataset)
            self.assertEqual(
                [signal.label for signal in metadata.signals],
                ["LMP", "Energy", "Congestion", "Losses", "GHG"],
            )
            self.assertEqual({signal.unit for signal in metadata.signals}, {"$/MWh"})
            selected = (metadata.signals[0].key, metadata.signals[2].key)
            bundle = loader.load_series(
                dataset,
                date(2024, 1, 1),
                date(2024, 1, 2),
                "UTC",
                selected,
                (("node", ("SP15",)),),
            )
            self.assertEqual(bundle.statistics.height, 2)
            self.assertEqual(bundle.points, 96)
            self.assertEqual(
                bundle.plot.filter(pl.col("series").str.starts_with("LMP"))[
                    "value"
                ].max(),
                57,
            )

    def test_nodes_are_separate_traces_and_empty_selection_returns_no_data(self):
        self.write("prices", price_data(nodes=("SP15", "NP15")))
        dataset = self.dataset("prices")
        bundle = loader.load_series(
            dataset, date(2024, 1, 1), date(2024, 1, 2), "UTC", ("value:LMP",)
        )
        self.assertEqual(bundle.statistics.height, 2)
        self.assertEqual(sorted(bundle.statistics["last"].to_list()), [57, 157])
        empty = loader.load_series(
            dataset,
            date(2024, 1, 1),
            date(2024, 1, 2),
            "UTC",
            ("value:LMP",),
            (("node", ()),),
        )
        self.assertTrue(empty.statistics.is_empty())

    def test_exact_24h_reference_negative_zero_and_missing_prices(self):
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        frame = pl.DataFrame(
            {
                "timestamp": [
                    start,
                    start + timedelta(hours=24),
                    start,
                    start + timedelta(hours=24),
                    start + timedelta(hours=25),
                ],
                "series": ["negative", "negative", "zero", "zero", "missing"],
                "value": [-10.0, -5.0, 0.0, 5.0, 9.0],
                "unit": ["$/MWh"] * 5,
            }
        )
        stats = loader.summarize(frame)
        self.assertEqual(
            stats.filter(pl.col("series") == "negative")["change_24h"][0], 50
        )
        self.assertIsNone(stats.filter(pl.col("series") == "zero")["change_24h"][0])
        self.assertIsNone(stats.filter(pl.col("series") == "missing")["change_24h"][0])
        self.assertIsNone(stats.filter(pl.col("series") == "missing")["std"][0])

    def test_downsampling_preserves_endpoints_and_spikes_without_changing_statistics(
        self,
    ):
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        values = [1.0] * 10_000
        values[5101], values[7004] = 999.0, -900.0
        frame = pl.DataFrame(
            {
                "timestamp": [start + timedelta(seconds=i) for i in range(len(values))],
                "series": ["signal"] * len(values),
                "value": values,
                "unit": ["MW"] * len(values),
            }
        )
        reduced = loader.reduce_plot_points(frame, max_points=400)
        self.assertLessEqual(reduced.height, 400)
        self.assertEqual(reduced["value"].min(), -900)
        self.assertEqual(reduced["value"].max(), 999)
        self.assertEqual(reduced["timestamp"][0], frame["timestamp"][0])
        self.assertEqual(reduced["timestamp"][-1], frame["timestamp"][-1])
        self.assertEqual(loader.summarize(frame)["count"][0], 10_000)

    def test_cache_fingerprint_changes_when_partition_replaced(self):
        path = self.write("prices", price_data(hours=2))
        before = self.dataset("prices")
        before_result = loader.load_series(
            before, date(2024, 1, 1), date(2024, 1, 1), "UTC", ("value:LMP",)
        )
        price_data(hours=3).write_parquet(path)
        loader.clear_caches()
        after = self.dataset("prices")
        after_result = loader.load_series(
            after, date(2024, 1, 1), date(2024, 1, 1), "UTC", ("value:LMP",)
        )
        self.assertNotEqual(before.files, after.files)
        self.assertEqual((before_result.points, after_result.points), (2, 3))

    def test_dated_queries_skip_distant_hive_files_but_keep_unpartitioned_files(self):
        self.write("prices", price_data(hours=2))
        distant = self.root / "prices/year=2024/month=01/day=20/data.parquet"
        distant.parent.mkdir(parents=True)
        price_data(hours=2).write_parquet(distant)
        unpartitioned = self.root / "prices/unpartitioned.parquet"
        price_data(hours=2).write_parquet(unpartitioned)
        loader.clear_caches()
        dataset = self.dataset("prices")
        scan_parquet = pl.scan_parquet
        with patch.object(loader.pl, "scan_parquet", wraps=scan_parquet) as scan:
            loader._scan(dataset, date(2024, 1, 1), date(2024, 1, 1))
        scanned_paths = {Path(call.args[0]).resolve() for call in scan.call_args_list}
        self.assertIn(unpartitioned.resolve(), scanned_paths)
        self.assertEqual(len(scanned_paths), 2)
        self.assertNotIn(distant.resolve(), scanned_paths)

    def test_schema_evolution_and_raw_pagination_keep_identifiers(self):
        table = pl.DataFrame(
            {
                "timestamp": [datetime(2024, 1, 1, tzinfo=timezone.utc)] * 230,
                "resource_id": [f"{number:04}" for number in range(230)],
                "price": [40.0] * 230,
            }
        )
        self.write("prices", table)
        dataset = self.dataset("prices")
        count = loader.raw_row_count(dataset, date(2024, 1, 1), date(2024, 1, 1), "UTC")
        page = loader.load_raw_page(
            dataset, date(2024, 1, 1), date(2024, 1, 1), "UTC", page=2, page_size=100
        )
        self.assertEqual((count, page.height), (230, 30))
        self.assertEqual(page.schema["resource_id"], pl.String)
        evolved = table.with_columns(pl.lit(5).alias("volume"))
        second = self.root / "prices/year=2024/month=01/day=02/data.parquet"
        second.parent.mkdir(parents=True)
        evolved.write_parquet(second)
        loader.clear_caches()
        self.assertIn(
            "volume", dict(loader.inspect_dataset(self.dataset("prices")).schema)
        )

    def test_last_hourly_aggregation_orders_actual_observation_times(self):
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        table = pl.DataFrame(
            {
                "timestamp": [
                    start + timedelta(minutes=40),
                    start + timedelta(minutes=5),
                    start + timedelta(minutes=20),
                ],
                "price": [40.0, 5.0, 20.0],
            }
        )
        self.write("prices", table)
        bundle = loader.load_series(
            self.dataset("prices"),
            date(2024, 1, 1),
            date(2024, 1, 1),
            "UTC",
            ("price",),
            frequency="1h",
            aggregation="Last",
        )
        self.assertEqual(bundle.statistics["last"][0], 40)

    def test_calendar_filter_boundaries_follow_dst(self):
        for day, hours in ((date(2025, 3, 9), 23), (date(2025, 11, 2), 25)):
            lower, upper = loader._utc_bounds(day, day, "America/Los_Angeles")
            self.assertEqual((upper - lower).total_seconds(), hours * 3600)

    def test_symlinks_and_manifest_escape_are_rejected(self):
        external = self.root.parent / f"{self.root.name}-outside.parquet"
        price_data().write_parquet(external)
        self.addCleanup(external.unlink)
        (self.root / "linked.parquet").symlink_to(external)
        self.assertEqual(loader.discover_lake(str(self.root)), ())
        dataset = loader.Dataset(
            str(self.root), "malicious", (loader.FileStamp(str(external), 1, 0),)
        )
        with self.assertRaises(loader.LakeError):
            loader.inspect_dataset(dataset)

@unittest.skipUnless(HAS_DASHBOARD, "Install .[dashboard] to run dashboard checks")
class DashboardApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.feed = "power_gas/napg/caiso/dam_lmp"
        path = (
            self.root
            / self.feed
            / "year=2024/month=01/day=01/data.parquet"
        )
        path.parent.mkdir(parents=True)
        price_data("mw", hours=48, nodes=("SP15", "NP15")).write_parquet(path)
        loader.clear_caches()
        handler = partial(DashboardHandler, lake_root=str(self.root))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.thread.join, 2)
        self.addCleanup(self.server.shutdown)
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def get(self, path):
        with urlopen(self.base_url + path, timeout=5) as response:
            return response.status, response.headers, response.read()

    def post(self, path, payload):
        request = Request(
            self.base_url + path,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            response = urlopen(request, timeout=10)
        except HTTPError as error:
            return error.code, error.headers, error.read()
        with response:
            return response.status, response.headers, response.read()

    def test_dashboard_assets_and_catalog_are_served_without_external_runtime(self):
        status, _, html = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn(b"/src/main.js", html)
        self.assertIn(b"Commodities Analytics Dashboard", html)
        status, headers, script = self.get("/src/main.js")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Security-Policy"].split(";")[0], "default-src 'self'")
        self.assertIn(b"DockManager", script)
        self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
        status, _, stylesheet = self.get("/styles/theme.css")
        self.assertEqual(status, 200)
        self.assertIn(b"--bg-primary: #131722", stylesheet)
        for asset in ("/vendor/dockview.min.js", "/vendor/uPlot.iife.min.js"):
            status, asset_headers, content = self.get(asset)
            self.assertEqual(status, 200)
            self.assertTrue(asset_headers["Content-Type"].startswith("text/javascript"))
            self.assertTrue(content)
        status, _, body = self.get("/api/datasets")
        self.assertEqual(status, 200)
        datasets = json.loads(body)
        self.assertEqual(datasets[0]["key"], self.feed)
        self.assertEqual(datasets[0]["files"], 1)

    def test_metadata_and_options_describe_dam_signals_and_series(self):
        status, _, body = self.get(f"/api/metadata?key={self.feed}")
        self.assertEqual(status, 200)
        metadata = json.loads(body)
        self.assertEqual(metadata["dataset"]["title"], "CAISO / Day-Ahead LMP")
        self.assertEqual(
            [signal["label"] for signal in metadata["signals"]],
            ["LMP", "Energy", "Congestion", "Losses", "GHG"],
        )
        status, _, body = self.get(f"/api/options?key={self.feed}&dimension=node")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["values"], ["NP15", "SP15"])

    def test_series_endpoint_returns_metrics_and_rejects_invalid_controls(self):
        request = {
            "key": self.feed,
            "start": "2024-01-01",
            "end": "2024-01-02",
            "zone": "UTC",
            "signals": ["mw:LMP"],
            "filters": {"node": ["SP15"]},
            "frequency": "native",
            "aggregation": "Mean",
        }
        status, _, body = self.post("/api/series", request)
        self.assertEqual(status, 200)
        result = json.loads(body)
        self.assertEqual(result["observations"], 48)
        self.assertEqual(result["statistics"][0]["series"], "LMP · SP15 · DAM")
        self.assertEqual(result["statistics"][0]["last"], 57)
        request["zone"] = ["not-a-zone"]
        status, _, body = self.post("/api/series", request)
        self.assertEqual(status, 400)
        self.assertIn("date zone", json.loads(body)["error"])

    def test_raw_rows_are_paged_and_rescan_refreshes_discovery(self):
        request = {
            "key": self.feed,
            "start": "2024-01-01",
            "end": "2024-01-01",
            "zone": "UTC",
            "filters": {"node": ["SP15"]},
            "page": 1,
            "pageSize": 100,
        }
        status, _, body = self.post("/api/rows", request)
        self.assertEqual(status, 200)
        result = json.loads(body)
        self.assertEqual(result["count"], 120)
        self.assertEqual(len(result["rows"]), 20)
        self.assertEqual(result["rows"][0]["node"], "SP15")
        self.assertEqual(self.post("/api/rescan", {})[0], 200)


if __name__ == "__main__":
    unittest.main()
