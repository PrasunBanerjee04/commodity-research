# CAISO workstation

From the repository root:

```bash
python -m pip install -e '.[dashboard]'
python server.py
```

The server uses port **8000**. `--lake /path/to/lake`, `--port 8001`, and
`--memory-limit 4GB` override defaults. `CAISO_LAKE_ROOT` and
`CAISO_MEMORY_LIMIT` provide the same settings. Watch the terminal for preload
progress, source counts, available UTC dates, and requested node histories.

At startup, DuckDB materializes DAM and RTM Parquet into **in-memory tables**
with `(node, timestamp)` indexes. It reads `dam_lmp`/`da_lmp` and
`rtm_lmp`/`rt_lmp` under `power_gas/napg/caiso/`. It filters `market_run_id`
when present. No interactive CAISO query scans Parquet. Memory spill is disabled;
if the lake exceeds available RAM, startup fails visibly rather than serving
disk-backed queries. Increase the limit only when the machine has enough RAM.
The default DuckDB budget is 2GB; JSON responses have a separate 256MiB LRU.

A default-node overview, computed at startup, paints the complete available
time span immediately. Its tooltip explicitly says **OVERVIEW** during this
initial sampled view. The browser then downloads the complete
node history once and replaces the overview. Exact timestamps and prices stay
in typed arrays, while a precomputed min/max hierarchy bounds drawn vertices
to the pane width and at most 1,500. Hover uses the exact cached ticks after
loading, including ticks omitted from the drawn line. The first load of a new
node can take longer than a warm interaction; a first-paint overview does not
mean the complete multi-year history has already downloaded.

## Controls

- The PNode dropdown uses `/api/nodes`. A new node downloads once; returning
  to it uses the browser cache. Component buttons and the upper-right legend
  change trace visibility locally, without fetching or remounting the chart.
- **1D / 5D / 1M / YTD / ALL** select local UTC ranges. 1M is 30 days; YTD
  starts January 1 of the latest available data year. These ranges anchor to
  available data, so older lake snapshots still render immediately.
- Drag a box to zoom horizontally, use the wheel to zoom around the cursor,
  and **Shift+drag** to pan. Each pane has independent state.
- Reopening a feed focuses its singleton tab. **2-SPLIT** places DAM left and
  RTM right. **1-PANE / 4-SPLIT** reuse existing panels; scarce feeds are never
  cloned to fill empty slots. Other lake datasets retain their generic controls.
- **RESCAN** explicitly rebuilds the RAM snapshot and invalidates browser
  history caches after ingestion. A failed preload retains the previous snapshot.
  Layout and selections persist locally; prices and overviews do not persist in
  browser storage. The workstation and legacy dashboard have separate layouts.

## Schema and API

| Parquet field | Meaning / API mapping |
| --- | --- |
| `interval_start_time_gmt` | UTC interval start → `timestamps` (`YYYY-MM-DDTHH:MM:SSZ`) |
| `node` | Pricing node identifier; returned as the active `node` |
| `lmp_type` | `LMP` → `lmp`; `MCE` → `energy`; `MCC` → `congestion`; `MCL` → `loss` |
| `mw` | Numeric price, **$/MWh** despite its name; legacy `value` is also supported |
| `market_run_id` | `DAM` day-ahead / `RTM` real-time market |

These four components use blue `#2962FF`, muted teal `#00897B`, dashed crimson
`#E53935`, and amber `#FB8C00`, respectively. LMP strokes are 1.75px; other
strokes are 1.25px. Generic feeds with GHG use violet `#8E24AA`.

Component badges and the docked sub-header legend use square or dashed keys;
muted keys indicate hidden traces. Flat range controls retain local filtering.
The `⤢` icon beside the node selector pops out the existing pane without
cloning its tab or reloading its history; allow browser popups to use it.
Closing the window returns the pane to the workspace. UTC tick labels,
a separated right-hand price ruler, and dashed crosshairs with axis pills
replace the old timestamp counters and metric strips.
Missing/nonfinite prices remain **null**, not fabricated zeros. Repeated
timestamp/node/component observations use the first price; this does not
establish the latest published revision when sources lack revision timestamps.
Other components such as GHG remain accessible through the generic dashboard.

`GET /api/data?feed=DAM&node=TH_NP15_GEN-APND` returns the **complete history**
as parallel `timestamps`, `lmp`, `energy`, `congestion`, and `loss` arrays, plus
`status`, active `node`, and snapshot `revision`. Missing nodes fall back to
NP15 if present, otherwise the first available node. Empty feeds return empty
arrays; unknown feeds return HTTP 400. Nodes are parameterized SQL values.
Responses are compressed and cached; the old `/api/series` point cap does not
truncate `/api/data`. No ingestion or live OASIS requests occur at server boot.

## Verification

```bash
python -m unittest discover -s tests -v
npm test
```

Chromium checks require `pip install playwright` and a local Chromium binary
(`DASHBOARD_CHROMIUM` overrides its path). They verify actual canvas pixels,
HTTP failures, UTC hover, singleton/layout behavior, local zoom/pan, node-cache
reuse, and sub-50ms warm update/paint timing without API requests. Timing is
hardware-dependent; startup indexing and first node transfer are separate costs.
