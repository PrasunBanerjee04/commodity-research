"""Research scripts and legacy imports work independently of notebook cwd."""
import importlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


class ResearchWorkflowTests(unittest.TestCase):
    def test_all_task_scripts_launch_from_another_directory(self):
        for market, task in (("weather", "ecmwf"), ("weather", "gefs"),
                             ("weather", "era5"), ("weather", "era5_actuals"), ("power", "oasis")):
            module = f"comm_research.markets.{market}.scripts.{task}"
            path = ROOT / "src" / Path(*module.split(".")).with_suffix(".py")
            for args in ((str(path), "--help"), ("-m", module, "--help")):
                with self.subTest(args=args):
                    result = subprocess.run([sys.executable, *args], cwd=tempfile.gettempdir(),
                                            capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("usage:", result.stdout)

    def test_notebook_imports_need_no_path_bootstrap(self):
        subprocess.run([sys.executable, "-c", """
from comm_research.setup.paths import PROJECT_ROOT, LAKE
from comm_research.markets.weather import WeatherGrid, gefs, ecmwf, era5
from comm_research.models.regression import ols
from comm_research.infra.lake import scan_parquet
assert LAKE == PROJECT_ROOT / 'data/lake'
"""], cwd=ROOT / "notebooks", check=True, capture_output=True, text=True)

    def test_previous_market_imports_keep_identity(self):
        for old, new in (
            ("common.models.regression.ols", "models.regression.ols"),
            ("markets.weather.models.grid", "infra.structures.grid"),
            ("markets.weather.ecmwf", "markets.weather.scripts.ecmwf"),
            ("markets.weather.scrapers.gefs.pipeline", "infra.ingestion.weather.gefs.pipeline"),
            ("markets.power_gas.napg.caiso.pipelines.oasis", "markets.power.oasis.pipelines.oasis"),
        ):
            with self.subTest(old=old):
                self.assertIs(importlib.import_module("comm_research." + old),
                              importlib.import_module("comm_research." + new))

    def test_ecmwf_cli_passes_dates_and_download_choice(self):
        from comm_research.markets.weather.scripts import ecmwf
        from datetime import date
        with patch.object(ecmwf, "_run", return_value={}) as run:
            self.assertEqual(ecmwf.main(["--reference-dates", "2020-01-01", "--download"]), 0)
        self.assertEqual(run.call_args.kwargs["reference_dates"], [date(2020, 1, 1)])
        self.assertFalse(run.call_args.kwargs["dry_run"])

    def test_regional_era5_passes_constructor_and_run_options(self):
        from comm_research.markets.weather.scripts import era5
        with patch("comm_research.markets.weather.ingestion.era5.ERA5Scraper") as scraper:
            era5.run_regional(grid="grid", start="start", end="end", workers=2, refresh=True)
        scraper.assert_called_once_with("grid", workers=2)
        scraper.return_value.run_bulk.assert_called_once_with("start", "end", refresh=True)
