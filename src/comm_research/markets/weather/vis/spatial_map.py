"""Standalone Bokeh weather maps for regular latitude/longitude grids."""

import webbrowser
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from html import escape
from math import prod
from numbers import Integral
from pathlib import Path
from tempfile import NamedTemporaryFile

import numpy as np
import pandas as pd
from bokeh.core.property.color import ColorHex
from bokeh.embed import file_html
from bokeh.layouts import column, row
from bokeh.models import (
    BasicTicker,
    BoxZoomTool,
    ColorBar,
    ColumnDataSource,
    CustomJS,
    CustomJSHover,
    Div,
    HoverTool,
    InlineStyleSheet,
    LayoutDOM,
    LinearColorMapper,
    PanTool,
    RadioButtonGroup,
    Range1d,
    ResetTool,
    Slider,
    Text,
    WheelZoomTool,
)
from bokeh.palettes import Blues256, Inferno256, Oranges256, RdBu, Turbo256, Viridis256
from bokeh.plotting import figure
from bokeh.resources import INLINE
from PIL import ImageColor

from ._spatial_regions import (
    EARTH_RADIUS,
    MASK_SUPERSAMPLE,
    MAX_LATITUDE,
    REGION_ASSETS,
    REGION_WINDOWS,
    display_mask,
    load_region,
    mercator_y,
    native_mask,
    natural_earth,
    project_path,
    region_geometry,
)

_MIB = 1024**2
_PALETTES = {
    "viridis": Viridis256,
    "temperature": Turbo256,
    "anomaly": RdBu[11],
    "precipitation": Blues256,
    "wind": Oranges256,
    "uncertainty": Inferno256,
}
_WATER = "#eaf0f4"
_LAND = "#f7f7f3"
_REGION_ALIASES = {"us48": "conus", "western_europe": "euro"}
_TEMPERATURE_LABELS = ("°F", "°C", "K")
_TEMPERATURE_CODES = {
    "f": 0,
    "fahrenheit": 0,
    "c": 1,
    "celsius": 1,
    "k": 2,
    "kelvin": 2,
}
_SLATE = "#293843"
_MUTED = "#536471"
_FONT = "system-ui, sans-serif"
_SLIDER_CSS = """
.noUi-horizontal.noUi-target {
  height: 8px; background: #e7ecef; border: 0; border-radius: 4px; box-shadow: none;
}
.noUi-horizontal .noUi-handle {
  width: 20px; height: 20px; top: -6px; right: -10px; border: 1px solid white;
  border-radius: 50%; background: #3f6476; box-shadow: 0 0 0 1px #3f6476;
}
.noUi-handle::before, .noUi-handle::after { display: none; }
.noUi-handle:focus-visible { outline: 2px solid #3f6476; outline-offset: 3px; }
"""
_HTML_TEMPLATE = """
{% block preamble %}
<meta name="viewport" content="width=device-width, initial-scale=1">
{% endblock %}
{% block postamble %}
<style>
  body { margin: 20px 16px; background: #fff; }
  body > div[data-root-id] { display: block !important; width: 100%; }
</style>
{% endblock %}
"""
_CLIPPED_FRAME_JS = """
const colors = mapper.rgba_mapper.v_compute(frames.data.image[i])
const pixels = new Uint32Array(colors.buffer)
const output = active.data.image[0]
const {alpha, rows, cols} = mask.data
const coverage = alpha[0]
const rowOffsets = rows[0]
const columns = cols[0]
for (let y = 0, k = 0; y < rowOffsets.length; y++) {
    const offset = rowOffsets[y]
    for (let x = 0; x < columns.length; x++, k++) {
        const a = coverage[k]
        const pixel = a === 0 ? 0 : pixels[offset + columns[x]]
        output[k] = a === 255 ? pixel :
            ((pixel & 0x00ffffff) | (Math.round((pixel >>> 24) * a / 255) << 24))
    }
}
active.setv({data: {image: [output]}}, {check_eq: false})
"""


