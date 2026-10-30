"""Exact weekly alignment, member completeness, GRIB decoding and restart safety."""
from datetime import date, datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

try:
    import numpy as np
except ImportError:
    np = None
import polars as pl

from comm_research.markets.weather.scrapers.ecmwf_s2s import normalize, pipeline
from comm_research.markets.weather.scrapers.ecmwf_s2s.client import request_for


@unittest.skipIf(np is None, "Install .[ecmwf] for forecast checks")
class ReforecastTests(unittest.TestCase):
    def test_requests_keep_control_and_all_ten_perturbations(self):
        r = request_for(date(2024, 1, 4), [date(2017, 1, 4)], [36, -123, 30, -114], True, 26)
        self.assertEqual(r['hdate'], '2017-01-04')
        self.assertEqual(r['number'], '1/2/3/4/5/6/7/8/9/10')
        self.assertEqual(r['step'].split('/'), [f'{24*d}-{24*(d+1)}' for d in range(26, 33)])
        self.assertNotIn('number', request_for(date(2024, 1, 4), [], [], False, 26))

    def records(self, missing=False, duplicate=False):
        lat = np.array([34.5, 34.5, 33, 33])
        lon = np.array([-120, -118.5, -120, -118.5])
        for m in range(11):
            for d in range(26, 33):
                if missing and (m, d) == (10, 32):
                    continue
                record = date(2017, 1, 4), m, d*24, (d+1)*24, lat, lon, 270+lat*.1+lon*.01+m+d
                yield record
                if duplicate and (m, d) == (0, 26):
                    yield record

    def test_seven_day_member_means_and_spatial_interpolation(self):
        result = normalize.weekly(['fixture'], [date(2017, 1, 4)], [(33.75, -119.25)], 26,
                                  reader=lambda _: self.records())
        for m in range(11):
            self.assertAlmostEqual(result[date(2017, 1, 4), m][0], 270+3.375-1.1925+m+29)

    def test_missing_duplicate_and_wrong_days_fail(self):
        for reader in (lambda _: self.records(missing=True), lambda _: self.records(duplicate=True)):
            with self.assertRaises(ValueError):
                normalize.weekly(['fixture'], [date(2017, 1, 4)], [(33.75, -119.25)], 26, reader=reader)
        with self.assertRaises(ValueError):
            normalize.weekly(['fixture'], [date(2017, 1, 4)], [(33.75, -119.25)], 25,
                             reader=lambda _: self.records())

    def test_interpolation_never_extrapolates_or_accepts_missing_values(self):
        record = next(self.records())
        for points in ([(40, -119)], [(33.5, -130)]):
            with self.assertRaises(ValueError):
                normalize.interpolate(*record[4:], points)
        values = record[-1].copy(); values[0] = np.nan
        with self.assertRaises(ValueError):
            normalize.interpolate(record[4], record[5], values, [(33.5, -119)])

    def test_exact_calendar_matching_and_twenty_year_boundary(self):
        ts = [datetime(y, 1, 30, tzinfo=timezone.utc) for y in (2006, 2017, 2023)]
        targets = pl.DataFrame(dict(timestamp=ts, lat=[33.75]*3, lon=[-119.25]*3))
        jobs, coverage = pipeline.plan(targets, [date(2024, 1, 4)], 26)
        self.assertEqual(jobs[0][1], [date(2006, 1, 4), date(2017, 1, 4), date(2023, 1, 4)])
        self.assertEqual(coverage['matched'].to_list(), [True]*3)
        jobs, coverage = pipeline.plan(targets, [date(2024, 1, 5)], 26)
        self.assertFalse(jobs)
        self.assertFalse(coverage['matched'].any())

    def test_raw_cache_is_atomic_and_request_specific(self):
        try:
            from comm_research.markets.weather.scrapers.ecmwf_s2s.client import Client
            import ecmwfapi
        except ImportError:
            self.skipTest('Install .[ecmwf] for API client checks')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def fetch(request):
                Path(request['target']).write_bytes(b'GRIB fixture')
            with patch.object(ecmwfapi, 'ECMWFDataServer') as server:
                server.return_value.retrieve.side_effect = fetch
                client = Client()
                first = client.retrieve({'date': '2024-01-04'}, root)
                self.assertEqual(client.retrieve({'date': '2024-01-04'}, root), first)
                self.assertEqual(server.return_value.retrieve.call_count, 1)
                second = client.retrieve({'date': '2024-01-08'}, root)
                self.assertNotEqual(first, second)
                def fail(request):
                    Path(request['target']).write_bytes(b'partial')
                    raise RuntimeError('network interrupted')
                server.return_value.retrieve.side_effect = fail
                with self.assertRaises(RuntimeError):
                    client.retrieve({'date': '2024-01-04'}, root, refresh=True)
                self.assertEqual(first.read_bytes(), b'GRIB fixture')
                self.assertEqual(list(root.glob('*.part')), [])

    def test_real_grib_to_output_and_resume(self):
        try:
            import eccodes as ec
        except ImportError:
            self.skipTest('Install .[ecmwf] for GRIB fixture checks')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            targets = root/'targets'; targets.mkdir()
            pl.DataFrame(dict(timestamp=['2017-01-30T00:00:00+00:00'],
                              lat=[33.75], lon=[-119.25])).write_csv(targets/'one.csv')
            gribs = []
            for members in ([0], range(1, 11)):
                path = root/f'{members[0]}.grib'
                with path.open('wb') as stream:
                    for m in members:
                        for d in range(26, 33):
                            g = ec.codes_grib_new_from_samples('regular_ll_sfc_grib2')
                            try:
                                for k, v in dict(productDefinitionTemplateNumber=11, paramId=167,
                                                 Ni=2, Nj=2, latitudeOfFirstGridPointInDegrees=34.5,
                                                 latitudeOfLastGridPointInDegrees=33,
                                                 longitudeOfFirstGridPointInDegrees=240,
                                                 longitudeOfLastGridPointInDegrees=241.5,
                                                 iDirectionIncrementInDegrees=1.5,
                                                 jDirectionIncrementInDegrees=1.5,
                                                 dataDate=20170104, dataTime=0,
                                                 perturbationNumber=m, stepType='avg',
                                                 startStep=d*24, endStep=(d+1)*24).items():
                                    ec.codes_set(g, k, v)
                                ec.codes_set_values(g, np.full(4, 270+m+d))
                                ec.codes_write(g, stream)
                            finally:
                                ec.codes_release(g)
                gribs.append(path)
            options = dict(targets=targets, reference_dates=[date(2024, 1, 4)],
                           output=root/'out', raw=root/'raw')
            with patch.object(pipeline.Client, 'retrieve', side_effect=gribs) as retrieve:
                result = pipeline.run(**options)
                self.assertEqual(retrieve.call_count, 2)
                self.assertEqual(result['matched_weeks'], 1)
                output = pl.read_parquet(result['files'][0])
                self.assertEqual(output['control'][0], 299)
                self.assertAlmostEqual(output['perturbed_10'][0], 309)
                self.assertEqual(output['initialization_date'][0], date(2017, 1, 4))
                pipeline.run(**options)
                self.assertEqual(retrieve.call_count, 2)
            # An incomplete refresh cannot replace a previously valid output.
            before = result['files'][0].read_bytes()
            with patch.object(pipeline.Client, 'retrieve', side_effect=gribs), \
                 patch.object(pipeline, 'weekly', side_effect=ValueError('missing member')):
                with self.assertRaises(ValueError):
                    pipeline.run(**options, refresh=True)
            self.assertEqual(result['files'][0].read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
