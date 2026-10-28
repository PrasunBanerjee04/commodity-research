"""Geographic plots with subdued geographic context and no tile API key."""

from ._core import COLORS, _get_figure


def plot_locations(longitude, latitude, *, values=None, fig=None, title=None, **kwargs):
    """Return a Plotly geographic scatter figure without displaying it.

    ``values`` supplies optional numeric point colors and a color scale.
    ``go.Scattergeo`` options, including ``name``, ``text``, and ``marker``, may
    be supplied as keywords. Longitudes and latitudes are in degrees.
    """
    from plotly.graph_objects import Scattergeo

    fig = _get_figure(fig)
    supplied_marker = kwargs.pop("marker", {})
    marker = {"size": 7, "color": COLORS["blue"], "opacity": 0.85,
              **supplied_marker}
    if values is not None:
        if "color" in supplied_marker:
            raise ValueError("Supply either values or marker.color, not both")
        marker.update(color=values, colorscale="Cividis", showscale=True)
    fig.add_trace(Scattergeo(lon=longitude, lat=latitude, **{"mode": "markers", "marker": marker, **kwargs}))
    fig.update_geos(fitbounds="locations", projection_type="natural earth",
                    showframe=False, showland=True, landcolor="#F8FAFC",
                    showcoastlines=True, coastlinecolor="#CBD5E1",
                    showcountries=True, countrycolor="#E2E8F0",
                    showocean=True, oceancolor="white", bgcolor="white")
    fig.update_layout(showlegend=len(fig.data) > 1)
    if title is not None:
        fig.update_layout(title_text=title)
    return fig
