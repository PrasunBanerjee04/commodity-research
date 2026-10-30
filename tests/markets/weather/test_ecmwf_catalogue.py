"""Archive discovery and deterministic selection, using real coordinate NetCDF."""
from datetime import date, datetime, timezone
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import URLError

import polars as pl

from comm_research.markets.weather import ecmwf
from comm_research.markets.weather.scrapers.ecmwf_s2s import catalogue, pipeline


def targets(*dates):
    return pl.DataFrame({"timestamp": [datetime.combine(d, datetime.min.time(), timezone.utc) for d in dates],
                         "lat": [33.75]*len(dates), "lon": [-119.25]*len(dates)})


class PlanningTests(unittest.TestCase):
    def test_newest_eligible_vintage_once_per_week(self):
        frame = targets(date(2017, 1, 30), date(2000, 1, 3), date(2026, 1, 26))
        references = {date(2023, 1, 4): 'CY48', date(2024, 1, 4): 'CY48',
                      date(2025, 1, 4): 'CY49', date(2027, 1, 4): 'CY50'}
        jobs, report = pipeline.plan(frame, references, 26, as_of=date(2026, 10, 30))
        self.assertEqual(jobs, [(date(2025, 1, 4), [date(2017, 1, 4)])])
        selected = report.filter(pl.col('matched'))
        self.assertEqual(selected['reference_date'].to_list(), [date(2025, 1, 4)])
        self.assertEqual(selected['model_cycle'].to_list(), ['CY49'])
        self.assertEqual(selected['status'].to_list(), ['planned'])
        self.assertTrue(all(report.filter(~pl.col('matched'))['reason']))

    def test_twenty_year_and_future_initialization_limits(self):
        frame = targets(date(2000, 1, 3), date(2026, 1, 26))
        # Initializations: 1999-12-08 and 2025-12-31.
        jobs, report = pipeline.plan(frame, [date(2019, 12, 8), date(2025, 12, 31)], 26,
                                     as_of=date(2026, 1, 1))
        self.assertEqual(jobs, [(date(2019, 12, 8), [date(1999, 12, 8)])])
        self.assertEqual(report['matched'].to_list(), [True, False])
        _, report = pipeline.plan(frame, [date(2020, 12, 8)], 26, as_of=date(2026, 1, 1))
        self.assertFalse(report['matched'].any())

    def test_leap_day_matches_exactly(self):
        frame = targets(date(2016, 3, 7))
        jobs, report = pipeline.plan(frame, [date(2020, 2, 29), date(2021, 2, 28)], 7)
        self.assertEqual(jobs, [(date(2020, 2, 29), [date(2016, 2, 29)])])
        self.assertEqual(report['initialization_date'][0], date(2016, 2, 29))

    def test_default_orchestrator_discovers_but_manual_override_does_not(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pl.DataFrame({'timestamp': ['2017-01-30T00:00:00Z'], 'lat': [33.75], 'lon': [-119.25]}).write_csv(root/'target.csv')
            options = dict(targets=root, raw=root/'raw', output=root/'out', dry_run=True,
                           as_of=date(2026, 1, 1))
            with patch.object(pipeline, 'discover', return_value={date(2025, 1, 4): 'CY49'}) as discovery, \
                 patch.object(pipeline.Client, 'retrieve') as retrieve:
                report = ecmwf.run(**options)
                self.assertEqual(report['model_cycle'][0], 'CY49')
                self.assertEqual(report['status'][0], 'planned')
                discovery.assert_called_once()
                retrieve.assert_not_called()
                self.assertFalse((root/'out').exists())
            with patch.object(pipeline, 'discover') as discovery:
                report = ecmwf.run(reference_dates=[date(2024, 1, 4)], **options)
                discovery.assert_not_called()
                self.assertTrue(report['matched'][0])

    def test_discovery_failure_never_falls_back_to_calendar_guesses(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pl.DataFrame({'timestamp': ['2017-01-30T00:00:00Z'], 'lat': [33.75], 'lon': [-119.25]}).write_csv(root/'target.csv')
            with patch.object(pipeline, 'discover', side_effect=RuntimeError('denied')), \
                 patch.object(pipeline.Client, 'retrieve') as retrieve:
                with self.assertRaisesRegex(RuntimeError, 'denied'):
                    ecmwf.run(targets=root, raw=root/'raw', output=root/'out', dry_run=True)
                retrieve.assert_not_called()


@unittest.skipUnless(importlib.util.find_spec('netCDF4'), 'Install .[ecmwf] for catalogue checks')
class CatalogueTests(unittest.TestCase):
    def coordinate(self, dates):
        import netCDF4 as nc
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'coordinate.nc'
            with nc.Dataset(path, mode='w') as ds:
                ds.createDimension('S', len(dates))
                var = ds.createVariable('S', 'f8', ('S',))
                var.units = 'hours since 1960-01-01 00:00:00'
                var[:] = nc.date2num([datetime.combine(d, datetime.min.time()) for d in dates], var.units)
            return path.read_bytes()

    def test_cycle_links_and_coordinate_decoding(self):
        self.assertEqual(catalogue.cycles_from_html(b'<a href=".CY48/">48</a><a href="/x/.CY49/">49</a>'), ['CY48', 'CY49'])
        with self.assertRaises(ValueError):
            catalogue.cycles_from_html(b'<html>Sign in</html>')
        dates = [date(2024, 1, 4), date(2024, 2, 29)]
        self.assertEqual(catalogue.dates_from_netcdf(self.coordinate(dates)), set(dates))
        with self.assertRaises(ValueError):
            catalogue.dates_from_netcdf(b'<html>Sign in</html>')

    def test_discovery_intersects_members_and_caps_as_of(self):
        cf = self.coordinate([date(2024, 1, 4), date(2024, 1, 8), date(2027, 1, 4)])
        pf = self.coordinate([date(2024, 1, 4), date(2027, 1, 4)])
        def fetch(url, cache, refresh=False):
            if url.endswith('index.html'):
                return b'<a href=".CY48/">cycle</a>'
            return cf if '/.control/' in url else pf
        with tempfile.TemporaryDirectory() as directory, patch.object(catalogue, 'fetch', side_effect=fetch):
            result = catalogue.discover(cache=directory, as_of=date(2026, 1, 1))
            self.assertEqual(result, {date(2024, 1, 4): 'CY48'})

    def test_network_denial_is_not_cached_and_fresh_cache_is_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(catalogue, 'open_catalogue', side_effect=URLError('403 Forbidden')) as request:
                with self.assertRaisesRegex(RuntimeError, 'No dates were guessed'):
                    catalogue.fetch(catalogue.ROOT, directory)
                self.assertEqual(list(Path(directory).iterdir()), [])
            data = self.coordinate([date(2024, 1, 4)])
            catalogue.cache_validated(catalogue.ROOT, data, directory)
            with patch.object(catalogue, 'open_catalogue') as request:
                self.assertEqual(catalogue.fetch(catalogue.ROOT, directory), data)
                request.assert_not_called()


if __name__ == '__main__':
    unittest.main()
