"""Framework-independent query validation and responses for generic lake feeds."""

from __future__ import annotations

import hashlib
import math
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from comm_research.dashboard.data.loader import (
    Dataset,
    LakeError,
    dimension_options,
    discover_lake,
    inspect_dataset,
    load_raw_page,
    load_series,
    raw_row_count,
)

TIMEZONES = {"UTC", "America/Los_Angeles", "America/New_York"}
FREQUENCIES = {"native", "5m", "1h", "1d"}
PAGE_SIZES = {100, 250, 500}


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _dataset_json(dataset: Dataset) -> dict[str, Any]:
    return {
        "key": dataset.key,
        "title": dataset.title,
        "name": dataset.name,
        "venue": dataset.venue,
        "tokens": list(dataset.tokens),
        "files": len(dataset.files),
        "revision": hashlib.sha256(repr(dataset.files).encode()).hexdigest()[:16],
    }


class LakeAPI:
    """One response builder per request; source snapshots live in the loader."""

    def __init__(self, lake_root: str) -> None:
        self.lake_root = lake_root
        self.result: Any = None

    def _json(self, value: Any) -> None:
        self.result = value

    def _metadata(self, key: str) -> None:
        dataset = self._dataset(key)
        metadata = inspect_dataset(dataset)
        earliest = (
            metadata.earliest.astimezone(ZoneInfo("UTC")).date()
            if metadata.earliest
            else None
        )
        latest = (
            metadata.latest.astimezone(ZoneInfo("UTC")).date()
            if metadata.latest
            else None
        )
        self._json(
            {
                "dataset": _dataset_json(dataset),
                "schema": [
                    {"field": field, "type": dtype} for field, dtype in metadata.schema
                ],
                "timeColumn": metadata.time_column,
                "earliest": earliest.isoformat() if earliest else None,
                "latest": latest.isoformat() if latest else None,
                "signals": [
                    {
                        "key": signal.key,
                        "label": signal.label,
                        "unit": signal.unit,
                        "component": signal.component,
                    }
                    for signal in metadata.signals
                ],
                "dimensions": list(metadata.dimensions),
            }
        )

    def _options(self, key: str, dimension: str) -> None:
        dataset = self._dataset(key)
        if dimension not in inspect_dataset(dataset).dimensions:
            raise LakeError("Unsupported dimension.")
        self._json({"values": dimension_options(dataset, dimension)})

    def _series(
        self,
        payload: dict[str, Any],
        dataset: Dataset,
        start: date | None,
        end: date | None,
        zone: str,
        filters: tuple[tuple[str, tuple[str, ...]], ...],
    ) -> None:
        if start is None or end is None:
            raise LakeError("This dataset has no recognized time field.")
        signals = payload.get("signals", [])
        if (
            not isinstance(signals, list)
            or len(signals) > 8
            or any(not isinstance(signal, str) for signal in signals)
        ):
            raise ValueError("Choose up to eight numeric signals.")
        frequency = payload.get("frequency", "native")
        aggregation = payload.get("aggregation", "Mean")
        if (
            not isinstance(frequency, str)
            or frequency not in FREQUENCIES
            or not isinstance(aggregation, str)
            or aggregation not in {"Mean", "Last"}
        ):
            raise ValueError("Invalid frequency or aggregation.")
        bundle = load_series(
            dataset,
            start,
            end,
            zone,
            tuple(signals),
            filters,
            frequency,
            aggregation,
            payload.get("fallbackToLatest") is True,
        )
        dimensions = inspect_dataset(dataset).dimensions
        self._json(
            {
                "observations": bundle.observations,
                "points": bundle.points,
                "downsampled": bundle.downsampled,
                "resolution": bundle.resolution,
                "fallbackHorizon": [day.isoformat() for day in bundle.fallback_horizon]
                if bundle.fallback_horizon
                else None,
                "plot": [
                    {
                        "timestamp": row["timestamp"]
                        .isoformat()
                        .replace("+00:00", "Z"),
                        "series": row["series"],
                        "value": _json_value(row["value"]),
                        "unit": row["unit"],
                        "component": row["component"],
                        "node": row["node"],
                        "signal": row["signal"],
                        "dimensions": {column: row[column] for column in dimensions},
                    }
                    for row in bundle.plot.iter_rows(named=True)
                ],
                "statistics": [
                    {key: _json_value(value) for key, value in row.items()}
                    for row in bundle.statistics.iter_rows(named=True)
                ],
            }
        )

    def _rows(
        self,
        payload: dict[str, Any],
        dataset: Dataset,
        start: date | None,
        end: date | None,
        zone: str,
        filters: tuple[tuple[str, tuple[str, ...]], ...],
        metadata: Any,
    ) -> None:
        page = payload.get("page", 0)
        page_size = payload.get("pageSize", 250)
        if not isinstance(page, int) or page < 0:
            raise ValueError("Page must be a non-negative integer.")
        if not isinstance(page_size, int) or page_size not in PAGE_SIZES:
            raise ValueError("Page size must be 100, 250 or 500.")
        if metadata.time_column is None:
            start = end = None
        count = raw_row_count(dataset, start, end, zone, filters)
        frame = load_raw_page(dataset, start, end, zone, filters, page, page_size)
        self._json(
            {
                "count": count,
                "page": page,
                "pageSize": page_size,
                "rows": [
                    {key: _json_value(value) for key, value in row.items()}
                    for row in frame.iter_rows(named=True)
                ],
            }
        )

    def _query_controls(
        self, payload: dict[str, Any], dataset: Dataset, metadata: Any
    ) -> tuple[
        date | None,
        date | None,
        str,
        tuple[tuple[str, tuple[str, ...]], ...],
    ]:
        zone = payload.get("zone", "UTC")
        if not isinstance(zone, str) or zone not in TIMEZONES:
            raise ValueError("Choose a supported date zone.")
        start, end = (
            self._parse_date(payload.get("start")),
            self._parse_date(payload.get("end")),
        )
        if (start is None) != (end is None):
            raise ValueError("Start and end dates must be provided together.")
        if start and end and start > end:
            raise ValueError("Start date must be on or before end date.")
        if (start or end) and metadata.time_column is None:
            raise LakeError("This dataset has no recognized time field.")
        raw_filters = payload.get("filters", {})
        if not isinstance(raw_filters, dict):
            raise TypeError("Filters must be an object.")
        normalized = []
        for column, values in raw_filters.items():
            if column not in metadata.dimensions:
                raise LakeError(f"Unsupported dimension: {column}")
            if (
                not isinstance(values, list)
                or len(values) > 8
                or any(
                    not isinstance(value, str) or len(value) > 256 for value in values
                )
            ):
                raise ValueError("Choose up to eight valid values for each filter.")
            normalized.append((column, tuple(values)))
        return start, end, zone, tuple(normalized)

    def _dataset(self, key: Any) -> Dataset:
        if not isinstance(key, str) or not key:
            raise ValueError("A dataset key is required.")
        dataset = next(
            (item for item in discover_lake(self.lake_root) if item.key == key), None
        )
        if dataset is None:
            raise KeyError("Dataset is no longer available. Rescan the lake.")
        return dataset

    @staticmethod
    def _parse_date(value: Any) -> date | None:
        if value in (None, ""):
            return None
        if not isinstance(value, str):
            raise TypeError("Dates must use YYYY-MM-DD format.")
        return date.fromisoformat(value)
