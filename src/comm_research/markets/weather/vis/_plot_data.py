"""Infer plotting columns and forecast frames from database and notebook data."""

import numpy as np
import pandas as pd
import polars as pl


def _column(columns, *names):
    return next((name for name in names if name in columns), None)


LATEST_COLUMN = "_latest_forecast"


def _grib_rows(df, columns, lat_col, lon_col, windows):
    if isinstance(df, pd.DataFrame):
        df = pl.from_pandas(df.loc[:, columns])
    rows = df.lazy().select(columns)
    area = "region"
    source_areas = ["us48", "europe"]
    if (
        area not in columns
        or df[area].null_count()
        or (windows and not df[area].is_in(source_areas).all())
    ):
        grid_area = pl.lit("forecast")
        if windows:
            lon = pl.col(lon_col)
            signed_lon = pl.when(lon >= 180).then(lon - 360).otherwise(lon)
            for name, (south, north, west, east) in reversed(tuple(windows.items())):
                inside = pl.col(lat_col).is_between(
                    south, north
                ) & signed_lon.is_between(west, east)
                tile = "europe" if name == "euro" else name
                grid_area = pl.when(inside).then(pl.lit(tile)).otherwise(grid_area)
        if area in columns:
            source = pl.col(area).cast(pl.String)
            grid_area = (
                pl.when(source.is_in(source_areas)).then(source).otherwise(grid_area)
                if windows
                else pl.coalesce(source, grid_area)
            )
        rows = rows.with_columns(grid_area.alias(area))
    if windows:
        # Both Europe views use the same ECMWF source tile.
        areas = {"europe" if name == "euro" else name for name in windows}
        rows = rows.filter(pl.col(area).is_in([*areas, "forecast"]))

    for name in ("dataTime", "validityTime"):
        if name in columns and df[name].null_count():
            rows = rows.with_columns(pl.col(name).fill_null(0))

    cycle = [name for name in ("dataDate", "dataTime") if name in columns]
    valid = [name for name in ("validityDate", "validityTime") if name in columns]
    member = ["perturbationNumber"] if "perturbationNumber" in columns else []
    keys = [area, *cycle, *valid, *member]
    # Rank forecast messages before joining back to the much larger grid table.
    catalog = rows.select(keys).unique().collect()
    if member:
        number = pl.col(member[0]).cast(pl.Int64)
        catalog = catalog.filter(
            number.eq_missing(number.min().over([area, *cycle, *valid]))
        )
    if cycle:
        order = [pl.col(name).cast(pl.Int64).fill_null(0) for name in cycle]
        catalog = catalog.sort(order, descending=True).with_columns(
            pl.struct(cycle)
            .eq_missing(pl.struct(cycle).first().over(area))
            .alias(LATEST_COLUMN)
        )
    else:
        catalog = catalog.with_columns(pl.lit(True).alias(LATEST_COLUMN))
    chosen = catalog.unique(subset=[area, *valid], keep="first")
    return (
        rows.join(chosen.lazy(), on=keys, how="inner", nulls_equal=True)
        .collect()
        .to_pandas()
    )


def _grib_valid_time(data):
    # Promote int32 database dates before appending the HHMM portion.
    date = pd.to_numeric(data["validityDate"], errors="coerce").astype("float64")
    raw_clock = data.get("validityTime")
    clock = (
        0
        if raw_clock is None
        else pd.to_numeric(raw_clock, errors="coerce").mask(raw_clock.isna(), 0)
    )
    stamp = (10000 * date + clock).where(
        date.between(10000101, 99991231)
        & (date % 1 == 0)
        & (clock % 1 == 0)
        & (clock >= 0)
        & (clock < 2400)
        & (clock % 100 < 60)
    )
    return pd.to_datetime(
        stamp.astype("Int64"), format="%Y%m%d%H%M", utc=True, errors="coerce"
    )


def _crop(data, lat_col, lon_col, windows):
    if not windows:
        return data
    lat = pd.to_numeric(data[lat_col])
    lon = pd.to_numeric(data[lon_col])
    lon = lon.where(lon < 180, lon - 360)
    inside = np.zeros(len(data), dtype=bool)
    for south, north, west, east in windows:
        inside |= (lat.between(south, north) & lon.between(west, east)).to_numpy(
            dtype=bool, na_value=False
        )
    if not inside.any():
        raise ValueError(
            "The weather data has no observations in the selected regions."
        )
    return data.loc[inside]


def _units(data, metric):
    if "units" in data:
        labels = {str(label).strip() for label in data["units"].dropna().unique()}
        labels.discard("")
        if len(labels) > 1:
            raise ValueError(
                f"{metric!r} contains mixed units. Select observations with one unit."
            )
        if labels:
            return labels.pop()
    if metric.casefold().startswith("temp"):
        suffix = metric.casefold().rsplit("_", 1)[-1]
        return {"f": "°F", "c": "°C", "k": "K"}.get(suffix, "")
    return ""


def plot_data(df, metric, windows):
    if not isinstance(df, (pd.DataFrame, pl.DataFrame)):
        raise TypeError("Weather data must be a pandas or Polars DataFrame.")
    if metric not in df.columns:
        raise ValueError(f"Missing metric column {metric!r}.")
    if len(df) == 0:
        raise ValueError("The weather data is empty.")

    lat_col = _column(df.columns, "latitude", "lat")
    lon_col = _column(df.columns, "longitude", "lon")
    time_col = _column(df.columns, "valid_time", "timestamp", "time", "datetime")
    grib = time_col is None and "validityDate" in df.columns
    if not lat_col or not lon_col:
        raise ValueError("Expected latitude and longitude columns (or lat and lon).")
    if time_col is None and not grib:
        raise ValueError(
            "Expected a valid_time, timestamp, time, datetime, or ECMWF validityDate column."
        )

    metadata = [
        "region",
        "validityDate",
        "validityTime",
        "dataDate",
        "dataTime",
        "perturbationNumber",
    ]
    selected = [metric, lat_col, lon_col, "units", *(metadata if grib else [time_col])]
    columns = [name for name in dict.fromkeys(selected) if name in df.columns]
    if (
        isinstance(df, pd.DataFrame)
        and df.columns[df.columns.duplicated()].isin(columns).any()
    ):
        raise ValueError("Each selected column name must appear once.")
    if grib:
        data = _grib_rows(df, columns, lat_col, lon_col, windows)
    elif isinstance(df, pl.DataFrame):
        data = df.select(columns).to_pandas()
    else:
        data = df.loc[:, columns]

    data = _crop(data, lat_col, lon_col, windows.values())
    units = _units(data, metric)
    if grib:
        time_col = "valid_time"
        data = data.assign(valid_time=_grib_valid_time(data))
    return data, time_col, lat_col, lon_col, units, grib
