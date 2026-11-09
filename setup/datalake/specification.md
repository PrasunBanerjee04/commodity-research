# QuantData: Unified Research Data Lake API — Codex Implementation Specification

**Status:** Implementation specification v1.0  
**Target:** Python 3.12+ library, local-first research platform with a path to shared deployment  
**Primary consumers:** Jupyter notebooks, Python research jobs, FastAPI services, scenario dashboards  
**Core stack:** Polars, PyArrow/Parquet, DuckDB, Pydantic v2, SQLAlchemy, SQLite for local metadata, optional PostgreSQL and S3-compatible object storage; xarray/Zarr optional forecast extension  
**Package name:** `quantdata`  
**Goal:** A discoverable, typed, point-in-time-aware, cache-aware, versioned interface to external market data, internal features, forecasts, and simulation outputs.

## 0. Implementation instructions for Codex

Implement the specification in phases in §16. Implement actual working code and tests; do not create stub methods that silently return dummy values. Build the v1 core using a local filesystem-backed Parquet lake, SQLite metadata catalog, DuckDB queries, and Polars return types. Preserve adapter boundaries for PostgreSQL and cloud object storage but do **not** implement those integrations until requested. Forecast xarray/Zarr support and background scenario execution belong to later phases; interfaces may be defined earlier only when accompanied by explicit `NotImplementedError` and excluded from the advertised v1 surface.

Use UTC for internal timestamps. Time bounds are **start-inclusive, end-exclusive**. Never silently forward-fill, timezone-convert ambiguously, use latest revised data in a historical-as-of query, or infer missing cache coverage from the presence of a file. All nontrivial data reads must be testable and reproducible. Publish a `py.typed` marker; require type checking, unit/integration tests, and examples.

**Non-goals for v1:** a distributed execution engine, user login, live market streaming, enterprise RBAC, arbitrary writable SQL, auto-generated trading signals, broker execution, and native Jupyter hosting.

## 1. Intended usage

```python
import polars as pl
from quantdata import DataLake, DataLakeConfig

lake = DataLake.open(DataLakeConfig.local("./research_lake"))

# Discover and understand datasets.
results = lake.search("caiso lmp", tags=["power"])
meta = lake.describe("caiso.lmp")
coverage = lake.coverage("caiso.lmp", filters={"market": "DA"})

# Get point-in-time-correct market data.
prices: pl.DataFrame = lake.read(
    "caiso.lmp",
    start="2025-01-01T00:00:00Z",
    end="2025-02-01T00:00:00Z",
    columns=["delivery_start_utc", "node", "lmp"],
    filters={"market": "DA", "node": ["NODE_A", "NODE_B"]},
    asof="2025-02-02T00:00:00Z",
    cache="only",
)

# Push down filters and aggregation; no implicit full-table collection.
scan: pl.LazyFrame = lake.scan("caiso.lmp", filters={"market": "DA"})
daily = scan.group_by_dynamic("delivery_start_utc", every="1d").agg(
    pl.col("lmp").mean()
).collect()

# Save a personal research output and retrieve a pinned version.
receipt = lake.write("users.me.sp15_features", daily, mode="replace")
frozen = lake.read("users.me.sp15_features", snapshot=receipt.snapshot_id)

# Reuse the same data layer from model jobs or dashboard endpoints.
```

All IDs are opaque strings, not file paths. Examples of identifiers (`caiso.lmp`, `users.me...`) are conventions, not hard-coded special cases.

## 2. Package layout

```text
quantdata/
  __init__.py                  # public exports, version
  api.py                       # DataLake facade
  config.py                    # DataLakeConfig
  types.py                     # enums, aliases, filter/query types
  schemas.py                   # typed public DTOs
  errors.py                    # typed exception hierarchy
  registry.py                  # DatasetRegistry
  query.py                     # validation/planning; Polars/DuckDB integrations
  time.py                      # UTC parsing and half-open interval rules
  storage/
    object_store.py            # protocol
    local_store.py             # atomic local object writes
    parquet_store.py           # immutable Parquet objects
    metadata_store.py          # protocol
    sqlite_catalog.py          # transactional metadata
  ingest/
    adapters.py                # SourceAdapter protocol
    manager.py                 # gap detection, fetching, validation, commit
  scenario/
    models.py                  # Study, ScenarioDefinition, Run
    store.py                   # output artifacts and comparisons
  forecast/
    models.py                  # forecast metadata
    store.py                   # optional xarray/Zarr
  features/
    registry.py                # optional versioned feature definitions
  http/                        # optional later: FastAPI adapter
  cli.py                       # optional minimal admin CLI
  py.typed
tests/
  unit/
  integration/
  fixtures/
examples/
  quickstart.py
  point_in_time.py
  scenario_usage.py
pyproject.toml
README.md
```

## 3. Data types and conventions

### 3.1 Public aliases and enums

