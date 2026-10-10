# Commodity Research

A Python workspace for commodity data, analytics, and market-specific research. All views and research in this repository are my own and sourced from public materials and data. 

## Project layout

| Directory | Purpose |
| --- | --- |
| `config/` | Project settings and market calendars |
| `data/raw/` | Local source data and downloads |
| `data/lake/` | Local processed data and analytical datasets |
| `dags/` | Standalone Airflow workflows |
| `notebooks/` | Research and visualizations |
| `src/comm_research/infra/` | Database, scraper, and developer-tool infrastructure |
| `src/comm_research/common/` | Analytics shared across markets: math, curves, and models |
| `src/comm_research/markets/` | Commodity-specific research and analytics |

Local data and `.env` are excluded from Git.

## Local data lake

Processed datasets live under `data/lake/<market>/<dataset>/`. Partition time-based
Parquet data by year and month; use a daily partition only when it avoids tiny files.

```text
data/
├── raw/                         # Source downloads, retained as needed
├── state/                       # Job checkpoints and manifests
└── lake/
    ├── power_gas/
    │   ├── nodal_prices/
    │   └── load/
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

## CAISO OASIS ingestion

The incremental runner covers eight OASIS categories, streams progress to the
terminal, and resumes downloads into daily Parquet partitions. Install and run a
three-day SP15 day-ahead price sample:

```bash
python -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m comm_research.markets.power_gas.napg.caiso.caiso_oasis_pipeline \
  --categories prices --datasets dam_lmp --nodes TH_SP15_GEN-APND \
  --start 2026-10-01 --end 2026-10-04
```

See [OASIS usage and data fields](docs/caiso_oasis.md) for `run()` configuration,
retention/publication limits, recovery, storage, and the report catalog.

## Desk dashboard

Inspect local lake datasets with cached CAISO history, local chart controls,
and a dockable workspace:

```bash
.venv/bin/python -m pip install -e '.[dashboard]'
.venv/bin/python server.py
```

The workstation uses port **8000**. CAISO prices preload into in-memory DuckDB
at startup; node histories are cached in the browser for local range selection,
zoom, pan, and component visibility. Use `--lake /path/to/lake` or `--port` to
choose another lake or port. See [CAISO workstation](docs/caiso_workstation.md).
The general-purpose Polars dashboard remains available with `python app.py`
on port 8502; see [generic analytics](docs/dashboard.md).

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
[the CLI guide](docs/fetch_caiso.md) for inclusive dates, node selection and refresh behavior.