@dataclass(frozen=True)
class _Bounds:
    west: float
    south: float
    east: float
    north: float

    @property
    def extent(self) -> tuple[float, float, float, float]:
        return self.west, self.south, self.east, self.north

    @classmethod
    def from_degrees(cls, south, north, west, east):
        return cls(
            EARTH_RADIUS * np.deg2rad(west),
            mercator_y(south),
            EARTH_RADIUS * np.deg2rad(east),
            mercator_y(north),
        )

    def ranges(self, aspect: float) -> tuple[Range1d, Range1d]:
        """Pad each edge by 6% and expand one axis to fit the map frame."""
        width = (self.east - self.west) * 1.12
        height = (self.north - self.south) * 1.12
        half_width = max(width, height * aspect) / 2
        half_height = max(height, width / aspect) / 2
        x = (self.west + self.east) / 2
        y = (self.south + self.north) / 2
        return Range1d(x - half_width, x + half_width), Range1d(
            y - half_height, y + half_height
        )


@lru_cache(maxsize=1)
def _basemap():
    """Project the packaged Natural Earth vectors once per Python process."""

    def project(path):
        x, y = project_path(path)
        return x.astype("float32"), y.astype("float32")

    projected = {}
    for name, shapes in natural_earth().items():
        if name in {"land", "lakes"}:
            polygons = [
                list(zip(*(project(ring) for ring in polygon))) for polygon in shapes
            ]
            projected[name] = {
                "xs": [[list(xs)] for xs, _ in polygons],
                "ys": [[list(ys)] for _, ys in polygons],
            }
        else:
            xs, ys = zip(*(project(line) for line in shapes))
            projected[name] = {"xs": list(xs), "ys": list(ys)}
    return projected


def _axis_spacing(axis: np.ndarray, label: str) -> float:
    if len(axis) < 2:
        raise ValueError(f"{label} needs at least two distinct grid coordinates.")
    spacing = (axis[-1] - axis[0]) / (len(axis) - 1)
    tolerance = min(3.1e-5, spacing * 0.005)
    expected = np.linspace(axis[0], axis[-1], len(axis))
    if not np.allclose(axis, expected, rtol=0, atol=tolerance):
        raise ValueError(
            f"{label} must form a regularly spaced axis on a rectilinear grid."
        )
    return spacing


def _axis_grid(axis, label, *, allow_gaps):
    if not allow_gaps or len(axis) < 2:
        return _axis_spacing(axis, label), len(axis), np.arange(len(axis))
    # The selected tiles may share a grid but have no observations in between.
    span = axis[-1] - axis[0]
    steps = span / np.diff(axis).min()
    if not np.isfinite(steps) or steps >= np.iinfo(np.int64).max:
        raise ValueError(
            f"{label} grid spacing is too small to combine the selected regions."
        )
    intervals = round(steps)
    spacing = span / intervals
    indices = np.rint((axis - axis[0]) / spacing).astype("int64")
    if not np.allclose(
        axis,
        axis[0] + indices * spacing,
        rtol=0,
        atol=min(3.1e-5, spacing * 0.005),
    ):
        raise ValueError(
            f"{label} must use a shared regular grid across selected regions."
        )
    return spacing, intervals + 1, indices


def _selected_regions(region, *, allow_empty=False):
    if region is None:
        return ()
    names = (region,) if isinstance(region, str) else region
    if not isinstance(names, Sequence) or (not names and not allow_empty):
        raise ValueError("region must be a name or a nonempty list of region names.")
    selected = []
    for name in names:
        key = name.strip().lower() if isinstance(name, str) else None
        if key not in {*REGION_ASSETS, *REGION_WINDOWS, *_REGION_ALIASES}:
            raise ValueError(
                f"Unknown map region {name!r}. Use us48, CONUS, europe, or euro."
            )
        selected.append(_REGION_ALIASES.get(key, key))
    return tuple(dict.fromkeys(selected))


def _temperature_conversion(units, *, difference=False):
    source = _TEMPERATURE_CODES.get(units.casefold().strip().lstrip("°º").strip())
    if source is None:
        return None
    to_celsius = (5 / 9, 1, 1)
    zeros = (32, 0, 273.15) if not difference else (0, 0, 0)
    scales = [to_celsius[source] / factor for factor in to_celsius]
    offsets = [zero - zeros[source] * scale for zero, scale in zip(zeros, scales)]
    return source, scales, offsets