```python
from __future__ import annotations
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Literal, Mapping, Sequence, TypeAlias

TimestampInput: TypeAlias = datetime | date | str
Scalar: TypeAlias = str | int | float | bool | None
FilterValue: TypeAlias = Scalar | Sequence[Scalar]
Filters: TypeAlias = Mapping[str, FilterValue]
DatasetId: TypeAlias = str
SnapshotId: TypeAlias = str
StudyId: TypeAlias = str
ScenarioId: TypeAlias = str
RunId: TypeAlias = str

class CachePolicy(StrEnum):
    ONLY = "only"                # never contact source; error on missing coverage
    FETCH_MISSING = "fetch_missing"  # sync fetch only if adapter supports bounded fetch
    REFRESH = "refresh"          # revalidate/refetch requested interval

class WriteMode(StrEnum):
    CREATE = "create"            # fail if dataset exists
    APPEND = "append"            # new unique rows only, reject duplicates
    UPSERT = "upsert"            # insert/update by declared logical key
    REPLACE = "replace"          # produce new complete snapshot, never erase history

class DatasetKind(StrEnum):
    TABULAR = "tabular"
    FORECAST_GRID = "forecast_grid"
    MODEL_OUTPUT = "model_output"

class RunStatus(StrEnum):
    CREATED = "created"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

class DataFormat(StrEnum):
    PARQUET = "parquet"
    CSV = "csv"
    ARROW = "arrow"
    EXCEL = "excel"
    NETCDF = "netcdf"     # optional forecast extension
    ZARR = "zarr"         # optional forecast extension
```

**Timestamp handling:** Accept an aware `datetime` or RFC 3339 string for time-of-day queries. A `date` or date-only ISO string is permitted only when the dataset declares a timezone/calendar; interpret as midnight in that calendar and convert to UTC. A naive datetime without an explicitly configured timezone raises `InvalidTimeError`. Represent delivery periods as UTC interval starts and an optional interval end or duration. Handle 23/25-hour DST days without collapsing two distinct intervals.

**`asof`:** Filters observations by when they became available to the research user, not by their delivery timestamp. A dataset declares `available_at_column` and revision key semantics. An `asof` query selects the latest revision with `available_at <= asof` for each logical observation. If a dataset does not retain sufficient history, raise `PointInTimeUnavailableError`; never substitute the current version. `snapshot` pins an immutable physical dataset commit. Supplying both `asof` and `snapshot` is permitted: evaluate as-of against the pinned snapshot, but reject if metadata cannot support the requested view.

**Cache:** `only` must not reach external sources. `fetch_missing` is permitted only with complete source adapter coverage tracking and known small bounded downloads; it may raise `FetchRequiresJobError` with a suggested refresh job for large/unsupported retrieval. `refresh` is disallowed with `snapshot` and triggers source revalidation before returning. A successful read returns a `pl.DataFrame` (not a DataFrame-or-job union).

### 3.2 Typed public models (Pydantic v2)

Create these as `BaseModel` classes with validated fields and accurate JSON schema:

| Model | Required fields | Other fields |
|---|---|---|
| `FieldSpec` | `name: str`, `dtype: str` | `unit: str|None`, `description: str|None`, `nullable: bool` |
| `DatasetSpec` | `id: DatasetId`, `kind: DatasetKind`, `schema: list[FieldSpec]`, `time_column: str`, `primary_key: list[str]` | `source: str|None`, `description`, `tags: list[str]`, `frequency: str|None`, `calendar: str|None`, `timezone: str`, `available_at_column: str|None`, `revision_column: str|None`, `dimensions: list[str]`, `quality_rules: list[QualityRule]`, `owner`, `visibility` |
| `DatasetSummary` | `id`, `kind`, `description`, `tags` | `source`, `frequency`, `min_time`, `max_time`, `latest_snapshot_id` |
| `CoverageInterval` | `start: datetime`, `end: datetime`, `status: Literal["complete","partial","stale"]` | `missing_count: int|None`, `checked_at: datetime|None` |
| `CoverageReport` | `dataset_id`, `intervals: list[CoverageInterval]`, `requested_filters: dict` | `missing_intervals`, `last_refreshed_at`, `latest_snapshot_id` |
| `DatasetSnapshot` | `id`, `dataset_id`, `committed_at`, `manifest_uri`, `row_count`, `schema_fingerprint` | `parent_snapshot_id`, `source_version`, `description` |
| `WriteReceipt` | `dataset_id`, `snapshot_id`, `written_rows`, `committed_at` | `rejected_rows: int=0`, `warnings: list[str]` |
| `RefreshJob` | `id`, `dataset_id`, `status: RunStatus`, `created_at` | `started_at`, `finished_at`, `requested_start`, `requested_end`, `error_message` |
| `FeatureSpec` | `id`, `version`, `input_datasets: list[str]`, `output_schema: list[FieldSpec]` | `description`, `lookback`, `availability_rules` |
| `Study` | `id`, `name`, `created_at` | `description`, `tags`, `owner` |
| `ScenarioDefinition` | `id`, `study_id`, `name`, `assumptions: dict[str, JSONValue]`, `created_at` | `baseline_run_id`, `description` |
| `ScenarioRun` | `id`, `scenario_id`, `model_id`, `model_version`, `status`, `created_at`, `input_snapshot_ids: dict[str, str]` | `started_at`, `finished_at`, `error_message`, `solver_settings`, `code_version`, `outputs: dict[str, ArtifactRef]` |
| `ArtifactRef` | `name`, `uri`, `format`, `schema_fingerprint`, `size_bytes` | `row_count`, `checksum` |
| `ExportReceipt` | `destination`, `format`, `bytes_written` | `row_count`, `snapshot_id` |
| `QueryStats` | `elapsed_ms`, `rows_returned`, `cache_hit: bool` | `scanned_bytes`, `snapshot_id`, `source_fetch_ms` |

