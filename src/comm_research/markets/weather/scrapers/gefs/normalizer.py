"""Validate GEFS instantaneous fields and sample only target-grid neighbors."""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone


def sampler(latitudes, longitudes, points):
    import numpy as np

    ys, xs = np.unique(latitudes), np.unique(longitudes)
    if len(ys) < 2 or len(xs) < 2 or len(latitudes) != len(ys)*len(xs):
        raise ValueError("GEFS source must be a complete rectangular grid")
    yi, xi = np.searchsorted(ys, latitudes), np.searchsorted(xs, longitudes)
    if len(np.unique(yi*len(xs)+xi)) != len(latitudes):
        raise ValueError("Duplicate GEFS source coordinates")
    lookup = np.empty((len(ys), len(xs)), dtype=np.int64)
    lookup[yi, xi] = np.arange(len(latitudes))
    # The global longitude axis is periodic; include the first column at +360.
    if np.allclose(np.diff(xs), xs[1]-xs[0]) and np.isclose(xs[-1]-xs[0]+xs[1]-xs[0], 360):
        xs = np.append(xs, xs[0]+360)
        lookup = np.column_stack((lookup, lookup[:, 0]))
    py, px = np.asarray(points, dtype=float).T
    if py.min() < ys[0] or py.max() > ys[-1] or px.min() < xs[0] or px.max() > xs[-1]:
        raise ValueError("Target coordinates exceed GEFS grid; extrapolation is prohibited")
    y = np.clip(np.searchsorted(ys, py, side="right")-1, 0, len(ys)-2)
    x = np.clip(np.searchsorted(xs, px, side="right")-1, 0, len(xs)-2)
    wy, wx = (py-ys[y])/(ys[y+1]-ys[y]), (px-xs[x])/(xs[x+1]-xs[x])
    indices = np.column_stack((lookup[y,x], lookup[y,x+1], lookup[y+1,x], lookup[y+1,x+1]))
    weights = np.column_stack(((1-wy)*(1-wx), (1-wy)*wx, wy*(1-wx), wy*wx))
    unique, inverse = np.unique(indices, return_inverse=True)
    return unique.tolist(), inverse.reshape(indices.shape), weights


def normalize(path, initialization, member, points, steps):
    """Return weekly means, unaggregated target samples, and source-grid metadata.

    A weekly value is the arithmetic mean of 28 six-hourly instantaneous samples
    in [target_start, target_end). Compare ERA5 sampled at these same hours for
    strict temporal equivalence; this is not an hourly-mean reconstruction.
    """
    import eccodes as ec
    import numpy as np
    import polars as pl

    expected_member = 0 if member == "c00" else int(member[1:])
    samples, mapping, signature = {}, None, None
    with open(path, "rb") as stream:
        while (g := ec.codes_grib_new_from_file(stream)) is not None:
            try:
                ec.codes_set(g, "stepUnits", 1)
                get = lambda key: ec.codes_get(g, key)
                hour = int(get("endStep"))
                if hour not in steps:
                    continue
                if hour in samples:
                    raise ValueError(f"Duplicate GEFS forecast hour {hour}")
                if (int(get("discipline")) != 0 or int(get("parameterCategory")) != 0
                        or int(get("parameterNumber")) != 0 or get("typeOfLevel") != "heightAboveGround"
                        or float(get("level")) != 2 or get("stepType") != "instant" or get("units") != "K"
                        or int(get("startStep")) != hour or get("gridType") != "regular_ll"):
                    raise ValueError("Expected instantaneous 2m temperature in Kelvin on a regular GEFS grid")
                if (int(get("dataDate")) != int(initialization.strftime("%Y%m%d"))
                        or int(get("dataTime")) != 0 or int(get("perturbationNumber")) != expected_member):
                    raise ValueError("GEFS initialization/member differs from the requested source")
                grid = tuple(get(k) for k in (
                    "Ni", "Nj", "latitudeOfFirstGridPointInDegrees", "longitudeOfFirstGridPointInDegrees",
                    "latitudeOfLastGridPointInDegrees", "longitudeOfLastGridPointInDegrees",
                    "iDirectionIncrementInDegrees", "jDirectionIncrementInDegrees", "scanningMode"))
                if mapping is None:
                    lat = np.asarray(ec.codes_get_array(g, "latitudes"))
                    lon = (np.asarray(ec.codes_get_array(g, "longitudes"))+180)%360-180
                    mapping = sampler(lat, lon, points)
                    signature = grid
                if grid != signature:
                    raise ValueError("GEFS grid changed within a member file")
                unique, inverse, weights = mapping
                values = np.asarray(ec.codes_get_double_elements(g, "values", unique))
                if not np.isfinite(values).all() or np.any(values == get("missingValue")):
                    raise ValueError("Missing GEFS values at target-grid interpolation neighbors")
                samples[hour] = (values[inverse]*weights).sum(axis=1)
            finally:
                ec.codes_release(g)
    if set(samples) != set(steps):
        raise ValueError(f"Incomplete GEFS member {member}: missing hours {sorted(set(steps)-samples.keys())}")
    values = np.vstack([samples[h] for h in steps])
    origin = datetime.combine(initialization, time(), timezone.utc)
    frame = pl.DataFrame({
        "valid_time": [origin+timedelta(hours=h) for h in steps for _ in points],
        "lat": np.tile([p[0] for p in points], len(steps)),
        "lon": np.tile([p[1] for p in points], len(steps)),
        "member": [member]*(len(steps)*len(points)),
        "forecast_hour": np.repeat(steps, len(points)),
        "temperature_k": values.ravel(),
    })
    return values.mean(axis=0), frame, dict(nx=signature[0], ny=signature[1],
                                           longitude_spacing=signature[6], latitude_spacing=signature[7])
