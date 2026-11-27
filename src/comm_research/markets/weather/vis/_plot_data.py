"""Infer plotting columns and forecast frames from database and notebook data."""

import numpy as np
import pandas as pd
import polars as pl


def _column(columns, *names):
    return next((name for name in names if name in columns), None)


def _latest_grib_rows(df, columns):
    if isinstance(df, pd.DataFrame):
        df = pl.from_pandas(df.loc[:, columns])
    rows = df.lazy().select(columns)
    for name in ("dataDate", "dataTime"):
        if name in columns:
            cycle = pl.col(name).cast(pl.Int64).fill_null(0)
            rows = rows.filter(cycle == cycle.max())
    if "perturbationNumber" in columns:
        member = pl.col("perturbationNumber").cast(pl.Int64)
        rows = rows.filter(member.eq_missing(member.min()))
    return rows.collect().to_pandas()


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
        data = _latest_grib_rows(df, columns)
    elif isinstance(df, pl.DataFrame):
        data = df.select(columns).to_pandas()
    else:
        data = df.loc[:, columns]

    data = _crop(data, lat_col, lon_col, windows)
    units = _units(data, metric)
    if grib:
        time_col = "valid_time"
        data = data.assign(valid_time=_grib_valid_time(data))
    return data, time_col, lat_col, lon_col, units, grib
