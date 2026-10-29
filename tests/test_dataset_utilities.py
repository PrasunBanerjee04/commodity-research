"""Research CSV output works with Polars without optional plotting imports."""

import contextlib
import importlib.util
import io
import tempfile
import unittest
from pathlib import Path

import polars as pl

SPEC = importlib.util.spec_from_file_location(
    "dataset_utilities", Path(__file__).resolve().parents[1] / "scripts/datasets.py"
)
utilities = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(utilities)


class DatasetUtilitiesTests(unittest.TestCase):
    def test_polars_csv_save_preserves_values_and_reports_success(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "temperature.csv"
            frame = pl.DataFrame({"location": ["A", "B"], "temperature": [27.1, None]})
            self.assertEqual(utilities.save(target, frame), 1)
            self.assertTrue(pl.read_csv(target).equals(frame))

    def test_pandas_style_csv_callers_keep_their_existing_method(self):
        class LegacyTable:
            def to_csv(self, path):
                Path(path).write_text("temperature\n27.1\n")

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "temperature.csv"
            self.assertEqual(utilities.save(target, LegacyTable()), 1)
            self.assertEqual(target.read_text(), "temperature\n27.1\n")

    def test_write_failures_preserve_the_existing_failure_status(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "missing" / "temperature.csv"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(utilities.save(target, pl.DataFrame({"t": [1]})), -1)
            self.assertFalse(target.exists())
