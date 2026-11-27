"""Build packaged display boundaries from pinned public datasets."""

import argparse
import gzip
import json
from io import BytesIO
from pathlib import Path
from urllib.request import urlretrieve
from zipfile import ZipFile

import shapefile
from pyproj import Transformer
from shapely import line_merge, make_valid, transform, union_all
from shapely.geometry import LineString, Polygon, box, shape

ASSET_DIR = Path(__file__).resolve().parent
CENSUS = "cb_2024_us_state_5m"
CENSUS_URL = f"https://www2.census.gov/geo/tiger/GENZ2024/shp/{CENSUS}.zip"
NATURAL_EARTH = (
    "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/v5.1.2/geojson"
)
COUNTRIES = "ne_50m_admin_0_countries_lakes"
CITIES = "ne_10m_populated_places_simple"
CITY_NAMES = (
    "Seattle",
    "San Francisco",
    "Phoenix",
    "Denver",
    "Minneapolis",
    "Chicago",
    "Houston",
    "Atlanta",
    "Miami",
    "New York",
)
TO_GEOGRAPHIC = Transformer.from_crs(4269, 4326, always_xy=True)
TO_MERCATOR = Transformer.from_crs(4326, 3857, always_xy=True)
MERCATOR_DOMAIN = box(-180, -85.0511287798, 180, 85.0511287798)
CENSUS_SIMPLIFICATION_M = 450
EUROPE_WINDOW = box(-25, 34, 60, 72)
EUROPE_EXTRA_CODES = {"TUR", "CYP", "CYN", "ESB", "WSB"}


def download(directory, url):
    path = directory / url.rsplit("/", 1)[-1]
    if not path.exists():
        download_path = path.with_suffix(path.suffix + ".download")
        urlretrieve(url, download_path)
        download_path.replace(path)
    return path


def parts(geometry, kind):
    if geometry.is_empty:
        return
    if geometry.geom_type == kind:
        yield geometry
    elif hasattr(geometry, "geoms"):
        for part in geometry.geoms:
            yield from parts(part, kind)


def project(geometry, tolerance=0):
    projected = transform(
        geometry.intersection(MERCATOR_DOMAIN),
        TO_MERCATOR.transform,
        interleaved=False,
    )
    return (
        projected.simplify(tolerance, preserve_topology=True)
        if tolerance
        else projected
    )


def coordinates(path):
    return [[round(x, 1), round(y, 1)] for x, y in path.coords]


def polygons(geometry, tolerance=0):
    return [
        [coordinates(ring) for ring in (polygon.exterior, *polygon.interiors)]
        for polygon in parts(project(geometry, tolerance), "Polygon")
    ]


def lines(geometry, tolerance=0):
    return [
        coordinates(line) for line in parts(project(geometry, tolerance), "LineString")
    ]


def read_states(path):
    with ZipFile(path) as archive:
        files = {
            ext: BytesIO(archive.read(f"{CENSUS}.{ext}"))
            for ext in ("shp", "shx", "dbf")
        }
    states = []
    with shapefile.Reader(**files) as reader:
        for record in reader.iterShapeRecords():
            if int(record.record.STATEFP) <= 56 and record.record.STUSPS not in {
                "AK",
                "HI",
            }:
                state = transform(
                    shape(record.shape.__geo_interface__),
                    TO_GEOGRAPHIC.transform,
                    interleaved=False,
                )
                states.append(make_valid(state))
    if len(states) != 49:
        raise ValueError(
            "Expected the 48 contiguous states and the District of Columbia."
        )
    return states


def read_features(path):
    return json.loads(path.read_text(encoding="utf-8"))["features"]


def write_asset(filename, layers):
    target = ASSET_DIR / filename
    pending = target.with_suffix(".gz.tmp")
    pending.write_bytes(
        gzip.compress(json.dumps(layers, separators=(",", ":")).encode(), mtime=0)
    )
    pending.replace(target)
    print(target)


