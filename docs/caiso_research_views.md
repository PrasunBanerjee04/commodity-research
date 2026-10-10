# CAISO research views

Launch `python server.py` from the repository root (port 8000), then open a feed
from the navigator. The existing Dockview tabs, layout presets, and singleton
behavior also apply to these views. Search filters the node/path/region dropdown;
RESET restores the default entity, filters, and one-month horizon. Date buttons
are 1D, 1W, 1M, and ALL, relative to the latest available interval.

| View | Lake directories / standalone filenames | Controls and measure |
| --- | --- | --- |
| Congestion Revenue Rights | `crr_bids`, `crr_clearing_prices`; `crr_bids.parquet` | `apnode_id`, Seasonal/Monthly `market_term`, `time_of_use`; plots `apnode_id_price`. Auction names and `xml_data_item` remain separate traces. |
| Transmission Usage | `transmission_usage`, `current_transmission_usage`, `dam_transmission_interface_usage`; `transmission_usage.parquet` | `ti_id`, `ti_direction`, `market_run_id`; overlays `USEAGE_MW` and `RATING_ATC` in MW. Other published metrics remain selectable. |
| DAM Ancillary Services | `as_req`, `dam_as_requirements`; `as_req.parquet` | `anc_region`, requirement measure, `anc_type`. Defaults to NR/RD/RU/SR and MINIMUM when available; stacks the selected types in MW. |

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

## API

- `GET /api/research/metadata?feed=crr` returns entities, dimension options,
  source timestamp bounds, units, counts, and snapshot revision.
- `GET /api/research/data?feed=transmission&entity=PATH&start=2026-10-01&end=2026-10-02`
  returns arrays per dimension combination. `feed` accepts `crr`, `transmission`,
  `ancillary`, recognized dataset aliases, and canonical CAISO paths.
- `filters` is a JSON object of dimension names to string lists, e.g.
  `{"market_run_id":["RTPD"],"measure":["USEAGE_MW","RATING_ATC"]}`.
  Empty lists select no records. Values are SQL parameters; dimensions come from
  a fixed allowlist. Unknown entities return empty results.
- `start` is inclusive and `end` exclusive. ISO timestamps may include offsets;
  calendar dates and timestamps without offsets mean UTC. `max_points` defaults
  to 1,500 per series and accepts 4–1,500, retaining bucket endpoints and extrema.
  Explicit `max_points=0` returns the complete history for one entity.

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
totals, singleton tabs, RESET, and split/window resizing. In this cloud workspace,
a separate 36-click warm-range benchmark measured maximum completion times of
39.2ms (CRR), 21.7ms (transmission), and 42.0ms (AS), with zero API requests.
These are measurements of this fixture and machine, not a hardware guarantee.
