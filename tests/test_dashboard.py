"""Analytics and workspace regressions. Install the optional dashboard extra."""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import polars as pl

HAS_DASHBOARD = all(
    importlib.util.find_spec(name) is not None
    for name in ("streamlit", "plotly", "pyarrow")
)
if HAS_DASHBOARD:
    from streamlit.testing.v1 import AppTest

    from comm_research.dashboard.config.taxonomy import display_name
    from comm_research.dashboard.data import loader
    from comm_research.dashboard.ui.components import build_chart
    from comm_research.dashboard.ui.theme import THEMES
    from comm_research.dashboard.ui.workspace import decode_workspace, panel_key

ROOT = Path(__file__).resolve().parents[1]


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
        loader.discover_lake.clear()
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
        loader.discover_lake.clear()
        after = self.dataset("prices")
        after_result = loader.load_series(
            after, date(2024, 1, 1), date(2024, 1, 1), "UTC", ("value:LMP",)
        )
        self.assertNotEqual(before.files, after.files)
        self.assertEqual((before_result.points, after_result.points), (2, 3))

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
        loader.discover_lake.clear()
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

    def test_chart_uses_utc_axis_transparency_crosshairs_and_range_controls(self):
        self.write("prices", price_data())
        bundle = loader.load_series(
            self.dataset("prices"),
            date(2024, 1, 1),
            date(2024, 1, 2),
            "UTC",
            ("value:LMP",),
        )
        figure = build_chart(bundle, THEMES["Dark"], "America/Los_Angeles")
        self.assertEqual(figure.layout.plot_bgcolor, "rgba(0,0,0,0)")
        self.assertEqual(figure.layout.hovermode, "x unified")
        self.assertTrue(figure.layout.xaxis.showspikes)
        self.assertEqual(
            [button.label for button in figure.layout.xaxis.rangeselector.buttons],
            ["1D", "5D", "1M", "YTD", "ALL"],
        )
        self.assertEqual(figure.data[0].x[0].utcoffset(), timedelta(0))


@unittest.skipUnless(HAS_DASHBOARD, "Install .[dashboard] to run dashboard checks")
class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for feed, column in (("dam_lmp", "mw"), ("rtm_lmp", "value")):
            path = (
                self.root
                / "power_gas/napg/caiso"
                / feed
                / "year=2024/month=01/day=01/data.parquet"
            )
            path.parent.mkdir(parents=True)
            price_data(column, hours=40 * 24, nodes=("SP15",)).write_parquet(path)
        self.env = patch.dict(os.environ, {"COMMODITY_LAKE_ROOT": str(self.root)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.dam = "power_gas/napg/caiso/dam_lmp"
        self.rtm = "power_gas/napg/caiso/rtm_lmp"

    def app(self):
        return AppTest.from_file(str(ROOT / "app.py"), default_timeout=30).run()

    def test_panel_controls_layout_theme_close_reopen_and_browser_reload(self):
        app = self.app()
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(app.session_state["active_panels"], [self.dam, self.rtm])
        self.assertEqual(
            app.date_input(key=panel_key(self.dam, "start")).value, date(2024, 1, 11)
        )
        app.multiselect(key=panel_key(self.dam, "signals")).set_value(
            ["mw:LMP", "mw:MCC"]
        ).run()
        app.get("segmented_control")[0].set_value("Split 1×2").run()
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(
            app.multiselect(key=panel_key(self.dam, "signals")).value,
            ["mw:LMP", "mw:MCC"],
        )
        self.assertEqual(len(app.date_input), 4)
        app.selectbox(key="desk_theme").select("Light").run()
        self.assertEqual(len(app.exception), 0)
        app.button(key=panel_key(self.rtm, "close")).click().run()
        self.assertEqual(app.session_state["active_panels"], [self.dam])
        app.button(key="open:" + self.rtm).click().run()
        self.assertEqual(app.session_state["active_panels"], [self.dam, self.rtm])
        encoded = app.query_params["workspace"]
        restored = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30)
        restored.query_params["workspace"] = encoded
        restored.run()
        self.assertEqual(len(restored.exception), 0)
        self.assertEqual(restored.session_state["desk_theme"], "Light")
        self.assertEqual(restored.session_state["desk_view"], "Split 1×2")
        self.assertEqual(
            restored.multiselect(key=panel_key(self.dam, "signals")).value,
            ["mw:LMP", "mw:MCC"],
        )

    def test_tabs_remount_without_losing_panel_horizons(self):
        app = self.app()
        app.date_input(key=panel_key(self.dam, "start")).set_value(
            date(2024, 1, 20)
        ).run()
        app.session_state["workspace_tabs"] = "CAISO / Real-Time LMP"
        app.run()
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(app.multiselect[0].value, ["value:LMP"])
        app.session_state["workspace_tabs"] = "CAISO / Day-Ahead LMP"
        app.run()
        self.assertEqual(
            app.date_input(key=panel_key(self.dam, "start")).value, date(2024, 1, 20)
        )

    def test_four_panel_grid_and_overflow_preserve_existing_feeds(self):
        for index in range(3):
            path = self.root / f"oil/test/prices_{index}/data.parquet"
            path.parent.mkdir(parents=True)
            price_data(hours=48, nodes=("CL",)).write_parquet(path)
        loader.discover_lake.clear()
        app = self.app()
        app.get("segmented_control")[0].set_value("Split 2×2").run()
        for index in range(3):
            app.button(key=f"open:oil/test/prices_{index}").click().run()
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(len(app.session_state["active_panels"]), 5)
        self.assertEqual(app.session_state["grid_page"], 1)
        self.assertEqual(len(app.date_input), 2)
        app.selectbox(key="grid_page").select(0).run()
        self.assertEqual(len(app.date_input), 8)
        self.assertEqual(app.session_state["active_panels"][:2], [self.dam, self.rtm])

    def test_raw_view_pagination_and_schema(self):
        app = self.app()
        app.get("segmented_control")[1].set_value("Raw data").run()
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(len(app.dataframe), 2)
        self.assertEqual(app.dataframe[0].value.shape[0], 250)
        app.button(key=panel_key(self.dam, "next")).click().run()
        self.assertEqual(app.session_state[panel_key(self.dam, "page")], 1)

    def test_bad_workspace_url_is_ignored_and_panel_limit_is_bounded(self):
        for value in (
            "not-base64",
            base64.urlsafe_b64encode(b"[]").decode(),
            "a" * 16_001,
        ):
            self.assertEqual(decode_workspace(value), {})
        payload = {
            "version": 1,
            "active": [f"dataset{i}" for i in range(20)],
            "panels": [],
        }
        encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
        result = decode_workspace(encoded)
        self.assertEqual(len(result["active"]), 12)
        self.assertEqual(result["panels"], {})


if __name__ == "__main__":
    unittest.main()