`JSONValue` is a recursive JSON-compatible type. Define `QualityRule` with `name`, `kind`, `params`, and `severity` (error/warning), with initial implementations for nulls, duplicates, bounded ranges, and timestamp monotonicity. Do not serialize NumPy objects or custom classes into scenario assumptions implicitly.

**Important schema detail:** The `primary_key` is the *logical observation key*, e.g. `(node, market, delivery_start_utc)`; a physical revision identity includes revision/availability information. `DatasetSpec` must validate all named columns exist in `schema`.

### 3.3 Config

```python
class DataLakeConfig(BaseModel):
    root_uri: str                      # e.g. file:///.../research_lake
    catalog_url: str                  # sqlite:///... or later postgresql://...
    default_timezone: str = "UTC"
    default_cache_policy: CachePolicy = CachePolicy.ONLY
    maximum_sync_fetch_rows: int = 1_000_000
    maximum_sync_fetch_bytes: int = 250_000_000
    query_timeout_seconds: int = 60
    max_memory_mb: int = 2048

    @classmethod
    def local(cls, path: str) -> DataLakeConfig: ...
```

`DataLake.open(config: DataLakeConfig) -> DataLake` initializes/reopens storage and metadata. `DataLake.from_config(name: str, *, config_path: str|None=None) -> DataLake` loads named configuration from YAML; environment variables may override credentials only (document precedence). `lake.close() -> None` releases resources and `with DataLake.open(...) as lake:` is supported.

## 4. Core `DataLake` method contracts

All methods below are instance methods. Signatures are normative. Unless stated otherwise, `start`/`end` are optional only when the dataset supports an unbounded read under configured size limits. Return DataFrames are Polars; inputs to writes may be `pl.DataFrame`, `pa.Table`, or `pd.DataFrame` with optional dependencies handled explicitly.

### 4.1 Discovery

```python
lake.search(
    text: str = "", *,
    tags: Sequence[str] | None = None,
    source: str | None = None,
    kind: DatasetKind | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[DatasetSummary]
```

Case-insensitive token search over dataset ID, description, and tags. All supplied selectors AND together; tags match all specified tags. Stable ordering by relevance, then ID. Reject invalid pagination.

```python
lake.describe(dataset: DatasetId) -> DatasetSpec
lake.list_datasets(*, namespace: str | None = None, limit: int = 100, offset: int = 0) -> list[DatasetSummary]
lake.history(dataset: DatasetId, *, limit: int = 100, before: datetime | None = None) -> list[DatasetSnapshot]
lake.coverage(
    dataset: DatasetId, *,
    start: TimestampInput | None = None,
    end: TimestampInput | None = None,
    filters: Filters | None = None,
    asof: TimestampInput | None = None,
) -> CoverageReport
```

Coverage is tracked by canonical dataset **and filter/partition keys**, not just dataset-wide min/max timestamps. Explicitly distinguish data absent because the market/source had no observation from data not fetched. `history` returns immutable commits newest first.

### 4.2 Retrieval

```python
lake.read(
    dataset: DatasetId, *,
    start: TimestampInput | None = None,
    end: TimestampInput | None = None,
    columns: Sequence[str] | None = None,
    filters: Filters | None = None,
    asof: TimestampInput | None = None,
    snapshot: SnapshotId | None = None,
    cache: CachePolicy = CachePolicy.ONLY,
    limit: int | None = None,
    order_by: Sequence[str] | None = None,
) -> pl.DataFrame
```

- Validate dataset, field names, filter types, and bounds before any I/O.
- Filters: a scalar means equality, sequences mean membership; `None` means SQL null equality, empty sequence returns empty result. Support richer predicates via `scan` or SQL, not arbitrary user-written expressions in `filters`.
- Default stable sort is `(time_column, primary_key remaining columns)` ascending. Never promise implicit row ordering from raw Parquet scans.
- Apply predicate/projection pushdown; enforce query timeout and memory/row constraints.
- If `cache=ONLY`, verify requested coverage and report missing ranges in `CacheMissError`. Empty legitimate datasets are allowed if the source reports complete coverage.
- For external fetch behavior, delegate to registered `SourceAdapter`, validate, atomically commit, then requery.
- For private snapshots, avoid accidental selection of subsequent writes or modifications.

```python
lake.scan(
    dataset: DatasetId, *,
    start: TimestampInput | None = None,
    end: TimestampInput | None = None,
    columns: Sequence[str] | None = None,
    filters: Filters | None = None,
    asof: TimestampInput | None = None,
    snapshot: SnapshotId | None = None,
) -> pl.LazyFrame
```

`scan()` reads **only persisted data**; does not do network fetching or refresh, and raises `CacheMissError` when explicit requested coverage is missing. It exposes a lazy Polars scan over a snapshot-consistent set of Parquet paths and applies compatible predicate pushdown. Keep referenced files available for the lifetime of a lazy query; immutable object files avoid invalidating existing plans.

```python
lake.query(
    sql: str, *,
    parameters: Mapping[str, Scalar] | None = None,
    snapshot_overrides: Mapping[DatasetId, SnapshotId] | None = None,
    limit: int | None = None,
) -> pl.DataFrame
```

Read-only, single-statement SQL `SELECT`/`WITH SELECT` against registered dataset views. Do not permit filesystem reads, arbitrary `read_parquet` URLs, `COPY`, `ATTACH`, extension loading, DDL/DML, or multiple statements. Use parameter binding, not SQL interpolation. Restrict dataset access to registered IDs and resolved manifests. Dataset names with periods must be supported using a documented SQL-safe quoting convention (e.g., `"caiso.lmp"`). Document the isolation limitations of embedded DuckDB; enforce strict SQL permissions if exposed to untrusted remote clients.

