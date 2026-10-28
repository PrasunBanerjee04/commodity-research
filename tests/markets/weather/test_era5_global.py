"""ERA5 source decoding, UTC partitions, restart behavior, and failure safety."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from comm_research.markets.weather import era5
from comm_research.markets.weather.scrapers.era5_global import pipeline
from comm_research.markets.weather.scrapers.era5_global.client import (
    ERA5Client, HTTPStore, SourceError, SourceInfo,
)

HAS_ERA5 = all(importlib.util.find_spec(name) is not None
               for name in ("numpy", "pyarrow", "zarr", "httpx"))


class EntryPointTests(unittest.TestCase):
    def test_direct_file_and_module_help_work_outside_checkout(self):
        for arguments in ([str(Path(era5.__file__).resolve()), "--help"],
                          ["-m", "comm_research.markets.weather.era5", "--help"]):
            with self.subTest(arguments=arguments):
                result = subprocess.run([sys.executable, *arguments], cwd=tempfile.gettempdir(),
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("--min-free-gb", result.stdout)

    def test_failed_ingestion_has_nonzero_exit_status(self):
        with patch.object(era5, "_run", side_effect=SourceError("source denied")):
            self.assertEqual(era5.main(["--start", "2000-01-01", "--end", "2000-01-02"]), 1)


@unittest.skipUnless(HAS_ERA5, "Install .[era5] for ERA5 checks")
class SourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import numpy as np
        import zarr

        cls.objects = zarr.MemoryStore()
        group = zarr.group(store=cls.objects)
        for name, values, units in (
            ("latitude", np.linspace(90, -90, 721), "degrees_north"),
            ("longitude", np.arange(1440) * 0.25, "degrees_east"),
            ("time", np.arange(48), "hours since 2000-01-01 00:00:00"),
        ):
            arr = group.create_dataset(name, data=values)
            arr.attrs.update({"units": units, "_ARRAY_DIMENSIONS": [name]})
        temperature = group.create_dataset(
            "2m_temperature", shape=(48, 721, 1440), chunks=(1, 721, 1440), dtype="f4"
        )
        temperature.attrs.update({"units": "K", "_ARRAY_DIMENSIONS": ["time", "latitude", "longitude"]})
        temperature[0, :, :] = np.full((721, 1440), 280.25, dtype="float32")
        group.attrs.update({"valid_time_start": "2000-01-01T00:00:00Z",
                            "valid_time_stop": "2000-01-02T22:00:00Z",
                            "valid_time_stop_era5t": "2000-01-02T23:00:00Z",
                            "last_updated": "2000-04-01T00:00:00Z"})
        zarr.consolidate_metadata(cls.objects)

    def make_client(self, objects=None):
        import httpx

        objects = self.objects if objects is None else objects
        real_client = httpx.Client
        requested = []

        def handler(request):
            key = request.url.path.lstrip("/")
            requested.append(key)
            try:
                return httpx.Response(200, content=objects[key])
            except KeyError:
                return httpx.Response(404)

        with patch("httpx.Client", side_effect=lambda **kwargs: real_client(
            transport=httpx.MockTransport(handler), **kwargs
        )):
            client = ERA5Client(url="https://fixture.test", workers=2, retries=0)
        return client, requested

    def test_real_zarr_decoder_and_finalized_complete_day_bounds(self):
        import numpy as np

        client, requested = self.make_client()
        with client:
            self.assertEqual(client.info.cells, 721 * 1440)
            # Finalized data ends at 22:00 on Jan 2: omit the incomplete day,
            # even though ERA5T and the physical time array extend further.
            self.assertEqual(client.info.end_day, date(2000, 1, 2))
            values = client.read_hour(np.datetime64("2000-01-01T00:00"))
            self.assertEqual(values.shape, (721, 1440))
            self.assertTrue(np.all(values == 280.25))
            self.assertEqual(requested.count("2m_temperature/0.0.0"), 1)
            self.assertFalse(any("wind" in key or "level" in key for key in requested))

    def test_missing_chunk_is_failure_not_implicit_fill(self):
        import numpy as np

        client, _ = self.make_client()
        with client, self.assertRaisesRegex(SourceError, "HTTP 404"):
            client.read_hour(np.datetime64("2000-01-01T01:00"))

    def test_wrong_units_or_missing_hours_are_rejected(self):
        import numpy as np
        import zarr

        for failure in ("units", "time"):
            with self.subTest(failure=failure):
                objects = zarr.MemoryStore()
                for key in self.objects:
                    objects[key] = self.objects[key]
                group = zarr.open_group(objects)
                if failure == "units":
                    group["2m_temperature"].attrs["units"] = "degC"
                else:
                    group["time"][:] = np.arange(48) * 2
                zarr.consolidate_metadata(objects)
                with self.assertRaises(SourceError):
                    self.make_client(objects)

    def test_retries_honor_retry_after_and_do_not_retry_access_denial(self):
        import httpx

        store = HTTPStore("https://fixture.test", workers=1, retries=2, timeout=1)
        try:
            with patch.object(store.client, "get", side_effect=[
                httpx.Response(429, headers={"Retry-After": "5"}),
                httpx.Response(200, content=b"verified transport bytes"),
            ]) as get, patch("time.sleep") as sleep:
                self.assertEqual(store["chunk"], b"verified transport bytes")
                self.assertEqual(get.call_count, 2)
                self.assertGreaterEqual(sleep.call_args.args[0], 5)
            with patch.object(store.client, "get", return_value=httpx.Response(403)) as get:
                with self.assertRaisesRegex(SourceError, "HTTP 403"):
                    store["chunk"]
                self.assertEqual(get.call_count, 1)
        finally:
            store.close()


@unittest.skipUnless(HAS_ERA5, "Install .[era5] for ERA5 checks")
class PipelineTests(unittest.TestCase):
    def setUp(self):
        import numpy as np

        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.lake = Path(self.temporary.name) / "lake"
        self.first = date(2000, 2, 28)
        self.end = date(2000, 3, 1)
        self.info = SourceInfo(
            url="https://fixture.test", latitude=np.array([90, -90], dtype="float32"),
            longitude=np.array([0, 180, 270], dtype="float32"),
            times=np.datetime64("2000-02-28T00") + np.arange(48) * np.timedelta64(1, "h"),
            first_day=self.first, end_day=self.end,
            valid_time_stop="2000-02-29T23:00:00Z", last_updated="fixture",
        )
        self.calls = []
        self.fail_hour = None
        self.nonfinite = False
        outer = self

        class FakeClient:
            info = outer.info

            def __init__(self, **_kwargs):
                pass

            def read_hour(self, hour):
                outer.calls.append(hour)
                if outer.fail_hour is not None and hour == outer.fail_hour:
                    raise SourceError("download interrupted")
                index = int((hour - outer.info.times[0]) / np.timedelta64(1, "h"))
                values = (np.arange(6, dtype="float32").reshape(2, 3) + 270 + index)
                if outer.nonfinite:
                    values[0, 0] = np.nan
                return values

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                pass

        self.source_patch = patch.object(pipeline, "ERA5Client", FakeClient)
        self.source_patch.start()
        self.addCleanup(self.source_patch.stop)

    def run_pipeline(self, **overrides):
        settings = dict(start=self.first, end=self.end, lake=self.lake, workers=2,
                        min_free_gb=0)
        settings.update(overrides)
        return pipeline.run(**settings)

    def test_leap_days_schema_coordinate_alignment_and_resume(self):
        import pyarrow.parquet as pq
        import polars as pl

        result = self.run_pipeline()
        self.assertEqual((result.downloaded, result.rows_written), (2, 288))
        self.assertEqual(len(self.calls), 48)
        target = pipeline.partition(self.lake, self.first)
        frame = pl.read_parquet(target)
        self.assertEqual(frame.columns, ["timestamp", "latitude", "longitude", "temperature_2m"])
        self.assertEqual(frame["timestamp"].n_unique(), 24)
        self.assertEqual(frame["longitude"][:3].to_list(), [-180.0, -90.0, 0.0])
        self.assertEqual(frame["temperature_2m"][:3].to_list(), [271.0, 272.0, 270.0])
        schema = pq.read_schema(target)
        self.assertEqual(schema.field("temperature_2m").metadata[b"units"], b"K")
        self.assertEqual(str(schema.field("timestamp").type), "timestamp[us, tz=UTC]")
        self.assertEqual(schema.metadata[b"source_status"], b"finalized")
        manifest = self.lake / ".state/weather/era5/t_2m/2000/02/29.json"
        self.assertEqual(json.loads(manifest.read_text())["rows"], 144)
        self.calls.clear()
        again = self.run_pipeline()
        self.assertEqual((again.downloaded, again.skipped), (0, 2))
        self.assertEqual(self.calls, [])

    def test_failed_day_is_not_published_and_completed_day_resumes(self):
        import numpy as np

        self.fail_hour = np.datetime64("2000-02-29T10", "us")
        with self.assertRaisesRegex(SourceError, "interrupted"):
            self.run_pipeline()
        self.assertTrue(pipeline.partition(self.lake, self.first).exists())
        self.assertFalse(pipeline.partition(self.lake, date(2000, 2, 29)).exists())
        self.assertEqual(list(self.lake.rglob("*.partial")), [])
        self.assertFalse((self.lake / ".state/weather/era5/t_2m/2000/02/29.json").exists())
        self.fail_hour = None
        self.calls.clear()
        result = self.run_pipeline()
        self.assertEqual((result.downloaded, result.skipped), (1, 1))
        self.assertEqual(len(self.calls), 24)

    def test_missing_manifest_is_rebuilt_without_download(self):
        self.run_pipeline()
        manifest = self.lake / ".state/weather/era5/t_2m/2000/02/28.json"
        manifest.unlink()
        self.calls.clear()
        self.assertEqual(self.run_pipeline().skipped, 2)
        self.assertTrue(manifest.exists())
        self.assertEqual(self.calls, [])

    def test_recovered_manifest_keeps_original_file_provenance(self):
        from dataclasses import replace

        self.run_pipeline()
        manifest = self.lake / ".state/weather/era5/t_2m/2000/02/28.json"
        manifest.write_text("invalid JSON")
        old_info = self.info

        class UpdatedClient:
            info = replace(old_info, last_updated="new upstream update")

            def __init__(self, **_kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                pass

        with patch.object(pipeline, "ERA5Client", UpdatedClient):
            self.assertEqual(self.run_pipeline().skipped, 2)
        self.assertEqual(json.loads(manifest.read_text())["source_last_updated"], "fixture")

    def test_incomplete_own_partition_is_rebuilt(self):
        import pyarrow.parquet as pq

        self.run_pipeline()
        target = pipeline.partition(self.lake, self.first)
        table = pq.ParquetFile(target).read().slice(0, 6)
        pq.write_table(table, target)
        self.calls.clear()
        result = self.run_pipeline()
        self.assertEqual((result.downloaded, result.skipped), (1, 1))
        self.assertEqual(len(self.calls), 24)

    def test_nonfinite_temperature_cannot_be_published(self):
        self.nonfinite = True
        with self.assertRaisesRegex(ValueError, "nonfinite"):
            self.run_pipeline()
        self.assertFalse(pipeline.partition(self.lake, self.first).exists())
        self.assertEqual(list(self.lake.rglob("*.partial")), [])

    def test_failed_refresh_preserves_previous_partition(self):
        import numpy as np

        self.run_pipeline()
        target = pipeline.partition(self.lake, self.first)
        original = target.read_bytes()
        self.fail_hour = np.datetime64("2000-02-28T10", "us")
        with self.assertRaises(SourceError):
            self.run_pipeline(refresh=True)
        self.assertEqual(target.read_bytes(), original)

    def test_disk_guard_stops_before_downloading(self):
        from collections import namedtuple

        usage = namedtuple("usage", "total used free")(10, 10, 0)
        with patch.object(pipeline.shutil, "disk_usage", return_value=usage):
            with self.assertRaisesRegex(OSError, "Insufficient lake storage"):
                self.run_pipeline()
        self.assertEqual(self.calls, [])

    def test_dates_outside_finalized_window_are_not_silently_clamped(self):
        for settings in (dict(start=date(1999, 1, 1)), dict(end=date(2000, 3, 2)),
                         dict(start=self.end, end=None)):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                self.run_pipeline(**settings)
        self.assertEqual(self.calls, [])

    def test_missing_end_uses_latest_complete_finalized_day(self):
        self.assertEqual(self.run_pipeline(end=None).downloaded, 2)

    def test_existing_unrelated_file_requires_explicit_refresh(self):
        import pyarrow as pa
        import pyarrow.parquet as pq

        target = pipeline.partition(self.lake, self.first)
        target.parent.mkdir(parents=True)
        pq.write_table(pa.table({"unrelated": [1]}), target)
        with self.assertRaisesRegex(ValueError, "--refresh"):
            self.run_pipeline()
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
