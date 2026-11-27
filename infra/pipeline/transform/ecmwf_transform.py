from tempfile import TemporaryFile
from typing import Literal

import eccodes
import pandas as pd
import polars as pl

from comm_research.markets.weather import ZONAL_MAP
from .schema import SCHEMAS

Frame = pl.DataFrame | pd.DataFrame
FrameType = Literal["polars", "pandas"]


def transform(
    raw_data: bytes,
    *,
    region: str | None = None,
    max_messages: int | None = None,
    type: FrameType = "polars",
) -> Frame:
    """Decode GRIB rows with optional limits on region and message count."""
    if type not in ("polars", "pandas"):
        raise ValueError(f"Unknown DataFrame type: {type!r}")
    columns = ["latitude", "longitude", "value"]
    keys = [
        "shortName", "units", "typeOfLevel", "level", "dataDate", "dataTime",
        "stepRange", "validityDate", "validityTime", "perturbationNumber",
    ]
    frames = []
    try:
        with TemporaryFile() as grib:
            grib.write(raw_data)
            grib.seek(0)
            while (message := eccodes.codes_grib_new_from_file(grib)) is not None:
                try:
                    points = eccodes.codes_get_array(message, "latLonValues").reshape(-1, 3)
                    if eccodes.codes_get(message, "bitmapPresent"):
                        bitmap = eccodes.codes_get_array(message, "bitmap")
                        points[bitmap == 0, 2] = float("nan")

                    metadata = {"message": len(frames)}
                    for key in keys:
                        if eccodes.codes_is_defined(message, key) and not eccodes.codes_is_missing(message, key):
                            metadata[key] = eccodes.codes_get(message, key)
                    if type == "pandas":
                        frame = pd.DataFrame(points, columns=columns).assign(**metadata)
                    else:
                        frame = pl.DataFrame(points, schema=columns, orient="row").with_columns(
                            pl.col("value").fill_nan(None),
                            **{key: pl.lit(value) for key, value in metadata.items()},
                        )
                    if region is not None:
                        frame = geofilter(frame, region, type=type)
                    frames.append(frame)
                finally:
                    eccodes.codes_release(message)
                if len(frames) == max_messages:
                    break
    except eccodes.CodesInternalError as error:
        raise ValueError(f"Could not decode raw_data as GRIB: {error}") from error

    if not frames:
        raise ValueError("No GRIB messages found in raw_data")

    if type == "pandas":
        return pd.concat(frames, ignore_index=True)
    return pl.concat(frames, how="diagonal_relaxed")


def _as_type(df: Frame, type: FrameType) -> Frame:
    if type == "polars":
        return df if isinstance(df, pl.DataFrame) else pl.from_pandas(df)
    if type == "pandas":
        return df if isinstance(df, pd.DataFrame) else df.to_pandas()
    raise ValueError(f"Unknown DataFrame type: {type!r}")


def geofilter(df: Frame, region: str, *, type: FrameType = "polars") -> Frame:
    min_lat, max_lat, min_lon, max_lon = ZONAL_MAP[region]
    lon = (df["longitude"] + 180) % 360 - 180
    if isinstance(df, pl.DataFrame):
        filtered = df.filter(
            df["latitude"].is_between(min_lat, max_lat) & lon.is_between(min_lon, max_lon)
        )
    else:
        filtered = df[df["latitude"].between(min_lat, max_lat) & lon.between(min_lon, max_lon)]
    return _as_type(filtered, type)


def get_schema(path): 
    parts = path.split("/")
    date_idx = -1
    for i, part in enumerate(parts): 
        if "date" in part:
            date_idx = i
            break 
    path_prefix_list = parts[:date_idx] 
    path_prefix = '/'.join(path_prefix_list)

    return SCHEMAS.get(path_prefix)['filter_columns'], SCHEMAS.get(path_prefix)['rename_columns']


def schema_filter(df: Frame, path: str, *, type: FrameType = "polars") -> Frame:
    schema, rename = get_schema(path)
    if isinstance(df, pl.DataFrame):
        filtered = df.select(schema).rename(rename or {}, strict=False)
    else:
        filtered = df[schema].rename(columns=rename or {})
    return _as_type(filtered, type)
