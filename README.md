# Commodity Research

A Python workspace for commodity data, analytics, and market-specific research.

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

Install the package in a virtual environment, then run the three-day DAM LMP
example for `TH_SP15_GEN-APND`:

```bash
python -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m comm_research.infra.scrapers.caiso_oasis --start 2025-01-01
```

The example requests `[2025-01-01, 2025-01-04)` in UTC. `--start` selects another
sample date. The raw client queries the SingleZip endpoint with `resultformat=6`,
validates the in-memory ZIP, and stages CSVs under `data/raw/caiso/{queryname}/`.
The Polars normalizer writes UTC interval-start-date partitions under
`data/lake/power_gas/napg/caiso/dam_lmp/year=YYYY/month=MM/day=DD/data.parquet`.
Raw CSVs are deleted only after all requested partitions are written.

For other reports, use `CAISOOASISClient.download()` with `DAM_LMP`, `RTM_LMP`, or
`DAM_LOAD_FORECAST`, then pass its returned paths and `report.dataset_name` to
`normalize_csvs()`. Query boundaries must be timezone-aware with minute precision;
the end is exclusive. RTM requests use one-day chunks; DAM uses at most 15 days.
LMP downloads require an explicit node. Timestamp columns become
`interval_start_time_gmt` and `interval_end_time_gmt`, typed as UTC datetimes;
known numeric fields are explicitly typed, and identifiers stay strings.

Requests, including retries and redirects, are serialized and spaced at least
five seconds apart across clients in one process. HTTP 429/503 responses receive
bounded exponential retries. Separate processes sharing an IP must coordinate
requests externally. Network access to `oasis.caiso.com` is required; no API key
is used.

Use a single normalizer writer per dataset. Existing rows are preserved and exact
duplicates removed on repeat ingestion; changed values are retained as distinct
rows. Partition replacement is atomic, but a multi-day batch is not a transaction.
On a failure, raw files remain available for retry; earlier successful download
chunks also remain staged if a later request fails.

Run the offline tests (including simulated ZIP-to-Parquet ingestion) with:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

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
