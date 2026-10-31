"""GEFS public archive contracts, real GRIB normalization and resumable pulls."""
from datetime import date, datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import polars as pl

from comm_research.markets.weather import gefs
from comm_research.markets.weather.scrapers.gefs import catalogue, client, normalizer, pipeline

HAS_GEFS = all(importlib.util.find_spec(name) for name in ('numpy', 'httpx', 'eccodes'))


class CatalogueTests(unittest.TestCase):
    def test_actual_noaa_index_format_and_exact_week_window(self):
        # Actual index lines from the live January 5, 2000 control probe.
        body = (b'1:0:d=2000010500:TMP:2 m above ground:246 hour fcst:ENS=low-res ctl\n'
                b'64:14754689:d=2000010500:TMP:2 m above ground:624 hour fcst:ENS=low-res ctl\n'
                b'65:14991371:d=2000010500:TMP:2 m above ground:630 hour fcst:ENS=low-res ctl\n')
        records = client.parse_index(body)
        self.assertEqual(client.byte_range(records, [624]), (14754689, 14991370))
        variants = b'1:0:d=2000010500:TMP:2 m above ground:26 day fcst:ENS=1\n2:100:d=2000010500:TMP:2 m above ground:630 hours fcst:ENS=1\n'
        self.assertEqual(client.parse_index(variants), [(0, 624), (100, 630)])
        self.assertEqual(catalogue.forecast_hours(26), list(range(624, 792, 6)))
        self.assertEqual(len(catalogue.forecast_hours(26)), 28)
        with self.assertRaisesRegex(ValueError, r"missing=\[636.*duplicate=\[\]"):
            client.byte_range(records, catalogue.forecast_hours(26))
        with self.assertRaisesRegex(ValueError, r"missing=\[\], duplicate=\[624\]"):
            client.byte_range([(0, 624), (100, 624), (200, 630)], [624, 630])
        for value in (True, 15, 29, 26.0):
            with self.assertRaises(ValueError): catalogue.forecast_hours(value)

    def test_exact_initialization_and_missing_runs_are_not_shifted(self):
        from unittest.mock import Mock
        fake = Mock()
        fake.members.side_effect = lambda d, **kw: set(catalogue.MEMBERS) if d == date(2000, 1, 5) else {'c00', 'p01'}
        fake.index.return_value = [(i*10, h) for i, h in enumerate(range(246, 841, 6))]
        days = [datetime(2000, 1, 3, tzinfo=timezone.utc), datetime(2000, 1, 31, tzinfo=timezone.utc),
                datetime(2000, 2, 1, tzinfo=timezone.utc), datetime(2026, 1, 26, tzinfo=timezone.utc)]
        frame = pl.DataFrame({'timestamp': days, 'lat': [34.0]*4, 'lon': [-119.0]*4})
        report = catalogue.plan(frame, fake, 26)
        self.assertEqual(report['matched'].to_list(), [False, True, False, False])
        self.assertEqual(report['initialization_date'][1], date(2000, 1, 5))
        self.assertEqual(fake.members.call_count, 2)  # Out-of-archive dates need no network.
        self.assertTrue(all(report.filter(~pl.col('matched'))['reason']))


