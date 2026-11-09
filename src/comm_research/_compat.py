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

CAISO = "comm_research.markets.power.oasis"
WEATHER = "comm_research.markets.weather.ingestion"
OLD_CAISO = "comm_research.markets.power_gas.napg.caiso"
OLD_WEATHER = "comm_research.markets.weather"
ALIASES = {
    "comm_research.common.models": "comm_research.models",
    "comm_research.common": "comm_research.models",
    "comm_research.infra.paths": "comm_research.setup.paths",
    "comm_research.infra.structures.grid": OLD_WEATHER + ".models.grid",
    "comm_research.infra.ingestion.weather": WEATHER,
    OLD_WEATHER + ".scrapers": WEATHER,
    OLD_WEATHER + ".ecmwf": OLD_WEATHER + ".scripts.ecmwf",
    OLD_WEATHER + ".era5": OLD_WEATHER + ".scripts.era5",
    OLD_WEATHER + ".gefs": OLD_WEATHER + ".scripts.gefs",
    OLD_CAISO: CAISO,
    "comm_research.infra.ingestion.caiso": CAISO,
    "comm_research.markets.power.scripts.caiso": "comm_research.markets.power.scripts.oasis",
    WEATHER + ".era5_global": WEATHER + ".era5.global_temperature",
    "comm_research.infra.scrapers.caiso.oasis.models": CAISO + ".scrapers.oasis.reports",
    "comm_research.infra.scrapers.caiso.oasis": CAISO + ".scrapers.oasis",
    "comm_research.infra.scrapers.caiso": CAISO + ".scrapers",
    "comm_research.infra.scrapers.weather": WEATHER,
    "comm_research.infra.tools.caiso_normalizer": CAISO + ".transforms.normalizer",
    "comm_research.infra.tools.caiso_schema": CAISO + ".transforms.schema",
    "comm_research.infra.tools.parquet_formatter": "comm_research.infra.lake.parquet",
    "comm_research.infra.tools.lake_methods": "comm_research.infra.lake",
    CAISO + ".caiso_oasis_pipeline": CAISO + ".pipelines.oasis",
}
NAMESPACES = {
    "comm_research.infra.scrapers",
    "comm_research.infra.ingestion",
    "comm_research.infra.structures",
    "comm_research.markets.power_gas",
    "comm_research.markets.power_gas.napg",
    "comm_research.markets.power_gas.eupg",
}
# Preserve imports of the former empty regional scaffolds without physical folders.
for region, names in (("napg", ("aeso", "ercot", "isone", "miso", "nyiso", "pjm")),
                      ("eupg", ("cwe", "ipex", "nordics"))):
    for name in names:
        prefix = "comm_research.markets.power_gas." + region + "." + name
        NAMESPACES.add(prefix)
        NAMESPACES.update(prefix + "." + part for part in
                          ("analytics", "models", "pipelines", "scrapers", "transforms"))

for market in ("oil", "ags", "weather"):
    for part in ("analytics", "pipelines", "scrapers", "transforms"):
        if market == "weather" and part == "scrapers":
            continue
        NAMESPACES.add("comm_research.markets." + market + "." + part)


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
        if not self.target:
            module.__getattr__ = lambda name: legacy_attribute(module.__name__, name)
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


_ATTRIBUTE_ALIASES = {
    ("comm_research.infra.structures", "WeatherGrid"):
        ("comm_research.markets.weather.models.grid", "WeatherGrid"),
    ("comm_research.infra.lake.datasets", "socal_temp_2m"):
        ("comm_research.markets.weather.models.datasets", "socal_temp_2m"),
}


def legacy_attribute(module, name):
    """Resolve former domain exports outside the shared infrastructure layer."""
    target = _ATTRIBUTE_ALIASES.get((module, name))
    if target is None:
        raise AttributeError(f"module {module!r} has no attribute {name!r}")
    return getattr(importlib.import_module(target[0]), target[1])


def legacy_main(module):
    """Preserve the former standalone temperature extraction example."""
    if module == "comm_research.infra.lake.datasets":
        return importlib.import_module("comm_research.markets.weather.models.datasets").main()
    raise ValueError(f"No legacy entry point for {module!r}")
