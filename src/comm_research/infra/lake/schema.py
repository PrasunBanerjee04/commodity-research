"""Shared UTC timestamp expressions for Polars lake tables."""

import polars as pl


def utc_expression(frame: pl.LazyFrame, column: str) -> pl.Expr:
    dtype = frame.collect_schema()[column]
    value = pl.col(column)
    if dtype == pl.Date:
        return value.cast(pl.Datetime("us")).dt.replace_time_zone("UTC")
    if dtype == pl.Null:
        return pl.lit(None, dtype=pl.Datetime("us", "UTC"))
    if isinstance(dtype, pl.Datetime):
        return (
            value.dt.convert_time_zone("UTC")
            if dtype.time_zone
            else value.dt.replace_time_zone("UTC")
        ).cast(pl.Datetime("us", "UTC"))
    if dtype == pl.String:
        # Explicit formats also handle columns containing only nulls/bad strings.
        return pl.coalesce(
            value.str.to_datetime(format="%+", time_zone="UTC", strict=False),
            value.str.to_datetime(
                format="%Y-%m-%d %H:%M:%S", time_zone="UTC", strict=False
            ),
            value.str.to_datetime(format="%Y-%m-%d", time_zone="UTC", strict=False),
        ).cast(pl.Datetime("us", "UTC"))
    raise ValueError(f"{column} is not a supported timestamp column")
