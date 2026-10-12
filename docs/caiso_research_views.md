# CAISO research views

Launch `python server.py` from the repository root (port 8000), then open a feed
from the navigator. The existing Dockview tabs, layout presets, and singleton
behavior also apply to these views. Search filters the node/path/region dropdown;
RESET restores the default entity, filters, and one-month horizon. Date buttons
are 1D, 1W, 1M, and ALL, relative to the latest available interval.
Drag a rectangle to zoom time and value axes; wheel zoom uses cached history.
Double-click restores the full history. RESET also restores default filters.

| View | Lake directories / standalone filenames | Controls and measure |
| --- | --- | --- |
| Congestion Revenue Rights | `crr_bids`, `crr_clearing_prices`; `crr_bids.parquet` | `apnode_id`, Seasonal/Monthly `market_term`, `time_of_use`; plots `apnode_id_price`. Auction names and `xml_data_item` remain separate traces. |
| Transmission Usage | `transmission_usage`, `current_transmission_usage`, `dam_transmission_interface_usage`; `transmission_usage.parquet` | `ti_id`, `ti_direction`, `market_run_id`; overlays `USEAGE_MW` and `RATING_ATC` in MW. Other published metrics remain selectable. |
| DAM Ancillary Services | `as_req`, `dam_as_requirements`; `as_req.parquet` | `anc_region`, requirement measure, `anc_type`. Defaults to NR/RD/RU/SR and MINIMUM when available; stacks the selected types in MW. |
| Transmission Outages | `transmission_outages`, `transmission_outage`, `trns_outage` | Searchable `ti_id`, import/export `ti_direction`, record status. Horizontal segments span each event's GMT start/end and show `curtailed_otc_mw` (curtailed line rating in MW). |

Partitioned directories live under `data/lake/power_gas/napg/caiso/`. Standalone
files can live there or directly under the lake root. Empty feeds have explicit
empty states. Matching filenames must contain their stated schemas.

Tables materialize into the same DuckDB RAM snapshot at startup and explicit
RESCAN; no interactive research query reads Parquet. Each table has entity/time
and timestamp indexes. Failed rescans retain the prior snapshot. The existing
server memory limit applies to these tables too; disk spill remains disabled.

All views use `interval_start_time_gmt` as UTC time. Prices/quantities are typed
doubles, nonfinite values remain null. Repeated timestamp/dimension observations
use the latest `as_of_date` when supplied; ties use the first loaded value, which
is not evidence of a later revision. CRR prices retain CAISO's auction convention,
not an assumed LMP $/MWh unit. Transmission recognizes both the published
`USEAGE_MW` spelling and `USAGE_MW`; `ATC_MW` / `RATING_ATC` normalize to capacity.
AS queries exclude RTM and keep minimum and maximum requirement records separate.
Their selector chooses one measure before stacking; tooltip values show each
service's original MW rather than cumulative boundaries. Missing quantities
remain gaps, not invented zeros.

## Transmission outage windows

TRNS_OUTAGE describes event intervals, unlike TRNS_USAGE's continuous usage and
capacity series. The outage view accepts the published uppercase CSV-style
Parquet columns or their normalized lowercase equivalents, including
`interval_start_time_gmt`, `interval_end_time_gmt`, `curtailed_otc_mw`, `ti_id`,
`ti_direction`, `equipment_outage`, `outage_notes`, `upd_date_gmt`, and
`oasis_rec_stat`. The MW field also accepts `outage_limit_mw` or `mw`.

Each event draws its own horizontal segment with square endpoints. Events with
the same start time remain separate, and gaps between event windows are not
connected. MW values are never summed or averaged into apparent path capacity.
The hover shows original start/end, equipment ID, notes, and update time; clipped
segments retain their original event boundaries in hover. Import uses blue and
export uses teal. Status `V` is selected initially when present; other published
statuses remain selectable without inferring their meanings.

Repeated equipment/start/end/direction records prefer the newest `upd_date_gmt`,
then `as_of_date`. Different effective windows remain separate. Revisions are
resolved before status filtering, so an older V record cannot reappear after a
newer status change for that same window. Missing/invalid start/end or reversed
windows are excluded; missing MW stays missing. A record without an equipment ID
uses its notes as the identity fallback.

1D/1W/1M show upcoming windows from the current UTC day when that falls inside
the lake's event extent. For past-only data they anchor to the last event end;
future-only data starts at the first available event. ALL spans start through
end, including long-duration events. Queries select **overlapping intervals**:
an event that started before the requested range still appears while active.
The panel displays at most 500 events per trace (1,500 vertices including gaps).
If more match, a ribbon notice asks you to narrow the range; representative
events retain their actual values and are not an aggregate or complete list.

## API

- `GET /api/research/metadata?feed=crr` returns entities, dimension options,
  source timestamp bounds, units, counts, and snapshot revision.
- `GET /api/research/data?feed=transmission&entity=PATH&start=2026-10-01&end=2026-10-02`
  returns arrays per dimension combination. `feed` accepts `crr`, `transmission`,
  `ancillary`, `outages`, recognized dataset aliases, and canonical CAISO paths.
- `filters` is a JSON object of dimension names to string lists, e.g.
  `{"market_run_id":["RTPD"],"measure":["USEAGE_MW","RATING_ATC"]}`.
  Empty lists select no records. Values are SQL parameters; dimensions come from
  a fixed allowlist. Unknown entities return empty results.
- `start` is inclusive and `end` exclusive. ISO timestamps may include offsets;
  calendar dates and timestamps without offsets mean UTC. `max_points` defaults
  to 1,500 per series and accepts 4–1,500, retaining bucket endpoints and extrema.
  Explicit `max_points=0` returns the complete history for one entity.
- For `outages`, responses contain `kind="intervals"` and per-trace `events`
  with `start`, `end`, `value`, `equipment`, `notes`, `auditType`, and `updatedAt`.
  Filters accept `ti_direction`, `record_status`, and `measure`.
  A bounded response retains at most `floor(max_points/3)` events per trace and
  supplies `sourceCount`; the browser explicitly fetches complete entity history.

The browser downloads each entity's complete history once and prepares local
min/max indexes. Range changes, dimensional toggles, legend visibility, and zoom
slice that cache; changing to an uncached entity makes one request. Display
traces have at most 1,500 vertices, and AS stacks share a bounded time grid.
RESCAN invalidates these caches. Cold transfers and GL initialization have a
separate cost from warm interactions; response times depend on dataset size and
hardware. These feeds use locally pinned Plotly 2.35.2 `scattergl` traces, docked
square legends, and shared terminal styling. Existing LMP views retain uPlot.

## Validation

Backend and Chromium checks cover the supplied schemas, date/dimension filters,
point budgets, latest CRR snapshots, minimum/maximum AS separation, source-file
deletion after preload, rescan rollback, WebGL traces, local controls, stacked MW
totals, singleton tabs, RESET, and split/window resizing. Outage checks additionally
cover coincident events, latest revisions, overlapping date windows, status
changes, future-only ranges, and time/value zoom persistence. A live TRNS_OUTAGE
sample with 3,865 rows across 23 paths was normalized into 434 UTC Hive partitions
in a temporary lake and rendered without browser errors. In this cloud workspace,
a separate 36-click warm-range benchmark measured maximum completion times of
39.2ms (CRR), 21.7ms (transmission), and 42.0ms (AS), with zero API requests.
These are measurements of this fixture and machine, not a hardware guarantee.
