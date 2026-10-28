# Global hourly ERA5 temperature

Fetch global **2 m air temperature only** from Google's public
[ARCO ERA5 mirror](https://github.com/google-research/arco-era5), starting on
**2000-01-01** by default. No API token, CDS account, or cloud credentials are
required. HTTPS access to **storage.googleapis.com** is required.

The source is ERA5 single-level reanalysis on a regular **0.25° grid**, including
land and oceans: 721 latitudes × 1,440 longitudes, or 1,038,240 cells per hour.
The native ERA5 model resolution is approximately 31 km; this regular-grid
distribution is not an additional higher-resolution observation product.
Hourly is the temporal resolution of this product. ERA5-Land is a different,
land-only product and is not mixed into these partitions.

## Install and run

From the repository root:

```bash
python -m venv .venv
.venv/bin/python -m pip install -e '.[era5]'

# One complete global UTC day. --end is exclusive.
.venv/bin/python src/comm_research/markets/weather/era5.py \
  --start 2000-01-01 --end 2000-01-02

# Full backfill from 2000 through the latest complete finalized day.
# Choose a larger mounted volume before running the full history.
.venv/bin/python src/comm_research/markets/weather/era5.py --lake /mnt/lake
```

The module entry point is equivalent:

```bash
.venv/bin/python -m comm_research.markets.weather.era5 --help
```

Edit `START_DATE`, `END_DATE`, `LAKE_ROOT`, `WORKERS`, and `MIN_FREE_GB` near the
top of `markets/weather/era5.py` if you prefer running the file without flags.
Python callers can import that module and call `run(start=..., end=...)`.
All retrieval, normalization, and orchestration live in
`markets/weather/scrapers/era5/`.

## Lake layout and fields

`--lake` is the top-level lake directory, not the dataset directory. The default
is `<project>/data/lake`, respecting `COMM_RESEARCH_ROOT` through the shared path
helper. Partitions follow the repository's existing convention:

```text
data/lake/weather/era5/t_2m/single_levels/
└── year=2000/month=01/day=01/data.parquet

data/state/weather/era5/t_2m/
└── 2000/01/01.json
```

With a custom lake root, state defaults to `<lake>/.state/weather/era5/t_2m/` so it
travels with that lake. `--state` overrides it explicitly.

Each Parquet file contains **24,917,760 rows**, covering all 24 UTC hours of one
day. Coordinates remain columns; there are no per-location directories.

| Column | Type | Meaning |
| --- | --- | --- |
| `timestamp` | timestamp, microseconds, UTC | Valid time of the hourly field |
| `latitude` | float32 | Degrees north, from −90 to 90 |
| `longitude` | float32 | Degrees east, normalized to [−180, 180) |
| `temperature_2m` | float32 | Instantaneous 2 m air temperature in **kelvin** |

Subtract 273.15 to obtain Celsius. Values retain the source's float32 precision.
Rows are ordered by hour, source latitude order (north to south), and ascending
normalized longitude. Parquet field metadata records units; file metadata
records the source URL, finalized-data watermark, upstream update attribute,
and schema fingerprint. Zstandard compression and row-group statistics support
lazy time and location filtering:

```python
import polars as pl

temperature = (
    pl.scan_parquet(
        "data/lake/weather/era5/t_2m/single_levels/**/*.parquet",
        hive_partitioning=True,
    )
    .filter(
        pl.col("latitude").is_between(30, 50)
        & pl.col("longitude").is_between(-125, -65)
    )
    .with_columns((pl.col("temperature_2m") - 273.15).alias("temperature_c"))
)
```

## Parallelism, recovery, and storage

Four workers fetch independent global hours through a shared HTTP connection
pool. Only a bounded number of hours are in flight; the main thread writes
hourly batches into a temporary daily file. No full-day DataFrame is required.
The Zarr reader handles the upstream compression; only temperature and its
coordinates are retrieved. The store name ends in `.zarr-v3` but exposes Zarr v2
metadata, so the optional dependency intentionally pins `zarr<3`.

Transient transport errors and HTTP 408/429/5xx responses are retried with
backoff, jitter, and `Retry-After`. Access denials and missing chunks fail the
run. Missing chunks and nonfinite temperature values are never silently
published as valid global coverage.

Each completed file is checked for schema, all 24 timestamps, per-hour row
counts, and null counts before atomic publication. A per-lake writer lock
prevents concurrent runs from replacing each other's files. Failed downloads
remove their temporary file and preserve an existing completed partition.
Successful days remain available if a later day fails; rerunning resumes from
the first missing/incomplete day. Resume checks read Parquet footers, not every
historical data page; they are completeness checks, not full corruption scans.

The client uses the mirror's `valid_time_start` and `valid_time_stop` metadata
and downloads **only complete finalized days**, excluding ERA5T and incomplete
latest days. Requested dates outside that window fail explicitly. The mirror
can lag CDS; omitted `--end` means its latest finalized day, not today.
Use `--refresh` to replace requested days after upstream corrections. The
mirror is not a historical publication-vintage archive: these fields should
not be treated as information available at their original delivery hour.

The default 2 GiB free-space reserve is checked before each day and during
writes, with an additional conservative allowance for one uncompressed daily
table before starting a day. `--min-free-gb` changes the reserve. Exhausting
storage returns a failure rather than silently truncating the requested range.
Caught failures remove temporary files; an uncatchable termination can leave
hidden `.era5-*.partial` files, which are never scanned as Parquet partitions.

A live validation of **2000-01-01** produced approximately **70 MiB** of Parquet
for the complete global day. This is one measured day, not a guaranteed daily
size. At that rate, 2000 onward requires hundreds of GiB. Run a small date range
first and provision a sufficiently large filesystem for the full backfill.
`--lake` currently accepts local/mounted filesystem paths, not `s3://` or `gs://`
URLs. Source chunks are streamed through memory and are not retained in a
duplicate raw archive.

## Verification

```bash
.venv/bin/python -m unittest discover -s tests/markets/weather -v
```

Tests exercise real Zarr decoding over an HTTP fixture, missing chunks, retry
behavior, coordinate/value alignment, leap-day partitions, failed refreshes,
disk limits, completeness checks, and restart recovery. Live validation should
also run the one-day command twice: the second run should report a verified
skip and retrieve no temperature chunks.
