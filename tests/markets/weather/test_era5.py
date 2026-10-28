"""Regional CDS ingestion contracts using actual NetCDF fixtures."""

from __future__ import annotations

import importlib.util
import tempfile
import time
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from threading import Event, Lock, get_ident
from unittest.mock import patch

import polars as pl

from comm_research.markets.weather.models.grid import WeatherGrid
from comm_research.markets.weather.scrapers import era5

HAS_WEATHER = all(importlib.util.find_spec(name) for name in ("numpy", "xarray", "netCDF4"))


class VariableStorageTests(unittest.TestCase):
    def test_variable_partitions_are_independent_and_resumable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            day = date(2000, 1, 1)
            grid = WeatherGrid(north=0, west=0, south=0, east=0)
            scraper = era5.ERA5Scraper(
                grid, lake_root=root / "lake", raw_root=root / "raw",
                variables=("2m_temperature", "total_precipitation"),
            )
            frames = [pl.DataFrame({
                "timestamp": [datetime(2000, 1, 1, hour, tzinfo=era5.timezone.utc) for hour in range(24)],
                "latitude": [0.0] * 24, "longitude": [0.0] * 24,
                "variable": [variable] * 24, "value": [value] * 24,
                "unit": [unit] * 24,
            }, schema=era5.SCHEMA) for variable, value, unit in (
                ("2m_temperature", 280.0, "K"), ("total_precipitation", 0.01, "m"),
            )]
            scraper._write_day(pl.concat(frames), day)
            for variable, folder in (("2m_temperature", "t_2m"), ("total_precipitation", "precip")):
                target = root / "lake" / folder / "year=2000/month=01/day=01/data.parquet"
                self.assertEqual(pl.read_parquet(target)["variable"].unique().to_list(), [variable])
            self.assertTrue(era5._complete(scraper._read_day(day), grid, scraper.variables))
            temperature = era5.ERA5Scraper(grid, lake_root=root / "lake", raw_root=root / "raw")
            temperature._write_day(frames[0].with_columns(pl.lit(290.0).alias("value")), day)
            self.assertTrue(scraper._read_day(day).filter(pl.col("variable") == "total_precipitation").equals(frames[1]))


class WeatherGridTests(unittest.TestCase):
    def test_native_cell_alignment_and_cds_coordinate_order(self):
        grid = WeatherGrid(north=38.14, west=-122.12, south=37.6, east=-121.6)
        self.assertEqual(grid.cds_areas, ((38.0, -122.0, 37.75, -121.75),))
        self.assertEqual(grid.latitudes, (37.75, 38.0))
        self.assertEqual(grid.longitudes, (-122.0, -121.75))
        self.assertEqual(grid.point_count, 4)

    def test_alaska_can_cross_antimeridian_without_duplicate_seam(self):
        grid = WeatherGrid(north=52, west=179.75, south=52, east=-179.75, name="Aleutians")
        self.assertEqual(grid.point_count, 3)
        self.assertEqual(grid.longitudes, (-180.0, -179.75, 179.75))
        self.assertEqual(len(grid.cds_areas), 2)

    def test_full_global_grid_has_1440_longitudes(self):
        grid = WeatherGrid(north=90, west=-180, south=-90, east=180)
        self.assertEqual(grid.point_count, 721 * 1440)

    def test_invalid_bounds_are_rejected(self):
        for values in (dict(north=91), dict(south=2), dict(west=-181), dict(east=float('nan')),
                       dict(north=True), dict(west=0.01, east=0.02), dict(name="")):
            kwargs = dict(north=1, west=0, south=0, east=1)
            kwargs.update(values)
            with self.subTest(values=values), self.assertRaises(ValueError):
                WeatherGrid(**kwargs)


