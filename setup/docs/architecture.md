# Repository architecture

```text
setup/
  pyproject.toml, setup.py, requirements.txt, paths.py
  config/, env/, assets/, docs/, tests/ # Market and integration tests
infra/
  http/, lake/, tools/vis/, tools/preproc/
  datalake/                    # Typed local API; installed as import datalake
  examples/datalake/
  dags/, tests/, prod/
src/comm_research/
  models/                      # Math, curves, regression, kernels
  markets/
    oil/, gas/, power/, weather/, ags/, metals/
      models/, scripts/
    power/oasis/               # OASIS client, schemas, reconciliation, pipelines
    weather/ingestion/         # ECMWF, GEFS, ERA5 clients and processing
    weather/models/grid.py     # ERA5 geographic lattice
notebooks/
data/                          # Existing paths and partitions preserved
```

## Research workflow

Install with `python -m pip install -r setup/requirements.txt` from the checkout
root. Select that interpreter as the notebook kernel. Imports work independently
of the current directory; no notebook bootstrap is required. The virtual
environment stays at `.venv/` because moving an existing environment breaks its
absolute executable paths. Environment instructions are in `setup/env/`.

Use `from comm_research.setup.paths import DATA, LAKE, PROJECT_ROOT, data_path`
for paths. `COMM_RESEARCH_ROOT` overrides checkout detection. A wheel installed
outside a checkout uses that override, or the working directory as a fallback.
The code hierarchy no longer dictates data locations: CAISO still writes to
`data/lake/power_gas/napg/caiso`.

## Ownership

`comm_research.models` holds general research models. Market research models
belong in `markets/<product>/models`. Each executable research task has one
user-facing file in `markets/<product>/scripts`: `ecmwf.py`, `gefs.py`, `era5.py`,
and `power/scripts/oasis.py`. Each supports direct execution and `python -m`.
Configure runs using the script defaults or CLI flags; call `run` from notebooks.
ERA5's script exposes both global ingestion and regional ingestion via
`run_regional(grid=..., ...)`.

Infrastructure owns shared transport, validation, manifests, locking, and lake
writes. Power-specific OASIS clients, schemas, normalization, reconciliation, and
pipelines live in `markets/power/oasis`. Weather clients and processing live in
`markets/weather/ingestion`; the ERA5 grid and point-temperature helpers live in
`markets/weather/models`. Infra contains no market code or market tests.
Shared infrastructure tests stay in `infra/tests`; market and integration tests
live in `setup/tests`. These remain small internal
modules rather than repeating their implementation in research scripts. New
tasks should reuse these helpers. Task scripts coordinate the chosen engine and
provide terminal logging. Operational dashboards and visualization helpers also
belong in `infra`.

## Compatibility

Former `comm_research.common`, `comm_research.infra.paths`, weather entry-point
and scraper imports, and `power_gas.napg.caiso` imports resolve lazily to the
canonical modules. Aliases share module identity so monkeypatching, request gates,
pickles, and mutable settings retain their behavior. Empty former regional
packages remain importable as virtual compatibility namespaces. New code uses
the canonical paths. Former CAISO infrastructure imports also resolve to
`comm_research.markets.power.oasis`. There is no root `scripts/` directory;
data helpers live in `comm_research.infra.lake.datasets` and the single power
task entry point is `comm_research.markets.power.scripts.oasis`. Former weather
infrastructure imports resolve to `comm_research.markets.weather.ingestion`;
legacy grid and temperature-helper exports resolve through `_compat.py` outside
infra, without duplicating implementations.

The physical root `infra` and `setup` directories are packaged as
`comm_research.infra` and `comm_research.setup`. Packaging maps them explicitly;
they should not be imported as top-level `infra` or `setup` packages.

## Validation

Run both test suites:

```sh
.venv/bin/python -m unittest discover -s infra/tests -v
.venv/bin/python -m unittest discover -s setup/tests -v
```

Tests cover
lake behavior, weather transforms, ingestion resumption, alias identity, CLI
launchers, package exports, and notebook helpers. Optional dependency tests run
when their corresponding extras are installed. Live data pulls require provider
access and are separate from the fixture-based tests.

## DataLake

The `datalake` distribution is packaged in `setup/datalake/`, with source in
`infra/datalake/`. See [functions and examples](datalake.md). It reads the existing
`data/lake/` tree directly; `area="clean"` and `area="raw"` select sibling areas.
Datasets are discovered from files, without registration or a metadata database.
Research writes use ordinary single-file Parquet outputs; market scripts own
partitioned ingestion data.
