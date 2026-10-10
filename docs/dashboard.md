# Commodities desk dashboard

For the preloaded DuckDB CAISO workstation (`python server.py`, port 8000), see
[CAISO workstation](caiso_workstation.md). This page describes the generic
Polars dashboard and its bounded `/api/series` API on port 8502.

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
the active theme. A fresh workspace opens CAISO Day-Ahead and Real-Time LMP side
by side when both are available; otherwise it opens the available LMP feed or
first discovered feed. Saved workspace layouts are preserved.

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

Every API plot is capped at 1,500 points per trace. Native sub-hourly windows
longer than seven days use hourly means, or four-hour means for windows of 60
days or more. Any remaining excess keeps each time bin's first, last, minimum,
and maximum points. API statistics use all selected intervals before display
reduction. Queries remain capped at 64 traces / two million analytic intervals.

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
  ui/src/query_cache.js                   # indexed browser windows and local statistics
  ui/src/line_renderer.js                 # batched WebGL price lines and Canvas fallback
  ui/src/canvas_paths.js                  # bounded Canvas strokes without point reduction
  ui/vendor/                              # self-hosted Dockview/uPlot bundles and licenses
  data/loader.py                          # cached discovery and lazy analytics
  config/taxonomy.py                      # hierarchy/field display labels
```

Caches use file size and nanosecond modification time in the discovered dataset
fingerprint and are cleared by **Rescan**. Opening a feed materializes its
normalized Parquet/Arrow rows once in RAM; subsequent filters and raw-page
queries use that snapshot. Node blocks are indexed and sorted by UTC timestamp,
so date slicing within a node uses binary searches.
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
up to eight nodes. A new panel initially displays the first node; its browser
cache loads the available components and nodes for that date window in batches.

Nodes retain stable hues across components: NP15 blue, SP15 green, ZP26 amber;
congestion is dashed and losses dotted. Click the legend at the top right to
hide/show a trace instantly, with no server request. Visibility survives theme
changes and reloads. Selecting no nodes/components intentionally clears the chart.

If the default **1M** horizon contains no observations for the selected series,
the app reanchors to that series' latest available timestamp, updates the dates,
and displays a notice. Explicit custom horizons stay empty when no data matches.

## Rendering and diagnostics

Default dates use the actual latest timestamp in the local lake, rather than
today. Saved date windows outside the dataset's bounds reset to its latest month.
Charts wait for a nonzero viewport before initializing and resize when tabs,
splits, the navigator, or the browser window change. Each chart has a 250px
minimum height; smaller panes scroll vertically instead of collapsing.

Loading, empty results (`NO_RECORDS_FOUND_FOR_DATE_RANGE`), HTTP/data failures
(`DATA_FETCH_ERROR`), and chart failures (`CHART_RENDER_ERROR`) appear directly
inside the viewport. Missing vendored chart/workspace libraries display
`ERR_DEPENDENCY_LOAD_FAILED`. **UPDATE** retries a data request. Libraries load
synchronously from the local server before workspace initialization.

Run `npm test` for browser-module checks. To run the rendering regressions with
a real browser, install Playwright (`python -m pip install playwright`) and
Chromium (`python -m playwright install chromium`), then run
`python -m unittest discover -s tests -p test_dashboard_browser.py -v`.
Alternatively set `DASHBOARD_CHROMIUM=/path/to/chromium` to use an installed
browser. These tests create an isolated lake and HTTP server; they do not change
the user's lake or download market data.

## Cached interactions

After the initial window loads, date presets, nodes, and components slice
indexed arrays in JavaScript and update the existing uPlot canvas. Covered
selections make no API request and display no loading state. ALL history warms
in the background so wider presets can also switch locally once ready. A cold
wider date range, cache eviction, or **Rescan** can require a request; those
operations are outside the warm interaction latency target. **Rescan** refreshes
open feeds, metadata, and both cache layers after ingestion.

uPlot maintains the axes, scales, and cursor. Price lines use a single batched
WebGL draw, preserving node hues, component opacity, dashes, and every returned
point. Native GPU hairlines prioritize fast comparisons; some drivers limit
their width to one physical pixel. WebGL resources are reused and released when
a chart closes. If WebGL is unavailable or loses its context, short Canvas
strokes keep the chart visible; that fallback has no 50ms latency guarantee.
Split presets move the existing panels rather than recreating canvases.

The narrowest cached window containing the selected dates supplies the view.
For example, 1D/5D sliced from a cached 30-day RTM window retains its hourly
means; it does not reconstruct native five-minute ticks from sampled data.
The chart badge identifies the resolution and statistics basis. **Source stats**
use exact source intervals when the entire cached window is selected;
**Display stats** use the displayed cached samples when slicing a smaller window.
The strip's tooltip explains this distinction.

The server retains up to eight feed snapshots within 512 MiB of source data and
rejects individual feeds over that limit or five million rows. Browser caches
retain four reusable feed identities, up to eight windows and 250,000 points per
feed; open panels pin their own feed cache until closed.
Oversize feeds require narrower dataset directories. Snapshots live only in
process/browser memory; source rows are not written to browser storage.
Only controls and layout are persisted, with deferred writes flushed on reload.

The Chromium suite includes a 90-day, three-node RTM fixture with 311,040 source
rows and twelve concurrent traces. It measures warm 1D/5D/1M/ALL, component, and
node changes through forced raster completion and the following painted frame,
enforcing a maximum of 50ms for both across forty-eight interactions, including
clearing and restoring all components. It checks canvas
identity and rejects API requests or loading states during those interactions.
These measurements apply to the tested Chromium/WebGL environment; cold loading,
hardware, browser scheduling, and much larger trace selections affect latency.
