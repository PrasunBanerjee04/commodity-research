# Python package APIs and figures

Install the project with plotting support:

```sh
pip install -e '.[vis]'
```

Every Python package declares `__all__`. Most package initializers expose an
explicit `_EXPORTS` mapping: `name: (relative_module, attribute)`. Use `None` for
the attribute to expose the module itself. Add entries there to extend the
public API. Imports resolve on demand and preserve the original object's identity;
importing a parent package does not load its optional dependencies. Star imports
resolve everything in `__all__`, so prefer named imports when extras are optional.
Empty packages use `__all__ = []` until they have implementations to expose.

```python
from comm_research.infra.lake import scan_parquet, write_atomic
from comm_research.markets.weather import ERA5Scraper, WeatherGrid
from comm_research.markets.power_gas.napg.caiso import CAISOOASISClient
```

## Plotly figures

All visualization helpers return Plotly `Figure` objects. They never display a
figure or change Plotly's global default template. Shared styling uses white
backgrounds, subtle gridlines, restrained colors, and consistent fonts. Edit
`vis/_core.py` to change the defaults. `plot_style` temporarily overrides defaults
for newly created figures, with nested dictionaries merged and restored on exit.

```python
from comm_research.infra.tools.vis import (
    plot_locations, plot_style, plot_timeseries, style_figure,
)

with plot_style({"font": {"size": 14}, "height": 480}):
    fig = plot_timeseries(dates, temperatures, name="Observed", ylabel="°C")
    plot_timeseries(dates, forecast, fig=fig, name="Forecast",
                    line={"dash": "dot"})

fig.show()
fig.write_html("temperatures.html")

stations = plot_locations(longitudes, latitudes, values=temperatures,
                          title="Station temperatures", marker={"size": 9})
stations.show()
```

`plot_timeseries` accepts Plotly `Scatter` trace options; `plot_locations` accepts
`Scattergeo` options and uses a geographic projection with subdued land and
coastlines. Geographic figures do not require a tile API key. Plotly may fetch
geographic boundary assets when displaying a map. Use `fig=` to add traces to an
existing figure. Existing figures retain their styling; use `style_figure(fig,
**layout)` to apply the shared style to any Plotly chart, including bars,
histograms, and subplots.

The plotting API now uses `fig=` instead of Matplotlib's `ax=` and returns a
Plotly figure rather than Matplotlib axes. Use `name=` for series labels and
`line={"color": "…"}` for line colors.
