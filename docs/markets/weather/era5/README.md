# ERA5 package map

Both historical ingestion workflows are active. They now share one package,
`comm_research.markets.weather.scrapers.era5`, while keeping their different
retrieval strategies and existing lake formats.

```text
markets/weather/
├── era5.py                         # Existing global CLI/settings entrypoint
└── scrapers/
    ├── era5/
    │   ├── __init__.py              # Lazy public API and compatible regional exports
    │   ├── __main__.py              # python -m ...scrapers.era5
    │   ├── regional.py              # WeatherGrid, CDS and regional ARCO reads
    │   └── global_temperature/
    │       ├── __init__.py
    │       ├── client.py            # Strict HTTP/Zarr source reader
    │       ├── normalizer.py        # Hourly batches and Parquet schema
    │       └── pipeline.py          # Global-day streaming, checkpoints and resume
    └── ecmwf/
        ├── __init__.py
        └── .gitkeep                # Forecast scraper scaffold; not implemented
```

| Workflow | Used by | Retrieval and output |
| --- | --- | --- |
| Regional, multiple variables | `notebooks/era5_ingestion.ipynb`, `ERA5Scraper` callers | A `WeatherGrid` subset through CDS `.run()` or public ARCO `.run_bulk()`. Long-format `variable/value/unit` tables with float64 coordinates and values. |
| Global, temperature only | `markets/weather/era5.py`, the grouped module CLI | Public ARCO HTTP/Zarr chunks streamed into daily global files. Float32 `temperature_2m` and coordinates. Finalized-data and completeness checks, disk guard, checkpoints and resume. |

These are ERA5 **reanalysis** workflows. The Google-hosted mirror is a data
archive; it is not a Google weather-forecast model. `ecmwf/` is reserved for
future forecast ingestion and currently has no download implementation.

## APIs and commands

```python
from comm_research.markets.weather.models import WeatherGrid
from comm_research.markets.weather.scrapers.era5 import ERA5Scraper
from comm_research.markets.weather.scrapers.era5.global_temperature import run

regional = ERA5Scraper(WeatherGrid(north=35, west=-121, south=32.5, east=-114))
# regional.run(...)       # CDS, credentials required
# regional.run_bulk(...)  # Anonymous public ARCO regional reads
# run(...)                # Full global temperature backfill
```

`era5.ERA5Scraper`, regional `RunSummary`, and existing regional constants such
as `DEFAULT_LAKE_ROOT` remain available. Explicit `regional` and
`global_temperature` submodules distinguish the APIs. The package also exports
`run_global` and `GlobalRunSummary` without changing what regional `RunSummary`
means. Importing the parent packages does not load optional provider libraries.

```bash
python -m pip install -e '.[weather,era5,notebooks]'
python -m comm_research.markets.weather.scrapers.era5 --help

# Existing commands remain valid:
python -m comm_research.markets.weather.era5 --help
python src/comm_research/markets/weather/era5.py --help
```

The existing `weather/era5.py` settings remain the defaults for both CLI forms.
The previous `scrapers.era5_global` import and its `client`, `normalizer`, and
`pipeline` submodules are lazy aliases to `era5.global_temperature`. Legacy
`infra.scrapers.weather` imports also resolve to those same module objects,
including chained global aliases. There is one implementation and one set of
module globals per workflow.

## Storage and dependency compatibility

Regional defaults remain `data/lake/weather/era5/{variable}/year=...`, while
global temperature remains
`data/lake/weather/era5/t_2m/single_levels/year=...`. Raw directories, global
state manifests, schemas, units, date boundaries, and resume behavior are
preserved. Read the exact dataset directory when selecting a schema; these two
formats are not automatically combined. No lake migration is needed.

Both optional extras now use `zarr>=2.18,<3`. Although the public store's name
ends in `zarr-v3`, its consolidated metadata uses Zarr format 2. The strict global
HTTP mapping uses the Zarr v2 Python API. Regional xarray is constrained to
`>=2024.10,<2026` so its backend also supports that API; newer xarray releases
require Zarr 3. A real local Zarr read/write test exercises the shared
installation, in addition to the global HTTP/decoder fixtures.

Reinstall the extras after updating an environment that used the former pins,
and restart its notebook kernel. See the [regional guide](../era5.md) and
[global guide](../era5_global.md) for workflow-specific settings and fields.
