"""Refactor contracts: public imports, paths, dependency boundaries, and CLIs."""

import ast
import importlib
import json
import os
import pickle
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
CAISO = "comm_research.markets.power_gas.napg.caiso"


class RepositoryLayoutTests(unittest.TestCase):
    def test_scrapers_are_market_owned_without_a_physical_infra_package(self):
        self.assertFalse((ROOT / "src/comm_research/infra/scrapers").exists())
        weather = ROOT / "src/comm_research/markets/weather/scrapers"
        for name in ("ecmwf", "era5"):
            with self.subTest(name=name):
                module = importlib.import_module(
                    f"comm_research.markets.weather.scrapers.{name}"
                )
                self.assertEqual(Path(module.__file__), weather / name / "__init__.py")

    def test_weather_legacy_imports_share_modules_in_either_import_order(self):
        # Fresh interpreters exercise both finder routes without sys.modules
        # entries left by another test masking a broken legacy import.
        for first in ("legacy", "canonical"):
            with self.subTest(first=first):
                program = f"""
import importlib
import json

legacy = 'comm_research.infra.scrapers.weather'
canonical = 'comm_research.markets.weather.scrapers'
for suffix in ('', '.ecmwf', '.era5'):
    names = [legacy + suffix, canonical + suffix]
    if {first!r} == 'canonical':
        names.reverse()
    modules = [importlib.import_module(name) for name in names]
    assert modules[0] is modules[1], names
    assert modules[0].__spec__.name == canonical + suffix
    assert importlib.reload(modules[0]) is modules[1]
from comm_research.infra.scrapers.weather import ecmwf, era5
assert ecmwf is importlib.import_module(canonical + '.ecmwf')
assert era5 is importlib.import_module(canonical + '.era5')
print(json.dumps([ecmwf.__name__, era5.__name__]))
"""
                result = subprocess.run(
                    [sys.executable, "-c", program],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    check=True,
                )
                self.assertEqual(
                    json.loads(result.stdout),
                    [
                        "comm_research.markets.weather.scrapers.ecmwf",
                        "comm_research.markets.weather.scrapers.era5",
                    ],
                )

    def test_legacy_aliases_preserve_identity_specs_and_mutable_settings(self):
        from comm_research._compat import ALIASES

        for old, new in ALIASES.items():
            with self.subTest(old=old):
                legacy, canonical = (
                    importlib.import_module(old),
                    importlib.import_module(new),
                )
                self.assertIs(legacy, canonical)
                self.assertEqual(canonical.__spec__.name, new)
        pipeline = importlib.import_module(CAISO + ".pipelines.oasis")
        legacy = importlib.import_module(CAISO + ".caiso_oasis_pipeline")
        with patch.object(legacy, "NODES", ("TEST_NODE",)):
            self.assertEqual(pipeline.NODES, ("TEST_NODE",))
        old_client = importlib.import_module(
            "comm_research.infra.scrapers.caiso.oasis.client"
        )
        client = importlib.import_module(CAISO + ".scrapers.oasis.client")
        self.assertIs(old_client._REQUEST_GATE, client._REQUEST_GATE)
        report = importlib.import_module(CAISO + ".scrapers.oasis.reports").Report
        legacy_pickle = b"ccomm_research.infra.scrapers.caiso.oasis.models\nReport\n."
        self.assertIs(pickle.loads(legacy_pickle), report)

    def test_default_data_paths_do_not_change_with_module_depth(self):
        from comm_research.infra.paths import PROJECT_ROOT

        client = importlib.import_module(CAISO + ".scrapers.oasis.client")
        pipeline = importlib.import_module(CAISO + ".pipelines.oasis")
        normalizer = importlib.import_module(CAISO + ".transforms.normalizer")
        self.assertEqual(PROJECT_ROOT, ROOT)
        self.assertEqual(client.DEFAULT_RAW_ROOT, ROOT / "data/raw/caiso")
        self.assertEqual(
            normalizer.DEFAULT_LAKE_ROOT, ROOT / "data/lake/power_gas/napg/caiso"
        )
        self.assertEqual(pipeline.STATE_ROOT, ROOT / "data/state/caiso_oasis")
        from comm_research.infra.paths import project_root

        with patch.dict(os.environ, {"COMM_RESEARCH_ROOT": str(ROOT / "custom-root")}):
            self.assertEqual(project_root(), ROOT / "custom-root")

    def test_shared_layers_do_not_depend_on_market_packages(self):
        for layer in ["common", "infra"]:
            for path in (ROOT / "src/comm_research" / layer).rglob("*.py"):
                for node in ast.walk(ast.parse(path.read_text())):
                    names = (
                        [node.module or ""]
                        if isinstance(node, ast.ImportFrom)
                        else [alias.name for alias in node.names]
                        if isinstance(node, ast.Import)
                        else []
                    )
                    self.assertFalse(
                        any(name.startswith("comm_research.markets") for name in names),
                        path,
                    )
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import comm_research.common.math, comm_research.infra.scrapers, sys, json; print(json.dumps([x for x in ('duckdb','polars','comm_research.markets') if x in sys.modules]))",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(json.loads(result.stdout), [])

    def test_existing_and_canonical_cli_entrypoints_still_launch(self):
        for args in [
            ["-m", CAISO + ".pipelines.oasis", "--help"],
            ["-m", CAISO + ".caiso_oasis_pipeline", "--help"],
            ["-m", CAISO + ".pipelines.backfill", "--help"],
            [str(ROOT / "scripts/fetch_caiso.py"), "--help"],
            [str(ROOT / "server.py"), "--help"],
            [str(ROOT / "app.py"), "--help"],
        ]:
            with self.subTest(args=args):
                result = subprocess.run(
                    [sys.executable, *args],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    check=True,
                )
                self.assertIn("usage:", result.stdout)
                self.assertNotIn("Traceback", result.stderr)
