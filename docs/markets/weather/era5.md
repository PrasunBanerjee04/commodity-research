# Regional ERA5 from Copernicus CDS

Use [the ingestion notebook](../../../notebooks/markets/weather/era5_ingestion.ipynb)
to request ERA5 hourly single-level reanalysis from the
[Copernicus Climate Data Store](https://cds.climate.copernicus.eu/datasets/reanalysis-era5-single-levels).
The default variable is **2 m temperature**. Nothing is downloaded from a mirror.

## Setup

From the repository root:

```bash
.venv/bin/python -m pip install -e '.[weather,notebooks]'
```

Select this repository's `.venv` Python interpreter as the notebook kernel.
Register a CDS account, accept the ERA5 single-level dataset terms through the
CDS download page, and configure the account's personal access token securely.
The scraper uses the official `cdsapi` client and its supported configuration:

- `CDSAPI_KEY` in environment settings; the URL is fixed to the current
  `https://cds.climate.copernicus.eu/api` endpoint.
- Alternatively, an existing `~/.cdsapirc` with `url` and `key`, or a file
  selected by `CDSAPI_RC`.

Never put the token in the notebook, repository, or logs. The notebook checks
only credential-source presence. Presence does not establish account access or
accepted dataset terms; CDS validates those when a request is submitted.
Network access is required to `cds.climate.copernicus.eu` and the signed download
destination returned by CDS. Current environment requirements include the
`object-store.os-api.cci1.ecmwf.int` and `object-store.os-api.cci2.ecmwf.int` hosts;
if CDS returns a different host, permit that specific destination in settings.

## Notebook workflow

Edit the labeled global settings in the first code cell:

- `NORTH`, `WEST`, `SOUTH`, `EAST`, and `REGION_NAME` define a rectangular region.
- `START_DATE` is inclusive; `END_DATE` is exclusive. Dates are **UTC**.
- `VARIABLES` defaults to `("2m_temperature",)`.
- `WORKERS` bounds concurrent CDS requests; start with two.
- `LAKE_ROOT` and `RAW_ROOT` optionally override the default directories.
- `REFRESH` forces fresh retrieval for the requested region/dates/variables.
- `KEEP_RAW` retains successful NetCDF downloads when enabled.
- `RUN_DOWNLOAD` starts as `False`; set it to `True` after checking the settings.

Run the cells in order. The last cell filters and previews the requested region
from the shared lake. Start with a single historical day before a long backfill.
CDS queue times, publication delays, and request limits are provider-controlled.
Unavailable dates fail explicitly; the scraper does not invent data or silently
clamp the requested interval. There is no implicit full-history download.

## Grid and Python API

`markets/weather/models/grid.py` contains one immutable class, `WeatherGrid`.
Its bounds include native ERA5 cell centers inside the box, snapped inward to
the global **0.25-degree** lattice. Requests for overlapping regions therefore
use identical cell coordinates. A box containing no native cell center fails.
The constructor rejects invalid or nonfinite geographic bounds.

```python
from datetime import date
from comm_research.markets.weather.models.grid import WeatherGrid
from comm_research.markets.weather.scrapers.era5 import ERA5Scraper

california = WeatherGrid(north=42, west=-124.5, south=32.5, east=-114, name="California")
scraper = ERA5Scraper(california)
summary = scraper.run(date(2000, 1, 1), date(2000, 1, 2))

# Extends the same day files with Alaska's cells.
alaska = WeatherGrid(north=72, west=170, south=51, east=-130, name="Alaska")
ERA5Scraper(alaska).run(date(2000, 1, 1), date(2000, 1, 2))
```

`west > east` denotes an antimeridian-crossing box. The scraper splits it into
two CDS areas and stores the ±180-degree seam only once. These are rectangular
selections, not state polygons; ocean and neighboring land cells within the
bounds are included. The native ERA5 model is approximately 31 km; the regular
0.25-degree grid is its standard distribution grid. ERA5-Land is a separate
product and is not mixed into this dataset.

The supported CDS variable names are listed in `scrapers/era5.py:VARIABLES`:
temperature, dew point, 10 m and 100 m wind components, surface pressure, downward
solar radiation, cloud cover, and precipitation. Each variable is requested
separately, keeping NetCDF field and unit interpretation explicit.

## Lake schema and merging

```text
data/lake/weather/era5/
└── year=2000/month=01/day=01/data.parquet

data/raw/weather/era5/
└── 2000/01/01/<request-fingerprint>.nc
```

`lake_root` is the ERA5 dataset directory, not the top-level lake directory.
It defaults to `<project>/data/lake/weather/era5`. Paths use the repository's
shared project-root helper and respect `COMM_RESEARCH_ROOT`.

| Column | Type | Meaning |
| --- | --- | --- |
| `timestamp` | Datetime(us, UTC) | ERA5 valid time |
| `latitude` | Float64 | Degrees north |
| `longitude` | Float64 | Degrees east, normalized to [−180, 180) |
| `variable` | String | CDS variable name, such as `2m_temperature` |
| `value` | Float64 | Measurement in the source unit |
| `unit` | String | NetCDF physical-unit metadata |

The schema uses UTC timestamps, numeric measurements, Zstandard compression,
and `year=/month=/day=/data.parquet` partitions consistent with the existing
lake conventions. ERA5-specific spatial and variable columns identify weather
observations. A long measurement table allows new variables to be added without
changing the schema.

The unique key is **timestamp + latitude + longitude + variable**. Region names
are labels for configuring requests, not part of that key or the storage path.
Every new pull merges with the existing file; incoming observations replace
matching keys, while all other regions and variables remain intact. Each file
is sorted by that key. Requesting an already complete region skips downloads.
Use `refresh=True` to fetch upstream corrections or replace preliminary data.

Temperature is stored in **kelvin**, with no implicit conversion. Radiation and
precipitation retain CDS units and accumulation semantics; the scraper does not
convert them into power or rates. Where the source includes an `expver`
dimension, finalized ERA5 (`1`) takes precedence, with ERA5T (`5`) filling missing
cells. This is the latest CDS reanalysis, not a historical publication-vintage
archive. Do not assume it was available at the original valid time.

## Reliability and limits

The scraper uses bounded concurrent CDS clients with TLS verification enabled.
NetCDF decoding is serialized because the netCDF4/HDF5 library is not
thread-safe. Source files must contain every requested cell and all 24 hourly
UTC timestamps for the day; missing/nonfinite values, conflicting duplicates,
unexpected units within a variable, and incompatible existing schemas fail
before a daily file is replaced.

The existing writer-lock and atomic-Parquet helpers protect the merge. Runs
sharing a lake or raw cache cannot write concurrently. Completed days survive
later failures. Validated raw responses are cached by exact request; after a
failed batch they can be reused on retry. Successful raw files are deleted only
after the daily merge succeeds, unless `KEEP_RAW` is enabled. Refresh downloads
fresh responses and preserves existing lake files if retrieval fails.

Atomicity is per day, not across an entire backfill. Each day and its existing
combined partition must fit in memory for the merge. Begin with regional
requests; this implementation is not intended to materialize the entire globe
in a single in-memory daily table. Interrupting a queued CDS job can require
resubmission; only completed local downloads and lake partitions are resumed.
Recent days may not yet be available at CDS and may later be revised.

## Validation

```bash
.venv/bin/python -m unittest discover -s tests/markets/weather -v
```

Tests use real NetCDF fixtures and exercise UTC dates, overlapping/disjoint
regions, new variables, antimeridian cells, recovery, finalized/preliminary
fields, locks, and failed writes. A live CDS download additionally requires the
user's account token and accepted dataset terms.
