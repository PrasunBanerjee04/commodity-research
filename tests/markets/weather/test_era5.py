"""Regional CDS ingestion contracts using actual NetCDF fixtures."""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from threading import Lock
from unittest.mock import patch

import polars as pl

from comm_research.markets.weather.models.grid import WeatherGrid
from comm_research.markets.weather.scrapers import era5

HAS_WEATHER = all(importlib.util.find_spec(name) for name in ("numpy", "xarray", "netCDF4"))


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
        return pl.read_parquet(era5._partition(self.root/'lake', self.day), hive_partitioning=False)

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

    def test_notebook_download_and_preview_cells_execute_with_cds_fixture(self):
        import contextlib
        import io

        if not importlib.util.find_spec('nbformat') or not importlib.util.find_spec('IPython'):
            self.skipTest('Install .[notebooks] to execute notebook checks')
        import nbformat

        root = Path(__file__).resolve().parents[3]
        notebook = nbformat.read(root/'notebooks/markets/weather/era5_ingestion.ipynb', as_version=4)
        nbformat.validate(notebook)
        namespace = {}
        first_code_cell = True
        with patch.object(era5.ERA5Scraper, '_client', new=staticmethod(self.client_factory)), contextlib.redirect_stdout(io.StringIO()):
            for index, cell in enumerate(notebook.cells):
                if cell.cell_type != 'code':
                    continue
                exec(compile(cell.source, f'notebook-cell-{index}', 'exec'), namespace)
                if first_code_cell:
                    namespace.update(
                        NORTH=self.grid.north, WEST=self.grid.west, SOUTH=self.grid.south, EAST=self.grid.east,
                        LAKE_ROOT=str(self.root/'lake'), RAW_ROOT=str(self.root/'raw'), RUN_DOWNLOAD=True,
                    )
                    first_code_cell = False
        self.assertEqual(namespace['summary'].written_days, 1)
        self.assertEqual(self.read().height, 96)


if __name__ == '__main__':
    unittest.main()
