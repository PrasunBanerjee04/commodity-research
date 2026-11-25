
import pandas as pd
import eccodes

from tempfile import TemporaryFile
from comm_research.markets.weather.map import ZONAL_MAP
from .schema import SCHEMAS

def transform(raw_data: bytes) -> pd.DataFrame:
    """Return one row per grid point in each GRIB message."""

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

                    frame = pd.DataFrame(points, columns=["latitude", "longitude", "value"])
                    frame["message"] = len(frames)
                    for key in keys:
                        if eccodes.codes_is_defined(message, key) and not eccodes.codes_is_missing(message, key):
                            frame[key] = eccodes.codes_get(message, key)
                    frames.append(frame)
                finally:
                    eccodes.codes_release(message)
    except eccodes.CodesInternalError as error:
        raise ValueError(f"Could not decode raw_data as GRIB: {error}") from error

    if not frames:
        raise ValueError("No GRIB messages found in raw_data")

    return pd.concat(frames, ignore_index=True)

def geofilter(df, region):
    df = df.copy()
    min_lat, max_lat, min_lon, max_lon = ZONAL_MAP[region] 
    lon = (df["longitude"] + 180) % 360 - 180
    return df[
        df["latitude"].between(min_lat, max_lat) & 
        lon.between(min_lon, max_lon)
    ]

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
    
def schema_filter(df, path):
    df = df.copy()
    schema, rename = get_schema(path)
    df = df[schema]
    if rename: 
        df = df.rename(columns=rename)

    return df