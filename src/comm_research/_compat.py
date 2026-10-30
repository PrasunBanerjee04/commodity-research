"""Lazy import aliases for pre-refactor callers; no duplicate implementations.

Canonical code never imports these paths. Aliases share module identity, which
preserves pipeline globals, monkeypatching, dataclasses, and the CAISO request
gate. No market or optional dependency loads until a caller asks.
"""

import importlib
import importlib.abc
import importlib.util
import sys
from types import ModuleType

CAISO = "comm_research.markets.power_gas.napg.caiso"
WEATHER = "comm_research.markets.weather"
ALIASES = {
    WEATHER + ".scrapers.era5_global": WEATHER + ".scrapers.era5.global_temperature",
    "comm_research.infra.scrapers.caiso.oasis.models": CAISO
    + ".scrapers.oasis.reports",
    "comm_research.infra.scrapers.caiso.oasis": CAISO + ".scrapers.oasis",
    "comm_research.infra.scrapers.caiso": CAISO + ".scrapers",
    "comm_research.infra.scrapers.weather": WEATHER + ".scrapers",
    "comm_research.infra.tools.caiso_normalizer": CAISO + ".transforms.normalizer",
    "comm_research.infra.tools.caiso_schema": CAISO + ".transforms.schema",
    "comm_research.infra.tools.parquet_formatter": "comm_research.infra.lake.parquet",
    "comm_research.infra.tools.lake_methods": "comm_research.infra.lake",
    CAISO + ".caiso_oasis_pipeline": CAISO + ".pipelines.oasis",
}
NAMESPACES = {"comm_research.infra.scrapers"}


class _AliasLoader(importlib.abc.Loader):
    def __init__(self, target: str | None, target_spec=None):
        self.target = target
        self.target_spec = target_spec

    def create_module(self, spec):
        return (
            importlib.import_module(self.target)
            if self.target
            else ModuleType(spec.name)
        )

    def exec_module(self, module):
        if self.target:
            # importlib assigns the alias spec while loading. Restore the
            # canonical metadata so reload/pickling still use the real module.
            module.__spec__ = self.target_spec
            module.__loader__ = self.target_spec.loader

    def get_code(self, fullname):
        # Preserve python -m <old pipeline> as well as ordinary imports.
        return self.target_spec.loader.get_code(self.target) if self.target else None


class _LegacyFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in NAMESPACES:
            return importlib.util.spec_from_loader(
                fullname, _AliasLoader(None), is_package=True
            )
        canonical = fullname
        seen = set()
        # Flatten chained prefixes (infra.scrapers.weather -> weather.scrapers
        # -> era5.global_temperature) before obtaining the canonical spec.
        while canonical not in seen:
            seen.add(canonical)
            for old in sorted(ALIASES, key=len, reverse=True):
                if canonical == old or canonical.startswith(old + "."):
                    canonical = ALIASES[old] + canonical[len(old) :]
                    break
            else:
                break
        else:
            raise ImportError(f"Circular compatibility alias for {fullname}")
        if canonical == fullname:
            return None
        spec = importlib.util.find_spec(canonical)
        if spec is None:
            return None
        return importlib.util.spec_from_loader(
            fullname,
            _AliasLoader(canonical, spec),
            origin=spec.origin,
            is_package=spec.submodule_search_locations is not None,
        )


def install():
    if not any(isinstance(finder, _LegacyFinder) for finder in sys.meta_path):
        sys.meta_path.insert(0, _LegacyFinder())
