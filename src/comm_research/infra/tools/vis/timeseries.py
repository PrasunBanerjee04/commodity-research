"""Interactive time-series figures accepting sequences or dataframe columns."""

from ._core import _get_figure


def plot_timeseries(time, values, *, fig=None, title=None, ylabel=None, **kwargs):
    """Return a Plotly Figure with line traces; never display it automatically.

    ``values`` accepts one series or a sequence of series, for example
    ``[df["actual"], df["forecast"]]``. Each series shares ``time`` and uses its
    column name as its legend label unless ``name`` is supplied.
    Pass an existing ``fig`` to overlay a series. Trace options such as ``name``,
    ``line``, and ``connectgaps`` are forwarded to ``go.Scatter``. Missing values
    remain gaps by default. Existing figures retain their layout.
    """
    from plotly.graph_objects import Scatter

    fig = _get_figure(fig)
    options = {"mode": "lines", "connectgaps": False, **kwargs}
    # Strings and missing values are scalar observations, not nested series.
    multiple = isinstance(values, (list, tuple)) and bool(values) and all(
        not isinstance(value, (str, bytes)) and hasattr(value, "__iter__")
        for value in values
    )
    for series in values if multiple else [values]:
        trace_options = options.copy()
        if "name" not in trace_options and getattr(series, "name", None) is not None:
            trace_options["name"] = str(series.name)
        fig.add_trace(Scatter(x=time, y=series, **trace_options))
    fig.update_layout(hovermode="x unified", showlegend=len(fig.data) > 1)
    fig.update_xaxes(title_text="Time", type="date")
    if title is not None:
        fig.update_layout(title_text=title)
    if ylabel is not None:
        fig.update_yaxes(title_text=ylabel)
    return fig
