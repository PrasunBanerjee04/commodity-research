"""Non-destructive CAISO schema adapters shared by lake readers and ingestion."""

from __future__ import annotations

import re

import polars as pl

COMPONENT_ALIASES = {
    "LMP": "LMP",
    "LMP_PRC": "LMP",
    "LMP_PRICE": "LMP",
    "MCE": "ENERGY",
    "ENERGY": "ENERGY",
    "LMP_ENE_PRC": "ENERGY",
    "MCC": "CONG",
    "CONG": "CONG",
    "CONGESTION": "CONG",
    "LMP_CONG_PRC": "CONG",
    "MCL": "LOSS",
    "LOSS": "LOSS",
    "LOSSES": "LOSS",
    "LMP_LOSS_PRC": "LOSS",
    "MGHG": "GHG",
    "GHG": "GHG",
    "LMP_GHG_PRC": "GHG",
}
COLUMN_ALIASES = {
    "intervalstarttime_gmt": "interval_start_time_gmt",
    "intervalendtime_gmt": "interval_end_time_gmt",
    "interval_start_gmt": "interval_start_time_gmt",
    "interval_end_gmt": "interval_end_time_gmt",
    "opr_date": "opr_dt",
    "pnode": "node",
    "pnode_id": "node",
}


def column_name(name: str) -> str:
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name.strip())
    name = re.sub(r"[^a-zA-Z0-9]+", "_", name).strip("_").lower()
    return COLUMN_ALIASES.get(name, name)


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


def normalize_schema(
    frame: pl.LazyFrame, dataset_key: str, *, interval_minutes: int | None = None
) -> pl.LazyFrame:
    """Unify names, component codes and UTC times while retaining source fields.

    Explicit GMT times take precedence. Operating-date/hour fallback is Pacific
    local time; ambiguous repeated hours require explicit GMT times. INTERVAL_NUM
    without OPR_HR is a 1-based interval index from Pacific midnight.
    """
    names = {name: column_name(name) for name in frame.collect_schema()}
    if len(set(names.values())) != len(names):
        raise ValueError("Column names collide after normalization")
    frame = frame.rename(names)
    schema = frame.collect_schema()
    is_lmp = "lmp" in dataset_key.lower()
    if not is_lmp:
        return frame
    if "node" not in schema:
        alias = next(
            (c for c in ("node_id", "node_id_xml", "pnode_resmrid") if c in schema),
            None,
        )
        if alias:
            frame = frame.with_columns(pl.col(alias).cast(pl.String).alias("node"))
    selector = next(
        (c for c in ("lmp_type", "xml_data", "xml_data_item") if c in schema), None
    )
    if selector:
        frame = frame.with_columns(
            pl.col(selector)
            .cast(pl.String)
            .str.strip_chars()
            .str.to_uppercase()
            .replace(COMPONENT_ALIASES)
            .alias("lmp_type")
        )
    prices = [
        c
        for c in schema
        if c in {"mw", "value", "lmp_prc", "price"} or c.upper() in COMPONENT_ALIASES
    ]
    frame = frame.with_columns(pl.col(c).cast(pl.Float64, strict=True) for c in prices)
    candidates = [
        c
        for c in ("interval_start_time_gmt", "timestamp", "datetime", "time")
        if c in schema
    ]
    if candidates:
        frame = frame.with_columns(
            pl.coalesce([utc_expression(frame, c) for c in candidates]).alias(
                "timestamp"
            )
        )
    elif "opr_dt" in schema:
        minutes = interval_minutes or (
            15
            if any(s in dataset_key.lower() for s in ("fmm", "rtpd"))
            else 5
            if any(s in dataset_key.lower() for s in ("rtm", "rt_lmp", "rtd"))
            else 60
        )
        step = pl.lit(minutes)
        if interval_minutes is None and "market_run_id" in schema:
            market = pl.col("market_run_id").cast(pl.String).str.to_uppercase()
            step = (
                pl.when(market.is_in(["FMM", "RTPD", "HASP"]))
                .then(15)
                .when(market == "DAM")
                .then(60)
                .when(market.is_in(["RTM", "RTD"]))
                .then(5)
                .otherwise(minutes)
            )
        day = (
            pl.col("opr_dt")
            .cast(pl.String)
            .str.slice(0, 10)
            .str.to_date(format="%Y-%m-%d", strict=True)
            .cast(pl.Datetime("us"))
        )
        if "opr_hr" in schema:
            hour = pl.col("opr_hr").cast(pl.Int64)
            hour = (
                pl.when(hour.is_between(1, 24))
                .then(hour.cast(pl.String))
                .otherwise(pl.lit("invalid OPR_HR"))
                .cast(pl.Int64)
            )
            offset = (hour - 1) * 60
            if minutes < 60 or any(
                c in schema for c in ("opr_interval", "interval_num", "interval_number")
            ):
                interval = next(
                    (
                        c
                        for c in ("opr_interval", "interval_num", "interval_number")
                        if c in schema
                    ),
                    None,
                )
                if interval is None:
                    raise ValueError(
                        "Sub-hourly prices require OPR_INTERVAL/INTERVAL_NUM or explicit GMT timestamps"
                    )
                count = pl.col(interval).cast(pl.Int64)
                if interval == "opr_interval":
                    valid = (step == 60) | count.is_between(1, 60 // step)
                    count = (
                        pl.when(valid)
                        .then(count.cast(pl.String))
                        .otherwise(pl.lit("invalid OPR_INTERVAL"))
                        .cast(pl.Int64)
                    )
                offset += (
                    pl.when(step < 60)
                    .then(
                        (count - 1) * step
                        if interval == "opr_interval"
                        else ((count - 1) % (60 // step)) * step
                    )
                    .otherwise(0)
                )
            timestamp = (
                (day + pl.duration(minutes=offset))
                .dt.replace_time_zone("America/Los_Angeles")
                .dt.convert_time_zone("UTC")
            )
        else:
            interval = next(
                (c for c in ("interval_num", "interval_number") if c in schema), None
            )
            if interval is None:
                raise ValueError(
                    "Prices need OPR_HR/INTERVAL_NUM or explicit GMT timestamps"
                )
            timestamp = day.dt.replace_time_zone(
                "America/Los_Angeles"
            ).dt.convert_time_zone("UTC") + pl.duration(
                minutes=(pl.col(interval).cast(pl.Int64) - 1) * step
            )
        frame = frame.with_columns(timestamp.alias("timestamp"))
    return frame