```python
lake.align(
    series: Mapping[str, DatasetId], *,
    start: TimestampInput,
    end: TimestampInput,
    frequency: str,
    join: Literal["inner", "left", "outer"] = "inner",
    time_key: str = "delivery_start_utc",
    aggregations: Mapping[str, str] | None = None,
    availability_cutoff: TimestampInput | None = None,
    fill: Literal["none", "forward"] = "none",
    max_fill_age: timedelta | None = None,
) -> pl.DataFrame
```

Prefer an explicit named-series join. Reject mixed frequencies or duplicate timestamps if aggregation/join policy is not supplied. A forward fill requires both an explicit `fill="forward"` and `max_fill_age`; it must honor availability timestamps if `availability_cutoff` is set. Do not add this method until plain read/scan/query correctness is complete.

### 4.3 Writes and exports

```python
lake.register(spec: DatasetSpec, *, if_exists: Literal["error", "ignore"] = "error") -> DatasetSpec

lake.write(
    dataset: DatasetId,
    frame: pl.DataFrame | pa.Table | pd.DataFrame, *,
    mode: WriteMode = WriteMode.CREATE,
    expected_snapshot: SnapshotId | None = None,
    metadata: Mapping[str, str] | None = None,
    validate: bool = True,
    idempotency_key: str | None = None,
) -> WriteReceipt

lake.export(
    dataset: DatasetId, *,
    destination: str,
    format: DataFormat = DataFormat.PARQUET,
    start: TimestampInput | None = None,
    end: TimestampInput | None = None,
    filters: Filters | None = None,
    asof: TimestampInput | None = None,
    snapshot: SnapshotId | None = None,
    columns: Sequence[str] | None = None,
) -> ExportReceipt
```

`register()` creates metadata, never imports data implicitly. `write()` validates schema and logical key constraints, creates immutable object files, then atomically switches the catalog's current manifest pointer inside a transaction. Support `expected_snapshot` for optimistic concurrency; raise `ConcurrentWriteError` on conflict. `APPEND` must reject duplicate logical keys unless new revisions are explicitly modeled; `UPSERT` creates a new snapshot of current logical rows. `REPLACE` updates the current pointer but does not delete earlier snapshots. Repeating an identical `idempotency_key` with identical request should return the original receipt; reuse with different payload fails.

`export()` creates a local output atomically (write temp, then rename), and it must use the same point-in-time and snapshot semantics as `read`. v1 exports Parquet and CSV; Excel and Arrow are extensions. Never overwrite a destination unless an explicit later `overwrite` option is added.

```python
lake.publish(
    source_dataset: DatasetId, *,
    target_dataset: DatasetId,
    source_snapshot: SnapshotId | None = None,
    expected_target_snapshot: SnapshotId | None = None,
    validate: bool = True,
) -> WriteReceipt
```

Later-phase controlled promotion from personal to shared namespace. Enforce access controls when shared deployment is implemented; do not imply RBAC in local v1.

### 4.4 Refresh and ingestion

```python
lake.refresh(
    dataset: DatasetId, *,
    start: TimestampInput,
    end: TimestampInput,
    filters: Filters | None = None,
    mode: Literal["incremental", "revalidate", "force"] = "incremental",
    wait: bool = False,
) -> RefreshJob

lake.get_refresh_job(job_id: str) -> RefreshJob
lake.list_refresh_jobs(
    *, dataset: DatasetId | None = None,
    status: RunStatus | None = None,
    limit: int = 100,
) -> list[RefreshJob]
```

- `incremental`: fetch missing/invalid coverage only.
- `revalidate`: check source changes/revisions; fetch if changed.
- `force`: fetch full interval and commit revisions even if already cached (without deleting old snapshots).
- `wait=False` queues a durable job and returns immediately. In v1 a persistent local job runner may process queued jobs in a separate worker process; if no runner exists, return an explicit configuration error rather than pretending the job will execute. `wait=True` executes/waits synchronously with documented limits, still returning a terminal `RefreshJob`.
- Do not claim a date interval complete based solely on successful HTTP response. Track source coverage, expected frequency, data release calendars, and known gaps.

```python
class SourceAdapter(Protocol):
    @property
    def source_id(self) -> str: ...
    def supports(self, dataset: DatasetSpec) -> bool: ...
    def fetch(
        self, dataset: DatasetSpec, *,
        start: datetime, end: datetime, filters: Filters | None,
    ) -> SourceBatch: ...
    def normalize(self, dataset: DatasetSpec, batch: SourceBatch) -> pl.DataFrame: ...
    def validate(self, dataset: DatasetSpec, frame: pl.DataFrame) -> ValidationReport: ...
```

`SourceBatch` contains raw bytes or objects, request parameters, source timestamp, source response provenance, and coverage assertions. Provide a fake deterministic adapter in tests and one optional real adapter only if reliable public fixtures and credentials permit it. HTTP retries with backoff, rate limits, pagination, credentials, and source-specific timestamps remain inside the adapter/infrastructure, never in research notebooks.

### 4.5 Materialized queries and features (phase 2)