def _add_hover(plot, renderer, variable, units, args):
    longitude = CustomJSHover(
        args={"radius": EARTH_RADIUS},
        code='return (value * 180 / (Math.PI * radius)).toFixed(3) + "°"',
    )
    latitude = CustomJSHover(
        args={"radius": EARTH_RADIUS},
        code='return (Math.atan(Math.sinh(value / radius)) * 180 / Math.PI).toFixed(3) + "°"',
    )
    reading = CustomJSHover(
        args={"units": units, **args},
        code="""
            let reading = value
            if (mask !== null) {
                const {i, j, flat_index} = special_vars.image_index
                if (!mask.data.alpha[0][flat_index]) return "No data"
                const frame = frames.data.image[slider === null ? 0 : slider.value]
                reading = frame[mask.data.rows[0][j] + mask.data.cols[0][i]]
            }
            if (!Number.isFinite(reading)) return "No data"
            const unit = selector === null ? units : unit_labels[selector.active]
            if (selector !== null) {
                reading = reading * scales[selector.active] + offsets[selector.active]
            }
            return reading.toFixed(1) + (unit ? " " + unit : "")
        """,
    )
    plot.add_tools(
        HoverTool(
            name="weather-hover",
            renderers=[renderer],
            point_policy="follow_mouse",
            tooltips=[
                ("Latitude", "$y{custom}"),
                ("Longitude", "$x{custom}"),
                (variable, "@image{custom}"),
            ],
            formatters={"$y": latitude, "$x": longitude, "@image": reading},
        )
    )


def _numeric_column(df: pd.DataFrame, name: str) -> np.ndarray:
    try:
        values = pd.to_numeric(df[name])
        if not np.iscomplexobj(values):
            return values.to_numpy(dtype="float64", na_value=np.nan)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(f"{name!r} must contain real numeric values.") from error
    raise ValueError(f"{name!r} must contain real numeric values.")


def _color_bounds(low, high, vmin, vmax, *, symmetric):
    if symmetric and vmin is None and vmax is None:
        limit = max(abs(low), abs(high))
        low, high = -limit, limit
    if vmin is not None:
        low = float(vmin)
    if vmax is not None:
        high = float(vmax)
    if low == high and (vmin is None or vmax is None):
        padding = max(abs(low) * 0.01, 1e-6)
        low = low - padding if vmin is None else low
        high = high + padding if vmax is None else high
    if not np.isfinite([low, high]).all() or low >= high:
        raise ValueError("Color bounds must be finite with vmin strictly below vmax.")
    return low, high


def _initial_rgba(frame, mapper, mask):
    """Color the first frame with Bokeh's linear palette and the display coverage."""
    colors = []
    for color in mapper.palette:
        hex_color = ColorHex().transform(color)
        colors.append(
            (0, 0, 0, 0)
            if hex_color == "transparent"
            else ImageColor.getcolor(hex_color, "RGBA")
        )
    palette = np.asarray(colors, dtype=np.uint8).view("<u4").ravel()
    missing = np.isnan(frame)
    # Match LinearColorMapper's bin calculation, including clipping at both limits.
    scaled = (frame.astype("float64") - mapper.low) * (1 / (mapper.high - mapper.low))
    bins = np.clip(
        np.where(missing, 0, scaled / (1 / len(palette))), 0, len(palette) - 1
    )
    packed = palette[bins.astype("intp")]
    packed[missing] = 0
    pixels = packed.ravel()[mask.rows[:, None] + mask.cols[None, :]]
    pixels[mask.alpha == 0] = 0
    alpha = ((pixels >> 24) * mask.alpha + 127) // 255
    return (pixels & 0x00FFFFFF) | (alpha << 24)


def _valid_time(stamp):
    if stamp.second or stamp.microsecond or stamp.nanosecond:
        time = stamp.isoformat().split("T")[1].removesuffix("+00:00")
    elif stamp.minute:
        time = stamp.strftime("%H:%M")
    else:
        time = stamp.strftime("%H")
    return f"{stamp:%a %d %b %Y} · {time}Z", f"{stamp:%d %b} · {time}Z"


