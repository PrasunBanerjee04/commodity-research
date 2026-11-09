# datalake

```sh
.venv/bin/python -m pip install -e './setup/datalake'
```

## `DataLake()` / `DataLake.open()`

Open the existing `data/lake/`. No registration or schema setup.

```python
import polars as pl
from datetime import datetime, timezone
from datalake import DataLake

lake = DataLake()
clean = DataLake(area="clean")       # data/clean/
raw = DataLake(area="raw")           # data/raw/
# Optional custom directory: DataLake("/absolute/path/to/tables")
```

## `lake.list_datasets(prefix=None, limit=100, offset=0)`

List table names discovered from Parquet/CSV files. Partition folders and `_audit/` are excluded.

```python
names = lake.list_datasets()
weather = lake.list_datasets(prefix="weather", limit=20)
```

## `lake.search(text, prefix=None, limit=100, offset=0)`

Find names containing all search terms; case-insensitive.

```python
names = lake.search("era5")
names = lake.search("t_2m", prefix="weather")
```

## `lake.describe(dataset, count_rows=False)`

Return columns/dtypes, file count, format and byte size. Row counting is optional.

```python
info = lake.describe("weather/era5/t_2m")
columns = info["columns"]
info = lake.describe("weather/era5/t_2m", count_rows=True)
rows = info["rows"]
```

## `lake.read(dataset, ...)`

Read a Polars DataFrame. Time windows include `start` and exclude `end`.

```python
df = lake.read(
    "weather/era5/t_2m",
    start="2025-01-01", end="2025-02-01",
    columns=["timestamp", "value"],
    filters={"latitude": 32.5, "longitude": -121.0},
)
preview = lake.read("weather/era5/t_2m", limit=100)
```

Use `time_column` when several date/time columns exist. `timezone` handles explicitly declared local times.

```python
prices = lake.read(
    "power_gas/napg/caiso/dam_lmp",
    start="2025-01-01", end="2025-01-02",
    time_column="interval_start_time_gmt",
    filters={"node": ["TH_SP15_GEN-APND"]},
)
```

Filters accept a scalar, a list, or `None` for nulls. Use `order_by=[...]` for custom sorting.

## `lake.scan(dataset, ...)`

Build a Polars LazyFrame; accepts the same selection arguments as `read`, except `limit`.

```python
summary = (
    lake.scan("weather/era5/t_2m", start="2025-01-01", end="2025-01-02")
    .group_by("timestamp")
    .agg(pl.col("value").mean())
    .collect()
)
```

## `lake.write(dataset, frame, mode="create", keys=None)`

Save a regular Parquet table. Columns and types come from the DataFrame.

| Mode | Behavior |
| --- | --- |
| `create` | Create a new table; fail if it exists |
| `append` | Add rows; check duplicates when `keys` is supplied |
| `upsert` | Insert/update rows by `keys` (required) |
| `replace` | Replace the table |

```python
features = pl.DataFrame({
    "timestamp": [datetime(2025, 1, 1, tzinfo=timezone.utc)],
    "value": [12.5],
})
path = lake.write("research/example", features, mode="replace")
path = lake.write("research/example", features, mode="upsert", keys=["timestamp"])
```

Writes support single-file Parquet outputs. Existing partitioned/multi-file data stays with ingestion scripts.

## `lake.export(dataset, destination=..., format=None, ...)`

Export a selection to CSV or Parquet; format follows the filename. Never overwrite an existing export.

```python
lake.export("research/example", destination="/tmp/features.parquet")
lake.export("research/example", destination="/tmp/features.csv", columns=["timestamp", "value"])
```

## `lake.close()`

Close the handle, or use a context manager.

```python
lake.close()
clean.close()
raw.close()
with DataLake.open() as lake:
    names = lake.search("era5")
```

**Read limits:** defaults are 1 million rows, 250 MB of returned data and a cooperative 60-second timeout.
Change with `DataLake(max_rows=..., max_bytes=..., timeout=...)`, or use `scan()` for caller-managed collection.
Date-only bounds use UTC unless `timezone` is supplied. Naive local timestamps require an explicit timezone.
Files describe stored observations, not guaranteed source completeness. No database, registration, snapshots or automatic fetching.
