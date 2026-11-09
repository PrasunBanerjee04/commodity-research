# Commodity Research

A Python workspace for commodity data, analytics, and market-specific research. All views and research in this repository are my own and sourced from public materials and data. Most of the repository is infrastructure and model code, but research notebooks can be viewed in `notebooks/`

## Project layout

| Directory | Purpose |
| --- | --- |
| `setup/` | Packaging, requirements, config, environment guidance, assets, docs, market and integration tests, and `paths.py` |
| `infra/` | Shared HTTP, lake utilities, visualizations, preprocessing, DAGs, infrastructure tests, and deployment |
| `src/comm_research/models/` | General math, statistics, curves, and regression models |
| `src/comm_research/markets/` | Oil, gas, power, weather, agriculture, and metals; each with `models/` and `scripts/` |
| `notebooks/` | Research notebooks |
| `data/` | Existing raw, clean, lake, and state layout |

Install once from the repository root, then select the `.venv` interpreter in your notebook:

```sh
python -m venv .venv
.venv/bin/python -m pip install -r setup/requirements.txt
```

The editable install makes `comm_research` available from any working directory;
notebooks need no `sys.path` cells. Paths are centralized in
[`setup/paths.py`](setup/paths.py), available as `comm_research.setup.paths`.
Set `COMM_RESEARCH_ROOT` to use a different checkout or data root.

Run a task by editing the defaults in its script or passing flags:

```sh
python src/comm_research/markets/weather/scripts/era5.py --help
python src/comm_research/markets/weather/scripts/ecmwf.py --help
python src/comm_research/markets/weather/scripts/gefs.py --help
python src/comm_research/markets/power/scripts/oasis.py --help
```

GEFS and ECMWF CLI runs produce plans by default; use `--download` to pull fields.
Use `python src/comm_research/markets/power/scripts/oasis.py refresh --help`
for incremental OASIS refreshes. Power-specific clients and processing live in
`src/comm_research/markets/power/oasis/`. Shared data helpers are available from
`comm_research.infra.lake.datasets`.

Weather provider clients and processing live in
`src/comm_research/markets/weather/ingestion/`; its grid and temperature helpers
live in `weather/models/`. Infra contains shared, market-independent utilities.

Scripts log configuration, progress, resume decisions, failures, and completion.
Provider credentials continue to use the existing provider configuration files.

See [repository architecture](setup/docs/architecture.md) for ownership and
compatibility rules. Local data and credentials are excluded from Git.

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

See [setup/docs/](setup/docs/) for infrastructure and model documentation.

## Markets

### Power & Gas

Spark spreads, nodal congestion, economic dispatch, renewables

![Power transmission lines at sunset](setup/assets/markets/power.jpg)

### Weather

Statistical methods for weather and atmospheric modeling

![Weather forecast map](setup/assets/markets/weather.jpg)

### Oil

Crack spreads, crude, distillates, liquids

![Crude oil](setup/assets/markets/crude.jpg)

### Agriculture

Crush spreads and crop progress, satelite models

![Soybean harvest](setup/assets/markets/soybeans.jpg)

For focused DAM/RTM/FMM backfills, use `python src/comm_research/markets/power/scripts/oasis.py --help`; see
[the CLI guide](setup/docs/markets/power/fetch_caiso.md) for inclusive dates, node selection and refresh behavior.
