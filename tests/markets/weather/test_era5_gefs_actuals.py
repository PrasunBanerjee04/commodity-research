"""Execute datasets.ipynb's matched-actuals helpers against hourly fixtures."""
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

import polars as pl

from comm_research.infra.lake.parquet import scan_parquet

NOTEBOOK = Path(__file__).resolve().parents[3] / 'notebooks/datasets.ipynb'


class MatchedActualsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.ids = self.root/'data/clean/weather/era5/t_2m/socal'
        self.output = self.root/'data/clean/weather/era5/t_2m/socal_gefs_6h'
        self.ids.mkdir(parents=True)
        self.week = datetime(2000,1,31,tzinfo=timezone.utc)
        self.coordinates = {0: (33.75,-121.), 7: (34.,-119.)}
        for grid_id, (lat,lon) in self.coordinates.items():
            pl.DataFrame({'timestamp':[self.week], 'lat':[lat], 'lon':[lon], '2m_mean':[999.]}).write_csv(self.ids/f'socal_temp_2m_{grid_id}.csv')
        self.original = {p.name:p.read_bytes() for p in self.ids.glob('*.csv')}
        self.ns = {'pl':pl,'Path':Path,'PROJECT_ROOT':self.root}
        notebook = json.loads(NOTEBOOK.read_text())
        helper = next(''.join(c['source']) for c in notebook['cells']
                      if c['cell_type']=='code' and 'def matched_era5_weeks(' in ''.join(c['source']))
        exec(compile(helper, str(NOTEBOOK), 'exec'), self.ns)
        self.grid = self.ns['existing_grid_ids'](self.ids,'socal')

    def hourly(self, weeks=(0,)):
        rows = []
        for w in weeks:
            for grid_id,(lat,lon) in self.coordinates.items():
                for hour in range(168):
                    rows.append({'timestamp':self.week+timedelta(weeks=w,hours=hour),
                                 'latitude':lat,'longitude':lon,'variable':'2m_temperature',
                                 'value':273.15+hour/10+grid_id,'unit':'K'})
        return pl.DataFrame(rows)

    def aggregate(self, hourly, **kwargs):
        return self.ns['matched_era5_weeks'](hourly,self.grid,**kwargs)

    def test_hourly_parquet_selects_exact_gefs_hours_and_preserves_ids_and_originals(self):
        hourly = self.hourly()
        raw = self.root/'hourly';raw.mkdir()
        hourly.head(100).write_parquet(raw/'part1.parquet')
        hourly.slice(100).write_parquet(raw/'part2.parquet')
        coverage = self.aggregate(scan_parquet(raw))
        self.assertEqual(coverage['status'].to_list(),['complete','complete'])
        self.assertEqual(coverage['n_samples'].to_list(),[28,28])
        exported = self.ns['export_matched_actuals'](coverage,self.grid,self.output,self.ids)
        for grid_id in (0,7):
            row = pl.read_csv(self.output/f'socal_temp_2m_{grid_id}.csv').row(0,named=True)
            self.assertEqual(row['grid_id'],grid_id)
            self.assertAlmostEqual(row['2m_mean'],273.15+8.1+grid_id)
            self.assertAlmostEqual(row['2m_max_6h'],273.15+16.2+grid_id)
            self.assertEqual(row['n_samples'],28)
            self.assertEqual(row['sampling_hours'],6)
        self.assertEqual(exported.height,2)
        self.assertEqual({p.name:p.read_bytes() for p in self.ids.glob('*.csv')},self.original)
        self.assertTrue((self.output/'_audit/coverage.csv').exists())
        self.assertEqual(len(list(self.output.glob('*.csv'))),2)

    def test_missing_calendar_week_and_partial_grid_week_are_audited_and_excluded(self):
        hourly = self.hourly(weeks=(0,2)).filter(~((pl.col('latitude')==34.) & (pl.col('timestamp')==self.week+timedelta(hours=6))))
        coverage = self.aggregate(hourly)
        self.assertEqual(coverage.filter(pl.col('status')=='missing').height,2)
        self.assertEqual(coverage.filter(pl.col('status')=='incomplete')['n_samples'].to_list(),[27])
        exported = self.ns['export_matched_actuals'](coverage,self.grid,self.output,self.ids)
        self.assertEqual(exported.height,3)
        self.assertTrue((exported['n_samples']==28).all())

    def test_duplicate_or_invalid_synoptic_samples_stop_export(self):
        hourly = self.hourly()
        duplicate = pl.concat([hourly,hourly.head(1)])
        invalid = hourly.with_columns(pl.when(pl.col('timestamp')==self.week).then(pl.lit(float('nan'))).otherwise(pl.col('value')).alias('value'))
        wrong_unit = hourly.with_columns(pl.lit('C').alias('unit'))
        for frame in (duplicate,invalid,wrong_unit):
            coverage = self.aggregate(frame)
            with self.assertRaisesRegex(ValueError,'Invalid or duplicate'):
                self.ns['export_matched_actuals'](coverage,self.grid,self.output,self.ids)
            self.assertFalse(list(self.output.glob('*.csv')))

    def test_global_schema_timezone_conversion_and_variable_selection(self):
        hourly = self.hourly()
        baseline = self.aggregate(hourly)
        global_frame = hourly.drop('variable','unit').rename({'value':'temperature_2m'})
        pacific = global_frame.with_columns(pl.col('timestamp').dt.convert_time_zone('America/Los_Angeles'))
        naive = global_frame.with_columns(pl.col('timestamp').dt.replace_time_zone(None))
        other_variable = hourly.with_columns(pl.lit('2m_dewpoint_temperature').alias('variable'),pl.lit(100.).alias('value'))
        for frame in (pacific,naive,pl.concat([hourly,other_variable])):
            means = self.aggregate(frame)['2m_mean'].to_list()
            for actual, expected in zip(means,baseline['2m_mean'].to_list()):
                self.assertAlmostEqual(actual,expected,places=9)

    def test_inclusive_start_exclusive_end_marks_boundary_week_incomplete(self):
        coverage = self.aggregate(self.hourly(),start=date(2000,2,1),end=date(2000,2,6))
        self.assertEqual(coverage['n_samples'].to_list(),[20,20])
        self.assertEqual(coverage['status'].to_list(),['incomplete','incomplete'])
        with self.assertRaisesRegex(ValueError,'No complete'):
            self.ns['export_matched_actuals'](coverage,self.grid,self.output,self.ids)
        with self.assertRaisesRegex(ValueError,'separate output'):
            self.ns['export_matched_actuals'](coverage,self.grid,self.ids,self.ids)


if __name__ == '__main__':
    unittest.main()