```python
lake.materialize(
    name: DatasetId, *,
    sql: str,
    refresh: Literal["manual", "hourly", "daily"] = "manual",
    description: str | None = None,
) -> DatasetSpec

lake.features.register(spec: FeatureSpec, computation: Callable[..., pl.DataFrame]) -> FeatureSpec
lake.features.compute(
    features: Sequence[str], *,
    start: TimestampInput,
    end: TimestampInput,
    asof: TimestampInput | None = None,
    parameters: Mapping[str, JSONValue] | None = None,
) -> pl.DataFrame
```

Feature definitions are versioned and declare dependencies plus observation/availability semantics. Never use function pickling as a durable definition; store a stable module entry point and code revision. Prevent future information leaking into historical feature calculations.

## 5. Forecast API (phase 3)

Expose through `lake.forecasts`. Forecast grid datasets are **not** accepted by `lake.scan()` or generic tabular `lake.read()`; those raise `UnsupportedDatasetKindError` with actionable guidance. A forecast may also have a tabular observation view registered separately.

```python
lake.forecasts.read(
    dataset: DatasetId, *,
    initialized: TimestampInput | None = None,
    initialized_between: tuple[TimestampInput, TimestampInput] | None = None,
    valid_between: tuple[TimestampInput, TimestampInput] | None = None,
    variables: Sequence[str] | None = None,
    region: str | BoundingBox | None = None,
    members: Literal["all"] | Sequence[int] | None = None,
    leads: Sequence[int] | None = None,   # forecast lead hours
    asof: TimestampInput | None = None,
    snapshot: SnapshotId | None = None,
) -> xr.Dataset

lake.forecasts.latest(
    dataset: DatasetId, *,
    cutoff: TimestampInput,
    variables: Sequence[str] | None = None,
    region: str | BoundingBox | None = None,
    members: Literal["all"] | Sequence[int] | None = None,
) -> xr.Dataset

lake.forecasts.history(
    dataset: DatasetId, *,
    initialized_between: tuple[TimestampInput, TimestampInput],
    variables: Sequence[str],
    lead_hours: Sequence[int] | None = None,
    region: str | BoundingBox | None = None,
    asof: TimestampInput | None = None,
) -> xr.Dataset

lake.forecasts.write(
    dataset: DatasetId,
    data: xr.Dataset, *,
    mode: WriteMode = WriteMode.APPEND,
    expected_snapshot: SnapshotId | None = None,
) -> WriteReceipt
```

`BoundingBox` has `west`, `south`, `east`, `north` floats in EPSG:4326; support antimeridian explicitly or reject it with a documented error. Forecast coordinates must distinguish initialization, valid time, lead, ensemble member, lat/lon, and source model version. Preserve runs and revisions: never overwrite an older initialization. Support chunked Zarr for multidimensional arrays.

## 6. Scenario studies and run artifacts (phase 2)

The scenario manager is a versioned metadata/artifact layer, not a solver itself. Model execution may be invoked via a separate, explicitly registered runner; it must never execute arbitrary strings as code.

```python
lake.scenarios.create_study(
    name: str, *, description: str = "", tags: Sequence[str] = (),
) -> Study

lake.scenarios.list_studies(
    *, text: str = "", limit: int = 100, offset: int = 0,
) -> list[Study]

lake.scenarios.get_study(study_id: StudyId) -> Study

lake.scenarios.create_scenario(
    study_id: StudyId, *,
    name: str,
    assumptions: Mapping[str, JSONValue],
    baseline_run_id: RunId | None = None,
    description: str = "",
) -> ScenarioDefinition

lake.scenarios.clone_scenario(
    scenario_id: ScenarioId, *,
    name: str,
    overrides: Mapping[str, JSONValue] | None = None,
) -> ScenarioDefinition

lake.scenarios.list_scenarios(study_id: StudyId) -> list[ScenarioDefinition]
lake.scenarios.get_scenario(scenario_id: ScenarioId) -> ScenarioDefinition

lake.scenarios.create_run(
    scenario_id: ScenarioId, *,
    model_id: str,
    model_version: str,
    input_snapshot_ids: Mapping[DatasetId, SnapshotId],
    code_version: str | None = None,
    solver_settings: Mapping[str, JSONValue] | None = None,
) -> ScenarioRun

lake.scenarios.get_run(run_id: RunId) -> ScenarioRun
lake.scenarios.list_runs(
    *, study_id: StudyId | None = None,
    scenario_id: ScenarioId | None = None,
    status: RunStatus | None = None,
    limit: int = 100,
) -> list[ScenarioRun]

lake.scenarios.write_output(
    run_id: RunId,
    name: str,
    data: pl.DataFrame | pa.Table | pd.DataFrame, *,
    metadata: Mapping[str, str] | None = None,
) -> ArtifactRef

lake.scenarios.complete_run(run_id: RunId) -> ScenarioRun
lake.scenarios.fail_run(run_id: RunId, *, error_message: str) -> ScenarioRun

lake.scenarios.read_output(
    run_id: RunId, *,
    name: str,
    columns: Sequence[str] | None = None,
    filters: Filters | None = None,
) -> pl.DataFrame

lake.scenarios.compare(
    run_ids: Sequence[RunId], *,
    output: str,
    key_columns: Sequence[str],
    value_columns: Sequence[str],
    baseline_run_id: RunId,
    metric: Literal["absolute", "percent", "side_by_side"] = "absolute",
) -> pl.DataFrame

lake.scenarios.export_output(
    run_id: RunId, *,
    name: str,
    destination: str,
    format: DataFormat = DataFormat.PARQUET,
) -> ExportReceipt
```

