"""ERA5 package, legacy API, and shared Zarr-installation contracts."""

import importlib.util
import json
import pickle
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

from comm_research.markets.weather.scrapers import era5


class ERA5PackageTests(unittest.TestCase):
    def test_public_regional_exports_keep_identity_and_default_paths(self):
        from comm_research.markets.weather import ERA5Scraper
        from comm_research.markets.weather.scrapers.era5 import regional

        self.assertIs(era5.ERA5Scraper, regional.ERA5Scraper)
        self.assertIs(ERA5Scraper, regional.ERA5Scraper)
        self.assertIs(era5.RunSummary, regional.RunSummary)
        self.assertEqual(era5.DEFAULT_LAKE_ROOT, regional.DEFAULT_LAKE_ROOT)
        self.assertEqual(era5.DEFAULT_RAW_ROOT, regional.DEFAULT_RAW_ROOT)
        self.assertIs(era5.VARIABLES, regional.VARIABLES)
        self.assertIs(era5.run_global, era5.global_temperature.run)
        self.assertIs(era5.GlobalRunSummary, era5.global_temperature.RunSummary)
        legacy_class = b"ccomm_research.markets.weather.scrapers.era5\nERA5Scraper\n."
        self.assertIs(pickle.loads(legacy_class), regional.ERA5Scraper)

    def test_global_aliases_share_modules_in_both_import_orders(self):
        for legacy_first in (True, False):
            with self.subTest(legacy_first=legacy_first):
                program = f"""
import importlib, pickle
canonical='comm_research.markets.weather.scrapers.era5.global_temperature'
for old in ('comm_research.markets.weather.scrapers.era5_global',
            'comm_research.infra.scrapers.weather.era5_global'):
    for suffix in ('', '.client', '.normalizer', '.pipeline'):
        names=[old+suffix,canonical+suffix]
        if not {legacy_first!r}: names.reverse()
        a,b=[importlib.import_module(name) for name in names]
        assert a is b, names
        assert a.__spec__.name==canonical+suffix
        assert importlib.reload(a) is b
from comm_research.markets.weather.scrapers import era5_global
assert era5_global is importlib.import_module(canonical)
assert pickle.loads(b'ccomm_research.markets.weather.scrapers.era5_global.client\\nSourceInfo\\n.') is importlib.import_module(canonical+'.client').SourceInfo
"""
                subprocess.run(
                    [sys.executable, "-c", program],
                    check=True,
                    capture_output=True,
                    text=True,
                )

    def test_provider_packages_are_cheap_to_import_and_scaffolds_are_visible(self):
        program = """
import sys
from comm_research.markets.weather.scrapers import era5, ecmwf
assert not {'numpy','polars','xarray','zarr','httpx'} & sys.modules.keys()
assert 'ERA5Scraper' in dir(era5)
assert ecmwf.__all__ == []
"""
        subprocess.run(
            [sys.executable, "-c", program], check=True, capture_output=True, text=True
        )
        scrapers = Path(era5.__file__).parent.parent
        self.assertTrue((scrapers / "ecmwf/.gitkeep").is_file())
        self.assertEqual([p.name for p in scrapers.glob("*.py")], ["__init__.py"])

    def test_grouped_cli_works_outside_the_checkout(self):
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "comm_research.markets.weather.scrapers.era5",
                "--help",
            ],
            cwd=tempfile.gettempdir(),
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertIn("--min-free-gb", result.stdout)
        self.assertIn("--refresh", result.stdout)

    @unittest.skipUnless(
        all(importlib.util.find_spec(name) for name in ("numpy", "xarray", "zarr")),
        "Install .[weather,era5] for real Zarr compatibility checks",
    )
    def test_regional_reader_opens_real_zarr_v2_with_global_dependencies_installed(
        self,
    ):
        import numpy as np
        import xarray as xr
        import zarr

        from comm_research.markets.weather.models import WeatherGrid

        self.assertEqual(zarr.__version__.split(".")[0], "2")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = root / "fixture.zarr"
            hours = np.datetime64("2000-01-01T00", "us") + np.arange(
                24
            ) * np.timedelta64(1, "h")
            xr.Dataset(
                {
                    "2m_temperature": (
                        ("time", "latitude", "longitude"),
                        np.arange(24, dtype="float32").reshape(24, 1, 1) + 280,
                        {"units": "K"},
                    )
                },
                coords={"time": hours, "latitude": [0.0], "longitude": [0.0]},
            ).to_zarr(store, mode="w", consolidated=True)
            self.assertEqual(
                json.loads((store / ".zgroup").read_text())["zarr_format"], 2
            )
            scraper = era5.ERA5Scraper(
                WeatherGrid(north=0, west=0, south=0, east=0),
                lake_root=root / "lake",
                raw_root=root / "raw",
            )
            result = scraper.run_bulk(
                date(2000, 1, 1),
                date(2000, 1, 2),
                store_url=str(store),
                storage_options={},
            )
            self.assertEqual((result.written_days, result.received_rows), (1, 24))
            self.assertEqual(
                scraper._read_day(date(2000, 1, 1))["value"].to_list(),
                list(range(280, 304)),
            )
            self.assertEqual(
                scraper.run_bulk(
                    date(2000, 1, 1),
                    date(2000, 1, 2),
                    store_url=str(store),
                    storage_options={},
                ).skipped_days,
                1,
            )
