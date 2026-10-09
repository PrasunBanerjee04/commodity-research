from datetime import date, datetime, timedelta, timezone
from email.message import Message
from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from zipfile import ZipFile

import polars as pl

from comm_research.infra.scrapers import caiso_oasis as oasis
from comm_research.infra.tools import caiso_normalizer as normalizer


START = datetime(2025, 1, 1, 8, tzinfo=timezone.utc)
HEADER = "INTERVALSTARTTIME_GMT,INTERVALENDTIME_GMT,OPR_DT,OPR_HR,NODE,MW,LMP_TYPE\n"


def csv_row(day=1, node="TH_SP15_GEN-APND", price="42.5"):
    return (f"2025-01-{day:02d}T00:00:00Z,2025-01-{day:02d}T01:00:00Z,"
            f"2025-01-{day:02d},1,{node},{price},LMP\n")


def zip_payload(members):
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def no_data_payload():
    return zip_payload(
        {
            "empty.xml": (
                "<OASISReport><ERROR><ERR_CODE>1000</ERR_CODE>"
                "<ERR_DESC>No data returned for the specified selection</ERR_DESC>"
                "</ERROR></OASISReport>"
            )
        }
    )


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def sleep(self, delay):
        self.sleeps.append(delay)
        self.now += delay


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.client = oasis.CAISOOASISClient(self.root)

    def test_windows_and_parameters(self):
        end = START + timedelta(days=31)
        self.assertEqual([int((b - a).total_seconds() / 86400)
                          for a, b in oasis.query_windows(START, end, 15)], [15, 15, 1])
        for report, expected in ((oasis.DAM_LMP, 1), (oasis.RTM_LMP, 3),
                                 (oasis.DAM_LOAD_FORECAST, 1)):
            with self.subTest(report=report):
                with patch.object(self.client, "_fetch", return_value=zip_payload(
                    {"data.csv": HEADER + csv_row()})) as fetch:
                    files = self.client.download(report, START, START + timedelta(days=3),
                                                 node="TH_SP15_GEN-APND" if report.requires_node else None)
                self.assertEqual(len(files), expected)
                self.assertEqual(fetch.call_count, expected)
                params = fetch.call_args_list[0].args[0]
                self.assertEqual(params["queryname"], report.queryname)
                self.assertEqual(params["market_run_id"], report.market_run_id)
                self.assertEqual(params["version"], 12 if report.queryname == "PRC_LMP" else 1)
                self.assertEqual(params["resultformat"], 6)
                self.assertEqual(params["startdatetime"], "20250101T08:00-0000")
                self.assertTrue(all(path.parent == self.root / report.queryname for path in files))

    def test_invalid_boundaries_and_missing_node(self):
        for start, end in ((START, START), (START.replace(tzinfo=None), START),
                           (START.replace(second=1), START + timedelta(days=1))):
            with self.assertRaises(ValueError):
                list(oasis.query_windows(start, end, 1))
        with self.assertRaises(ValueError):
            self.client.download(oasis.DAM_LMP, START, START + timedelta(days=1))

    def test_main_downloads_inclusive_start_and_end_dates(self):
        with patch("sys.argv", ["caiso_oasis", "--start", "2025-01-01", "--end", "2025-01-03"]), \
                patch.object(oasis, "run_backfill", return_value=[]) as backfill:
            oasis.main()
        backfill.assert_called_once_with(
            START,
            end=START + timedelta(days=3),
            nodes=oasis.DEFAULT_NODES,
        )

    def test_main_defaults_to_backfill(self):
        with patch("sys.argv", ["caiso_oasis"]), \
                patch.object(oasis, "run_backfill", return_value=[]) as backfill:
            oasis.main()
        backfill.assert_called_once_with(nodes=oasis.DEFAULT_NODES)

    def test_backfill_skips_existing_dates_and_refreshes_latest_per_node(self):
        lake = self.root / "lake"
        latest = lake / "dam_lmp/year=2025/month=01/day=03/data.parquet"
        latest.parent.mkdir(parents=True)
        pl.DataFrame(
            {
                "interval_start_time_gmt": [
                    datetime(2025, 1, 2, 8, tzinfo=timezone.utc),
                    datetime(2025, 1, 3, 8, tzinfo=timezone.utc),
                    datetime(2025, 1, 2, 8, tzinfo=timezone.utc),
                    datetime(2025, 1, 4, 8, tzinfo=timezone.utc),
                ],
                "node": [
                    "TH_SP15_GEN-APND",
                    "TH_SP15_GEN-APND",
                    "TH_NP15_GEN-APND",
                    "TH_NP15_GEN-APND",
                ],
            }
        ).write_parquet(latest)
        end = START + timedelta(days=5)
        staged = [self.root / "raw.csv"]

        with patch.object(oasis.CAISOOASISClient, "download", return_value=staged) as download, \
                patch.object(normalizer, "normalize_csvs", return_value=[]) as normalize:
            oasis.run_backfill(
                START,
                end=end,
                nodes=oasis.DEFAULT_NODES,
                raw_root=self.root / "raw",
                lake_root=lake,
            )

        expected = [
            ("TH_SP15_GEN-APND", START, START + timedelta(days=1)),
            ("TH_SP15_GEN-APND", START + timedelta(days=2), end),
            ("TH_NP15_GEN-APND", START, START + timedelta(days=1)),
            ("TH_NP15_GEN-APND", START + timedelta(days=2), end),
        ]
        actual = [
            (call.kwargs["node"], call.args[1], call.args[2])
            for call in download.call_args_list
        ]
        self.assertEqual(actual, expected)
        self.assertEqual(normalize.call_count, len(expected))

    def test_backfill_uses_supplied_start_when_no_partition_exists(self):
        lake = self.root / "empty_lake"
        staged = [self.root / "raw.csv"]

        with patch.object(oasis.CAISOOASISClient, "download", return_value=staged) as download, \
                patch.object(normalizer, "normalize_csvs", return_value=[]):
            oasis.run_backfill(
                START,
                end=START + timedelta(days=1),
                nodes=("TH_SP15_GEN-APND",),
                raw_root=self.root / "raw",
                lake_root=lake,
            )

        download.assert_called_once_with(
            oasis.DAM_LMP,
            START,
            START + timedelta(days=1),
            node="TH_SP15_GEN-APND",
        )

    def test_backfill_prints_flushed_progress_before_fetch(self):
        messages = []
        lake = self.root / "empty_lake"
        end = START + timedelta(days=1)

        def capture_print(message, **kwargs):
            messages.append(message)

        def download(*args, **kwargs):
            self.assertTrue(any("fetching 1 missing range(s)" in message for message in messages))
            return []

        with patch.object(oasis, "print", side_effect=capture_print, create=True), \
                patch.object(oasis.CAISOOASISClient, "download", side_effect=download):
            oasis.run_backfill(
                START,
                end=end,
                nodes=("TH_SP15_GEN-APND",),
                raw_root=self.root / "raw",
                lake_root=lake,
            )

        self.assertTrue(any("fetching 1 missing range(s)" in message for message in messages))
        self.assertTrue(any("no new files returned" in message for message in messages))

    def test_trade_day_windows_follow_pacific_dst(self):
        spring_start = oasis._trade_day_start(date(2025, 3, 8))
        spring_end = oasis._trade_day_start(date(2025, 3, 11))
        windows = list(oasis._trade_day_windows(spring_start, spring_end, 15))

        self.assertEqual(
            [(end - start).total_seconds() / 3600 for start, end in windows],
            [71],
        )
        self.assertEqual(windows[0][0].strftime("%Y%m%dT%H:%M-0000"), "20250308T08:00-0000")
        self.assertEqual(windows[0][1].strftime("%Y%m%dT%H:%M-0000"), "20250311T07:00-0000")

    def test_singlezip_start_date_uses_rolling_39_month_limit(self):
        self.assertEqual(
            oasis.earliest_singlezip_date(date(2026, 10, 8)),
            date(2023, 7, 8),
        )

    def test_missing_date_ranges_skip_existing_dates_but_refresh_latest(self):
        ranges = oasis._missing_date_ranges(
            date(2025, 1, 1),
            date(2025, 1, 6),
            {
                date(2025, 1, 2),
                date(2025, 1, 3),
                date(2025, 1, 5),
            },
        )

        self.assertEqual(
            ranges,
            [
                (date(2025, 1, 1), date(2025, 1, 2)),
                (date(2025, 1, 4), date(2025, 1, 6)),
            ],
        )

    def test_empty_backfill_does_not_call_normalizer(self):
        with patch.object(oasis.CAISOOASISClient, "download", return_value=[]), \
                patch.object(normalizer, "normalize_csvs") as normalize:
            outputs = oasis.run_backfill(
                START,
                end=START + timedelta(days=1),
                nodes=("TH_SP15_GEN-APND",),
                raw_root=self.root / "raw",
                lake_root=self.root / "lake",
            )

        self.assertEqual(outputs, [])
        normalize.assert_not_called()

    def test_errors_are_checked_before_any_csv_is_staged(self):
        payloads = [b"not a zip", zip_payload({}),
                    zip_payload({"ok.csv": HEADER + csv_row(), "nested/ERR.xml": "<ERR_CODE>1000</ERR_CODE>"}),
                    zip_payload({"error.csv": "<?xml version='1.0'?><error/>"}),
                    zip_payload({"data.csv": ""})]
        for payload in payloads:
            with self.subTest(payload=payload[:20]):
                with self.assertRaises(oasis.OASISError):
                    self.client._stage(payload, oasis.DAM_LMP)
                self.assertEqual(list(self.root.rglob("*.csv")), [])

    def test_download_skips_only_empty_data_windows(self):
        with patch.object(
            self.client,
            "_fetch",
            side_effect=[
                no_data_payload(),
                zip_payload({"data.csv": HEADER + csv_row()}),
                no_data_payload(),
            ],
        ) as fetch:
            files = self.client.download(
                oasis.DAM_LMP,
                START,
                START + timedelta(days=2),
                node="TH_SP15_GEN-APND",
            )

        self.assertEqual(fetch.call_count, 3)
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].read_text(), HEADER + csv_row())

    def test_zip_paths_do_not_escape_staging(self):
        files = self.client._stage(zip_payload({"../../escape.csv": HEADER + csv_row()}), oasis.DAM_LMP)
        self.assertEqual(files[0].parent, self.root / "PRC_LMP")
        self.assertEqual(files[0].read_text(), HEADER + csv_row())

    def test_shared_gate_spaces_successful_and_failed_requests(self):
        clock = FakeClock()
        gate = oasis.RequestGate()
        starts = []

        def request():
            starts.append(clock.now)
            if len(starts) == 2:
                raise RuntimeError("failed request")

        with patch.object(oasis.time, "monotonic", side_effect=lambda: clock.now), \
                patch.object(oasis.time, "sleep", side_effect=clock.sleep):
            gate.run(request)
            with self.assertRaises(RuntimeError):
                gate.run(request)
            gate.run(request)
        self.assertEqual(starts, [0.0, 5.0, 10.0])

    def test_retries_and_redirects_are_paced(self):
        clock = FakeClock()
        starts = []
        redirect_headers = Message()
        redirect_headers["Location"] = "https://oasis.caiso.com/oasisapi/SingleZip?resultformat=6"
        outcomes = [HTTPError(oasis.ENDPOINT, 302, "redirect", redirect_headers, None),
                    HTTPError(oasis.ENDPOINT, 429, "limited", Message(), None),
                    HTTPError(oasis.ENDPOINT, 503, "busy", Message(), None), BytesIO(b"zip bytes")]

        def open_response(url, **kwargs):
            starts.append(clock.now)
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        with patch.object(oasis, "_REQUEST_GATE", oasis.RequestGate()), \
                patch.object(oasis.time, "monotonic", side_effect=lambda: clock.now), \
                patch.object(oasis.time, "sleep", side_effect=clock.sleep), \
                patch.object(self.client._opener, "open", side_effect=open_response) as opener:
            self.assertEqual(self.client._fetch({"resultformat": 6}), b"zip bytes")
        self.assertEqual(starts, [0.0, 5.0, 10.0, 20.0])
        self.assertEqual(parse_qs(urlsplit(opener.call_args_list[0].args[0]).query), {"resultformat": ["6"]})
        self.assertEqual(clock.sleeps, [5.0, 5.0, 10.0])

    def test_retry_exhaustion_and_non_retryable_errors(self):
        self.client.max_retries = 1
        for status, expected in ((429, 2), (503, 2), (400, 1)):
            with self.subTest(status=status):
                with patch.object(oasis._REQUEST_GATE, "run", side_effect=lambda fn: fn()), \
                        patch.object(oasis.time, "sleep"), \
                        patch.object(self.client._opener, "open", side_effect=lambda *a, **k:
                                     (_ for _ in ()).throw(HTTPError(oasis.ENDPOINT, status, "error", Message(), None))) as opener:
                    with self.assertRaises(HTTPError):
                        self.client._fetch({})
                self.assertEqual(opener.call_count, expected)


class NormalizerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.lake = self.root / "lake"

    def stage(self, name, rows):
        path = self.root / name
        path.write_text(HEADER + rows)
        return path

    def test_three_day_pipeline_and_typed_parquet(self):
        client = oasis.CAISOOASISClient(self.root / "raw")
        with patch.object(client, "_fetch", return_value=zip_payload(
            {"prices.csv": HEADER + "".join(csv_row(day) for day in (1, 2, 3))})):
            raw = client.download(oasis.DAM_LMP, START, START + timedelta(days=3), node="TH_SP15_GEN-APND")
        outputs = normalizer.normalize_csvs(raw, "dam_lmp", lake_root=self.lake)
        self.assertEqual(len(outputs), 3)
        for day, path in enumerate(outputs, 1):
            self.assertEqual(path, self.lake / "dam_lmp" / "year=2025" / "month=01"
                             / f"day={day:02d}" / "data.parquet")
            table = pl.read_parquet(path, hive_partitioning=False)
            self.assertEqual(table.height, 1)
            self.assertEqual(table.schema["interval_start_time_gmt"], pl.Datetime("us", "UTC"))
            self.assertEqual(table.schema["interval_end_time_gmt"], pl.Datetime("us", "UTC"))
            self.assertEqual(table.schema["mw"], pl.Float64)
            self.assertEqual(table.schema["opr_hr"], pl.Int64)
            self.assertEqual(table.schema["opr_dt"], pl.Date)
            self.assertEqual(table["node"][0], "TH_SP15_GEN-APND")
        self.assertTrue(all(not path.exists() for path in raw))

    def test_existing_nodes_are_preserved_and_retries_deduplicate(self):
        first = self.stage("first.csv", csv_row(node="OTHER_NODE"))
        normalizer.normalize_csvs([first], "dam_lmp", lake_root=self.lake)
        for iteration in range(2):
            new = self.stage(f"new{iteration}.csv", csv_row())
            outputs = normalizer.normalize_csvs([new], "dam_lmp", lake_root=self.lake)
        table = pl.read_parquet(outputs[0], hive_partitioning=False)
        self.assertEqual(table.height, 2)
        self.assertEqual(set(table["node"]), {"OTHER_NODE", "TH_SP15_GEN-APND"})

    def test_invalid_input_and_failed_writes_retain_all_raw_files(self):
        valid = self.stage("valid.csv", csv_row())
        invalid = self.stage("invalid.csv", csv_row(price="not-numeric"))
        with self.assertRaises(pl.exceptions.InvalidOperationError):
            normalizer.normalize_csvs([valid, invalid], "dam_lmp", lake_root=self.lake)
        self.assertTrue(valid.exists() and invalid.exists())
        self.assertFalse(self.lake.exists())
        with patch.object(normalizer, "_write_atomic", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                normalizer.normalize_csvs([valid], "dam_lmp", lake_root=self.lake)
        self.assertTrue(valid.exists())

    def test_partial_batch_failure_can_be_retried_without_duplicates(self):
        raw = self.stage("batch.csv", csv_row(1) + csv_row(2))
        write = normalizer._write_atomic
        calls = 0

        def fail_second(frame, target):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("disk full")
            write(frame, target)

        with patch.object(normalizer, "_write_atomic", side_effect=fail_second):
            with self.assertRaises(OSError):
                normalizer.normalize_csvs([raw], "dam_lmp", lake_root=self.lake)
        self.assertTrue(raw.exists())
        outputs = normalizer.normalize_csvs([raw], "dam_lmp", lake_root=self.lake)
        self.assertTrue(all(pl.read_parquet(path).height == 1 for path in outputs))
        self.assertFalse(raw.exists())

    def test_utc_date_drives_partition_even_with_offset_timestamp(self):
        path = self.stage("offset.csv", csv_row().replace("2025-01-01T00:00:00Z", "2024-12-31T16:00:00-08:00")
                          .replace("2025-01-01T01:00:00Z", "2024-12-31T17:00:00-08:00"))
        outputs = normalizer.normalize_csvs([path], "dam_lmp", lake_root=self.lake)
        self.assertIn("year=2025/month=01/day=01", str(outputs[0]))

    def test_bad_schema_and_paths_are_rejected(self):
        for content in (HEADER, "NODE,MW\nNODE,1\n", HEADER + csv_row().replace("2025-01-01T00:00:00Z", ""),
                        HEADER + csv_row().replace("2025-01-01T01:00:00Z", "2025-01-01T00:00:00Z"),
                        "INTERVALSTARTTIME_GMT,interval_start_time_gmt\nfoo,bar\n"):
            path = self.root / "bad.csv"
            path.write_text(content)
            with self.assertRaises(ValueError):
                normalizer.read_csv(path)
            self.assertTrue(path.exists())
        with self.assertRaises(ValueError):
            normalizer.normalize_csvs([], "../escape", lake_root=self.lake)


if __name__ == "__main__":
    unittest.main()