**Lifecycle:** `create_run` pins assumptions, input snapshots, model ID/version, and settings and returns `CREATED`. A job runner may transition it to `QUEUED` / `RUNNING`. In a manual v2 workflow, an explicit `start_run(run_id) -> ScenarioRun` transitions `CREATED` to `RUNNING` before output writes. `write_output` permits writes only while `RUNNING`, and uses immutable artifacts plus checksums. `complete_run` transactionally publishes the output manifest and sets `COMPLETED`; incomplete outputs are never exposed as completed. `fail_run` records the failure; retrying creates a *new* run. An executed run's assumptions, snapshots, and existing artifacts are immutable. Add an optional `submit_run(run_id) -> ScenarioRun` only once a durable worker/registry is implemented; require an allowlisted model runner and queue, never client-submitted arbitrary code.

**Comparison:** `run_ids` must include baseline; validate compatible output schemas/units; join on `key_columns` with explicit missing-row handling (surface a `row_status` field or raise), compute scenario-minus-baseline. For percent changes, return null and an explicit zero-denominator indicator when baseline is zero. No silent interpolation or positional subtraction.

## 7. Dataset registry contract

Example registration:

```python
from quantdata import DatasetSpec, DatasetKind, FieldSpec

lake.register(DatasetSpec(
    id="caiso.lmp",
    kind=DatasetKind.TABULAR,
    source="caiso_oasis",
    description="CAISO nodal LMP observations",
    tags=["power", "caiso", "prices"],
    schema=[
        FieldSpec(name="node", dtype="string", nullable=False),
        FieldSpec(name="market", dtype="string", nullable=False),
        FieldSpec(name="delivery_start_utc", dtype="datetime[us, UTC]", nullable=False),
        FieldSpec(name="available_at_utc", dtype="datetime[us, UTC]", nullable=False),
        FieldSpec(name="lmp", dtype="float64", unit="USD/MWh"),
    ],
    time_column="delivery_start_utc",
    available_at_column="available_at_utc",
    primary_key=["node", "market", "delivery_start_utc"],
    dimensions=["node", "market"],
    timezone="UTC",
    frequency="PT1H",  # illustrative: must match real dataset resolution
    revision_policy="versioned", # include this typed field on DatasetSpec
))
```

Declare `revision_policy: Literal["append_only","versioned","replace_only"]` on `DatasetSpec` (default `append_only`). For versioned data, ensure stable deterministic revision ordering: use availability time then explicit revision identifier or ingestion sequence as tie-breaker; duplicate revisions without a valid tie-breaker fail validation. Track dataset ownership, provenance, quality status, and last successful source refresh. Support dataset schema evolution only through a later migration command; v1 rejects unapproved incompatible changes.

## 8. Storage and atomicity

Local layout (physical detail, not exposed through API):

```text
research_lake/
  catalog.sqlite
  objects/<content-or-object-id>.parquet
  raw/<source>/<request-id>/...
  manifests/<dataset-id>/<snapshot-id>.json
  scenario_artifacts/<run-id>/<output-name>/<artifact-id>.parquet
  temporary/<job-id>/...
```

Use immutable Parquet object files. A snapshot manifest enumerates its objects and metadata. Write temp files, flush/validate, atomically publish immutable objects, then atomically update the current snapshot pointer and coverage metadata in a SQLite transaction. Readers first resolve one snapshot and remain pinned to it. Orphaned temporary objects may be garbage collected later. Do not delete snapshots on `REPLACE`. Use checksums for artifact integrity. Document crash consistency on local filesystems; cloud deployment must use backend-appropriate atomic metadata commits.

Partition intelligently by dataset/time and optionally high-cardinality filters, but avoid one tiny Parquet file per hour/node. Aim for bounded file counts and row-group pruning. File compaction is a maintenance operation and must not alter snapshot-visible data.

## 9. Data correctness rules

1. All intervals `[start, end)`; reject `end <= start`.
2. Internal times are timezone-aware UTC; no implicit ambiguous DST conversion.
3. Preserve both **event/delivery time** and **availability/publication time** where applicable. Record ingestion time separately.
4. `asof` resolves the last *available* historical revision as of cutoff. Inadequate revision history causes an explicit error.
5. `snapshot` pins physical lake version; reproducibility depends on immutable manifests and referenced objects.
6. Dataset semantics (units, calendars, delivery-hour conventions, location identifiers, market) are declared, not guessed.
7. Nulls/duplicates, expected cadence, and missing intervals have explicit validation states.
8. A coverage record cannot be inferred from min/max data timestamps alone.
9. Cache freshness and temporal completeness are separate dimensions.
10. All model runs pin data snapshots, assumption JSON, and code/model versions.
11. Feature alignment forbids unbounded implicit forward fills and future information.
12. Never interpolate nodal/geospatial values without a named transformation and method metadata.

## 10. Error model

Implement `QuantDataError` as the base exception and these subclasses, each carrying `code: str`, `message: str`, `details: dict`:

