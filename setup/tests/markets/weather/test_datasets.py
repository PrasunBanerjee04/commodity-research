"""Weather helpers retain their results and former public exports after moving."""
import tempfile
import unittest
from pathlib import Path

import polars as pl

from comm_research.markets.weather.models.datasets import socal_temp_2m


class WeatherDatasetTests(unittest.TestCase):
    def test_point_extraction_preserves_rows_and_nulls(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "temperature.parquet"
            frame = pl.DataFrame({
                "latitude": [32.5, 32.5, 33.0, 32.5],
                "longitude": [-121.0, -121.0, -121.0, -120.0],
                "value": [280.0, None, 290.0, 300.0],
            })
            frame.write_parquet(path)
            self.assertTrue(socal_temp_2m(directory, -121.0, 32.5).equals(frame.head(2)))

    def test_previous_exports_share_canonical_objects(self):
        from comm_research.infra.lake.datasets import socal_temp_2m as previous
        from comm_research.infra.structures import WeatherGrid as previous_grid
        from comm_research.markets.weather.models.grid import WeatherGrid

        self.assertIs(previous, socal_temp_2m)
        self.assertIs(previous_grid, WeatherGrid)
