"""Interactive time-series figures accepting sequences or dataframe columns."""

from ._core import _get_figure


def plot_timeseries(time, values, *, fig=None, title=None, ylabel=None, **kwargs):
    """Return a Plotly Figure with a line trace; never display it automatically.

    Pass an existing ``fig`` to overlay a series. Trace options such as ``name``,
    ``line``, and ``connectgaps`` are forwarded to ``go.Scatter``. Missing values
    remain gaps by default. Existing figures retain their layout.
    """
    from plotly.graph_objects import Scatter

    fig = _get_figure(fig)
    options = {"mode": "lines", "connectgaps": False, **kwargs}
    fig.add_trace(Scatter(x=time, y=values, **options))
    fig.update_layout(hovermode="x unified", showlegend=len(fig.data) > 1)
    fig.update_xaxes(title_text="Time", type="date")
    if title is not None:
        fig.update_layout(title_text=title)
    if ylabel is not None:
        fig.update_yaxes(title_text=ylabel)
    return fig