@unittest.skipUnless(HAS_WEATHER, "Install .[weather] to test CDS NetCDF ingestion")
class ERA5Tests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.day = date(2000, 1, 1)
        self.end = date(2000, 1, 2)
        self.grid = WeatherGrid(north=38, west=-122, south=37.75, east=-121.75, name="California sample")
        self.calls = []
        self.fail_variable = None
        self.missing_hour = False
        self.nonfinite = False
        self.experiments = False
        self.zipped = False
        self.wrong_grid = False
        self.offset = 0
        self.fixture_lock = Lock()
        outer = self

        class Client:
            def retrieve(self, dataset, request, target):
                import numpy as np
                import xarray as xr
                import zipfile

                with outer.fixture_lock:  # fixture writes also use netCDF4.
                    outer.calls.append((dataset, request))
                    variable = request["variable"][0]
                    if variable == outer.fail_variable:
                        raise RuntimeError("CDS unavailable")
                    north, west, south, east = request["area"]
                    latitudes = np.arange(round(south * 4), round(north * 4) + 1)[::-1] / 4
                    longitudes = np.arange(round(west * 4), round(east * 4) + 1) / 4
                    times = np.datetime64(f"{request['year'][0]}-{request['month'][0]}-{request['day'][0]}") + np.arange(24) * np.timedelta64(1, 'h')
                    if outer.missing_hour:
                        times = times[:-1]
                    if outer.wrong_grid:
                        latitudes = latitudes + 0.125
                    signed_lon = (longitudes + 180) % 360 - 180
                    values = (280 + outer.offset + np.arange(len(times))[:, None, None]
                              + latitudes[None, :, None] * 0.01 + signed_lon[None, None, :] * 0.001)
                    if outer.nonfinite:
                        values[0, 0, 0] = np.nan
                    dims = ('valid_time', 'latitude', 'longitude')
                    coords = dict(valid_time=times, latitude=latitudes, longitude=longitudes % 360)
                    if outer.experiments:
                        preliminary = np.full(values.shape, np.nan)
                        preliminary[0] = values[0]
                        values[0] = np.nan
                        values = np.stack([values, preliminary], axis=1)
                        dims = ('valid_time', 'expver', 'latitude', 'longitude')
                        coords['expver'] = [1, 5]
                    dataset = xr.Dataset({era5.VARIABLES[variable][0]: (dims, values, {'units': 'K'})}, coords=coords)
                    with era5._NETCDF_LOCK:
                        if outer.zipped:
                            nc = Path(target).with_suffix('.fixture.nc')
                            try:
                                dataset.to_netcdf(nc)
                                with zipfile.ZipFile(target, 'w') as archive:
                                    archive.write(nc, 'data.nc')
                            finally:
                                nc.unlink(missing_ok=True)
                        else:
                            dataset.to_netcdf(target)

        self.client_factory = Client

    def scraper(self, grid=None, **kwargs):
        return era5.ERA5Scraper(
            grid or self.grid, lake_root=self.root/'lake', raw_root=self.root/'raw',
            client_factory=self.client_factory, **kwargs,
        )

    def read(self):
        return pl.read_parquet(str(self.root/'lake'/'*'/f'year={self.day:%Y}/month={self.day:%m}/day={self.day:%d}/data.parquet'), hive_partitioning=False)

    def test_cds_request_and_hourly_schema(self):
        result = self.scraper().run(self.day, self.end)
        self.assertEqual((result.downloaded_requests, result.written_days, result.received_rows), (1, 1, 96))
        dataset, request = self.calls[0]
        self.assertEqual(dataset, 'reanalysis-era5-single-levels')
        self.assertEqual(request['area'], [38, -122, 37.75, -121.75])
        self.assertEqual(request['time'], [f'{hour:02d}:00' for hour in range(24)])
        self.assertEqual(request['grid'], [0.25, 0.25])
        self.assertEqual(self.read().schema, era5.SCHEMA)
        self.assertEqual(self.read()['timestamp'].n_unique(), 24)
        self.assertEqual(self.read()['unit'].unique().to_list(), ['K'])
        first = self.read().filter((pl.col('latitude') == 38) & (pl.col('longitude') == -122)).sort('timestamp')
        self.assertAlmostEqual(first['value'][0], 280 + 38 * .01 - 122 * .001)

    def test_bulk_arco_zarr_writes_daily_partitions_and_refreshes(self):
        import numpy as np
        import xarray as xr

        times = np.datetime64(self.day, "us") + np.arange(48) * np.timedelta64(1, "h")
        coordinates = {
            "time": times,
            "latitude": list(self.grid.latitudes),
            "longitude": [longitude % 360 for longitude in self.grid.longitudes],
        }

        def store(offset):
            values = np.arange(48 * self.grid.point_count, dtype="float64").reshape(
                48, len(self.grid.latitudes), len(self.grid.longitudes)
            ) + offset
            return xr.Dataset(
                {
                    "2m_temperature": (
                        ("time", "latitude", "longitude"),
                        values,
                        {"units": "K"},
                    )
                },
                coords=coordinates,
            )

        scraper = self.scraper()
        with patch("xarray.open_zarr", return_value=store(0)) as open_zarr:
            result = scraper.run_bulk(self.day, date(2000, 1, 3), store_url="fixture.zarr")
        self.assertEqual(
            (result.downloaded_requests, result.written_days, result.received_rows),
            (1, 2, 2 * 24 * self.grid.point_count),
        )
        self.assertEqual(open_zarr.call_args.kwargs, {
            "chunks": None, "storage_options": {"token": "anon"},
        })
        self.assertEqual(self.read().height, 24 * self.grid.point_count)
        second_day = pl.read_parquet(
            era5._partition(self.root / "lake", date(2000, 1, 2)),
            hive_partitioning=False,
        )
        self.assertEqual(second_day.height, 24 * self.grid.point_count)

        with patch("xarray.open_zarr") as open_zarr:
            skipped = scraper.run_bulk(self.day, date(2000, 1, 3), store_url="fixture.zarr")
        self.assertEqual(skipped.skipped_days, 2)
        open_zarr.assert_not_called()

        with patch("xarray.open_zarr", return_value=store(1000)):
            refreshed = scraper.run_bulk(
                self.day, self.end, store_url="fixture.zarr", refresh=True,
            )
        self.assertEqual(refreshed.written_days, 1)
        self.assertEqual(self.read()["value"].min(), 1000)

    def test_bulk_days_are_fetched_concurrently_and_written_without_duplicate_keys(self):
        import numpy as np
        import xarray as xr

        day_count = 4
        times = np.datetime64(self.day, "us") + np.arange(24 * day_count) * np.timedelta64(1, "h")
        dataset = xr.Dataset(
            {
                "2m_temperature": (
                    ("time", "latitude", "longitude"),
                    np.ones((len(times), len(self.grid.latitudes), len(self.grid.longitudes))),
                    {"units": "K"},
                )
            },
            coords={
                "time": times,
                "latitude": list(self.grid.latitudes),
                "longitude": [longitude % 360 for longitude in self.grid.longitudes],
            },
        )
        active = 0
        maximum_active = 0
        state_lock = Lock()
        active_selections = 0
        maximum_active_selections = 0
        selection_state_lock = Lock()
        normalize = era5._normalize_arco
        original_sel = xr.DataArray.sel

        def track_normalization(*args):
            nonlocal active, maximum_active
            with state_lock:
                active += 1
                maximum_active = max(maximum_active, active)
            try:
                time.sleep(0.02)
                return normalize(*args)
            finally:
                with state_lock:
                    active -= 1

        def track_selection(data_array, *args, **kwargs):
            nonlocal active_selections, maximum_active_selections
            with selection_state_lock:
                active_selections += 1
                maximum_active_selections = max(maximum_active_selections, active_selections)
            try:
                time.sleep(0.02)
                return original_sel(data_array, *args, **kwargs)
            finally:
                with selection_state_lock:
                    active_selections -= 1

        with (
            patch("xarray.open_zarr", return_value=dataset),
            patch.object(xr.DataArray, "sel", track_selection),
            patch.object(era5, "_normalize_arco", side_effect=track_normalization),
        ):
            result = self.scraper(workers=3).run_bulk(
                self.day, self.day + timedelta(days=day_count), store_url="fixture.zarr",
            )

        self.assertEqual(result.written_days, day_count)
        self.assertGreater(maximum_active, 1)
        self.assertEqual(maximum_active_selections, 1)
        for offset in range(day_count):
            frame = pl.read_parquet(
                era5._partition(self.root / "lake", self.day + timedelta(days=offset)),
                hive_partitioning=False,
            )
            self.assertEqual(frame.height, 24 * self.grid.point_count)
            self.assertFalse(frame.select(era5.KEYS).is_duplicated().any())

    def test_california_and_alaska_extend_the_same_parquet(self):
        self.scraper().run(self.day, self.end)
        old = self.read()
        alaska = WeatherGrid(north=61.25, west=-150, south=61, east=-149.75, name='Alaska sample')
        self.scraper(alaska).run(self.day, self.end)
        combined = self.read()
        self.assertEqual(combined.height, 192)
        self.assertEqual(len(list((self.root/'lake').rglob('*.parquet'))), 1)
        self.assertTrue(combined.join(old, on=list(era5.SCHEMA), how='semi').sort(era5.KEYS).equals(old))

    def test_repeated_and_contained_grids_skip_without_requests(self):
        self.scraper().run(self.day, self.end)
        self.assertEqual(self.scraper().run(self.day, self.end).skipped_days, 1)
        point = WeatherGrid(north=38, west=-122, south=38, east=-122)
        self.assertEqual(self.scraper(point).run(self.day, self.end).skipped_days, 1)
        self.assertEqual(len(self.calls), 1)

    def test_overlapping_grid_upserts_keys_and_refresh_keeps_outside_cells(self):
        self.scraper().run(self.day, self.end)
        expanded = WeatherGrid(north=38.25, west=-122.25, south=38, east=-122)
        self.scraper(expanded).run(self.day, self.end)
        self.assertEqual(self.read().height, 7 * 24)
        self.offset = 10
        self.scraper(expanded).run(self.day, self.end, refresh=True)
        frame = self.read()
        self.assertEqual(frame.height, 7 * 24)
        untouched = frame.filter((pl.col('latitude') == 37.75) & (pl.col('longitude') == -121.75)).sort('timestamp')
        self.assertAlmostEqual(untouched['value'][0], 280 + 37.75*.01 - 121.75*.001)
        self.assertFalse(frame.select(era5.KEYS).is_duplicated().any())

    def test_new_variable_preserves_existing_temperature(self):
        self.scraper().run(self.day, self.end)
        self.scraper(variables=('2m_dewpoint_temperature',)).run(self.day, self.end)
        self.assertEqual(self.read().height, 192)
        self.assertEqual(self.read()['variable'].n_unique(), 2)
        for variable in ('2m_temperature', '2m_dewpoint_temperature'):
            frame = pl.read_parquet(era5._partition(self.root/'lake', self.day, variable))
            self.assertEqual(frame['variable'].unique().to_list(), [variable])
        self.assertEqual(era5._partition(self.root/'lake', self.day).parts[-5], 't_2m')
        self.assertEqual(era5._partition(self.root/'lake', self.day, 'total_precipitation').parts[-5], 'precip')

    def test_antimeridian_seam_is_written_once(self):
        grid = WeatherGrid(north=52, west=179.75, south=52, east=-179.75)
        self.scraper(grid).run(self.day, self.end)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.read().height, 3 * 24)
        self.assertFalse(self.read().select(era5.KEYS).is_duplicated().any())

    def test_missing_hours_nan_and_wrong_grid_do_not_overwrite_lake(self):
        self.scraper().run(self.day, self.end)
        original = self.read()
        for mode in ('missing_hour', 'nonfinite', 'wrong_grid'):
            setattr(self, mode, True)
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                self.scraper().run(self.day, self.end, refresh=True)
            self.assertTrue(self.read().equals(original))
            setattr(self, mode, False)
        self.assertEqual(list((self.root/'raw').rglob('*.partial')), [])

    def test_final_expver_preferred_with_preliminary_filling_missing_values(self):
        self.experiments = True
        self.scraper().run(self.day, self.end)
        self.assertEqual(self.read().height, 96)
        self.assertEqual(self.read()['value'].null_count(), 0)

    def test_single_netcdf_in_cds_zip_response(self):
        self.zipped = True
        self.scraper().run(self.day, self.end)
        self.assertEqual(self.read().height, 96)

    def test_cached_completed_requests_resume_after_failure(self):
        self.fail_variable = '2m_dewpoint_temperature'
        scraper = self.scraper(variables=('2m_temperature', '2m_dewpoint_temperature'), workers=1)
        with self.assertRaisesRegex(RuntimeError, 'CDS unavailable'):
            scraper.run(self.day, self.end)
        self.assertFalse(era5._partition(self.root/'lake', self.day).exists())
        self.assertEqual(len(list((self.root/'raw').rglob('*.nc'))), 1)
        self.fail_variable = None
        result = scraper.run(self.day, self.end)
        self.assertEqual(result.downloaded_requests, 1)
        self.assertEqual(self.read().height, 192)
        self.assertEqual(list((self.root/'raw').rglob('*.nc')), [])

    def test_leap_day_partitions_and_exclusive_end(self):
        result = self.scraper().run(date(2000, 2, 28), date(2000, 3, 1))
        self.assertEqual(result.written_days, 2)
        self.assertTrue(era5._partition(self.root/'lake', date(2000, 2, 29)).exists())
        self.assertFalse(era5._partition(self.root/'lake', date(2000, 3, 1)).exists())

    def test_invalid_dates_variables_and_worker_counts_fail(self):
        for kwargs in (dict(variables='2m_temperature'), dict(variables=()),
                       dict(variables=('not_a_variable',)), dict(workers=0)):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.scraper(**kwargs)
        for start, end in ((self.end, self.day), (date(1939, 1, 1), self.end),
                           (datetime(2000, 1, 1), self.end)):
            with self.subTest(start=start), self.assertRaises(ValueError):
                self.scraper().run(start, end)

    def test_two_writers_cannot_share_lake_or_raw_cache(self):
        from comm_research.infra.lake.writer_lock import writer_lock

        with writer_lock(self.root/'raw'):
            with self.assertRaisesRegex(RuntimeError, 'Another writer'):
                self.scraper().run(self.day, self.end)

    def test_failed_atomic_merge_preserves_file_and_raw_for_retry(self):
        self.scraper().run(self.day, self.end)
        original = self.read()
        with patch.object(era5, 'write_atomic', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(OSError, 'disk full'):
                self.scraper(variables=('2m_dewpoint_temperature',)).run(self.day, self.end)
        self.assertTrue(self.read().equals(original))
        self.assertEqual(len(list((self.root/'raw').rglob('*.nc'))), 1)

    def test_minimal_notebook_resumes_bulk_ingestion_with_configured_globals(self):
        import contextlib
        import io

        if not importlib.util.find_spec('nbformat') or not importlib.util.find_spec('IPython'):
            self.skipTest('Install .[notebooks] to execute notebook checks')
        import nbformat

        root = Path(__file__).resolve().parents[3]
        notebook = nbformat.read(root/'notebooks/era5_ingestion.ipynb', as_version=4)
        nbformat.validate(notebook)
        namespace = {}
        configured = False
        import numpy as np
        import xarray as xr

        times = np.datetime64(self.day, "us") + np.arange(24) * np.timedelta64(1, "h")
        dataset = xr.Dataset(
            {
                "2m_temperature": (
                    ("time", "latitude", "longitude"),
                    np.ones((24, len(self.grid.latitudes), len(self.grid.longitudes))),
                    {"units": "K"},
                )
            },
            coords={
                "time": times,
                "latitude": list(self.grid.latitudes),
                "longitude": [longitude % 360 for longitude in self.grid.longitudes],
            },
        )
        with patch("xarray.open_zarr", return_value=dataset), contextlib.redirect_stdout(io.StringIO()):
            for index, cell in enumerate(notebook.cells):
                if cell.cell_type != 'code':
                    continue
                exec(compile(cell.source, f'notebook-cell-{index}', 'exec'), namespace)
                if not configured and "START_DATE" in namespace:
                    namespace.update(
                        NORTH=self.grid.north, WEST=self.grid.west, SOUTH=self.grid.south, EAST=self.grid.east,
                        START_DATE=self.day, END_DATE=self.end, VARIABLES=("2m_temperature",),
                        LAKE_ROOT=str(self.root/'lake'),
                    )
                    configured = True
        self.assertEqual(len([cell for cell in notebook.cells if cell.cell_type == "code"]), 2)
        self.assertEqual(namespace['summary'].written_days, 1)
        self.assertEqual(self.read().height, 96)

    def test_dates_download_concurrently_with_only_one_coordinator_writing(self):
        workers = 3
        all_started = Event()
        guard = Lock()
        active = peak = 0
        dates = set()
        writes = []
        coordinator = get_ident()
        fixture_client = self.client_factory
        real_write = era5.write_atomic

        class ConcurrentClient:
            def retrieve(self, dataset, request, target):
                nonlocal active, peak
                with guard:
                    active += 1
                    peak = max(peak, active)
                    dates.add(tuple(request[key][0] for key in ('year', 'month', 'day')))
                    if active == workers:
                        all_started.set()
                try:
                    if not all_started.wait(timeout=10):
                        raise RuntimeError('Dates were not downloaded concurrently')
                    fixture_client().retrieve(dataset, request, target)
                finally:
                    with guard:
                        active -= 1

        def write(frame, target):
            self.assertEqual(get_ident(), coordinator)
            writes.append(target)
            real_write(frame, target)

        self.client_factory = ConcurrentClient
        with patch.object(era5, 'write_atomic', side_effect=write):
            result = self.scraper(workers=workers).run(self.day, date(2000, 1, 4))
        self.assertEqual(peak, workers)
        self.assertEqual(len(dates), workers)
        self.assertEqual(result.written_days, workers)
        self.assertEqual(len(writes), len(set(writes)))

    def test_fast_date_is_published_before_slow_earlier_date_finishes(self):
        later_written = Event()
        fixture_client = self.client_factory
        real_write = era5.write_atomic
        written = []

        class DelayedClient:
            def retrieve(self, dataset, request, target):
                if request['day'] == ['01'] and not later_written.wait(timeout=10):
                    raise RuntimeError('A slow date blocked the completed later date')
                fixture_client().retrieve(dataset, request, target)

        def write(frame, target):
            real_write(frame, target)
            written.append(target.parent.name)
            if target.parent.name == 'day=02':
                later_written.set()

        self.client_factory = DelayedClient
        with patch.object(era5, 'write_atomic', side_effect=write):
            result = self.scraper(workers=2).run(self.day, date(2000, 1, 3))
        self.assertEqual(result.written_days, 2)
        self.assertEqual(written, ['day=02', 'day=01'])

    def test_scheduler_limits_lookahead_until_a_day_is_published(self):
        # The first day needs two variables. Both must be attempted within the
        # active two-day window before any request for a third day is allowed.
        release = Event()
        guard = Lock()
        started = []
        active = peak = 0
        published = []
        fixture_client = self.client_factory
        real_write = era5.write_atomic

        class WindowClient:
            def retrieve(self, dataset, request, target):
                nonlocal active, peak
                with guard:
                    active += 1
                    peak = max(peak, active)
                    started.append((request['day'][0], request['variable'][0]))
                    if len(started) == 2:
                        release.set()
                    if not published and request['day'][0] not in ('01', '02'):
                        raise RuntimeError('Unbounded date lookahead')
                try:
                    if not release.wait(timeout=10):
                        raise RuntimeError('Initial date window did not start')
                    fixture_client().retrieve(dataset, request, target)
                finally:
                    with guard:
                        active -= 1

        def write(frame, target):
            real_write(frame, target)
            with guard:
                published.append(target)

        self.client_factory = WindowClient
        with patch.object(era5, 'write_atomic', side_effect=write):
            result = self.scraper(workers=2, variables=('2m_temperature', '2m_dewpoint_temperature')).run(
                self.day, date(2000, 1, 8)
            )
        self.assertLessEqual(peak, 2)
        self.assertEqual(len(started), 14)
        self.assertEqual(result.written_days, 7)
        self.assertEqual(len(published), 7)

    def test_skipped_days_do_not_occupy_download_window(self):
        self.scraper().run(self.day, date(2000, 1, 4))
        era5._partition(self.root/'lake', date(2000, 1, 2)).unlink()
        self.calls.clear()
        result = self.scraper(workers=4).run(self.day, date(2000, 1, 4))
        self.assertEqual((result.written_days, result.skipped_days), (1, 2))
        self.assertEqual([request['day'] for _, request in self.calls], [['02']])

    def test_failure_does_not_enqueue_the_rest_of_history(self):
        self.fail_variable = '2m_temperature'
        with self.assertRaisesRegex(RuntimeError, 'CDS unavailable'):
            self.scraper(workers=1).run(self.day, date(2000, 2, 1))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(list((self.root/'lake').rglob('*.parquet')), [])

    def test_parallel_failure_keeps_locks_until_running_download_is_cached(self):
        from concurrent.futures import ThreadPoolExecutor
        from comm_research.infra.lake.writer_lock import writer_lock

        second_started = Event()
        failure_started = Event()
        release_second = Event()
        fixture_client = self.client_factory

        class FailingClient:
            def retrieve(self, dataset, request, target):
                if request['day'] == ['01']:
                    if not second_started.wait(timeout=10):
                        raise RuntimeError('Second date did not start')
                    failure_started.set()
                    raise RuntimeError('First date failed')
                second_started.set()
                if not release_second.wait(timeout=10):
                    raise RuntimeError('Running download was not released')
                fixture_client().retrieve(dataset, request, target)

        self.client_factory = FailingClient
        with ThreadPoolExecutor(max_workers=1) as caller:
            result = caller.submit(self.scraper(workers=2).run, self.day, date(2000, 2, 1))
            try:
                self.assertTrue(failure_started.wait(timeout=10))
                self.assertFalse(result.done())
                for root in (self.root/'lake', self.root/'raw'):
                    with self.assertRaisesRegex(RuntimeError, 'Another writer'):
                        with writer_lock(root):
                            pass
            finally:
                release_second.set()
            with self.assertRaisesRegex(RuntimeError, 'First date failed'):
                result.result(timeout=10)
        self.assertEqual(list((self.root/'lake').rglob('*.parquet')), [])
        self.assertEqual(len(list((self.root/'raw').rglob('*.nc'))), 1)
        self.assertEqual(list((self.root/'raw').rglob('*.partial')), [])
        self.client_factory = fixture_client
        retry = self.scraper(workers=2).run(self.day, date(2000, 1, 3))
        self.assertEqual((retry.downloaded_requests, retry.written_days), (1, 2))

    def test_lake_lock_prevents_a_second_writer_and_releases_raw_lock(self):
        from comm_research.infra.lake.writer_lock import writer_lock

        with writer_lock(self.root/'lake'):
            with self.assertRaisesRegex(RuntimeError, 'Another writer'):
                self.scraper(workers=4).run(self.day, self.end)
        self.assertEqual(self.scraper().run(self.day, self.end).written_days, 1)

    def test_each_cds_client_owns_a_separate_verified_http_session(self):
        if not importlib.util.find_spec('cdsapi'):
            self.skipTest('Install cdsapi to check session ownership')
        sessions = []

        def create_client(**kwargs):
            self.assertTrue(kwargs['verify'])
            sessions.append(kwargs['session'])
            return object()

        try:
            with patch('cdsapi.Client', side_effect=create_client):
                era5.ERA5Scraper._client()
                era5.ERA5Scraper._client()
            self.assertIsNot(sessions[0], sessions[1])
        finally:
            for session in sessions:
                session.close()


if __name__ == '__main__':
    unittest.main()
