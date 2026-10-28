# Regional ERA5 ingestion

Use [the ingestion notebook](../../../notebooks/era5_ingestion.ipynb) to bulk
ingest ERA5 hourly single-level reanalysis from the public ARCO Zarr mirror on
Google Cloud Storage. The default variable is **2 m temperature**. The Python
API also supports requests directly from the
[Copernicus Climate Data Store](https://cds.climate.copernicus.eu/datasets/reanalysis-era5-single-levels).

## Setup

From the repository root:

```bash
.venv/bin/python -m pip install -e '.[weather,notebooks]'
```

Select this repository's `.venv` Python interpreter as the notebook kernel.
The notebook accesses the public Zarr mirror anonymously and does not require
CDS credentials. For direct CDS requests through `ERA5Scraper.run`, register a
CDS account, accept the ERA5 single-level dataset terms, and configure the
account's personal access token securely. The scraper uses the official
`cdsapi` client and its supported configuration:

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

Edit the global settings in the first code cell:

- `NORTH`, `WEST`, `SOUTH`, `EAST`, and `REGION_NAME` define a rectangular region.
- `START_DATE` is inclusive; `END_DATE` is exclusive. Dates are **UTC**.
- `VARIABLES` defaults to `("2m_temperature",)`.
- `WORKERS` bounds concurrent day reads from the public Zarr store; start small.
- `LAKE_ROOT` optionally overrides the default lake directory.
- `REFRESH` forces retrieval for days that are already complete.

CDS retrieval details:

- `WORKERS` bounds concurrent CDS requests across dates and the number of
  active daily batches. Two is the default; try four if CDS limits and memory
  permit. One uses sequential retrieval.
- `LAKE_ROOT` and `RAW_ROOT` optionally override the default directories.
- `REFRESH` forces fresh retrieval for the requested region/dates/variables.
- `KEEP_RAW` retains successful NetCDF downloads when enabled.
- `RUN_DOWNLOAD` starts as `False`; set it to `True` after checking the settings.

Run the two code cells in order. To resume an interrupted backfill, keep the
same region, variables, and date interval: already-complete days are skipped,
and incomplete days are fetched again. Start with a short interval before a
long backfill. The notebook does not start a download until the execution cell
is run.

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

# Bulk read from the public ARCO Zarr mirror; writes to the same daily lake.
summary = scraper.run_bulk(date(2000, 1, 1), date(2000, 2, 1))

# Extends the same day files with Alaska's cells.
alaska = WeatherGrid(north=72, west=170, south=51, east=-130, name="Alaska")
ERA5Scraper(alaska).run(date(2000, 1, 1), date(2000, 1, 2))
```

`run_bulk` reads the public ARCO ERA5 Zarr store (anonymous GCS access by
default), selecting only the requested region, variables, and `[start, end)`
time interval. It fetches days concurrently up to `workers`, skips complete
partitions unless `refresh=True`, and merges into the same lake without
removing other regions or variables. It avoids a separate CDS request for
every variable/area/day, but still incurs remote GCS reads and needs network
access to the public bucket. Partition updates are serialized and atomically
replaced while the lake writer lock is held, so parallel reads cannot race to
overwrite a daily file.

Install `.[weather,notebooks]` to include the Zarr v3 and GCS filesystem
dependencies required by `run_bulk`. The CDS downloader remains available and
continues to use the CDS account credentials described above.

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
└── t_2m/year=2000/month=01/day=01/data.parquet

data/raw/weather/era5/
└── t_2m/2000/01/01/<request-fingerprint>.nc
```

`lake_root` is the ERA5 dataset directory, not the top-level lake directory.
It defaults to `<project>/data/lake/weather/era5`. Paths use the repository's
shared project-root helper and respect `COMM_RESEARCH_ROOT`. Each variable gets
its own subdirectory: `2m_temperature` uses `t_2m`, `total_precipitation`
uses `precip`, and the remaining names are in `VARIABLE_DIRECTORIES`.
Custom lake and raw roots also receive these variable subdirectories.

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
thread-safe. Bulk Zarr day reads are concurrent, but lake merges and writes
remain serialized under the lake writer lock. Source data must contain every
requested cell and all 24 hourly UTC timestamps for the day;
missing/nonfinite values, conflicting duplicates, unexpected units within a
variable, and incompatible existing schemas fail before a daily file is
replaced.

CDS retrieval details:

The scraper schedules requests round-robin across a bounded window of dates.
Even a temperature-only pull for one region can retrieve several dates at once.
Each request owns its CDS client and HTTP session, with TLS verification enabled.
There are at most `WORKERS` requests in flight and at most `WORKERS` active daily
batches; the entire history is never queued. Network retrieval continues while
the coordinator validates and writes completed dates. A slow earlier date does
not prevent a later complete date from being published.

Only the calling/coordinator thread merges and writes Parquet. All variables
and antimeridian areas for a date must finish before its variable files are
updated, once per run. Each variable file is published atomically; a failure
between variable writes can leave some variables complete. Rerunning safely
merges the cached responses and completes the remaining files. NetCDF decoding is serialized because the netCDF4/HDF5 library is not
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

When the coordinator detects a failure or interruption, it stops submitting
requests and cancels queued work. Already-running requests finish and cache
validated responses before the executor exits and the lake/raw locks are released. Workers never
publish Parquet. A second notebook or process using the same lake or raw cache
fails fast on its writer lock rather than racing with the first run.

To update an existing notebook, pull the branch and restart its kernel so it
imports the new scraper. Its existing `WORKERS` setting now controls cross-date
parallelism; no notebook API changes are required. Stop the previous pull first,
then rerun the original date range with `REFRESH = False`. Completed dates skip
automatically, and cached responses from unfinished days are reused. Dates can
finish out of order, so resume the original range rather than advancing the
start date to the last log message. Actual throughput depends on CDS queue,
account concurrency limits, network bandwidth, and local processing.

Atomicity is per day, not across an entire backfill. Up to `WORKERS` active
daily batches, their decoded responses, and existing combined partitions may
be held in memory. Reduce `WORKERS` for large regions or many variables. Begin
with regional requests; this implementation is not intended to materialize the
entire globe in a single in-memory daily table. Interrupting a queued CDS job can require
resubmission; only completed local downloads and lake partitions are resumed.
Recent days may not yet be available at CDS and may later be revised.

## Validation

```bash
.venv/bin/python -m unittest discover -s tests/markets/weather -v
```

Tests use real NetCDF fixtures and exercise UTC dates, overlapping/disjoint
regions, new variables, antimeridian cells, recovery, finalized/preliminary
fields, locks, and failed writes. Event-coordinated tests also verify concurrent
dates, bounded lookahead, out-of-order completion, one coordinator writing each
date, and retention of locks/raw data while parallel downloads finish after a
failure. A live CDS download additionally requires the
user's account token and accepted dataset terms.