@unittest.skipUnless(HAS_GEFS, 'Install .[gefs]')
class GEFSIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import eccodes as ec
        import numpy as np
        cls.objects = {}
        initialization = date(2000, 1, 5)
        for member in catalogue.MEMBERS:
            number = 0 if member == 'c00' else int(member[1:])
            messages, lines, offset = [], [], 0
            for i, hour in enumerate(range(246, 841, 6), 1):
                g = ec.codes_grib_new_from_samples('regular_ll_sfc_grib2')
                try:
                    for key, value in dict(centre='kwbc', productDefinitionTemplateNumber=1,
                                           paramId=167, Ni=4, Nj=3,
                                           latitudeOfFirstGridPointInDegrees=36,
                                           latitudeOfLastGridPointInDegrees=30,
                                           longitudeOfFirstGridPointInDegrees=237,
                                           longitudeOfLastGridPointInDegrees=246,
                                           iDirectionIncrementInDegrees=3,
                                           jDirectionIncrementInDegrees=3,
                                           dataDate=20000105, dataTime=0,
                                           perturbationNumber=number, forecastTime=hour).items():
                        ec.codes_set(g, key, value)
                    lat = np.repeat([36., 33., 30.], 4)
                    lon = np.tile([-123., -120., -117., -114.], 3)
                    ec.codes_set_values(g, 270 + lat*.1 + lon*.01 + number + hour/10000)
                    message = ec.codes_get_message(g)
                finally:
                    ec.codes_release(g)
                lines.append(f'{i}:{offset}:d=2000010500:TMP:2 m above ground:{hour} hour fcst:ENS={member}\n')
                messages.append(message); offset += len(message)
            key = catalogue.key_for(initialization, member)
            cls.objects[key] = b''.join(messages)
            if member == 'p01':
                cls.incomplete_p01 = b''.join(message for hour, message in zip(range(246,841,6), messages) if hour != 624)
            cls.objects[key+'.idx'] = ''.join(lines).encode()

        # A second real-GRIB week verifies that record mode continues safely.
        for key, body in list(cls.objects.items()):
            other_key = key.replace('2000010500', '2000011200')
            if key.endswith('.idx'):
                cls.objects[other_key] = body.replace(b'2000010500', b'2000011200')
            else:
                import tempfile
                with tempfile.TemporaryFile() as stream:
                    stream.write(body); stream.seek(0)
                    messages = []
                    while (g := ec.codes_grib_new_from_file(stream)) is not None:
                        try:
                            ec.codes_set(g, 'dataDate', 20000112)
                            messages.append(ec.codes_get_message(g))
                        finally:
                            ec.codes_release(g)
                cls.objects[other_key] = b''.join(messages)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.targets = self.root/'targets'; self.targets.mkdir()
        pl.DataFrame(dict(timestamp=['2000-01-31T00:00:00Z']*2,
                          lat=[33.75, 34.25], lon=[-119.25, -117.75])).write_csv(self.targets/'points.csv')
        self.calls, self.mode = [], None

    def transport(self, request):
        import httpx
        self.calls.append(request)
        self.assertNotIn('authorization', request.headers)
        if request.url.params.get('list-type'):
            names = catalogue.MEMBERS if self.mode != 'five-members' else catalogue.MEMBERS[:5]
            prefix = request.url.params['prefix']
            body = ('<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
                    '<IsTruncated>false</IsTruncated>' + ''.join(
                        f'<CommonPrefixes><Prefix>{prefix}{m}/</Prefix></CommonPrefixes>' for m in names)
                    + '</ListBucketResult>')
            return httpx.Response(200, content=body.encode())
        key = request.url.path.lstrip('/')
        body = self.objects.get(key)
        if body is None or (self.mode == 'missing-member' and '/p10/' in key):
            return httpx.Response(404)
        if key.endswith('.idx'):
            if self.mode in ('bad-index', 'missing-grib-step') and '/p01/' in key and '/2000010500/' in key:
                body = b'\n'.join(line for line in body.splitlines() if b':624 hour fcst:' not in line)+b'\n'
            return httpx.Response(200, content=body)
        if 'range' not in request.headers:
            if self.mode == 'missing-grib-step' and '/p01/' in key and '/2000010500/' in key:
                body = self.incomplete_p01
            return httpx.Response(200, content=body)
        if self.mode == 'ignore-range':
            return httpx.Response(200, content=body)
        start, stop = request.headers['range'].removeprefix('bytes=').split('-')
        start, stop = int(start), int(stop) if stop else len(body)-1
        return httpx.Response(206, content=body[start:stop+1], headers={
            'Content-Range': f'bytes {start}-{stop}/{len(body)}', 'ETag': '"fixture-etag"'})

    def make_client(self, *args, **kwargs):
        import httpx
        source = client.Client(*args, **kwargs)
        source.http.close()
        source.http = httpx.Client(transport=httpx.MockTransport(self.transport))
        return source

    def options(self):
        return dict(targets=self.targets, output=self.root/'lake', raw=self.root/'raw',
                    state=self.root/'state', min_free_gb=0, workers=3, retries=0)

    def test_full_pull_real_grib_all_members_samples_and_resume(self):
        import numpy as np
        with patch.object(pipeline, 'Client', side_effect=self.make_client):
            report = gefs.run(**self.options(), dry_run=True)
            self.assertEqual(report['status'].to_list(), ['planned'])
            self.assertFalse(any('range' in r.headers for r in self.calls))
            self.assertFalse((self.root/'lake').exists())
            result = gefs.run(**self.options(), dry_run=False)
            self.assertEqual(result['downloaded'], 1)
            self.assertEqual(result['coverage']['status'].to_list(), ['completed'])
            weekly = pl.read_parquet(result['files'][0]['weekly_path'])
            samples = pl.read_parquet(result['files'][0]['samples_path'])
            self.assertEqual(weekly.height, 2)
            self.assertEqual(samples.height, 2*28*11)
            self.assertEqual(samples['member'].n_unique(), 11)
            self.assertEqual(samples['forecast_hour'].unique().sort().to_list(), list(range(624, 792, 6)))
            self.assertEqual(samples['valid_time'].min(), datetime(2000, 1, 31, tzinfo=timezone.utc))
            self.assertEqual(samples['valid_time'].max(), datetime(2000, 2, 6, 18, tzinfo=timezone.utc))
            for m, column in enumerate(pipeline.COLUMNS):
                expected = 270+weekly['lat'].to_numpy()*.1+weekly['lon'].to_numpy()*.01+m+np.mean(range(624, 792, 6))/10000
                np.testing.assert_allclose(weekly[column].to_numpy(), expected, atol=2e-5)
            self.assertFalse(list((self.root/'raw').rglob('*.grib2')))
            count = len(self.calls)
            resumed = gefs.run(**self.options(), dry_run=False)
            self.assertEqual(resumed['skipped'], 1)
            self.assertEqual(len(self.calls), count)
            manifest = Path(result['files'][0]['weekly_path']).parent/'manifest.json'
            info = json.loads(manifest.read_text())
            self.assertEqual(info['source_grid']['nx'], 4)
            self.assertEqual(len(info['sources']), 11)
            self.assertEqual(pl.read_parquet(self.root/'state/files.parquet').height, 1)
            Path(result['files'][0]['weekly_path']).write_bytes(b'corrupt')
            repaired = gefs.run(**self.options(), dry_run=False)
            self.assertEqual(repaired['downloaded'], 1)
            self.assertEqual(pl.read_parquet(repaired['files'][0]['weekly_path']).height, 2)

    def test_missing_member_does_not_publish_week_and_retry_reuses_cache(self):
        self.mode = 'missing-member'
        with patch.object(pipeline, 'Client', side_effect=self.make_client):
            with self.assertRaises(FileNotFoundError):
                gefs.run(**self.options(), dry_run=False)
            self.assertFalse(list((self.root/'lake').rglob('weekly.parquet')))
            self.assertEqual(pl.read_parquet(self.root/'state/coverage.parquet')['status'].to_list(), ['planned'])
            self.assertTrue(list((self.root/'raw').rglob('*.grib2')))
            self.mode = None
            result = gefs.run(**self.options(), dry_run=False)
            self.assertEqual(result['downloaded'], 1)

    def test_range_ignored_and_wrong_member_fail(self):
        source = self.make_client(self.root/'raw', retries=0, min_free_gb=0)
        self.addCleanup(source.close)
        self.mode = 'ignore-range'
        with self.assertRaisesRegex(ValueError, 'invalid HTTP byte range'):
            source.download(catalogue.key_for(date(2000, 1, 5), 'c00'), catalogue.forecast_hours(26))
        self.assertFalse(list((self.root/'raw').rglob('*.grib2')))
        self.mode = None
        path, _ = source.download(catalogue.key_for(date(2000, 1, 5), 'c00'), catalogue.forecast_hours(26))
        with self.assertRaisesRegex(ValueError, 'initialization/member'):
            normalizer.normalize(path, date(2000, 1, 5), 'p01', [(33.75, -119.25)], catalogue.forecast_hours(26))
        with self.assertRaisesRegex(ValueError, 'Incomplete'):
            normalizer.normalize(path, date(2000, 1, 5), 'c00', [(33.75, -119.25)], list(range(624, 798, 6)))

    def test_cached_raw_integrity_and_metadata_run_rejection(self):
        source = self.make_client(self.root/'raw', retries=0, min_free_gb=0)
        self.addCleanup(source.close)
        key, steps = catalogue.key_for(date(2000, 1, 5), 'c00'), catalogue.forecast_hours(26)
        path, info = source.download(key, steps)
        self.assertFalse(info['cached'])
        _, info = source.download(key, steps)
        self.assertTrue(info['cached'])
        path.write_bytes(b'corrupt')
        _, info = source.download(key, steps)
        self.assertFalse(info['cached'])
        self.mode = 'five-members'
        report = catalogue.plan(catalogue.targets_from_csv(self.targets), source, 26, refresh=True)
        self.assertFalse(report['matched'][0])

    def test_incomplete_index_recovers_from_complete_grib_without_losing_samples(self):
        self.mode = 'bad-index'
        with patch.object(pipeline, 'Client', side_effect=self.make_client):
            result = gefs.run(**self.options(), dry_run=False)
        self.assertEqual(result['coverage']['status'].to_list(), ['completed'])
        path = Path(result['files'][0]['weekly_path'])
        info = json.loads((path.parent/'manifest.json').read_text())
        fallback = [source for source in info['sources'] if source['download_mode'] == 'full_file']
        self.assertEqual(len(fallback), 1)
        self.assertIn('/p01/', fallback[0]['key'])
        self.assertIn('missing=[624]', fallback[0]['index_issue'])
        samples = pl.read_parquet(result['files'][0]['samples_path'])
        self.assertEqual(samples.filter(pl.col('member') == 'p01')['forecast_hour'].n_unique(), 28)

    def test_full_file_fallback_still_rejects_genuinely_missing_samples(self):
        self.mode = 'missing-grib-step'
        with patch.object(pipeline, 'Client', side_effect=self.make_client):
            with self.assertRaisesRegex(ValueError, r'target=2000-01-31.*member=p01.*missing hours \[624\]'):
                gefs.run(**self.options(), dry_run=False)
        self.assertFalse(list((self.root/'lake').rglob('weekly.parquet')))
        self.assertEqual(pl.read_parquet(self.root/'state/coverage.parquet')['status'].to_list(), ['unavailable'])

    def test_record_mode_marks_whole_week_unavailable_and_continues(self):
        self.mode = 'missing-grib-step'
        frame = pl.read_csv(self.targets/'points.csv')
        pl.concat([frame, frame.with_columns(pl.lit('2000-02-07T00:00:00Z').alias('timestamp'))]).write_csv(self.targets/'points.csv')
        with patch.object(pipeline, 'Client', side_effect=self.make_client):
            result = gefs.run(**self.options(), dry_run=False, on_incomplete='record')
        self.assertEqual(result['unavailable'], 1)
        self.assertEqual(result['downloaded'], 1)
        self.assertEqual(result['coverage']['status'].to_list(), ['unavailable', 'completed'])
        self.assertIn('member=p01', result['coverage']['reason'][0])
        self.assertEqual(len(result['files']), 1)
        weekly = pl.read_parquet(result['files'][0]['weekly_path'])
        self.assertEqual(weekly['timestamp'].to_list(), [datetime(2000,2,7,tzinfo=timezone.utc)]*2)
        self.assertEqual(pl.read_parquet(self.root/'state/files.parquet').height, 1)
        self.assertEqual(pl.read_parquet(self.root/'state/coverage.parquet')['status'].to_list(), ['unavailable','completed'])

    def test_early_forecast_file_reports_available_hours_without_filling(self):
        import eccodes as ec
        key = catalogue.key_for(date(2000,1,5), 'p06')
        with tempfile.TemporaryFile() as stream:
            stream.write(self.objects[key]); stream.seek(0)
            messages = []
            while (g := ec.codes_grib_new_from_file(stream)) is not None:
                try:
                    ec.codes_set(g, 'stepUnits', 1)
                    if ec.codes_get(g, 'endStep') <= 384:
                        messages.append(ec.codes_get_message(g))
                finally:
                    ec.codes_release(g)
        path = self.root/'early.grib2'; path.write_bytes(b''.join(messages))
        with self.assertRaises(normalizer.IncompleteForecastError) as caught:
            normalizer.normalize(path, date(2000,1,5), 'p06', [(33.75,-119.25)], catalogue.forecast_hours(26))
        self.assertEqual(caught.exception.missing_hours, catalogue.forecast_hours(26))
        self.assertEqual(max(caught.exception.available_hours), 384)
        # An identity mismatch must stay a validation error, not a skippable gap.
        with self.assertRaisesRegex(ValueError, 'initialization/member'):
            normalizer.normalize(path, date(2000,1,12), 'p06', [(33.75,-119.25)], catalogue.forecast_hours(26))

    def test_interrupted_transfer_retries_without_publishing_partial_bytes(self):
        import httpx
        source = self.make_client(self.root/'raw', retries=1, min_free_gb=0)
        self.addCleanup(source.close)
        attempts = []
        def interrupted_once(request):
            response = self.transport(request)
            if 'range' in request.headers:
                attempts.append(1)
                if len(attempts) == 1:
                    return httpx.Response(206, content=response.content[:-5], headers=response.headers)
            return response
        source.http.close()
        source.http = httpx.Client(transport=httpx.MockTransport(interrupted_once))
        with patch.object(client.time, 'sleep'):
            path, _ = source.download(catalogue.key_for(date(2000, 1, 5), 'c00'), catalogue.forecast_hours(26))
        self.assertEqual(len(attempts), 2)
        self.assertTrue(path.exists())
        self.assertFalse(list((self.root/'raw').rglob('*.partial')))

    def test_grib_day_and_six_hour_units_are_normalized_to_elapsed_hours(self):
        import eccodes as ec
        import numpy as np
        key = catalogue.key_for(date(2000,1,5), 'c00')
        with tempfile.TemporaryFile() as stream:
            stream.write(self.objects[key]); stream.seek(0)
            messages = []
            while (g := ec.codes_grib_new_from_file(stream)) is not None:
                try:
                    ec.codes_set(g, 'stepUnits', 1)
                    hour = int(ec.codes_get(g, 'endStep'))
                    factor, unit = (24, 2) if hour % 24 == 0 else (6, 11)
                    ec.codes_set(g, 'indicatorOfUnitOfTimeRange', unit)
                    ec.codes_set(g, 'forecastTime', hour//factor)
                    messages.append(ec.codes_get_message(g))
                finally:
                    ec.codes_release(g)
        path = self.root/'time-units.grib2'; path.write_bytes(b''.join(messages))
        mean, values, _ = normalizer.normalize(path, date(2000,1,5), 'c00', [(33.75,-119.25)], catalogue.forecast_hours(26))
        expected = 270+33.75*.1-119.25*.01+np.mean(range(624,792,6))/10000
        np.testing.assert_allclose(mean, [expected], atol=2e-5)
        self.assertEqual(values['forecast_hour'].to_list(), catalogue.forecast_hours(26))

    def test_sparse_decoding_handles_noaa_complex_packing(self):
        import eccodes as ec
        import numpy as np
        key = catalogue.key_for(date(2000, 1, 5), 'c00')
        source_path = self.root/'source.grib2'
        source_path.write_bytes(self.objects[key])
        messages = []
        with source_path.open('rb') as stream:
            while (g := ec.codes_grib_new_from_file(stream)) is not None:
                try:
                    values = ec.codes_get_values(g)
                    ec.codes_set(g, 'packingType', 'grid_complex_spatial_differencing')
                    ec.codes_set(g, 'bitsPerValue', 24)
                    ec.codes_set_values(g, values)
                    messages.append(ec.codes_get_message(g))
                finally:
                    ec.codes_release(g)
        path = self.root/'complex.grib2'; path.write_bytes(b''.join(messages))
        points = [(33.75, -119.25)]
        means, _, _ = normalizer.normalize(path, date(2000, 1, 5), 'c00', points, catalogue.forecast_hours(26))
        expected = 270 + 33.75*.1 -119.25*.01 + np.mean(range(624,792,6))/10000
        np.testing.assert_allclose(means, [expected], atol=2e-5)

    def test_interpolation_periodicity_and_boundaries(self):
        import numpy as np
        lat = np.repeat([1., 0.], 4)
        lon = np.tile([-180., -90., 0., 90.], 2)
        unique, inverse, weights = normalizer.sampler(lat, lon, [(0.5, 135.)])
        values = np.array([10., 20., 30., 40., 10., 20., 30., 40.])
        np.testing.assert_allclose((values[unique][inverse]*weights).sum(axis=1), [25.])
        with self.assertRaises(ValueError):
            normalizer.sampler(lat, lon, [(2., 0.)])


if __name__ == '__main__':
    unittest.main()
