# Commodities desk dashboard

From the repository root:

```bash
.venv/bin/python -m pip install -e '.[dashboard]'
.venv/bin/python app.py
```

The standard-library HTTP server hosts the static HTML/CSS/JavaScript dashboard
at `http://127.0.0.1:8502`; Polars continues to handle lake discovery and
analytics. No Streamlit, charting runtime, market-data API calls or downloads are
required. The app discovers local Parquet and Arrow IPC files under `data/lake/`.
To choose another lake, set `COMMODITY_LAKE_ROOT=/absolute/path` before starting,
or run `python app.py --lake /absolute/path`. **Rescan** refreshes the catalog
and analytics caches after an ingestion run.

## Workspace

The market-data navigator follows the data lake's commodity → region → venue →
dataset hierarchy. Search filters feed names; selecting a leaf opens or focuses its singleton
Dockview tab. Drag tabs between panel edges to create and resize horizontal or
vertical splits, or use **1-PANE**, **2-SPLIT**, and **4-SPLIT** to arrange one,
two, or four feeds. Dockview's native tab drag-and-drop supports additional
groups. The layout, theme, and each panel's date, signal, and dimension filters
are saved locally in the browser; source rows are not stored there. The **DATA**
button collapses the navigator.

Each panel has its own date range, signal selection, node/dimension filters,
metrics, and uPlot chart. The node picker supports multiple selections and defaults to the first available
node. Empty default horizons reanchor to that selected series' latest data. **UPDATE**
refreshes that panel. The
**LIGHT**/**DARK** control switches the workstation palette; charts redraw with
the active theme. The initial panel opens the first discovered feed if no saved
layout is present.

## Signals and analytics

Both supplied CAISO formats are supported directly:

| Source | Price column | Component field | Signals |
| --- | --- | --- | --- |
| DAM | `mw` | `lmp_type` | LMP, Energy (MCE), Congestion (MCC), Losses (MCL), GHG (MGHG) |
| RTM | `value` | `lmp_type` | The same five signals |

These price values are **$/MWh**, including the DAM column named `mw`.
Hourly/interval counters are not offered as signals. Other datasets expose
numeric fields discovered from their schema; categorical series remain separate
traces. Generic units are labeled **source units** unless the schema/feed makes
the unit clear. Identifiers retain their original types in raw tables.

Dates include both selected calendar days and initially cover the latest
available month to keep large-feed views responsive. The 1D/5D/1M/1Y/ALL
presets apply to each panel independently. Date filtering and the chart axis use
UTC to preserve ordering across daylight saving transitions.

Exact duplicate signal observations are removed. **Mean** takes the arithmetic
mean of repeated observations in an interval; **Last** uses the final observation
in timestamp/source order. Last does not establish the latest published revision
when the source lacks revision timestamps. Hourly/daily frequency aggregates UTC
bins; Native preserves source timestamps. Node, resource, product and other
series filters apply before aggregation. An empty selection returns no rows.

The metric strip summarizes the first returned trace: last, 24h change, minimum,
maximum, period mean, and sample standard deviation (one observation displays
`—`). 24h change requires an observation exactly 24 hours before the last point:
`(last - reference) / abs(reference) × 100`. A missing or zero reference displays
`—`; shorter samples are not presented as daily changes.

Large charts keep each bin's first, last, minimum and maximum points, capped at
4,000 displayed points per trace. Metrics use all selected aggregated intervals
before display reduction. Queries are capped at 64 traces / two million analytic
intervals; narrow the horizon or choose a coarser frequency when needed.

## Implementation and checks

```text
app.py                                    # local HTML dashboard launcher
src/comm_research/dashboard/
  app.py                                  # local HTTP server and JSON API
  ui/index.html                           # dashboard document
  ui/styles/theme.css                     # dark/light institutional terminal theme
  ui/src/main.js                          # workstation boot and control wiring
  ui/src/dock_manager.js                  # Dockview panels, presets and persistence
  ui/src/tree_navigator.js                # local lake hierarchy and feed opening
  ui/src/chart_panel.js                   # isolated controls, statistics and uPlot
  ui/vendor/                              # self-hosted Dockview/uPlot bundles and licenses
  data/loader.py                          # cached discovery and lazy analytics
  config/taxonomy.py                      # hierarchy/field display labels
```

Caches use file size and nanosecond modification time in the discovered dataset
fingerprint and are cleared by **Rescan**. Dated Hive partitions are pruned to the
selected horizon (with a one-day boundary buffer); Polars then lazily
projects/filters source columns before collection. Arrow IPC uses memory mapping.
Sources are read only, and symlinked files/directories are excluded. Run the
server on its default loopback address; the app does not provide authentication
or remote storage connectors.

Dockview and uPlot are pinned in `package.json`; their browser distributions are
vendored locally so the running dashboard does not load a CDN or require Node.
The HTTP server only serves HTML, CSS, and JavaScript assets contained within the
dashboard UI directory. Dockview positions panes through runtime inline styles,
so the content-security policy allows inline styles while keeping scripts
same-origin only.

Run `python -m unittest discover -s tests -v` to validate analytics and API
behavior. Dashboard tests require the optional `dashboard` extra, which provides
Arrow IPC support.

## RTM compatibility and series controls

Dataset paths are singleton tab identities: opening an existing feed focuses its
panel. Closing unregisters it; reopening mounts one panel. Older saved layouts
with duplicate dataset tabs are repaired on reload. Layout presets retain open
feeds and their controls, and never duplicate a feed to fill an empty grid cell.

CAISO reads normalize uppercase/lowercase fields in each Parquet/Arrow file.
Explicit GMT interval starts take precedence; otherwise `OPR_DT` and 1-based
`OPR_HR` reconstruct Pacific local operating times. RTD uses 5-minute intervals,
FMM/RTPD uses 15 minutes, and DAM uses 60 minutes. `OPR_INTERVAL` is 1-based
within the hour; `INTERVAL_NUM` without an hour is a 1-based index from Pacific
midnight. Ambiguous repeated local hours require explicit GMT timestamps. The
API emits the unified UTC `timestamp` in ISO-8601 form ending in `Z`.

`LMP_TYPE`, `XML_DATA`, `XML_DATA_ITEM`, and wide price fields map to `LMP`,
`ENERGY`, `CONG`, `LOSS` (and optional `GHG`). Legacy MCE/MCC/MCL signal settings
are migrated. **LMP / Energy / Congestion / Loss** checkboxes and the searchable
**NODES** dropdown on each ribbon update its series immediately. All unique
PNodes are searchable; large dropdowns render only 200 matches at a time. Select
up to eight nodes. A new panel selects the first node to bound the initial query.

Nodes retain stable hues across components: NP15 blue, SP15 green, ZP26 amber;
congestion is dashed and losses dotted. Click the legend at the top right to
hide/show a trace instantly, with no server request. Visibility survives theme
changes and reloads. Selecting no nodes/components intentionally clears the chart.

If the default **1M** horizon contains no observations for the selected series,
the app reanchors to that series' latest available timestamp, updates the dates,
and displays a notice. Explicit custom horizons stay empty when no data matches.
