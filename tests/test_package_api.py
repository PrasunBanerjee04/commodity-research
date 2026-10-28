"""Public package APIs remain explicit and cheap to import."""

import ast
import importlib
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PackageAPITests(unittest.TestCase):
    def test_every_python_package_defines_public_exports(self):
        for path in (ROOT / "src/comm_research").rglob("__init__.py"):
            with self.subTest(path=path):
                tree = ast.parse(path.read_text())
                self.assertTrue(any(
                    isinstance(node, ast.Assign)
                    and any(isinstance(target, ast.Name) and target.id == "__all__"
                            for target in node.targets)
                    for node in tree.body
                ))

    def test_convenience_imports_share_implementation_identity(self):
        from comm_research.infra.lake.writer_lock import writer_lock as lock
        from comm_research.infra.lake import scan_parquet, writer_lock
        from comm_research.infra.lake.parquet import scan_parquet as implementation
        from comm_research.markets.weather import ERA5Scraper, WeatherGrid
        from comm_research.markets.weather.scrapers.era5 import ERA5Scraper as scraper
        from comm_research.markets.weather.models.grid import WeatherGrid as grid

        self.assertIs(scan_parquet, implementation)
        self.assertIs(writer_lock, lock)
        self.assertIs(ERA5Scraper, scraper)
        self.assertIs(WeatherGrid, grid)

    def test_parent_imports_do_not_load_optional_dependencies(self):
        subprocess.run([sys.executable, "-c", """
import sys
import comm_research
from comm_research import common, infra, markets, dashboard
from comm_research.infra.tools import vis
assert not {'plotly', 'matplotlib', 'polars', 'numpy', 'duckdb', 'fastapi'} & sys.modules.keys()
assert 'plot_timeseries' in dir(vis)
try:
    infra.this_attribute_does_not_exist
except AttributeError:
    pass
else:
    raise AssertionError('Unknown attributes must raise AttributeError')
"""], cwd=ROOT, check=True, capture_output=True, text=True)

    def test_each_explicit_lazy_export_points_to_a_real_attribute(self):
        # Validate names statically so this also runs without optional extras.
        base = ROOT / "src"
        for path in (base / "comm_research").rglob("__init__.py"):
            tree = ast.parse(path.read_text())
            for node in tree.body:
                if not isinstance(node, ast.Assign):
                    continue
                if not any(isinstance(t, ast.Name) and t.id == "_EXPORTS" for t in node.targets):
                    continue
                exports = ast.literal_eval(node.value)
                package = ".".join(path.parent.relative_to(base).parts)
                for name, (module, attribute) in exports.items():
                    with self.subTest(package=package, name=name):
                        target = importlib.util.resolve_name(module, package)
                        file = base.joinpath(*target.split(".")).with_suffix(".py")
                        if not file.exists():
                            file = base.joinpath(*target.split("."), "__init__.py")
                        self.assertTrue(file.exists(), target)
                        if attribute:
                            self.assertIn(attribute, file.read_text())


if __name__ == "__main__":
    unittest.main()