| Exception | When raised |
|---|---|
| `DatasetNotFoundError` | Unknown dataset ID |
| `DatasetAlreadyExistsError` | `CREATE` or registration collision |
| `InvalidQueryError` | Bad filters, columns, unsupported SQL, invalid limits |
| `InvalidTimeError` | Naive/ambiguous/malformed timestamps or invalid ranges |
| `CacheMissError` | Requested coverage missing under `ONLY` or `scan` |
| `FetchRequiresJobError` | On-demand fetch too large or unsupported synchronously |
| `SourceUnavailableError` | Source failure, authentication, rate-limit exhaustion |
| `ValidationError` | Data schema, quality, or key violation (avoid ambiguity with Pydantic's error name by exporting `DataValidationError`) |
| `PointInTimeUnavailableError` | Unable to reconstruct specified historical as-of view |
| `SnapshotNotFoundError` | Invalid dataset snapshot reference |
| `ConcurrentWriteError` | Optimistic concurrency conflict |
| `UnsupportedDatasetKindError` | Tabular API called for grid dataset or vice versa |
| `UnsupportedFormatError` | Format not implemented |
| `RunNotFoundError` | Missing scenario run |
| `InvalidRunStateError` | Illegal run state transition/output write |
| `PermissionDeniedError` | Reserved for later shared deployment |
| `QueryLimitExceededError` | Query exceeds configured memory/time/row bounds |

Errors must include actionable dataset/run IDs and missing intervals where appropriate. Never return an empty DataFrame for a failed data fetch.

## 11. Synchronous versus asynchronous guarantees

- `search`, `describe`, `coverage`, `history`, `read`, `scan`, `query`, `write`, and `export` are synchronous, deterministic operations given pinned snapshots.
- `refresh(..., wait=False)` submits a durable background refresh **only if** a configured worker exists; status is pollable.
- Interactive model/LP runs use a separate job execution service added in phase 2/3. Submitting a run must not block a UI request.
- The browser should access the system through an optional FastAPI service; never connect directly to SQLite or object storage from the browser for arbitrary access.
- API calls that require costly work expose explicit job IDs; never masquerade as fast reads.

## 12. Optional HTTP endpoint mapping (after Python core)

The FastAPI layer must call the same service functions, not independently implement data semantics.

| HTTP | Endpoint | Python counterpart |
|---|---|---|
| `GET` | `/v1/datasets` | `search` / `list_datasets` |
| `GET` | `/v1/datasets/{id}` | `describe` |
| `GET` | `/v1/datasets/{id}/coverage` | `coverage` |
| `POST` | `/v1/query` | `read` (JSON request, Arrow stream or paginated JSON response) |
| `POST` | `/v1/query/sql` | `query` (admin/trusted contexts only) |
| `POST` | `/v1/datasets/{id}/refresh` | `refresh` |
| `GET` | `/v1/jobs/{id}` | `get_refresh_job` |
| `POST` | `/v1/studies` | `create_study` |
| `POST` | `/v1/scenarios` | `create_scenario` |
| `POST` | `/v1/scenarios/{id}/runs` | `create_run` / submit when available |
| `GET` | `/v1/runs/{id}` | `get_run` |
| `GET` | `/v1/runs/{id}/outputs/{name}` | `read_output` |

Use Pydantic models for requests/responses; `id` path parameters may contain periods and must be handled correctly. Paginate large responses or stream Arrow/Parquet rather than sending unlimited JSON. Do not expose unrestricted SQL to untrusted clients. Include schema version and explicit units in responses.

## 13. Test requirements and acceptance criteria

Use pytest, hypothesis for property checks where useful, Ruff, and mypy or pyright. Tests run without internet or paid credentials.

**Mandatory v1 tests**

1. Register, list, search, describe, and reopen catalog from disk.
2. Write DataFrame; read identical typed values; verify immutable snapshot ID.
3. `CREATE`, `APPEND`, `UPSERT`, and `REPLACE` behavior with key collisions.
4. Concurrent write conflict with `expected_snapshot`.
5. Repeat `idempotency_key` with identical payload; reject changed payload.
6. Half-open time-window boundaries; ordering; column/filter pushdown.
7. DST transition: distinct UTC delivery hours survive and are unambiguous.
8. Historical versioned observation with two release timestamps: `asof` returns correct revision, not latest.
9. `asof` fails for nonversioned dataset without reliable available-at history.
10. `snapshot` returns prior values after a later write.
11. Coverage missing *inside* an otherwise populated date range yields `CacheMissError` and exact missing interval.
12. Legitimate empty complete interval returns empty DataFrame, not cache miss.
13. Fake source adapter fetches only missing intervals, stores normalized data, records coverage, returns reproducible read.
14. `cache="only"` never contacts adapter (verify spy call count zero).
15. `cache="refresh"` refuses combination with pinned snapshot.
16. SQL query uses parameters and rejects write statements, multiple statements, and arbitrary external file access.
17. Export/reimport Parquet and CSV preserves documented supported fields and ordering.
18. Bad schema, duplicated primary keys, and naive timestamps raise typed errors.
19. Interrupted write leaves previous snapshot visible; temp object cleanup possible.
20. Running quickstart examples works on a clean environment.

**Mandatory scenario phase tests**

1. Create study, scenario, clone with overrides; original remains unchanged.
2. Create run pins model/version/snapshots/assumptions and respects state machine.
3. Write multiple outputs; only completed run is marked complete after artifact commit.
4. Read output by run ID, compare scenario with baseline on declared keys.
5. Mismatched schemas/units or missing comparison keys cause explicit failure or surfaced row-status.
6. Partial/failed run does not return as completed and retains error diagnostics.

**Mandatory forecast phase tests**

1. Multiple forecast initializations share valid time without collision.
2. Select `initialized`, `valid_between`, ensemble member and lead correctly.
3. Historical as-of excludes later issued or revised forecast runs.
4. xarray dimension/coordinate conventions are preserved after Zarr roundtrip.

**Performance acceptance (targets, not guarantees):** For a realistic local fixture with several million tabular observations, filtered Parquet/DuckDB reads should complete in a few seconds on a developer laptop. Include a benchmark script recording warm/cold timings, selected rows, scanned bytes, and peak memory; do not make fragile timing assertions in ordinary unit tests.

## 14. Security and governance constraints

- Local mode trusts the executing OS user but must still prevent path traversal via dataset IDs, writes outside configured storage roots, or unsafe output path handling.
- Use parameterized SQL and an allowlisted query execution environment; no arbitrary Python execution through the API.
- Do not store provider API keys or secrets in dataset metadata; use env variables or a separate secrets provider.
- Store source provenance and licensing metadata; no assumption that purchased data can be redistributed or cached indefinitely.
- PostgreSQL/shared deployment must introduce tenant/user identity and namespace entitlements before external rollout.
- Always log dataset ID, query ID, elapsed time, snapshot, cache status, and errors without logging raw secrets.

## 15. Design decisions and tradeoffs

- **Polars is the default tabular interface.** `scan()` returns `pl.LazyFrame`; `read()` returns `pl.DataFrame`.
- **DuckDB does SQL and Parquet scans.** Do not build a custom query parser beyond validations and dataset view resolution.
- **Parquet is canonical v1 tabular storage.** SQLite stores metadata, not bulk time-series rows.
- **Immutable snapshots rather than mutable overwrites.** v1 uses manifest snapshots; Iceberg is a later replacement if concurrency and schema evolution justify it.
- **Source adapters are pluggable.** Ingestion infrastructure (retry, coverage, cache, atomic commit) is generic; source parsing is isolated.
- **No automatic implicit alignment.** Cross-market joins need explicit timezone/calendar/availability decisions.
- **Forecasts and scenario artifacts are different from market-data tables.** They share identity, provenance, persistence, and export concepts but need specialized APIs.
- **Avoid API overengineering.** Implement minimal, correct core first. The surface area below is the intended long-term target, not a mandate to ship every method in the first PR.

## 16. Phased implementation plan and definition of done

### Phase 1A — Local core (must ship first)

Implement `DataLake.open`, `from_config`, `close`, context manager, `register`, `search`, `list_datasets`, `describe`, `history`, `read` (cache only), `scan`, `write`, and `export` (CSV/Parquet). Implement public models, validation, errors, manifest snapshots, SQLite catalog, immutable Parquet objects, examples, tests. **Done when:** all tests for local writes/reads/snapshots/time conventions pass after restarting Python.

### Phase 1B — Query + cache-aware ingestion

Implement `query`, `coverage`, source adapter protocol, fake test adapter, `read(cache="fetch_missing")`, `read(cache="refresh")`, `refresh`, `get_refresh_job`, `list_refresh_jobs`, and robust point-in-time revision queries. Include source coverage assertions, gaps, statuses, and fake source refresh flow. **Done when:** historical as-of, missing-range retrieval, and no-network `only` tests pass.

### Phase 2A — Scenario persistence

Implement scenario study, scenario, run, output write/read/compare/export methods and lifecycle; run metadata in SQLite and outputs as immutable Parquet. **Done when:** a sample DCOPF *fixture* (not a real solver) can register a base case, save outage scenario outputs, compare by node/hour, and reopen all results after process restart.

### Phase 2B — Research utilities

Implement `align`, `materialize`, feature registration/compute, and optional minimal FastAPI endpoints. Add explicit lag/availability rules. **Done when:** a notebook can load, align, calculate, and persist a repeatable feature study without touching physical file paths.

### Phase 3 — Gridded forecasts and scalable jobs

Add `forecasts` API using xarray/Zarr, durable model job execution (allowlisted registry), and optional PostgreSQL/S3 adapters only after tests and workloads require them. **Done when:** an ECMWF-like synthetic ensemble fixture can be stored, retrieved by initialization/member/lead, and visualized by a separate map application with correct vintages.

### Deliverables Codex must produce

1. Installable `quantdata` package with `pyproject.toml`, type hints, and `py.typed`.
2. README with architecture, method catalog, and explicit temporal/caching guarantees.
3. Working quickstart that registers a dataset, writes data, queries snapshots, and exports data.
4. Automated pytest suite (offline) covering the acceptance checklist for the completed phase.
5. Brief `IMPLEMENTATION_STATUS.md` stating implemented/deferred methods and concrete known limitations.
6. No fabricated external market data or vendor integrations presented as complete.

## 17. First Codex task / recommended prompt

> Build **Phase 1A only** of `quantdata` per `quantdata_codex_spec.md`. First inspect the repository and state the proposed file layout. Implement working typed Python code with local SQLite catalog, immutable Parquet snapshot manifests, Polars read/scan, schema validation, CSV/Parquet export, and comprehensive offline tests. Do not start Phase 1B or scenario/forecast extensions. Run the test suite and type checker; fix failures. Document all unsupported methods in `IMPLEMENTATION_STATUS.md`. Preserve the interfaces and invariants in the spec, especially UTC time semantics, half-open intervals, snapshot isolation, and atomic metadata publication.
