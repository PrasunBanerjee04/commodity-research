"""Fingerprint-cached, lazy Parquet/Arrow discovery and desk analytics."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import polars as pl
import streamlit as st

from comm_research.dashboard.config.taxonomy import (
    COMPONENT_NAMES,
    COMPONENT_ORDER,
    DIMENSION_PRIORITY,
    NON_MEASUREMENTS,
    TIME_COLUMNS,
    display_name,
    metric_unit,
)

FORMATS = {".parquet", ".arrow", ".ipc", ".feather"}
MAX_POINTS_PER_TRACE = 4_000
MAX_ANALYTIC_ROWS = 2_000_000
MAX_TRACES = 64
FilterSet = tuple[tuple[str, tuple[str, ...]], ...]


class LakeError(ValueError):
    """A lake query needs a different source, date horizon or filter selection."""


@dataclass(frozen=True)
class FileStamp:
    path: str
    size: int
    mtime_ns: int


@dataclass(frozen=True)
class Dataset:
    root: str
    key: str
    files: tuple[FileStamp, ...]

    @property
    def tokens(self) -> tuple[str, ...]:
        return tuple(self.key.split("/"))

    @property
    def name(self) -> str:
        return display_name(self.tokens[-1])

    @property
    def venue(self) -> str:
        return display_name(self.tokens[-2]) if len(self.tokens) > 1 else "LOCAL"

    @property
    def title(self) -> str:
        return f"{self.venue} / {self.name}"


@dataclass(frozen=True)
class Signal:
    key: str
    label: str
    column: str
    unit: str
    selector_column: str | None = None
    selector_value: str | None = None


@dataclass(frozen=True)
class Metadata:
    schema: tuple[tuple[str, str], ...]
    time_column: str | None
    earliest: datetime | None
    latest: datetime | None
    signals: tuple[Signal, ...]
    dimensions: tuple[str, ...]


@dataclass
class SeriesBundle:
    plot: pl.DataFrame
    statistics: pl.DataFrame
    observations: int
    points: int
    downsampled: bool


@st.cache_data(ttl=10, max_entries=8, show_spinner=False)
def discover_lake(root: str) -> tuple[Dataset, ...]:
    """Group Hive partitions into datasets; exclude hidden paths and symlinks."""
    lake = Path(root).expanduser().resolve()
    if not lake.is_dir():
        return ()
    grouped: dict[str, list[FileStamp]] = {}
    for folder, directories, filenames in os.walk(lake, followlinks=False):
        directory = Path(folder)
        directories[:] = sorted(
            name
            for name in directories
            if not name.startswith(".") and not (directory / name).is_symlink()
        )
        for name in sorted(filenames):
            path = directory / name
            if (
                name.startswith(".")
                or path.suffix.lower() not in FORMATS
                or path.is_symlink()
            ):
                continue
            owner = directory
            while owner != lake and "=" in owner.name:
                owner = owner.parent
            key = owner.relative_to(lake).as_posix()
            if key == ".":
                key = "unpartitioned"
            try:
                info = path.stat()
            except FileNotFoundError:
                continue  # A refresh replaced a partition during discovery.
            grouped.setdefault(key, []).append(
                FileStamp(str(path), info.st_size, info.st_mtime_ns)
            )
    return tuple(
        Dataset(str(lake), key, tuple(files)) for key, files in sorted(grouped.items())
    )


def _scan(dataset: Dataset) -> pl.LazyFrame:
    lake = Path(dataset.root).resolve()
    frames: list[pl.LazyFrame] = []
    for file in dataset.files:
        path = Path(file.path)
        if path.is_symlink() or not path.resolve().is_relative_to(lake):
            raise LakeError("The dataset contains a file outside the selected lake.")
        if path.suffix.lower() == ".parquet":
            frames.append(pl.scan_parquet(path, hive_partitioning=False))
        else:
            frames.append(pl.scan_ipc(path, memory_map=True))
    if not frames:
        raise LakeError("No supported data files are available.")
    # Union schema evolution without narrowing numeric types or adding Hive columns.
    return pl.concat(frames, how="diagonal_relaxed")


def _timestamp(frame: pl.LazyFrame, column: str) -> pl.Expr:
    dtype = frame.collect_schema()[column]
    value = pl.col(column)
    if dtype == pl.Date:
        return value.cast(pl.Datetime("us")).dt.replace_time_zone("UTC")
    if isinstance(dtype, pl.Datetime):
        if dtype.time_zone:
            return value.dt.convert_time_zone("UTC").cast(pl.Datetime("us", "UTC"))
        return value.dt.replace_time_zone("UTC").cast(pl.Datetime("us", "UTC"))
    if dtype == pl.String:
        return value.str.to_datetime(time_zone="UTC", strict=False).cast(
            pl.Datetime("us", "UTC")
        )
    raise LakeError(f"{column} is not a supported time column.")


def _utc_bounds(start: date, end: date, zone: str) -> tuple[datetime, datetime]:
    if start > end:
        raise LakeError("Start date must be on or before end date.")
    tz = ZoneInfo(zone)
    return (
        datetime.combine(start, time(), tz).astimezone(timezone.utc),
        datetime.combine(end + timedelta(days=1), time(), tz).astimezone(timezone.utc),
    )


def _filtered(
    dataset: Dataset,
    metadata: Metadata,
    start: date | None,
    end: date | None,
    zone: str,
    filters: FilterSet,
) -> pl.LazyFrame:
    frame = _scan(dataset)
    schema = frame.collect_schema()
    for column, values in filters:
        if column not in metadata.dimensions:
            raise LakeError(f"Unsupported dimension: {column}")
        frame = frame.filter(pl.col(column).cast(pl.String).is_in(values))
    if metadata.time_column:
        frame = frame.with_columns(
            _timestamp(frame, metadata.time_column).alias("__desk_time")
        )
        if start is not None and end is not None:
            lower, upper = _utc_bounds(start, end, zone)
            frame = frame.filter(
                (pl.col("__desk_time") >= lower) & (pl.col("__desk_time") < upper)
            )
    elif start is not None or end is not None:
        raise LakeError(
            "This table has no recognized time field; inspect its raw rows."
        )
    if any(column.startswith("__desk_") for column in schema):
        raise LakeError("Source columns use reserved dashboard field names.")
    return frame


@st.cache_data(ttl=300, max_entries=64, show_spinner=False)
def inspect_dataset(dataset: Dataset) -> Metadata:
    frame = _scan(dataset)
    schema = frame.collect_schema()
    time_column = next((column for column in TIME_COLUMNS if column in schema), None)
    if time_column is None:
        time_column = next(
            (
                column
                for column, dtype in schema.items()
                if isinstance(dtype, pl.Datetime) or dtype == pl.Date
            ),
            None,
        )
    earliest = latest = None
    if time_column:
        bounds = frame.select(
            _timestamp(frame, time_column).min().alias("min"),
            _timestamp(frame, time_column).max().alias("max"),
        ).collect(engine="streaming")
        earliest, latest = bounds.row(0)
    numeric = [
        column
        for column, dtype in schema.items()
        if dtype.is_numeric() and column not in NON_MEASUREMENTS
    ]
    selector = (
        "lmp_type"
        if "lmp_type" in schema
        else "xml_data_item"
        if "xml_data_item" in schema and any(c in numeric for c in ("value", "mw"))
        else None
    )
    signals: list[Signal] = []
    for column in numeric:
        if selector and column in ("value", "mw", "lmp_prc"):
            options = (
                frame.select(
                    pl.col(selector).cast(pl.String).drop_nulls().unique().sort()
                )
                .limit(129)
                .collect(engine="streaming")[selector]
                .to_list()
            )
            if len(options) > 128:
                raise LakeError(
                    "More than 128 signal types; split this feed into narrower datasets."
                )
            for value in sorted(
                options, key=lambda item: (COMPONENT_ORDER.get(item, 99), item)
            ):
                label = COMPONENT_NAMES.get(value, display_name(value))
                if sum(name in numeric for name in ("value", "mw", "lmp_prc")) > 1:
                    label += f" ({column})"
                signals.append(
                    Signal(
                        f"{column}:{value}",
                        label,
                        column,
                        metric_unit(dataset.key, column),
                        selector,
                        value,
                    )
                )
        else:
            signals.append(
                Signal(
                    column,
                    display_name(column),
                    column,
                    metric_unit(dataset.key, column),
                )
            )
    dimensions = tuple(
        column
        for column in DIMENSION_PRIORITY
        if column in schema and schema[column] == pl.String
    )
    if not dimensions:
        excluded = {
            selector,
            "xml_data_item",
            "label",
            "units",
            "unit",
            "description",
            "reason",
        }
        dimensions = tuple(
            column
            for column, dtype in schema.items()
            if dtype == pl.String and column not in excluded and column != time_column
        )[:4]
    return Metadata(
        tuple((column, str(dtype)) for column, dtype in schema.items()),
        time_column,
        earliest,
        latest,
        tuple(signals),
        dimensions,
    )


@st.cache_data(ttl=300, max_entries=128, show_spinner=False)
def dimension_options(dataset: Dataset, column: str) -> tuple[str, ...]:
    if column not in inspect_dataset(dataset).dimensions:
        raise LakeError("Unsupported dimension.")
    values = (
        _scan(dataset)
        .select(pl.col(column).cast(pl.String).drop_nulls().unique().sort())
        .limit(5_001)
        .collect(engine="streaming")[column]
        .to_list()
    )
    return tuple(values[:5_000])


def _signal_rows(
    frame: pl.LazyFrame, metadata: Metadata, signals: tuple[Signal, ...]
) -> pl.LazyFrame:
    columns = list(dict.fromkeys(signal.column for signal in signals))
    selectors = list(
        dict.fromkeys(
            signal.selector_column for signal in signals if signal.selector_column
        )
    )
    index = ["__desk_time", *metadata.dimensions, *selectors]
    frame = frame.select(list(dict.fromkeys([*index, *columns]))).unpivot(
        on=columns,
        index=list(dict.fromkeys(index)),
        variable_name="__desk_metric",
        value_name="__desk_value",
    )
    expression = pl.lit(None, dtype=pl.String)
    unit = pl.lit(None, dtype=pl.String)
    for signal in signals:
        condition = pl.col("__desk_metric") == signal.column
        if signal.selector_column:
            condition &= pl.col(signal.selector_column) == signal.selector_value
        expression = pl.when(condition).then(pl.lit(signal.label)).otherwise(expression)
        unit = pl.when(condition).then(pl.lit(signal.unit)).otherwise(unit)
    labels: list[pl.Expr] = [expression]
    labels.extend(pl.col(column).fill_null("∅") for column in metadata.dimensions)
    return (
        frame.with_columns(
            pl.concat_str(labels, separator=" · ").alias("series"),
            unit.alias("unit"),
            pl.col("__desk_value").cast(pl.Float64).alias("value"),
        )
        .filter(expression.is_not_null() & pl.col("value").is_finite())
        .select(pl.col("__desk_time").alias("timestamp"), "series", "value", "unit")
        .unique(subset=["timestamp", "series", "value", "unit"], maintain_order=True)
    )


def summarize(frame: pl.DataFrame) -> pl.DataFrame:
    """Statistics over all plotted observations before visual point reduction.

    The 24h reference must exist exactly; missing/zero references yield null.
    An absolute reference denominator keeps negative electricity prices meaningful.
    """
    if frame.is_empty():
        return pl.DataFrame()
    ordered = frame.sort("timestamp")
    stats = ordered.group_by("series", maintain_order=True).agg(
        pl.col("timestamp").last().alias("as_of"),
        pl.col("value").last().alias("last"),
        pl.col("value").min().alias("min"),
        pl.col("value").max().alias("max"),
        pl.col("value").mean().alias("mean"),
        pl.col("value").std(ddof=1).alias("std"),
        pl.len().alias("count"),
        pl.col("unit").first(),
    )
    reference = ordered.select(
        "series",
        (pl.col("timestamp") + timedelta(hours=24)).alias("as_of"),
        pl.col("value").alias("reference_24h"),
    )
    return stats.join(reference, on=["series", "as_of"], how="left").with_columns(
        pl.when(pl.col("reference_24h").is_not_null() & (pl.col("reference_24h") != 0))
        .then(
            (pl.col("last") - pl.col("reference_24h"))
            / pl.col("reference_24h").abs()
            * 100
        )
        .otherwise(None)
        .alias("change_24h")
    )


def reduce_plot_points(
    frame: pl.DataFrame, max_points: int = MAX_POINTS_PER_TRACE
) -> pl.DataFrame:
    """Keep first/last and both extrema in each time-ordered bin, per trace."""
    if max_points < 4:
        raise ValueError("Require at least four points per trace")
    traces: list[pl.DataFrame] = []
    for trace in frame.partition_by("series", maintain_order=True):
        trace = trace.sort("timestamp")
        if trace.height <= max_points:
            traces.append(trace)
            continue
        buckets = max_points // 4
        ranked = trace.with_row_index("__index").with_columns(
            (pl.col("__index") * buckets // trace.height).alias("__bucket")
        )
        indices = (
            ranked.group_by("__bucket")
            .agg(
                pl.col("__index").first().alias("first"),
                pl.col("__index").sort_by("value").first().alias("min"),
                pl.col("__index").sort_by("value").last().alias("max"),
                pl.col("__index").last().alias("last"),
            )
            .unpivot(index="__bucket", on=["first", "min", "max", "last"])["value"]
            .unique()
        )
        traces.append(
            ranked.filter(pl.col("__index").is_in(indices.implode()))
            .drop("__index", "__bucket")
            .sort("timestamp")
        )
    return pl.concat(traces) if traces else frame


@st.cache_data(ttl=120, max_entries=8, show_spinner=False)
def load_series(
    dataset: Dataset,
    start: date,
    end: date,
    zone: str,
    signal_keys: tuple[str, ...],
    filters: FilterSet = (),
    frequency: str = "native",
    aggregation: str = "Mean",
) -> SeriesBundle:
    metadata = inspect_dataset(dataset)
    if not metadata.time_column:
        raise LakeError("No recognized time field; switch to raw data.")
    selected = tuple(signal for signal in metadata.signals if signal.key in signal_keys)
    if set(signal_keys) - {signal.key for signal in selected}:
        raise LakeError(
            "A selected signal is no longer available; choose another signal."
        )
    if not selected:
        return SeriesBundle(pl.DataFrame(), pl.DataFrame(), 0, 0, False)
    if frequency not in ("native", "5m", "1h", "1d") or aggregation not in (
        "Mean",
        "Last",
    ):
        raise LakeError("Invalid frequency or aggregation.")
    rows = _signal_rows(
        _filtered(dataset, metadata, start, end, zone, filters), metadata, selected
    )
    rows = rows.sort("timestamp", maintain_order=True)
    if frequency != "native":
        rows = rows.with_columns(pl.col("timestamp").dt.truncate(frequency))
    value = pl.col("value").mean() if aggregation == "Mean" else pl.col("value").last()
    series = (
        rows.group_by("timestamp", "series", "unit", maintain_order=True)
        .agg(value.alias("value"), pl.len().alias("observations"))
        .limit(MAX_ANALYTIC_ROWS + 1)
        .collect(engine="streaming")
        .sort("timestamp")
    )
    if series.height > MAX_ANALYTIC_ROWS:
        raise LakeError(
            "More than two million intervals selected. Narrow dates/series or choose hourly/daily frequency."
        )
    if series.is_empty():
        return SeriesBundle(series, pl.DataFrame(), 0, 0, False)
    if series["series"].n_unique() > MAX_TRACES:
        raise LakeError("More than 64 traces selected. Narrow the series filters.")
    stats = summarize(series)
    plot = reduce_plot_points(series)
    return SeriesBundle(
        plot,
        stats,
        series["observations"].sum(),
        series.height,
        plot.height < series.height,
    )


@st.cache_data(ttl=120, max_entries=64, show_spinner=False)
def raw_row_count(
    dataset: Dataset,
    start: date | None,
    end: date | None,
    zone: str,
    filters: FilterSet = (),
) -> int:
    return (
        _filtered(dataset, inspect_dataset(dataset), start, end, zone, filters)
        .select(pl.len())
        .collect(engine="streaming")
        .item()
    )


@st.cache_data(ttl=120, max_entries=32, show_spinner=False)
def load_raw_page(
    dataset: Dataset,
    start: date | None,
    end: date | None,
    zone: str,
    filters: FilterSet = (),
    page: int = 0,
    page_size: int = 250,
) -> pl.DataFrame:
    if page < 0 or page_size not in (100, 250, 500):
        raise LakeError("Invalid raw-data page settings.")
    frame = _filtered(dataset, inspect_dataset(dataset), start, end, zone, filters)
    if "__desk_time" in frame.collect_schema():
        frame = frame.sort("__desk_time").drop("__desk_time")
    return frame.slice(page * page_size, page_size).collect(engine="streaming")


def clear_caches() -> None:
    for function in (
        discover_lake,
        inspect_dataset,
        dimension_options,
        load_series,
        raw_row_count,
        load_raw_page,
    ):
        function.clear()