def build_europe(directory):
    source = download(directory, f"{NATURAL_EARTH}/{COUNTRIES}.geojson")
    countries = []
    for feature in read_features(source):
        properties = feature["properties"]
        if (
            properties["CONTINENT"] == "Europe"
            or properties["ADM0_A3"] in EUROPE_EXTRA_CODES
        ):
            geometry = make_valid(shape(feature["geometry"]))
            countries.append(geometry.intersection(EUROPE_WINDOW))
    boundary = union_all(countries)
    if boundary.is_empty or not boundary.is_valid:
        raise ValueError("Invalid Europe boundary.")
    rings = polygons(boundary, CENSUS_SIMPLIFICATION_M)
    write_asset("europe_50m.json.gz", {"region": rings})


def build(directory):
    states = read_states(download(directory, CENSUS_URL))
    mainland = union_all(states)
    interior_borders = union_all([state.boundary for state in states]).difference(
        mainland.boundary
    )
    with gzip.open(ASSET_DIR / "natural_earth.json.gz", "rt") as source:
        world = json.load(source)
    # The Census coastline already excludes the Great Lakes. Keep the bundled
    # Natural Earth inland lakes that lie wholly within that coastline.
    inland_lakes = [
        make_valid(Polygon(rings[0], rings[1:])) for rings in world["lakes"]
    ]
    region = mainland.difference(
        union_all([lake for lake in inland_lakes if mainland.contains(lake)])
    )

    conus_area = box(-126, 23, -65, 50)
    context = []
    features = read_features(
        download(directory, f"{NATURAL_EARTH}/{COUNTRIES}.geojson")
    )
    for feature in features:
        country = make_valid(shape(feature["geometry"]))
        context.extend(
            polygon
            for polygon in parts(country, "Polygon")
            if feature["properties"]["ADM0_A3"] != "USA"
            or not polygon.intersects(conus_area)
        )
    surrounding_land = union_all(context).simplify(0.06, preserve_topology=True)
    surrounding_land = union_all(
        [
            polygon
            for polygon in parts(surrounding_land, "Polygon")
            if polygon.area >= 0.0125
        ]
    )
    # Use only the Census strokes along the CONUS shore and land borders.
    us_area = mainland.buffer(0.09)
    surrounding_coast = surrounding_land.boundary.difference(us_area)

    context_states = union_all(
        [LineString(line) for line in world["states"]]
    ).difference(us_area)
    context_borders = union_all(
        [LineString(line) for line in world["borders"]]
    ).difference(us_area)
    places = {}
    for feature in read_features(
        download(directory, f"{NATURAL_EARTH}/{CITIES}.geojson")
    ):
        name = feature["properties"]["name"]
        lon, lat = feature["geometry"]["coordinates"]
        if name in CITY_NAMES and region.covers(shape(feature["geometry"])):
            places[name] = [round(v, 1) for v in TO_MERCATOR.transform(lon, lat)]

    if not region.is_valid or len(places) != len(CITY_NAMES):
        raise ValueError(
            f"Invalid CONUS polygon or missing cities: {set(CITY_NAMES) - places.keys()}"
        )
    layers = {
        "region": polygons(region, CENSUS_SIMPLIFICATION_M),
        "land": polygons(surrounding_land),
        "coast": lines(surrounding_coast),
        "states": lines(
            line_merge(interior_borders.intersection(region)), CENSUS_SIMPLIFICATION_M
        )
        + lines(line_merge(context_states)),
        "borders": lines(context_borders),
        "cities": [[name, *places[name]] for name in CITY_NAMES],
    }
    write_asset("conus_2024.json.gz", layers)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "source_dir", type=Path, help="Directory to cache public source files"
    )
    parser.add_argument("--region", choices=("conus", "europe"), default="conus")
    args = parser.parse_args()
    args.source_dir.mkdir(parents=True, exist_ok=True)
    build_region = build_europe if args.region == "europe" else build
    build_region(args.source_dir)
