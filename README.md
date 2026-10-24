# Commodity Research

A Python workspace for commodity data, analytics, and market-specific research. All views and research in this repository are my own and sourced from public materials and data. Most of the repository is infrastructure and model code, but research notebooks can be viewed in root/notebooks/

## Project layout

| Directory | Purpose |
| --- | --- |
| `config/` | Project settings and market calendars |
| `data/raw/` | Local source data and downloads |
| `data/lake/` | Local processed data and analytical datasets |
| `dags/` | Standalone Airflow workflows |
| `notebooks/` | Research and visualizations |
| `src/comm_research/infra/` | Shared HTTP, lake storage, locking, and path infrastructure |
| `src/comm_research/common/` | Analytics shared across markets: math, curves, and models |
| `src/comm_research/markets/` | Market-owned scrapers, transforms, pipelines, models, analytics, and query adapters |
| `src/comm_research/dashboard/` | Shared dashboard server, chart engines, and workspace UI |

See [repository architecture](docs/architecture.md) for ownership rules, the new
scaffolds, canonical imports, and compatibility paths. Local data and `.env` are
excluded from Git.

## Local data lake

Processed datasets follow the market package taxonomy under `data/lake/`, for
example `power_gas/napg/caiso/<dataset>/`. Partition time-based
Parquet data by year and month; use a daily partition only when it avoids tiny files.

```text
data/
├── raw/                         # Source downloads, retained as needed
├── state/                       # Job checkpoints and manifests
└── lake/
    ├── power_gas/
    │   ├── napg/
    │   │   └── caiso/
    │   │       ├── dam_lmp/
    │   │       └── dam_load_forecast/
    │   └── eupg/
    ├── weather/
    │   └── forecasts/
    ├── oil/
    │   └── crude_flows/
    └── ags/
        └── crop_progress/
            └── year=YYYY/
```

Use `year=YYYY/month=MM/` partitions for most datasets. Keep query fields such as
timestamps, locations, and symbols in the Parquet data rather than adding them to
the directory path. Forecast datasets can use `run_date=YYYY-MM-DD/` when forecast
runs need to remain distinct.

## Documentation

See [docs/](docs/) for infrastructure and model documentation.

## Markets

### Power & Gas

Spark spreads, nodal congestion, economic dispatch, renewables

![Power transmission lines at sunset](assets/markets/power.jpg)

### Weather

Statistical methods for weather and atmospheric modeling

![Weather forecast map](assets/markets/weather.jpg)

### Oil

Crack spreads, crude, distillates, liquids

![Crude oil](assets/markets/crude.jpg)

### Agriculture

Crush spreads and crop progress, satelite models

![Soybean harvest](assets/markets/soybeans.jpg)

For focused DAM/RTM/FMM backfills, use `python scripts/fetch_caiso.py --help`; see
[the CLI guide](docs/markets/power_gas/napg/caiso/fetch_caiso.md) for inclusive dates, node selection and refresh behavior.