def _time_ticks(labels):
    end = len(labels) - 1
    indices = sorted({0, (end + 1) // 2, end})
    parts = []
    for i in indices:
        offset = 0 if i == 0 else -100 if i == end else -50
        parts.append(
            f'<span style="position:absolute;left:{100 * i / end:g}%;transform:translateX({offset}%);'
            f'white-space:nowrap">{escape(labels[i])}</span>'
        )
    return "".join(parts)


class SpatialMap:
    """Create notebook-ready maps and export the same layouts as standalone HTML."""

    def __init__(self, *, max_frame_mb: float = 64) -> None:
        self.max_frame_mb = float(max_frame_mb)
        if not np.isfinite(self.max_frame_mb) or self.max_frame_mb <= 0:
            raise ValueError("max_frame_mb must be positive and finite (in MiB).")

    def _prepare(
        self,
        df,
        metric,
        time_col,
        lat_col,
        lon_col,
        *,
        scale_region,
        allow_grid_gaps,
    ):
        required = {metric, time_col, lat_col, lon_col}
        missing = required.difference(df.columns)
        if missing:
            raise ValueError(f"Missing columns: {', '.join(sorted(missing))}.")
        if df.empty:
            raise ValueError("The weather data is empty.")
        if df.columns[df.columns.duplicated()].isin(required).any():
            raise ValueError("Each selected column name must appear once.")

        times = pd.to_datetime(df[time_col], utc=True, format="mixed", errors="coerce")
        if times.isna().any():
            raise ValueError(f"{time_col!r} contains missing or invalid timestamps.")
        ti, timestamps = pd.factorize(times, sort=True)
        lat = _numeric_column(df, lat_col)
        lon = _numeric_column(df, lon_col)
        if not np.isfinite(lat).all() or np.any(np.abs(lat) > MAX_LATITUDE):
            raise ValueError(
                f"{lat_col!r} must be finite and within Web Mercator's latitude limits."
            )
        if not np.isfinite(lon).all() or np.any((lon < -180) | (lon >= 360)):
            raise ValueError(
                f"{lon_col!r} must use longitudes in [-180, 180) or [0, 360)."
            )
        lon = np.where(lon >= 180, lon - 360, lon)
        latitudes, yi = np.unique(lat, return_inverse=True)
        longitudes, xi = np.unique(lon, return_inverse=True)
        if pd.MultiIndex.from_arrays([ti, yi, xi]).has_duplicates:
            raise ValueError(
                "Duplicate time/latitude/longitude observations. Select one run or member."
            )

        dy, nlat, y_indices = _axis_grid(latitudes, lat_col, allow_gaps=allow_grid_gaps)
        dx, nlon, x_indices = _axis_grid(
            longitudes, lon_col, allow_gaps=allow_grid_gaps
        )
        south, north = latitudes[0] - dy / 2, latitudes[-1] + dy / 2
        west, east = longitudes[0] - dx / 2, longitudes[-1] + dx / 2
        if west < -180 or east > 180:
            raise ValueError(
                "Use a regional grid that does not cross the antimeridian. Crop or split the data."
            )
        if south < -MAX_LATITUDE or north > MAX_LATITUDE:
            raise ValueError(
                "The grid's cell edges exceed Web Mercator's latitude limits. Crop the data."
            )
        bounds = _Bounds.from_degrees(south, north, west, east)

        # Choose evenly spaced projected rows fine enough to sample the shortest native cell.
        closest_lat = (
            abs(latitudes[0] + np.clip(round(-latitudes[0] / dy), 0, nlat - 1) * dy)
            if nlat != len(latitudes)
            else np.abs(latitudes).min()
        )
        shortest_cell = mercator_y(closest_lat + dy / 2) - mercator_y(
            closest_lat - dy / 2
        )
        raster_height = int(np.ceil((bounds.north - bounds.south) / shortest_cell))
        raw_shape = (len(timestamps), nlat, nlon)
        projected_shape = (len(timestamps), raster_height, nlon)
        raster_bytes = 4 * (prod(raw_shape) + prod(projected_shape))
        raster_bytes += 4 * raster_height * nlon  # temporary resampled frame
        if raster_bytes > self.max_frame_mb * _MIB:
            raise ValueError(
                f"Preparing this raster needs about {raster_bytes / _MIB:.1f} MiB of grid arrays "
                f"(limit {self.max_frame_mb:g} MiB). Filter the region or times, or downsample the input."
            )

        values = _numeric_column(df, metric)
        valid = np.isfinite(values)
        if not valid.any():
            raise ValueError(f"{metric!r} has no finite values.")
        if np.any(np.abs(values[valid]) > np.finfo(np.float32).max):
            raise ValueError(
                f"{metric!r} contains values outside the float32 raster range."
            )
        values = np.where(valid, values, np.nan).astype("float32")
        cube = np.full(raw_shape, np.nan, dtype="float32")
        if nlat != len(latitudes):
            yi = y_indices[yi]
            latitudes = latitudes[0] + dy * np.arange(nlat)
        if nlon != len(longitudes):
            xi = x_indices[xi]
            if scale_region:
                longitudes = longitudes[0] + dx * np.arange(nlon)
        cube[ti, yi, xi] = values
        if scale_region:
            inside = native_mask(
                scale_region,
                tuple(EARTH_RADIUS * np.deg2rad(longitudes)),
                tuple(mercator_y(latitudes)),
            )
            low = float(
                np.fmin.reduce(cube, axis=None, where=inside[None], initial=np.inf)
            )
            high = float(
                np.fmax.reduce(cube, axis=None, where=inside[None], initial=-np.inf)
            )
            if not np.isfinite(low):
                area = (
                    "the land mask"
                    if scale_region == ("land",)
                    else ", ".join(scale_region).upper()
                )
                raise ValueError(
                    f"No finite observations have cell centers in {area}. "
                    "Check coverage or supply both vmin and vmax to display with a fixed scale."
                )
        else:
            low, high = float(np.nanmin(values)), float(np.nanmax(values))
        y = (
            bounds.south
            + (np.arange(raster_height) + 0.5)
            * (bounds.north - bounds.south)
            / raster_height
        )
        lat_at_pixel = np.rad2deg(np.arctan(np.sinh(y / EARTH_RADIUS)))
        midpoints = (latitudes[:-1] + latitudes[1:]) / 2
        source_rows = np.searchsorted(midpoints, lat_at_pixel)
        projected = np.empty(projected_shape, dtype="float32")
        for source_frame, projected_frame in zip(cube, projected):
            projected_frame[:] = source_frame[source_rows]
        return projected, timestamps, bounds, low, high

    def _display_mask(
        self, footprints, frames, bounds, x_range, y_range, width, height
    ):
        rows, cols = frames.shape[1:]
        # Target about one coverage pixel per CSS pixel at the initial view.
        view_width = width * (bounds.east - bounds.west) / (x_range.end - x_range.start)
        view_height = (
            height * (bounds.north - bounds.south) / (y_range.end - y_range.start)
        )
        shape = (
            max(rows, int(np.ceil(min(view_height, 960)))),
            max(cols, int(np.ceil(min(view_width, 1600)))),
        )
        # The native cube is already released. Bound this separate working set,
        # including the supersampled mask, packed RGBA and composition scratch.
        pixels = shape[0] * shape[1]
        display_bytes = frames.nbytes + (MASK_SUPERSAMPLE**2 + 16) * pixels
        if display_bytes > self.max_frame_mb * _MIB:
            raise ValueError(
                f"Preparing the clipped display needs about {display_bytes / _MIB:.1f} MiB "
                f"(limit {self.max_frame_mb:g} MiB). Use a smaller map, time range, or input grid."
            )
        return display_mask(footprints, bounds.extent, (rows, cols), shape)

    def plot(
        self,
        df: pd.DataFrame,
        metric: str,
        *,
        region: str | Sequence[str] | None = None,
        clip_to_region: bool | None = None,
        time_col: str = "valid_time",
        lat_col: str = "latitude",
        lon_col: str = "longitude",
        title: str = "Weather Map",
        subtitle: str | None = None,
        units: str = "",
        metric_label: str | None = None,
        palette: str | Sequence[str] = "viridis",
        vmin: float | None = None,
        vmax: float | None = None,
        opacity: float = 0.9,
        width: int = 1200,
        height: int = 590,
        allow_grid_gaps: bool = False,
    ) -> LayoutDOM:
        """Plot a regular grid with naive timestamps interpreted as UTC.

        ``region="us48"`` and ``region="CONUS"`` clip the rendered field to the
        contiguous U.S. by default, with geographic padding around the outline.
        Pass a list of names to display their combined footprint on one map.
        ``"europe"`` and ``"euro"`` clip to European countries within their
        respective forecast windows.
        Set ``clip_to_region=False`` to show the full field in the same viewport.
        Without a region the view fits the input grid. Set ``clip_to_region=True``
        to mask water in that view. Width and height set the preferred map size.
        Set ``allow_grid_gaps=True`` to allow missing rows or
        columns in data from separate tiles when the viewport fits the grid.
        """
        regions = _selected_regions(region)
        if clip_to_region is not None and not isinstance(clip_to_region, bool):
            raise ValueError("clip_to_region must be True, False, or None.")
        clipping = bool(regions) if clip_to_region is None else clip_to_region
        if any(
            not isinstance(size, Integral) or isinstance(size, bool) or size < 1
            for size in (width, height)
        ):
            raise ValueError("width and height must be positive integer pixel sizes.")
        if not 0 <= opacity <= 1:
            raise ValueError("opacity must be between 0 and 1.")
        palette_name = palette.lower() if isinstance(palette, str) else None
        if palette_name is not None:
            if palette_name not in _PALETTES:
                raise ValueError(
                    f"Unknown palette {palette!r}. Available palettes: {', '.join(_PALETTES)}."
                )
            palette = _PALETTES[palette_name]
        if len(palette) < 2:
            raise ValueError("A custom palette needs at least two colors.")

        footprints = regions or ("land",)
        scale_region = footprints if clipping and (vmin is None or vmax is None) else ()
        frames, timestamps, bounds, data_low, data_high = self._prepare(
            df,
            metric,
            time_col,
            lat_col,
            lon_col,
            scale_region=scale_region,
            allow_grid_gaps=allow_grid_gaps or len(regions) > 1,
        )
        low, high = _color_bounds(
            data_low, data_high, vmin, vmax, symmetric=palette_name == "anomaly"
        )
        geography = load_region("conus") if "conus" in regions else None
        view = _Bounds(*region_geometry(regions).bounds) if regions else bounds
        x_range, y_range = view.ranges(width / height)

        wheel_zoom = WheelZoomTool(zoom_on_axis=False)
        plot = figure(
            name="weather-map",
            width=width,
            frame_height=height,
            sizing_mode="stretch_width",
            x_range=x_range,
            y_range=y_range,
            x_axis_type=None,
            y_axis_type=None,
            background_fill_color=_WATER,
            outline_line_color=None,
            min_border=0,
            toolbar_location="right",
            toolbar_inner=False,
            tools=[PanTool(), wheel_zoom, BoxZoomTool(match_aspect=True), ResetTool()],
            active_scroll=wheel_zoom,
        )
        # The legend and toolbar have fixed sizes, so keep the map frame itself proportional.
        plot.js_on_change(
            "inner_width",
            CustomJS(
                args={"aspect": width / height},
                code="""
                    if (cb_obj.inner_width > 0) cb_obj.frame_height = cb_obj.inner_width / aspect
                """,
            ),
        )
        plot.toolbar.logo = None
        plot.grid.visible = False
        base = {
            layer: ColumnDataSource(data)
            for layer, data in (geography.layers if geography else _basemap()).items()
        }
        plot.multi_polygons(source=base["land"], fill_color=_LAND, line_alpha=0)
        if geography:
            plot.multi_polygons(source=base["region"], fill_color=_LAND, line_alpha=0)
        else:
            plot.multi_polygons(source=base["lakes"], fill_color=_WATER, line_alpha=0)
        mapper = LinearColorMapper(
            palette=list(palette), low=low, high=high, nan_color="#00000000"
        )
        conversion = _temperature_conversion(
            units, difference=palette_name == "anomaly"
        )
        unit_selector = None
        legend_mapper = mapper
        display_units = units
        unit_args = {"selector": None}
        if conversion:
            initial_unit, scales, offsets = conversion
            display_units = _TEMPERATURE_LABELS[initial_unit]
            legend_mapper = mapper.clone()
            unit_selector = RadioButtonGroup(
                name="weather-units",
                labels=list(_TEMPERATURE_LABELS),
                active=initial_unit,
                width=118,
                height=28,
                html_attributes={"aria-label": "Temperature units"},
            )
            unit_args = {
                "selector": unit_selector,
                "scales": scales,
                "offsets": offsets,
                "unit_labels": _TEMPERATURE_LABELS,
            }
        image_spec = {
            "image": "image",
            "x": bounds.west,
            "y": bounds.south,
            "dw": bounds.east - bounds.west,
            "dh": bounds.north - bounds.south,
            "origin": "bottom_left",
            "anchor": "bottom_left",
            "global_alpha": opacity,
        }
        active = ColumnDataSource(name="weather-frame")
        callback_args = {"active": active}
        mask_source = None
        frame_store = ColumnDataSource(
            data={"image": list(frames)}, name="weather-frames"
        )
        if clipping:
            mask = self._display_mask(
                footprints, frames, bounds, x_range, y_range, width, height
            )
            active.data = {"image": [_initial_rgba(frames[0], mapper, mask)]}
            renderer = plot.image_rgba(source=active, **image_spec)
            mask_source = ColumnDataSource(
                name="weather-region-mask",
                data={
                    "alpha": [mask.alpha],
                    "rows": [mask.rows],
                    "cols": [mask.cols],
                },
            )
            callback_args.update(mapper=mapper, mask=mask_source)
            update_frame = _CLIPPED_FRAME_JS
        else:
            active.data = {"image": [frames[0]]}
            renderer = plot.image(source=active, color_mapper=mapper, **image_spec)
            update_frame = "active.data = {image: [frames.data.image[i]]}"
        plot.multi_line(
            source=base["states"], line_color=_SLATE, line_width=0.6, line_alpha=0.52
        )
        plot.multi_line(
            source=base["borders"], line_color=_MUTED, line_width=0.9, line_alpha=0.68
        )
        if geography:
            plot.multi_line(
                source=base["coast"], line_color=_MUTED, line_width=0.8, line_alpha=0.53
            )
            plot.multi_polygons(
                source=base["region"],
                name="weather-region-outline",
                fill_alpha=0,
                line_color=_SLATE,
                line_width=0.9,
                line_alpha=0.77,
            )
            cities = ColumnDataSource(geography.cities)
            plot.scatter(
                x="x",
                y="y",
                source=cities,
                size=3.5,
                fill_color=_SLATE,
                line_color="#ffffff",
                line_width=0.6,
            )
            label_spec = {
                "x": "x",
                "y": "y",
                "text": "label",
                "source": cities,
                "x_offset": 5,
                "y_offset": 3,
                "text_font": {"value": _FONT},
                "text_font_size": "11px",
            }
            # Draw the halo first so Bokeh's text stroke leaves the 11px type intact.
            halo_width = (
                {"text_outline_width": 1.9}
                if "text_outline_width" in Text.properties()
                else {}
            )
            plot.text(
                **label_spec,
                text_color="#fafaf6",
                text_outline_color="#fafaf6",
                **halo_width,
            )
            plot.text(**label_spec, text_color=_SLATE)
        else:
            for layer in ("land", "lakes"):
                plot.multi_polygons(
                    source=base[layer],
                    fill_alpha=0,
                    line_color=_MUTED,
                    line_width=0.8,
                    line_alpha=0.64,
                )
        variable = metric.replace("_", " ")
        variable = metric_label or variable[:1].upper() + variable[1:]
        legend = ColorBar(
            color_mapper=legend_mapper,
            orientation="horizontal",
            title=f"{variable} ({display_units})" if display_units else variable,
            title_text_font=_FONT,
            title_text_font_style="bold",
            title_text_font_size="12px",
            title_text_color=_SLATE,
            title_standoff=6,
            major_label_text_font=_FONT,
            major_label_text_font_size="11px",
            major_label_text_color=_MUTED,
            major_tick_line_color=_MUTED,
            major_tick_in=0,
            major_tick_out=3,
            minor_tick_line_color=None,
            ticker=BasicTicker(desired_num_ticks=7),
            label_standoff=4,
            height=10,
            padding=0,
            margin=12,
        )
        plot.add_layout(legend, "below")
        if unit_selector is not None:
            unit_selector.js_on_change(
                "active",
                CustomJS(
                    args={
                        **unit_args,
                        "legend": legend,
                        "source_mapper": mapper,
                        "variable": variable,
                    },
                    code="""
                        const i = cb_obj.active
                        legend.color_mapper.setv({
                            low: source_mapper.low * scales[i] + offsets[i],
                            high: source_mapper.high * scales[i] + offsets[i],
                        })
                        legend.title = variable + " (" + unit_labels[i] + ")"
                    """,
                ),
            )

        header = (
            f'<h2 style="margin:0;font:600 22px/1.2 {_FONT};color:{_SLATE}">'
            f"{escape(title)}</h2>"
        )
        if subtitle:
            header += f'<p style="margin:4px 0 0;font:400 13px/1.4 {_FONT};color:{_MUTED}">{escape(subtitle)}</p>'
        labels, ticks = zip(*(_valid_time(stamp) for stamp in timestamps))
        time = Div(
            name="weather-valid-time",
            text=f"Valid {labels[0]}",
            width=220,
            align="end",
            disable_math=True,
            styles={
                "font": f"500 13px/1.4 {_FONT}",
                "color": _SLATE,
                "text-align": "right",
                "font-variant-numeric": "tabular-nums",
            },
            html_attributes={"aria-live": "polite", "aria-atomic": "true"},
        )
        heading = row(
            Div(text=header, sizing_mode="stretch_width", disable_math=True),
            time,
            sizing_mode="stretch_width",
            margin=(0, 0, 12, 0),
        )
        children = [heading, plot]
        slider = None
        if len(labels) > 1:
            slider = Slider(
                name="weather-time",
                title="",
                start=0,
                end=len(labels) - 1,
                step=1,
                value=0,
                show_value=False,
                tooltips=False,
                sizing_mode="stretch_width",
                bar_color="#6d8b9b",
                margin=(12, 37, 2, 7),
                stylesheets=[InlineStyleSheet(css=_SLIDER_CSS)],
                html_attributes={"role": "group", "aria-label": "Forecast valid time"},
            )
            slider.js_on_change(
                "value",
                CustomJS(
                    args={
                        **callback_args,
                        "frames": frame_store,
                        "labels": labels,
                        "time": time,
                    },
                    code="const i = cb_obj.value\n"
                    + update_frame
                    + '\ntime.text = "Valid " + labels[i]',
                ),
            )
            children.extend(
                [
                    slider,
                    Div(
                        text=_time_ticks(ticks),
                        sizing_mode="stretch_width",
                        height=16,
                        styles={
                            "position": "relative",
                            "font": f"400 11px/1.4 {_FONT}",
                            "color": _MUTED,
                            "font-variant-numeric": "tabular-nums",
                        },
                        margin=(0, 37, 0, 7),
                    ),
                ]
            )
        _add_hover(
            plot,
            renderer,
            variable,
            units,
            {
                **unit_args,
                "slider": slider,
                "mask": mask_source,
                "frames": frame_store if clipping else None,
            },
        )
        attribution = Div(
            text="U.S. Census Bureau · Natural Earth" if geography else "Natural Earth",
            styles={"font": f"400 11px/1.4 {_FONT}", "color": _MUTED},
            sizing_mode="stretch_width",
            align="center",
        )
        footer = (
            [attribution] if unit_selector is None else [attribution, unit_selector]
        )
        children.append(row(*footer, sizing_mode="stretch_width", margin=(8, 37, 0, 0)))
        return column(
            *children,
            sizing_mode="stretch_width",
            max_width=width,
            styles={"margin-inline": "auto"},
        )

    def save(
        self,
        plot: LayoutDOM,
        path: str | Path,
        *,
        open_browser: bool = False,
    ) -> Path:
        """Write a self-contained HTML file and optionally open it in a browser."""
        target = Path(path).expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        html = file_html(plot, INLINE, title="Weather Map", template=_HTML_TEMPLATE)
        with NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=target.parent, delete=False
        ) as file:
            pending = Path(file.name)
            try:
                file.write(html)
                file.close()
                pending.replace(target)
            finally:
                pending.unlink(missing_ok=True)
        if open_browser:
            webbrowser.open(target.as_uri())
        return target
