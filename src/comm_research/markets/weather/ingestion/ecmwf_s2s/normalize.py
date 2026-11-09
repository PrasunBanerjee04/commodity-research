"""Validate daily GRIB means and interpolate each member onto target points."""
from __future__ import annotations

from datetime import datetime


def fields(path):
    """Yield historical initialization, member, lead bounds, and a Kelvin field."""
    import eccodes as ec
    import numpy as np

    with open(path, "rb") as source:
        while (message := ec.codes_grib_new_from_file(source)) is not None:
            try:
                get = lambda key: ec.codes_get(message, key)
                if (int(get("paramId")) not in (167, 228004) or get("stepType") != "avg"
                        or get("units") != "K" or str(get("stepUnits")) != "1"
                        or int(get("dataTime")) != 0):
                    raise ValueError("Expected 00 UTC initialized daily-mean 2m temperature in K, steps in hours")
                if int(get("bitmapPresent")):
                    raise ValueError("Missing GRIB values are not valid temperature inputs")
                yield (
                    datetime.strptime(str(get("dataDate")), "%Y%m%d").date(),
                    int(get("perturbationNumber")), int(get("startStep")), int(get("endStep")),
                    np.asarray(ec.codes_get_array(message, "latitudes")),
                    (np.asarray(ec.codes_get_array(message, "longitudes")) + 180) % 360 - 180,
                    np.asarray(ec.codes_get_values(message)),
                )
            finally:
                ec.codes_release(message)


def interpolate(latitudes, longitudes, values, points):
    """Bilinear interpolation on a regular archive grid; never extrapolate."""
    import numpy as np

    ys, xs = np.unique(latitudes), np.unique(longitudes)
    if len(ys) < 2 or len(xs) < 2 or len(values) != len(ys) * len(xs):
        raise ValueError("Expected a complete rectangular source grid")
    iy, ix = np.searchsorted(ys, latitudes), np.searchsorted(xs, longitudes)
    if len(np.unique(iy * len(xs) + ix)) != len(values) or not np.isfinite(values).all():
        raise ValueError("Duplicate grid coordinates or nonfinite temperatures")
    grid = np.empty((len(ys), len(xs)))
    grid[iy, ix] = values
    py, px = np.asarray(points).T
    if (py.min() < ys[0] or py.max() > ys[-1] or px.min() < xs[0] or px.max() > xs[-1]):
        raise ValueError("Target coordinates fall outside the requested archive grid")
    y = np.clip(np.searchsorted(ys, py, side="right") - 1, 0, len(ys) - 2)
    x = np.clip(np.searchsorted(xs, px, side="right") - 1, 0, len(xs) - 2)
    wy, wx = (py - ys[y]) / (ys[y+1] - ys[y]), (px - xs[x]) / (xs[x+1] - xs[x])
    return ((1-wy) * ((1-wx)*grid[y, x] + wx*grid[y, x+1])
            + wy * ((1-wx)*grid[y+1, x] + wx*grid[y+1, x+1]))


def weekly(paths, hdates, points, lead_days, *, reader=fields):
    """Require all seven days for every historical date and all eleven members."""
    import numpy as np

    sums, seen = {}, set()
    expected = {(h, m, 24*d, 24*(d+1)) for h in hdates for m in range(11)
                for d in range(lead_days, lead_days + 7)}
    for path in paths:
        for hdate, member, start, end, lat, lon, values in reader(path):
            key = (hdate, member, start, end)
            if key not in expected or key in seen:
                raise ValueError(f"Unexpected or duplicate ECMWF field: {key}")
            seen.add(key)
            pair = hdate, member
            sums.setdefault(pair, np.zeros(len(points)))
            sums[pair] += interpolate(lat, lon, values, points) / 7
    if seen != expected:
        raise ValueError(f"Incomplete ECMWF response: {len(expected - seen)} daily member fields missing")
    return sums
