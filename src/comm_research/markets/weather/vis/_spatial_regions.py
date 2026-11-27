"""Packaged projected geography and display masks for spatial weather maps."""

import gzip
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw
from shapely import intersects_xy, make_valid, prepare, union_all
from shapely.geometry import MultiPolygon, Polygon, box

from .map import ZONAL_MAP

REGION_ASSETS = {
    "conus": Path(__file__).with_name("conus_2024.json.gz"),
    "europe": Path(__file__).with_name("europe_50m.json.gz"),
}
REGION_WINDOWS = {**ZONAL_MAP, "euro": (34.0, 72.0, -25.0, 45.0)}
EARTH_RADIUS = 6_378_137.0
MAX_LATITUDE = 85.0511287798066
MASK_SUPERSAMPLE = 4


def mercator_y(latitude):
    return EARTH_RADIUS * np.arcsinh(np.tan(np.deg2rad(latitude)))


def project_path(path):
    lon, lat = np.asarray(path).T
    return EARTH_RADIUS * np.deg2rad(lon), mercator_y(
        np.clip(lat, -MAX_LATITUDE, MAX_LATITUDE)
    )


@lru_cache(maxsize=1)
def natural_earth():
    with gzip.open(Path(__file__).with_name("natural_earth.json.gz"), "rt") as file:
        return json.load(file)


def _polygon_data(polygons):
    xs, ys = [], []
    for polygon in polygons:
        rings = [np.asarray(ring, dtype="float32").T for ring in polygon]
        xs.append([[x for x, _ in rings]])
        ys.append([[y for _, y in rings]])
    return {"xs": xs, "ys": ys}


def _line_data(lines):
    xs, ys = zip(*(np.asarray(line, dtype="float32").T for line in lines))
    return {"xs": list(xs), "ys": list(ys)}


@dataclass(frozen=True)
class Region:
    geometry: MultiPolygon
    layers: dict
    cities: dict


@lru_cache(maxsize=2)
def load_region(name: str) -> Region:
    path = REGION_ASSETS[name]
    try:
        with gzip.open(path, "rt", encoding="utf-8") as file:
            data = json.load(file)
    except FileNotFoundError as error:
        raise FileNotFoundError(
            f"Boundary data for {name.upper()} is missing ({path.name}). "
            "Reinstall the weather map package or rebuild the asset with prepare_conus.py."
        ) from error
    except (OSError, ValueError) as error:
        raise ValueError(
            f"Could not read the {name.upper()} boundary asset {path.name}."
        ) from error

    try:
        geometry = MultiPolygon(
            [Polygon(rings[0], rings[1:]) for rings in data["region"]]
        )
        if geometry.is_empty or not geometry.is_valid:
            raise ValueError("Empty or invalid polygon.")
        prepare(geometry)
        if name == "europe":
            return Region(geometry, {}, {})
        layers = {
            "region": _polygon_data(data["region"]),
            "land": _polygon_data(data["land"]),
            **{
                layer: _line_data(data[layer])
                for layer in ("coast", "states", "borders")
            },
        }
        labels, x, y = zip(*data["cities"])
    except (KeyError, TypeError, ValueError, IndexError) as error:
        raise ValueError(
            f"Invalid boundary data for {name.upper()} in {path.name}. Rebuild the asset."
        ) from error
    return Region(geometry, layers, {"label": list(labels), "x": list(x), "y": list(y)})


@lru_cache(maxsize=1)
def _land_geometry():
    def dissolve(layer):
        polygons = []
        for rings in natural_earth()[layer]:
            outer, *holes = [np.column_stack(project_path(ring)) for ring in rings]
            polygons.append(make_valid(Polygon(outer, holes)))
        return union_all(polygons)

    return dissolve("land").difference(dissolve("lakes"))


def _footprint(name):
    if name == "land":
        return _land_geometry()
    if name == "euro":
        south, north, west, east = REGION_WINDOWS[name]
        x0, x1 = EARTH_RADIUS * np.deg2rad([west, east])
        return load_region("europe").geometry.intersection(
            box(x0, mercator_y(south), x1, mercator_y(north))
        )
    return load_region(name).geometry


@lru_cache(maxsize=4)
def region_geometry(footprints: tuple[str, ...]):
    """Combine projected land or selected country outlines."""
    shapes = [_footprint(name) for name in footprints]
    geometry = shapes[0] if len(shapes) == 1 else union_all(shapes)
    prepare(geometry)
    return geometry


@lru_cache(maxsize=4)
def native_mask(footprints: tuple, xs: tuple, ys: tuple) -> np.ndarray:
    """Include a native cell for autoscaling if its geographic center is covered."""
    mask = intersects_xy(
        region_geometry(footprints), np.asarray(xs)[None, :], np.asarray(ys)[:, None]
    )
    mask.setflags(write=False)
    return mask


@dataclass(frozen=True)
class DisplayMask:
    alpha: np.ndarray
    rows: np.ndarray
    cols: np.ndarray


def _polygons(geometry):
    if geometry.geom_type == "Polygon" and not geometry.is_empty:
        yield geometry
    elif hasattr(geometry, "geoms"):
        for part in geometry.geoms:
            yield from _polygons(part)


@lru_cache(maxsize=4)
def display_mask(
    footprints: tuple, extent: tuple, native_shape: tuple, shape: tuple
) -> DisplayMask:
    """Rasterize the region once into a south-up antialiased coverage mask."""
    west, south, east, north = extent
    height, width = shape
    nx, ny = width * MASK_SUPERSAMPLE, height * MASK_SUPERSAMPLE
    coverage = Image.new("L", (nx, ny))
    draw = ImageDraw.Draw(coverage)
    xscale, yscale = nx / (east - west), ny / (north - south)

    def pixels(ring):
        return [((x - west) * xscale, (y - south) * yscale) for x, y in ring.coords]

    intersection = region_geometry(footprints).intersection(box(*extent))
    # Paint enclosing exteriors before any islands that lie in their holes.
    for polygon in sorted(
        _polygons(intersection), key=lambda p: Polygon(p.exterior).area, reverse=True
    ):
        draw.polygon(pixels(polygon.exterior), fill=255)
        for hole in polygon.interiors:
            draw.polygon(pixels(hole), fill=0)
    alpha = np.asarray(coverage.resize((width, height), Image.Resampling.BOX))
    native_height, native_width = native_shape
    rows = ((np.arange(height) + 0.5) * native_height / height).astype("int32")
    cols = ((np.arange(width) + 0.5) * native_width / width).astype("int32")
    rows *= native_width
    rows.setflags(write=False)
    cols.setflags(write=False)
    return DisplayMask(alpha, rows, cols)
