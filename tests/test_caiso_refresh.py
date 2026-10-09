import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

import polars as pl
from test_caiso_pipeline import HEADER, zip_payload

from comm_research.infra.scrapers.caiso.oasis import (
    SCRAPERS,
    CAISOOASISClient,
    NoDataError,
)
from comm_research.infra.scrapers.caiso.oasis.models import CategoryScraper, Report
from comm_research.infra.scrapers.caiso.oasis.prices import DAM_LMP, RTM_LMP
from comm_research.infra.tools.caiso_normalizer import normalize_csvs, read_csv
from comm_research.infra.tools.lake_methods import _clear_raw
from comm_research.markets.power_gas.napg.caiso import caiso_oasis_pipeline as pipeline


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.raw = self.root / "raw"
        self.lake = self.root / "lake"
        self.state = self.root / "state"
        self.client = CAISOOASISClient(self.raw)
        self.logs = StringIO()
        self.day = datetime.now(timezone.utc).date() - timedelta(days=10)
        self.requests = []

    def price_payload(
        self,
        day,
        node="TH_SP15_GEN-APND",
        hours=24,
        components=("LMP", "MCE", "MCC", "MCL"),
    ):
        lower = pipeline._query_bounds(DAM_LMP, day)[0].astimezone(timezone.utc)
        rows = []
        for hour in range(hours):
            start = lower + timedelta(hours=hour)
            finish = start + timedelta(hours=1)
            for component in components:
                rows.append(
                    f"{start.isoformat()},{finish.isoformat()},{day},{hour + 1},{node},42.5,{component}\n"
                )
        return zip_payload({"prices.csv": HEADER + "".join(rows)})

    def fetch(self, params, **kwargs):
        day = datetime.strptime(params["startdatetime"], "%Y%m%dT%H:%M%z").date()
        self.requests.append((day, params.get("node")))
        return self.price_payload(day, params.get("node", "TH_SP15_GEN-APND"))

    def run_pipeline(self, **kwargs):
        settings = {
            "categories": ["prices"],
            "datasets": ["dam_lmp"],
            "start_date": self.day,
            "end_date": self.day + timedelta(days=3),
            "ignore_end_date": False,
            "nodes": ["TH_SP15_GEN-APND"],
            "raw_root": self.raw,
            "lake_root": self.lake,
            "state_root": self.state,
            "client": self.client,
            "log_stream": self.logs,
        }
        settings.update(kwargs)
        return pipeline.run(**settings)

    def test_refresh_skips_completed_days_and_only_fetches_new_tail(self):
        with patch.object(self.client, "_fetch", side_effect=self.fetch):
            first = self.run_pipeline()
            second = self.run_pipeline(end_date=self.day + timedelta(days=4))
        self.assertEqual(first.downloaded, 3)
        self.assertEqual((second.skipped, second.downloaded), (3, 1))
        self.assertEqual(len(self.requests), 4)
        self.assertEqual(list(self.raw.rglob("*.csv")), [])
        for event in (
            "Pull dam_lmp",
            "Wrote",
            "Skip complete",
            "Raw cleanup",
            "Refresh finished",
        ):
            self.assertIn(event, self.logs.getvalue())

    def test_missing_middle_partition_is_repaired(self):
        with patch.object(self.client, "_fetch", side_effect=self.fetch):
            self.run_pipeline()
            pipeline._partition(
                self.lake, DAM_LMP, self.day + timedelta(days=1)
            ).unlink()
            self.requests.clear()
            summary = self.run_pipeline()
        self.assertEqual(summary.downloaded, 2)
        self.assertEqual(summary.skipped, 1)
        self.assertEqual(
            {value[0] for value in self.requests},
            {self.day, self.day + timedelta(days=1)},
        )

    def test_changing_nodes_cannot_reuse_wrong_completion_marker(self):
        with patch.object(self.client, "_fetch", side_effect=self.fetch):
            self.run_pipeline(nodes=["NODE_A"])
            summary = self.run_pipeline(nodes=["NODE_B"])
        self.assertEqual(summary.downloaded, 3)
        for path in self.lake.rglob("*.parquet"):
            self.assertEqual(set(pl.read_parquet(path)["node"]), {"NODE_A", "NODE_B"})

    def test_failed_conversion_is_resumed_without_redownloading(self):
        real_normalize = pipeline.normalize_csvs
        failed_once = False

        def normalize(paths, dataset, **kwargs):
            nonlocal failed_once
            if not failed_once:
                failed_once = True
                raise OSError("disk full")
            return real_normalize(paths, dataset, **kwargs)

        with (
            patch.object(self.client, "_fetch", side_effect=self.fetch),
            patch.object(pipeline, "normalize_csvs", side_effect=normalize),
        ):
            first = self.run_pipeline()
        self.assertEqual((first.failed, first.downloaded), (1, 2))
        self.assertEqual(len(list(self.raw.rglob("*.csv"))), 1)
        with patch.object(
            self.client, "_fetch", side_effect=AssertionError("must resume staged CSV")
        ):
            second = self.run_pipeline()
        self.assertEqual((second.resumed, second.skipped, second.failed), (1, 2, 0))
        self.assertEqual(list(self.raw.rglob("*.csv")), [])

    def test_no_data_does_not_mark_a_day_complete(self):
        def fetch(params):
            if not self.requests:
                self.requests.append((self.day, None))
                raise NoDataError("No data returned")
            return self.fetch(params)

        with patch.object(self.client, "_fetch", side_effect=fetch):
            first = self.run_pipeline()
        with patch.object(self.client, "_fetch", side_effect=self.fetch):
            second = self.run_pipeline()
        self.assertEqual(first.no_data, 1)
        self.assertEqual((second.downloaded, second.skipped), (1, 2))

    def test_partial_price_day_gets_refreshed(self):
        with patch.object(
            self.client, "_fetch", return_value=self.price_payload(self.day, hours=1)
        ):
            first = self.run_pipeline(end_date=self.day + timedelta(days=1))
        self.assertEqual(first.incomplete, 1)
        with patch.object(self.client, "_fetch", side_effect=self.fetch):
            second = self.run_pipeline(end_date=self.day + timedelta(days=1))
        self.assertEqual(
            (second.downloaded, second.skipped, second.incomplete), (1, 0, 0)
        )
        self.assertEqual(
            sum(pl.read_parquet(path).height for path in self.lake.rglob("*.parquet")),
            96,
        )

    def test_full_legacy_price_partition_is_adopted_without_request(self):
        staged = self.client._stage(self.price_payload(self.day), DAM_LMP)
        normalize_csvs(staged, "dam_lmp", lake_root=self.lake)
        with patch.object(
            self.client,
            "_fetch",
            side_effect=AssertionError("existing day should be adopted"),
        ):
            summary = self.run_pipeline(end_date=self.day + timedelta(days=1))
        self.assertEqual((summary.adopted, summary.failed), (1, 0))

    def test_missing_price_components_prevent_adoption(self):
        staged = self.client._stage(
            self.price_payload(self.day, components=("LMP",)), DAM_LMP
        )
        normalize_csvs(staged, "dam_lmp", lake_root=self.lake)
        with patch.object(self.client, "_fetch", side_effect=self.fetch):
            summary = self.run_pipeline(end_date=self.day + timedelta(days=1))
        self.assertEqual(
            (summary.adopted, summary.downloaded, summary.incomplete), (0, 1, 0)
        )

    def test_price_coverage_and_singlezip_windows_follow_dst(self):
        for day, hours in ((date(2025, 3, 9), 23), (date(2025, 11, 2), 25)):
            with self.subTest(day=day):
                report = replace(DAM_LMP, max_days=1)
                start, end = pipeline._query_bounds(report, day)
                with patch.object(
                    self.client,
                    "_fetch",
                    return_value=self.price_payload(day, hours=hours),
                ) as fetch:
                    staged = self.client.download(
                        report, start, end, node="TH_SP15_GEN-APND"
                    )
                self.assertEqual(fetch.call_count, 1)
                normalize_csvs(staged, "dam_lmp", lake_root=self.lake)
                self.assertTrue(
                    pipeline._legacy_complete(
                        pipeline._partition(self.lake, report, day),
                        report,
                        day,
                        "TH_SP15_GEN-APND",
                    )
                )

    def test_empty_or_wrong_node_legacy_partition_is_not_adopted(self):
        staged = self.client._stage(
            self.price_payload(self.day, node="OTHER_NODE"), DAM_LMP
        )
        normalize_csvs(staged, "dam_lmp", lake_root=self.lake)
        with patch.object(self.client, "_fetch", side_effect=self.fetch):
            summary = self.run_pipeline(end_date=self.day + timedelta(days=1))
        self.assertEqual((summary.adopted, summary.downloaded), (0, 1))

    def test_all_categories_are_executable_and_use_shared_client(self):
        sample = Report("TEST_DATA", None, 1, 1, "sample", snapshot=True)
        scrapers = {
            name: CategoryScraper(name, (replace(sample, dataset_name=name),))
            for name in SCRAPERS
        }
        payload = zip_payload({"sample.csv": "RESOURCE_ID,MW\n0001,3.5\n"})
        with (
            patch.object(pipeline, "SCRAPERS", scrapers),
            patch.object(self.client, "_fetch", return_value=payload) as fetch,
        ):
            summary = self.run_pipeline(
                categories=list(scrapers),
                datasets=list(scrapers),
                end_date=self.day + timedelta(days=1),
            )
        self.assertEqual(summary.downloaded, 8)
        self.assertEqual(fetch.call_count, 8)
        self.assertEqual(len(list(self.lake.rglob("*.parquet"))), 8)

    def test_published_date_limits_and_calendar_month_clamping(self):
        self.assertEqual(
            pipeline._subtract_months(date(2024, 3, 31), 1), date(2024, 2, 29)
        )
        today = date(2026, 10, 9)
        bid = SCRAPERS["public_bids"].reports[0]
        lower, upper = pipeline._bounds(bid, date(2000, 1, 1), today, today)
        self.assertEqual(lower, date(2023, 7, 9))
        self.assertEqual(upper, today - timedelta(days=90))
        self.assertEqual(
            pipeline._bounds(RTM_LMP, today, today, today),
            (today, today),
        )
        self.assertEqual(
            pipeline._bounds(RTM_LMP, today, today + timedelta(days=1), today),
            (today, today + timedelta(days=1)),
        )
        csp = SCRAPERS["resource_adequacy"].reports[1]
        self.assertEqual(pipeline._bounds(csp, None, today, today)[1], date(2025, 7, 1))

    def test_groupzip_uses_pacific_trading_days_including_dst(self):
        report = SCRAPERS["public_bids"].reports[0]
        start, end = pipeline._query_bounds(report, date(2025, 3, 9))
        self.assertEqual(
            (
                end.astimezone(timezone.utc) - start.astimezone(timezone.utc)
            ).total_seconds(),
            23 * 3600,
        )
        self.assertEqual(start.astimezone(timezone.utc).hour, 8)

    def test_fall_dst_groupzip_is_one_request_for_a_25_hour_day(self):
        report = SCRAPERS["public_bids"].reports[0]
        start, end = pipeline._query_bounds(report, date(2025, 11, 2))
        payload = zip_payload({"bids.csv": "RESOURCE_ID,MW\n0001,2\n"})
        with patch.object(self.client, "_fetch", return_value=payload) as fetch:
            self.client.download(report, start, end)
        fetch.assert_called_once()
        params = fetch.call_args.args[0]
        self.assertEqual(params["startdatetime"], "20251102T07:00-0000")
        self.assertEqual(params["enddatetime"], "20251103T08:00-0000")

    def test_invalid_report_configuration_stops_remaining_dates(self):
        payload = zip_payload(
            {
                "ERR.xml": "<ERROR><ERR_CODE>1001</ERR_CODE><ERR_DESC>Invalid parameters</ERR_DESC></ERROR>"
            }
        )
        with patch.object(self.client, "_fetch", return_value=payload) as fetch:
            summary = self.run_pipeline()
        self.assertEqual((fetch.call_count, summary.failed), (1, 1))

    def test_groupzip_processing_response_is_retried_before_staging(self):
        report = SCRAPERS["public_bids"].reports[0]
        start, end = pipeline._query_bounds(report, self.day)
        pending = zip_payload(
            {
                "ERR.xml": "<ERROR><ERR_CODE>1015</ERR_CODE><ERR_DESC>Processing</ERR_DESC></ERROR>"
            }
        )
        ready = zip_payload({"bids.csv": "RESOURCE_ID,MW\n0001,2\n"})
        with (
            patch.object(self.client, "_fetch", side_effect=[pending, ready]) as fetch,
            patch(
                "comm_research.infra.scrapers.caiso.oasis.client.time.sleep"
            ) as sleep,
        ):
            paths = self.client.download(report, start, end)
        self.assertEqual(fetch.call_count, 2)
        sleep.assert_called_once_with(5.0)
        self.assertEqual(len(paths), 1)

    def test_http_access_denial_stops_report_without_error_storm(self):
        with patch.object(
            self.client,
            "_fetch",
            side_effect=HTTPError("url", 403, "Forbidden", {}, None),
        ) as fetch:
            summary = self.run_pipeline()
        self.assertEqual((fetch.call_count, summary.failed), (1, 1))

    def test_configuration_errors_fail_before_network_or_cleanup(self):
        for kwargs in (
            {"categories": ["unknown"]},
            {"datasets": ["unknown"]},
            {"ignore_end_date": False, "end_date": None},
            {"nodes": []},
            {"raw_root": self.lake},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.run_pipeline(**kwargs)

    def test_concurrent_runner_is_rejected(self):
        with pipeline._run_lock(self.state), self.assertRaises(RuntimeError):
            self.run_pipeline()


class SnapshotAndCleanupTests(unittest.TestCase):
    def test_public_bid_local_times_and_nullable_gmt_columns_remain_distinct(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "bids.csv"
            raw.write_text(
                "STARTTIME,STOPTIME,STARTTIME_GMT,STOPTIME_GMT,UPDATED_GMT,XAXISDATA\n"
                "07/01/2026 00:00,07/01/2026 01:00,2026-07-01T07:00:00Z,2026-07-01T08:00:00Z,,3.5\n"
            )
            table = read_csv(raw, report=SCRAPERS["public_bids"].reports[0])
            self.assertEqual(table["starttime"][0], "07/01/2026 00:00")
            self.assertEqual(
                table.schema["interval_start_time_gmt"], pl.Datetime("us", "UTC")
            )
            self.assertEqual(table.schema["updated_gmt"], pl.Datetime("us", "UTC"))
            self.assertEqual(table.schema["xaxisdata"], pl.Float64)

    def test_snapshot_with_no_interval_and_alternate_interval_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / "snapshot.csv"
            raw.write_text("RESOURCE_ID,ON_MW\n0001,12.5\n")
            report = Report("TEST", None, 1, 1, "snapshot", snapshot=True)
            paths = normalize_csvs(
                [raw],
                "snapshot",
                lake_root=root / "lake",
                report=report,
                request_day=date(2025, 1, 2),
            )
            frame = pl.read_parquet(paths[0], hive_partitioning=False)
            self.assertEqual(frame["resource_id"][0], "0001")
            self.assertEqual(frame.schema["on_mw"], pl.Float64)
            self.assertEqual(frame["as_of_date"][0], date(2025, 1, 2))
            raw.write_text(
                "INTERVAL_START_GMT,INTERVAL_END_GMT,VALUE\n2025-01-01T00:00:00Z,2025-01-01T01:00:00Z,2\n"
            )
            paths = normalize_csvs([raw], "interval", lake_root=root / "lake")
            self.assertIn("day=01", str(paths[0]))

    def test_clear_raw_removes_files_and_dirs_but_preserves_failures_and_symlink_targets(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / "raw"
            folder = raw / "query"
            folder.mkdir(parents=True)
            (folder / "success.csv").write_text("persisted")
            failed = folder / "failed.csv"
            failed.write_text("unpersisted")
            target = root / "outside.txt"
            target.write_text("keep")
            (raw / "link").symlink_to(target)
            removed = _clear_raw(raw, keep=[failed])
            self.assertEqual(removed, 2)
            self.assertTrue(failed.exists() and target.exists())
            _clear_raw(raw)
            self.assertEqual(list(raw.iterdir()), [])


class RegistryTests(unittest.TestCase):
    def test_unique_datasets_and_protocol_parameters(self):
        reports = [
            report for scraper in SCRAPERS.values() for report in scraper.reports
        ]
        self.assertEqual(len(reports), len({report.dataset_name for report in reports}))
        for report in reports:
            with self.subTest(report=report.dataset_name):
                overrides = {key: "example" for key in report.required_parameters}
                params = report.request_parameters(
                    overrides, node="NODE" if report.requires_node else None
                )
                self.assertNotIn("resultformat", params)
                self.assertNotIn("startdatetime", params)
        with self.assertRaises(ValueError):
            DAM_LMP.request_parameters({"resultformat": "5"}, node="NODE")


if __name__ == "__main__":
    unittest.main()
