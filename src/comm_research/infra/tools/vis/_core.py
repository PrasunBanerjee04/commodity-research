"""Minimal Plotly styling, applied per figure without global template changes."""

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy

COLORS = {
    "blue": "#2563EB", "orange": "#D97706", "green": "#059669",
    "red": "#DC2626", "purple": "#7C3AED",
}
DEFAULT_STYLE = {
    "template": "plotly_white",
    "colorway": list(COLORS.values()),
    "font": {"family": "Arial, sans-serif", "size": 13, "color": "#334155"},
    "paper_bgcolor": "white", "plot_bgcolor": "white",
    "margin": {"l": 60, "r": 30, "t": 60, "b": 50},
    "height": 420,
    "title": {"x": 0.02, "xanchor": "left", "font": {"size": 18}},
    "legend": {"orientation": "h", "y": 1.02, "x": 0, "title_text": ""},
    "xaxis": {"showgrid": False, "zeroline": False, "showline": False},
    "yaxis": {"gridcolor": "#EEF2F6", "zeroline": False, "showline": False},
    "hoverlabel": {"bgcolor": "white", "font_size": 13},
}
_STYLE = ContextVar("vis_style", default=None)


def _merge(base, overrides):
    result = deepcopy(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


@contextmanager
def plot_style(overrides=None):
    """Temporarily override layout defaults for figures made in this context.

    Accepts nested Plotly layout dictionaries. Contexts nest safely and restore
    the previous style even when plotting raises an exception.
    """
    token = _STYLE.set(_merge(_STYLE.get() or {}, overrides or {}))
    try:
        yield
    finally:
        _STYLE.reset(token)


def style_figure(fig, **layout):
    """Apply the shared style to any Plotly figure, update layout, and return it."""
    fig.update_layout(_merge(_merge(DEFAULT_STYLE, _STYLE.get() or {}), layout))
    return fig


def _get_figure(fig=None):
    if fig is None:
        from plotly.graph_objects import Figure

        return style_figure(Figure())
    return fig
