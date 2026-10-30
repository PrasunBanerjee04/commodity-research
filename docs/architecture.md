# Repository architecture

Market-specific code lives under `comm_research.markets`; shared research code
lives in `common`, and shared operational code lives in `infra`. The market path
matches its data-lake prefix. Interactive research and visualizations live in
notebooks and shared visualization helpers under `infra/tools/vis/`.

```text
src/comm_research/
├── common/
│   ├── math/
│   ├── curves/
│   └── models/
├── infra/
│   ├── paths.py
│   ├── http/rate_limit.py
│   └── lake/
│       ├── cleanup.py
│       ├── parquet.py
│       ├── schema.py
│       └── writer_lock.py
└── markets/
    ├── power_gas/
    │   ├── napg/
    │   │   ├── caiso/
    │   │   │   ├── scrapers/oasis/
    │   │   │   ├── transforms/
    │   │   │   ├── pipelines/
    │   │   │   ├── models/
    │   │   │   └── analytics/
    │   │   ├── aeso/
    │   │   ├── ercot/
    │   │   ├── isone/
    │   │   ├── miso/
    │   │   ├── nyiso/
    │   │   └── pjm/
    │   └── eupg/
    │       ├── cwe/
    │       ├── ipex/
    │       └── nordics/
    ├── oil/
    ├── ags/
    └── weather/
        └── scrapers/
            ├── ecmwf/             # Forecast-ingestion scaffold
            └── era5/
                ├── regional.py
                └── global_temperature/
```

Every listed market leaf has `scrapers`, `transforms`, `pipelines`, `models`, and
`analytics` packages. Empty scaffold packages contain `.gitkeep` so they remain
visible in Git and the editor. Oil, agriculture, and weather currently have
those packages at the commodity level; add benchmark/provider subdivisions when
needed instead of forcing an ISO-style hierarchy onto them.

## Ownership and dependencies

| Location | Owns |
| --- | --- |
| `common/` | General mathematics, curves, and research models. |
| `infra/http/` | Reusable request-spacing machinery; providers supply their limits. |
| `infra/lake/` | Generic scans, atomic Parquet writes, timestamp expressions, cleanup, and locks. |
| `markets/.../scrapers/` | API definitions, report parameters, provider errors, download logic. |
| `markets/.../transforms/` | Provider schemas, measurement typing, and normalization. |
| `markets/.../pipelines/` | Refresh/backfill orchestration, resume state, and market CLI implementations. |
| `markets/.../models/` | Market-specific research models; ingestion report declarations instead live in `scrapers/oasis/reports.py`. |
| `markets/.../analytics/` | Market-specific calculations and derived measures. |

`common` and `infra` do not import market packages. Market code uses shared
helpers. Notebook plots use the reusable helpers in `infra/tools/vis/`.

All scraper modules belong to their market's `scrapers/` package. The weather
`ecmwf/` scaffold and `era5/` ingestion package live in `markets/weather/scrapers/`;
there is no physical `infra/scrapers/` directory. Shared HTTP and lake tooling
remain under `infra/http/` and `infra/lake/`.

Legacy `comm_research.infra.scrapers.weather` imports, including its `ecmwf`
and `era5` packages, resolve lazily to the same market-owned module objects
through `_compat.py`. The legacy `infra.scrapers` namespace is virtual, so
existing imports continue to work without maintaining duplicate scraper files
or loading market code when shared infrastructure is imported. See the
[ERA5 package map](markets/weather/era5/README.md) for regional/global ownership,
storage formats, compatibility imports, and install requirements.

## CAISO imports and entrypoints

```python
from comm_research.markets.power_gas.napg.caiso.pipelines import oasis
from comm_research.markets.power_gas.napg.caiso.scrapers.oasis.client import CAISOOASISClient
from comm_research.markets.power_gas.napg.caiso.transforms.normalizer import normalize_csvs
from comm_research.infra.lake import _clear_raw
from comm_research.infra.lake.parquet import scan_parquet, write_atomic
```

Configure pipeline globals on `oasis`, then call `oasis.run()`. The command-line
entrypoint is:

```bash
python -m comm_research.markets.power_gas.napg.caiso.pipelines.oasis --help
python scripts/fetch_caiso.py --help
```

`scripts/fetch_caiso.py` is a checkout launch wrapper; its implementation lives
in `markets/power_gas/napg/caiso/pipelines/backfill.py`. Existing flags and date
semantics are preserved. The previous `caiso.caiso_oasis_pipeline` module command
also works.

Previous `infra.scrapers.caiso.oasis`, `infra.tools.caiso_normalizer`,
`infra.tools.caiso_schema`, `infra.tools.lake_methods`, and
`infra.tools.parquet_formatter` imports resolve lazily through `_compat.py`.
Aliases return the same module objects as canonical imports: pipeline globals,
request gates, classes, and saved class references remain shared. Canonical code
uses the new paths; the old implementation directories have been removed.

## Data, notebooks, documentation, and tests

The CAISO lake remains:

```text
data/lake/power_gas/napg/caiso/{dataset}/year=YYYY/month=MM/day=DD/data.parquet
```

Existing `data/raw/caiso`, `data/state/caiso_oasis`, dataset names, schemas,
completion fingerprints, and writer lock location stay the same. This is a
code-layout migration; it needs no data backfill or lake move.
Defaults now use `infra.paths.PROJECT_ROOT`, independent of module depth.
`COMM_RESEARCH_ROOT` overrides checkout discovery; explicit CLI path options and
existing CAISO lake environment settings still take precedence.

CAISO material is organized under:

- `notebooks/markets/power_gas/napg/caiso/`
- `docs/markets/power_gas/napg/caiso/`
- `tests/markets/power_gas/napg/caiso/`

Old notebook paths are relative symlinks to the canonical files, avoiding copied
execution outputs. Existing outputs, cell IDs, and notebook metadata are retained.
Old documentation URLs have short redirects. Shared utility tests remain
under `tests/`; market-specific ingestion tests follow the taxonomy.

Install and verify from the checkout root:

```bash
python -m pip install -e '.[weather,era5,notebooks,vis]'
python -m unittest discover -s tests
```
