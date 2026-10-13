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
                "import comm_research.common.math, sys, json; print(json.dumps([x for x in ('duckdb','polars','comm_research.markets') if x in sys.modules]))",
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
