"""Weather region bounding boxes in (min_lat, max_lat, min_lon, max_lon) order."""

ZONAL_MAP = {
    # NOAA nClimGrid-Daily CONUS bounds, in degrees with west negative.
    "us48": (24.0, 50.0, -125.0, -66.0),
}
