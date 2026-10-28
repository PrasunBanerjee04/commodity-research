"""Regional hourly ERA5 ingestion from the Copernicus Climate Data Store.

Each variable has its own UTC-day partition containing every requested region.
Re-fetching an observation updates its value without dropping other regions.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import zipfile
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from threading import Lock

import polars as pl

from comm_research.infra.lake.parquet import write_atomic
from comm_research.infra.lake.writer_lock import writer_lock
from comm_research.infra.paths import PROJECT_ROOT
from comm_research.markets.weather.models.grid import WeatherGrid

CDS_URL = "https://cds.climate.copernicus.eu/api"
DATASET = "reanalysis-era5-single-levels"
DEFAULT_LAKE_ROOT = PROJECT_ROOT / "data/lake/weather/era5"
DEFAULT_RAW_ROOT = PROJECT_ROOT / "data/raw/weather/era5"
ARCO_ZARR_STORE = "gs://gcp-public-data-arco-era5/ar/full_37-1h-0p25deg-chunk-1.zarr-v3"
VARIABLES = {
    "2m_temperature": ("t2m", "2t"),
    "2m_dewpoint_temperature": ("d2m", "2d"),
    "10m_u_component_of_wind": ("u10", "10u"),
    "10m_v_component_of_wind": ("v10", "10v"),
    "100m_u_component_of_wind": ("u100", "100u"),
    "100m_v_component_of_wind": ("v100", "100v"),
    "surface_pressure": ("sp",),
    "surface_solar_radiation_downwards": ("ssrd",),
    "total_cloud_cover": ("tcc",),
    "total_precipitation": ("tp",),
}
SCHEMA = {
    "timestamp": pl.Datetime("us", "UTC"),
    "latitude": pl.Float64,
    "longitude": pl.Float64,
    "variable": pl.String,
    "value": pl.Float64,
    "unit": pl.String,
}
KEYS = ["timestamp", "latitude", "longitude", "variable"]
logger = logging.getLogger(__name__)
_NETCDF_LOCK = Lock()  # netCDF4/HDF5 decoding is not thread-safe.
_ARCO_INDEX_LOCK = Lock()  # xarray/pandas label selection is unsafe on a shared dataset.


VARIABLE_DIRECTORIES = {
    "2m_temperature": "t_2m",
    "2m_dewpoint_temperature": "dewpoint_2m",
    "10m_u_component_of_wind": "u_10m",
    "10m_v_component_of_wind": "v_10m",
    "100m_u_component_of_wind": "u_100m",
    "100m_v_component_of_wind": "v_100m",
    "surface_pressure": "surface_pressure",
    "surface_solar_radiation_downwards": "solar_radiation",
    "total_cloud_cover": "cloud_cover",
    "total_precipitation": "precip",
}


def _partition(root: Path, day: date, variable: str = "2m_temperature") -> Path:
    return root / VARIABLE_DIRECTORIES[variable] / f"year={day:%Y}/month={day:%m}/day={day:%d}/data.parquet"


def _validate(frame: pl.DataFrame, day: date):
    if frame.schema != SCHEMA:
        raise ValueError(f"Unexpected ERA5 lake schema: {frame.schema}")
    if frame.is_empty() or any(frame.null_count().row(0)):
        raise ValueError("ERA5 observations cannot be empty or null")
    if frame.filter(
        (pl.col("timestamp").dt.date() != day)
        | (pl.col("timestamp").dt.minute() != 0)
        | (pl.col("timestamp").dt.second() != 0)
        | (pl.col("timestamp").dt.microsecond() != 0)
        | ~pl.col("latitude").is_between(-90, 90)
        | ~pl.col("latitude").is_finite() | ~pl.col("longitude").is_finite()
        | (pl.col("longitude") < -180) | (pl.col("longitude") >= 180)
        | ~pl.col("value").is_finite()
        | ((pl.col("latitude") * 4) != (pl.col("latitude") * 4).round())
        | ((pl.col("longitude") * 4) != (pl.col("longitude") * 4).round())
        | (pl.col("unit").str.len_chars() == 0)
    ).height:
        raise ValueError("Invalid hourly UTC ERA5 observations or coordinates")
    if frame.select(KEYS).is_duplicated().any():
        raise ValueError("Duplicate ERA5 observation keys")
    if frame.group_by("variable").agg(pl.col("unit").n_unique()).filter(pl.col("unit") != 1).height:
        raise ValueError("A variable cannot mix physical units")


def _selection(frame, grid, variables):
    return frame.filter(pl.col("latitude").is_in(grid.latitudes)
                        & pl.col("longitude").is_in(grid.longitudes)
                        & pl.col("variable").is_in(variables))


def _complete(frame, grid, variables):
    selected = _selection(frame, grid, variables)
    # _validate has checked unique keys and hourly timestamps within this day.
    return selected.height == grid.point_count * 24 * len(variables)


def _normalize(path: Path, variable: str, area, day: date) -> pl.DataFrame:
    with _NETCDF_LOCK:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as archive:
                entries = [entry for entry in archive.infolist()
                           if not entry.is_dir() and entry.filename.lower().endswith(".nc")]
                if len(entries) != 1:
                    raise ValueError("Expected one NetCDF field per CDS variable request")
                with tempfile.TemporaryDirectory(prefix="era5-netcdf-") as directory:
                    unpacked = Path(directory) / "field.nc"
                    unpacked.write_bytes(archive.read(entries[0]))
                    return _decode_netcdf(unpacked, variable, area, day)
        return _decode_netcdf(path, variable, area, day)


def _decode_netcdf(path: Path, variable: str, area, day: date) -> pl.DataFrame:
    import numpy as np
    import xarray as xr

    with xr.open_dataset(path, engine="netcdf4") as dataset:
        names = [name for name in (*VARIABLES[variable], variable) if name in dataset.data_vars]
        if len(names) != 1:
            raise ValueError(f"Expected one {variable} field in the CDS NetCDF response")
        values = dataset[names[0]]
        unit = values.attrs.get("units")
        if not isinstance(unit, str) or not unit.strip():
            raise ValueError("CDS response has no physical units")
        unit = unit.strip()
        if "expver" in values.dims:
            versions = list(values.expver.values)
            if not all(str(v) in ("1", "5") for v in versions):
                raise ValueError("Unexpected ERA5 experiment versions")
            ordered = sorted(versions, key=lambda v: str(v) != "1")
            combined = values.sel(expver=ordered[0], drop=True)
            for version in ordered[1:]:
                combined = combined.combine_first(values.sel(expver=version, drop=True))
            values = combined
        time_name = "valid_time" if "valid_time" in values.dims else "time"
        wanted = (time_name, "latitude", "longitude")
        for dimension in set(values.dims) - set(wanted):
            if values.sizes[dimension] != 1:
                raise ValueError(f"Unexpected CDS dimension: {dimension}")
            values = values.squeeze(dimension, drop=True)
        if set(values.dims) != set(wanted):
            raise ValueError("Missing CDS time/latitude/longitude dimensions")
        values = values.transpose(*wanted)
        times = np.asarray(values[time_name].values)
        if not np.issubdtype(times.dtype, np.datetime64):
            raise ValueError("Expected Gregorian UTC time coordinates")
        if np.isnat(times).any() or not np.array_equal(
            np.sort(times),
            np.datetime64(day, "us") + np.arange(24) * np.timedelta64(1, "h"),
        ):
            raise ValueError("CDS response does not contain exactly 24 UTC hours")
        latitudes = np.asarray(values.latitude.values, dtype="float64")
        longitudes = (np.asarray(values.longitude.values, dtype="float64") + 180) % 360 - 180
        if len(np.unique(latitudes)) != len(latitudes) or len(np.unique(longitudes)) != len(longitudes):
            raise ValueError("Duplicate CDS coordinate values")
        north, west, south, east = area
        expected_lat = np.arange(round(south * 4), round(north * 4) + 1) / 4
        expected_lon = np.unique((np.arange(round(west * 4), round(east * 4) + 1) / 4 + 180) % 360 - 180)
        if not np.array_equal(np.sort(latitudes), expected_lat) or not np.array_equal(np.sort(longitudes), expected_lon):
            raise ValueError("CDS response does not match the requested native grid")
        data = np.asarray(values.values, dtype="float64")
        cells = len(latitudes) * len(longitudes)
        frame = pl.DataFrame({
            "timestamp": np.repeat(times.astype("datetime64[us]"), cells),
            "latitude": np.tile(np.repeat(latitudes, len(longitudes)), 24),
            "longitude": np.tile(longitudes, 24 * len(latitudes)),
            "variable": [variable] * (24 * cells),
            "value": data.reshape(-1),
            "unit": [unit] * (24 * cells),
        }).with_columns(pl.col("timestamp").dt.replace_time_zone("UTC"))
    _validate(frame, day)
    return frame


def _normalize_arco(dataset, variable: str, grid: WeatherGrid, day: date) -> pl.DataFrame:
    import numpy as np

    names = [name for name in (*VARIABLES[variable], variable) if name in dataset.data_vars]
    if len(names) != 1:
        raise ValueError(f"Expected one {variable} field in the ARCO ERA5 store")
    values = dataset[names[0]]
    unit = values.attrs.get("units")
    if not isinstance(unit, str) or not unit.strip():
        raise ValueError("ARCO ERA5 field has no physical units")
    unit = unit.strip()
    if "expver" in values.dims:
        versions = list(values.expver.values)
        if not all(str(version) in ("1", "5") for version in versions):
            raise ValueError("Unexpected ERA5 experiment versions")
        ordered = sorted(versions, key=lambda version: str(version) != "1")
        combined = values.sel(expver=ordered[0], drop=True)
        for version in ordered[1:]:
            combined = combined.combine_first(values.sel(expver=version, drop=True))
        values = combined
    time_name = "valid_time" if "valid_time" in values.dims else "time"
    wanted = (time_name, "latitude", "longitude")
    for dimension in set(values.dims) - set(wanted):
        if values.sizes[dimension] != 1:
            raise ValueError(f"Unexpected ARCO ERA5 dimension: {dimension}")
        values = values.squeeze(dimension, drop=True)
    if set(values.dims) != set(wanted):
        raise ValueError("Missing ARCO ERA5 time/latitude/longitude dimensions")

    source_longitudes = [longitude % 360 for longitude in grid.longitudes]
    with _ARCO_INDEX_LOCK:
        values = values.sel(
            latitude=list(grid.latitudes),
            longitude=source_longitudes,
            **{
                time_name: slice(
                    np.datetime64(day, "us"),
                    np.datetime64(day, "us") + np.timedelta64(23, "h"),
                )
            },
        ).transpose(*wanted)
    times = np.asarray(values[time_name].values)
    expected_times = np.datetime64(day, "us") + np.arange(24) * np.timedelta64(1, "h")
    if (not np.issubdtype(times.dtype, np.datetime64) or np.isnat(times).any()
            or not np.array_equal(times.astype("datetime64[us]"), expected_times)):
        raise ValueError("ARCO ERA5 response does not contain exactly 24 UTC hours")

    latitudes = np.asarray(values.latitude.values, dtype="float64")
    longitudes = (np.asarray(values.longitude.values, dtype="float64") + 180) % 360 - 180
    if (not np.array_equal(latitudes, grid.latitudes)
            or not np.array_equal(longitudes, grid.longitudes)):
        raise ValueError("ARCO ERA5 response does not match the requested native grid")
    data = np.asarray(values.values, dtype="float64")
    cells = len(latitudes) * len(longitudes)
    frame = pl.DataFrame({
        "timestamp": np.repeat(times.astype("datetime64[us]"), cells),
        "latitude": np.tile(np.repeat(latitudes, len(longitudes)), 24),
        "longitude": np.tile(longitudes, 24 * len(latitudes)),
        "variable": [variable] * (24 * cells),
        "value": data.reshape(-1),
        "unit": [unit] * (24 * cells),
    }).with_columns(pl.col("timestamp").dt.replace_time_zone("UTC"))
    _validate(frame, day)
    return frame


@dataclass
class RunSummary:
    downloaded_requests: int = 0
    written_days: int = 0
    skipped_days: int = 0
    received_rows: int = 0


@dataclass
class _DayBatch:
    day: date
    existing: pl.DataFrame | None
    jobs: deque
    remaining: int
    results: list = field(default_factory=list)


class ERA5Scraper:
    """Pull a WeatherGrid from CDS or ARCO and upsert it into shared daily Parquet."""

    def __init__(
        self, grid: WeatherGrid, *, variables=("2m_temperature",),
        lake_root: Path | str = DEFAULT_LAKE_ROOT,
        raw_root: Path | str = DEFAULT_RAW_ROOT, workers: int = 2,
        keep_raw: bool = False, client_factory=None,
    ):
        if not isinstance(grid, WeatherGrid):
            raise TypeError("grid must be a WeatherGrid")
        if isinstance(variables, str):
            raise ValueError("variables must be a sequence of CDS variable names")
        variables = tuple(dict.fromkeys(variables))
        if not variables or any(v not in VARIABLES for v in variables):
            raise ValueError(f"Choose variables from {sorted(VARIABLES)}")
        if isinstance(workers, bool) or not isinstance(workers, int) or workers < 1:
            raise ValueError("workers must be a positive integer")
        self.grid, self.variables = grid, variables
        self.lake_root = Path(lake_root).expanduser().resolve()
        self.raw_root = Path(raw_root).expanduser().resolve()
        self.workers, self.keep_raw = workers, keep_raw
        self.client_factory = client_factory or self._client

    @staticmethod
    def _client():
        try:
            import cdsapi
            import requests
        except ImportError as error:
            raise RuntimeError("Install weather dependencies: python -m pip install -e '.[weather]'") from error

        session = requests.Session()
        try:
            return cdsapi.Client(url=CDS_URL, quiet=True, progress=False,
                                 timeout=120, retry_max=5, sleep_max=60, verify=True,
                                 session=session)
        except Exception as error:
            session.close()
            raise RuntimeError(
                "Configure CDSAPI_KEY securely in the environment or ~/.cdsapirc, "
                "and accept the ERA5 dataset terms in your CDS account."
            ) from error

    def _fetch(self, day, variable, area, refresh=False):
        request = {
            "product_type": ["reanalysis"], "variable": [variable],
            "year": [f"{day:%Y}"], "month": [f"{day:%m}"], "day": [f"{day:%d}"],
            "time": [f"{hour:02d}:00" for hour in range(24)],
            "area": list(area), "grid": [0.25, 0.25],
            "data_format": "netcdf", "download_format": "unarchived",
        }
        fingerprint = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
        target = self.raw_root / VARIABLE_DIRECTORIES[variable] / f"{day:%Y/%m/%d}" / f"{fingerprint}.nc"
        target.parent.mkdir(parents=True, exist_ok=True)
        downloaded = 0
        if refresh or not target.exists():
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(dir=target.parent, suffix=".partial", delete=False) as stream:
                    temporary = Path(stream.name)
                client = self.client_factory()
                try:
                    client.retrieve(DATASET, request, str(temporary))
                finally:
                    session = getattr(client, "session", None)
                    if session is not None:
                        session.close()
                # Validate before allowing a cached response to be reused.
                frame = _normalize(temporary, variable, area, day)
                os.replace(temporary, target)
                downloaded = 1
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        else:
            frame = _normalize(target, variable, area, day)
        return frame, target, downloaded

    def run(self, start: date, end: date, *, refresh=False) -> RunSummary:
        """Pull complete UTC days in [start, end), preserving other regions.

        Refresh bypasses the lake completeness check and cached CDS responses.
        No request is silently clamped to provider availability.
        ``workers`` bounds both in-flight requests and active daily batches.
        Only the calling thread writes Parquet; dates may finish out of order.
        """
        if (not isinstance(start, date) or isinstance(start, datetime)
                or not isinstance(end, date) or isinstance(end, datetime)
                or start < date(1940, 1, 1) or start >= end
                or end > datetime.now(timezone.utc).date()):
            raise ValueError("Use date objects with 1940-01-01 <= start < end <= today (UTC); end is exclusive")
        summary = RunSummary()
        if self.raw_root == self.lake_root:
            raise ValueError("raw_root and lake_root must be different directories")
        with writer_lock(self.raw_root), writer_lock(self.lake_root), ThreadPoolExecutor(max_workers=self.workers) as executor:
            plans = self._plan_days(start, end, refresh, summary)
            batches = {}
            ready = deque()
            pending = {}
            exhausted = False

            def fill_window():
                nonlocal exhausted
                # Do not enqueue the entire history or retain unbounded frames.
                while not exhausted and len(batches) < self.workers:
                    batch = next(plans, None)
                    if batch is None:
                        exhausted = True
                        break
                    batches[batch.day] = batch
                    ready.append(batch.day)
                # Round-robin requests across dates, including when each day
                # contains multiple variables or antimeridian areas.
                while ready and len(pending) < self.workers:
                    day = ready.popleft()
                    batch = batches[day]
                    variable, area = batch.jobs.popleft()
                    pending[executor.submit(self._fetch, day, variable, area, refresh)] = day
                    if batch.jobs:
                        ready.append(day)

            try:
                fill_window()
                while pending:
                    done, _ = wait(pending, return_when=FIRST_COMPLETED)
                    for future in sorted(done, key=pending.__getitem__):
                        day = pending.pop(future)
                        batch = batches[day]
                        batch.results.append(future.result())
                        batch.remaining -= 1
                        if batch.remaining == 0:
                            self._merge_day(batch, summary)
                            del batches[day]
                    fill_window()
            finally:
                # Queued jobs are cancelled on failure/interruption. Running
                # requests finish/cache before the executor exits and the file
                # locks are released; workers never publish Parquet files.
                for future in pending:
                    future.cancel()
        return summary

    def run_bulk(
        self,
        start: date,
        end: date,
        *,
        store_url: str = ARCO_ZARR_STORE,
        storage_options: dict | None = None,
        refresh: bool = False,
    ) -> RunSummary:
        """Pull an interval from the public ARCO ERA5 Zarr store into daily lake files.

        The date range is [start, end). Requested days are fetched concurrently
        up to ``workers``; only the calling thread merges and atomically writes
        completed partitions. Existing complete partitions are skipped unless
        refreshing.
        """
        if (not isinstance(start, date) or isinstance(start, datetime)
                or not isinstance(end, date) or isinstance(end, datetime)
                or start < date(1940, 1, 1) or start >= end
                or end > datetime.now(timezone.utc).date()):
            raise ValueError("Use date objects with 1940-01-01 <= start < end <= today (UTC); end is exclusive")
        if not isinstance(store_url, str) or not store_url.strip():
            raise ValueError("store_url must be a nonempty string")

        summary = RunSummary()
        with writer_lock(self.lake_root):
            pending_days = []
            day = start
            while day < end:
                existing = self._read_day(day)
                if existing is not None:
                    _validate(existing, day)
                    if not refresh and _complete(existing, self.grid, self.variables):
                        summary.skipped_days += 1
                        day += timedelta(days=1)
                        continue
                pending_days.append((day, existing))
                day += timedelta(days=1)
            if not pending_days:
                return summary

            import xarray as xr

            options = {"token": "anon"} if storage_options is None else storage_options
            with xr.open_zarr(store_url, chunks=None, storage_options=options) as dataset:
                def fetch_day(item):
                    requested_day, existing = item
                    incoming = pl.concat(
                        [
                            _normalize_arco(dataset, variable, self.grid, requested_day)
                            for variable in self.variables
                        ]
                    )
                    _validate(incoming, requested_day)
                    if not _complete(incoming, self.grid, self.variables):
                        raise ValueError(
                            "Incomplete ARCO ERA5 grid coverage; daily partition was not changed"
                        )
                    return requested_day, existing, incoming

                pending = iter(pending_days)
                with ThreadPoolExecutor(max_workers=self.workers) as executor:
                    futures = {}
                    for _ in range(min(self.workers, len(pending_days))):
                        item = next(pending)
                        futures[executor.submit(fetch_day, item)] = item

                    while futures:
                        completed, _ = wait(futures, return_when=FIRST_COMPLETED)
                        for future in completed:
                            futures.pop(future)
                            day, existing, incoming = future.result()
                            merged = pl.concat([existing, incoming]) if existing is not None else incoming
                            merged = merged.unique(subset=KEYS, keep="last").sort(KEYS)
                            _validate(merged, day)
                            self._write_day(merged, day)
                            summary.written_days += 1
                            summary.received_rows += incoming.height
                            logger.info(
                                "SAVED %s from ARCO Zarr: %d incoming rows; %d total rows",
                                day, incoming.height, merged.height,
                            )

                            try:
                                item = next(pending)
                            except StopIteration:
                                continue
                            futures[executor.submit(fetch_day, item)] = item

            summary.downloaded_requests = 1
        return summary

    def _read_day(self, day):
        frames = []
        for variable in self.variables:
            target = _partition(self.lake_root, day, variable)
            if target.exists():
                frame = pl.read_parquet(target, hive_partitioning=False)
                _validate(frame, day)
                if frame.filter(pl.col("variable") != variable).height:
                    raise ValueError(f"Unexpected variable in {target}")
                frames.append(frame)
        return pl.concat(frames) if frames else None

    def _write_day(self, frame, day):
        for variable in self.variables:
            write_atomic(
                frame.filter(pl.col("variable") == variable),
                _partition(self.lake_root, day, variable),
            )

    def _plan_days(self, start, end, refresh, summary):
        day = start
        while day < end:
            existing = self._read_day(day)
            if existing is not None:
                _validate(existing, day)
                if not refresh and _complete(existing, self.grid, self.variables):
                    summary.skipped_days += 1
                    logger.info("SKIP %s: all requested grid observations already present", day)
                    day += timedelta(days=1)
                    continue
            jobs = deque((variable, area) for variable in self.variables for area in self.grid.cds_areas)
            logger.info("FETCH %s: %s, %d CDS requests", day, self.grid.name, len(jobs))
            yield _DayBatch(day, existing, jobs, len(jobs))
            day += timedelta(days=1)

    def _merge_day(self, batch, summary):
        # Called exclusively by the coordinator under the lake writer lock.
        day, existing, results = batch.day, batch.existing, batch.results
        incoming = pl.concat([result[0] for result in results])
        # Antimeridian requests can both return the +/-180 boundary.
        conflicts = incoming.group_by(KEYS).agg(
            pl.col("value").n_unique().alias("values"),
            pl.col("unit").n_unique().alias("units"),
        ).filter((pl.col("values") != 1) | (pl.col("units") != 1))
        if conflicts.height:
            raise ValueError("Conflicting observations in the same CDS response batch")
        incoming = incoming.unique(subset=KEYS, keep="last")
        _validate(incoming, day)
        if not _complete(incoming, self.grid, self.variables):
            raise ValueError("Incomplete ERA5 grid coverage; daily partition was not changed")
        merged = pl.concat([existing, incoming]) if existing is not None else incoming
        merged = merged.unique(subset=KEYS, keep="last").sort(KEYS)
        _validate(merged, day)
        self._write_day(merged, day)
        if not self.keep_raw:
            for _, raw, _ in results:
                raw.unlink(missing_ok=True)
        summary.downloaded_requests += sum(result[2] for result in results)
        summary.written_days += 1
        summary.received_rows += incoming.height
        logger.info("SAVED %s: %d incoming rows; %d total rows", day, incoming.height, merged.height)
