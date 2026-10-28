"""Research plotting helpers.

Add public helpers to the imports and ``__all__`` below as this package grows.
Install plotting dependencies with ``pip install -e '.[vis]'``.
"""

from ._core import COLORS, DEFAULT_STYLE, plot_style, style_figure
from .geospatial import plot_locations
from .timeseries import plot_timeseries

__all__ = [
    "COLORS",
    "DEFAULT_STYLE",
    "plot_style",
    "style_figure",
    "plot_timeseries",
    "plot_locations",
]
