"""Build and export interactive weather maps."""

from collections.abc import Sequence
from pathlib import Path

import pandas as pd
import polars as pl

from ._plot_data import plot_data
from ._spatial_regions import REGION_WINDOWS
from .spatial_map import (
    SpatialMap,
    _selected_regions,
    _temperature_conversion,
)


def _output_dir() -> Path:
    cwd = Path.cwd()
    root = next(
        (
            parent
            for parent in (cwd, *cwd.parents)
            if (parent / "src/comm_research").is_dir()
        ),
        cwd,
    )
    return root / "data" / "outputs" / "weather"


def plot(
    df: pd.DataFrame | pl.DataFrame,
    metric: str,
    regions: str | Sequence[str] | None = None,
    title: str = "Weather Map",
    subtitle: str | None = None,
    *,
    output_dir: str | Path | None = None,
) -> Path:
    """Write a standalone weather map and return its absolute HTML path.

    Pass a metric column and optionally region names such as ``us48`` and
    ``europe``. Weather fills the selected countries' land areas. Empty
    regions fit the available grid and mask water. Raw ECMWF rows use the
    latest forecast cycle and the lowest-numbered available ensemble member.
    Valid times are shown in UTC. Output defaults to ``data/outputs/weather``
    at the repository root, with the filename based on the selected regions.
    """
    names = _selected_regions(regions, allow_empty=True)
    file_regions = tuple("us48" if name == "conus" else name for name in names)
    windows = tuple(REGION_WINDOWS[name] for name in file_regions)
    data, time_col, lat_col, lon_col, units, grib = plot_data(df, metric, windows)
    temperature = _temperature_conversion(units) is not None

    chart = SpatialMap()
    layout = chart.plot(
        data,
        metric,
        region=names or None,
        clip_to_region=True,
        time_col=time_col,
        lat_col=lat_col,
        lon_col=lon_col,
        title=title,
        subtitle=subtitle,
        units=units,
        metric_label="Temperature"
        if grib and temperature and metric == "value"
        else None,
        palette="temperature" if temperature else "viridis",
        allow_grid_gaps=grib,
    )
    directory = _output_dir() if output_dir is None else Path(output_dir)
    filename = "-".join(file_regions) or "weather"
    return chart.save(layout, directory / f"{filename}.html")
