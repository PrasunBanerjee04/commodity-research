# Commodities desk dashboard

From the repository root:

```bash
.venv/bin/python -m pip install -e '.[dashboard]'
.venv/bin/python -m streamlit run app.py
```

The app discovers local Parquet and Arrow IPC files under `data/lake/`. To choose
another lake, set `COMMODITY_LAKE_ROOT=/absolute/path` before starting, or use
**Lake source → Apply source** in the sidebar. **Rescan** refreshes the catalog and
cached data after an ingestion run. No market-data API calls or downloads occur.

## Workspace

The collapsible lake explorer follows commodity → region → venue → dataset.
Select a feed to append or focus a panel. **Close** removes only that panel.
CAISO DAM/RTM panels open initially when present; otherwise the first available
feed opens. Search filters the explorer without changing the workspace.

Choose **Tabs view**, **Split 1×2**, or **Split 2×2**. Tabs load their selected panel
on demand; split views show up to two/four panels per page. A workspace can hold
12 panels; additional grid pages keep all open feeds available. Each panel keeps
its own dates, signals, series filters, frequency, aggregation and inspection mode.
Dark/light appearance uses slate panels, sharp borders and monospace figures.

Preferences live in `st.session_state`; the `workspace` URL parameter restores
active feeds, horizons and controls on browser reload. The URL contains relative
feed names and preferences, not source rows. An alternate lake path should be set
through the environment if it must survive a reload. An unavailable feed remains
closable. Malformed workspace parameters fall back to defaults.

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

Dates include both selected calendar days and default to the latest available
30-day horizon, clamped to the source's actual bounds. **Date zone** sets date
boundaries and hover timestamps. The axis always uses UTC to preserve ordering
across daylight saving transitions. Range buttons (1D/5D/1M/YTD/ALL) zoom within
the selected horizon; they do not expand the data query.

Exact duplicate signal observations are removed. **Mean** takes the arithmetic
mean of repeated observations in an interval; **Last** uses the final observation
in timestamp/source order. Last does not establish the latest published revision
when the source lacks revision timestamps. Hourly/daily frequency aggregates UTC
bins; Native preserves source timestamps. Node, resource, product and other series
filters apply before aggregation. An empty selection returns no rows.

Choose the **Summary series** for the ticker:

- Last, minimum, maximum and arithmetic period mean use the selected series/frequency.
- Std dev is the sample standard deviation (one observation displays `—`).
- 24h change requires an observation exactly 24 hours before the last point:
  `(last - reference) / abs(reference) × 100`. A missing or zero reference displays
  `—`; shorter samples are not presented as daily changes.

Large charts keep each bin's first, last, minimum and maximum points, capped at
4,000 displayed points per trace. Metrics use all selected aggregated intervals
before display reduction. Queries are capped at 64 traces / two million analytic
intervals; narrow the horizon or choose a coarser frequency when needed.

**Raw data** provides sorted, server-paged source rows (100/250/500 per page),
a CSV export of the current page, and field/type inspection. It includes all
source fields and components matching the dates and series filters, independent
of the chart's signal selection. Undated or nonnumeric tables remain inspectable.

## Implementation and checks

```text
app.py                                    # checkout launcher
src/comm_research/dashboard/
  app.py                                  # main loop, explorer, viewport routing
  ui/theme.py                             # theme tokens and compact CSS
  ui/components.py                        # controls, ticker, charts, raw paging
  ui/workspace.py                         # panel registry and persistence
  data/loader.py                          # cached discovery and lazy analytics
  config/taxonomy.py                      # hierarchy/field display labels
```

Caches include file size and nanosecond modification time so refreshed partitions
invalidate their results after rediscovery. Polars lazy scans project/filter source
columns before collection; Arrow IPC uses memory mapping. Sources are read only,
and symlinked files/directories are excluded. Broken sources fail within their
panel while other feeds remain open. Use one local trusted lake per deployment;
this app does not provide authentication or remote storage connectors.

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Dashboard tests run when the optional extra is installed; a base-only installation
skips those tests explicitly. Browser checks cover layouts, theme changes, panel
close/reopen and reload, using the supplied DAM and RTM files in a temporary lake.
The uploaded files and temporary browser captures are not added to Git.
