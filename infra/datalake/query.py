"""Infer table fields and validate filters without requiring dataset declarations."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import date, datetime

import polars as pl

from .errors import InvalidQueryError, InvalidTimeError
from .time import utc
from .types import Filters, TimestampInput


def columns_exist(schema: pl.Schema, columns: Sequence[str] | None, label: str) -> None:
    if columns is None:
        return
    if isinstance(columns, str) or not columns or any(not isinstance(c, str) for c in columns):
        raise InvalidQueryError(f"{label} must be a nonempty list of column names")
    if len(set(columns)) != len(columns):
        raise InvalidQueryError(f"Duplicate {label}")
    if unknown := set(columns) - set(schema):
        raise InvalidQueryError(f"Unknown {label}: {sorted(unknown)}")


def time_column(schema: pl.Schema, explicit: str | None, *, required: bool) -> str | None:
    if explicit is not None:
        columns_exist(schema, [explicit], "time column")
        if schema[explicit] != pl.Date and not isinstance(schema[explicit], pl.Datetime):
            raise InvalidTimeError(
                f"{explicit!r} is not a date/datetime column; parse it before writing"
            )
        return explicit
    candidates = [
        name for name, dtype in schema.items() if isinstance(dtype, pl.Datetime) or dtype == pl.Date
    ]
    for name in ("timestamp", "time", "datetime", "date"):
        if name in candidates:
            return name
    if len(candidates) == 1:
        return candidates[0]
    if required:
        raise InvalidQueryError(f"Pass time_column=...; temporal columns found: {candidates}")
    return None


def query_bounds(
    start: TimestampInput | None, end: TimestampInput | None, zone: str | None
) -> tuple[datetime | None, datetime | None]:
    def parse(value: TimestampInput | None) -> datetime | None:
        if value is None:
            return None
        is_date = isinstance(value, date) and not isinstance(value, datetime)
        is_date = is_date or isinstance(value, str) and len(value) == 10
        effective_zone = (zone or "UTC") if is_date else zone
        return utc(value, effective_zone)

    left, right = parse(start), parse(end)
    if left is not None and right is not None and right <= left:
        raise InvalidTimeError("end must be after start; intervals are [start, end)")
    return left, right


def time_expression(schema: pl.Schema, name: str, zone: str | None) -> pl.Expr:
    kind = schema[name]
    expression = pl.col(name)
    if kind == pl.Date:
        expression = expression.cast(pl.Datetime("us"))
        return expression.dt.replace_time_zone(zone or "UTC").dt.convert_time_zone("UTC")
    if isinstance(kind, pl.Datetime) and kind.time_zone is None:
        if zone is None:
            raise InvalidTimeError(f"{name!r} has naive timestamps; pass timezone=... explicitly")
        return expression.dt.replace_time_zone(
            zone, ambiguous="raise", non_existent="raise"
        ).dt.convert_time_zone("UTC")
    return expression.dt.convert_time_zone("UTC")


def predicate(schema: pl.Schema, filters: Filters | None, zone: str | None) -> pl.Expr:
    if filters is not None and not isinstance(filters, Mapping):
        raise InvalidQueryError("filters must be a mapping of columns to values")
    expression = pl.lit(True)
    for name, value in (filters or {}).items():
        columns_exist(schema, [name], "filter columns")
        values = (
            list(value) if isinstance(value, Sequence) and not isinstance(value, str) else [value]
        )
        matches: list[pl.Expr] = []
        kind = schema[name]
        for item in values:
            column = pl.col(name)
            if item is None:
                matches.append(column.is_null())
                continue
            if isinstance(kind, pl.Datetime):
                if not isinstance(item, str):
                    raise InvalidQueryError(f"Use an ISO timestamp string for {name!r}")
                converted = query_bounds(item, None, zone)[0]
                matches.append(time_expression(schema, name, zone) == pl.lit(converted))
                continue
            if kind == pl.Date:
                if not isinstance(item, str) or len(item) != 10:
                    raise InvalidQueryError(f"Use YYYY-MM-DD for {name!r}")
                try:
                    matches.append(column == pl.lit(date.fromisoformat(item)))
                except ValueError as exc:
                    raise InvalidQueryError(f"Invalid date filter for {name!r}") from exc
                continue
            valid = False
            if kind == pl.String:
                valid = isinstance(item, str)
            elif kind == pl.Boolean:
                valid = isinstance(item, bool)
            elif kind.is_integer():
                valid = isinstance(item, int) and not isinstance(item, bool)
            elif kind.is_float():
                valid = (
                    isinstance(item, (float, int))
                    and not isinstance(item, bool)
                    and math.isfinite(item)
                )
            if not valid:
                raise InvalidQueryError(f"Invalid {kind} filter value for {name!r}: {item!r}")
            matches.append(column == pl.lit(item))
        expression &= pl.any_horizontal(matches) if matches else pl.lit(False)
    return expression


def positive_limit(value: int | None, name: str = "limit") -> None:
    if value is not None and (type(value) is not int or value <= 0):
        raise InvalidQueryError(f"{name} must be a positive integer")
