# CAISO OASIS ingestion

Install with `.venv/bin/python -m pip install -e .`. Run a bounded three-day
SP15 day-ahead price sample from the repository root:

```bash
.venv/bin/python -m comm_research.markets.power_gas.napg.caiso.caiso_oasis_pipeline \
  --categories prices --datasets dam_lmp \
  --nodes TH_SP15_GEN-APND --start 2026-10-01 --end 2026-10-04
```

Or configure and call the pipeline directly:

```python
from datetime import date
from comm_research.markets.power_gas.napg.caiso import caiso_oasis_pipeline as oasis

oasis.CATEGORIES = ("prices", "system_demand")
oasis.DATASETS = ("dam_lmp", "rtm_lmp", "dam_load_forecast")
oasis.START_DATE = date(2026, 10, 1)
oasis.IGNORE_END_DATE = True
summary = oasis.run()
```

## Settings and date limits

The globals at the top of `caiso_oasis_pipeline.py` are the default configuration;
keyword arguments to `run()` override them. Importing the module does not download data.

| Setting | Meaning |
| --- | --- |
| `CATEGORIES` | Category modules to include; defaults to all eight. |
| `DATASETS` | Dataset names to include; `None` selects every defined report. See the [catalog](caiso_oasis_reports.md). |
| `START_DATE` | Inclusive date; `None` starts at each report's rolling retention floor. |
| `IGNORE_END_DATE` | `True` uses today, excluding the incomplete current day. |
| `END_DATE` | Exclusive end date when `IGNORE_END_DATE=False`. CLI `--end` sets both. |
| `RETENTION_MONTHS` | Backfill limit, default 39 calendar months, clamped to report availability. |
| `NODES` | Explicit price nodes; defaults to SP15 and NP15. |
| `REPORT_PARAMETERS` | Dataset-specific filter overrides; keys must match the report definition. |
| `RAW_ROOT`, `LAKE_ROOT`, `STATE_ROOT` | Separate staging, Parquet, and checkpoint directories. |
| `LOG_LEVEL` | Python logging level; defaults to `INFO`. |

CAISO's current portal advertises 39 months of API history. Older history, as far
back as 2016, uses its separate Historical OASIS Data Downloader. Public energy
and convergence bids are delayed 90 days; CSP offers are delayed five quarters.
CRR bid timing depends on the auction; the catalog uses a conservative 90-day
cutoff. Reports launched more recently can return no data within the retention
window. Such dates remain eligible for later attempts.

Pipeline dates are Pacific trading dates. Requests convert boundaries to UTC,
preserving 23/25-hour daylight saving days; the default end uses Pacific today.
The low-level SingleZip client also accepts arbitrary timezone-aware boundaries. Current transmission usage is a fresh current-day snapshot and
cannot be backfilled. The client limits DAM windows to at most 15 days and RTM
to one day; the incremental pipeline requests individual days for checkpoints.

CRR inventory requires a market name. Obtain it from `crr_market_names`, then set:

```python
oasis.REPORT_PARAMETERS = {"crr_inventory": {"market_name": "<CAISO market name>"}}
```

An unconfigured inventory report logs a configuration failure and the remaining
reports continue. Full defaults initiate a large backfill; use explicit datasets
and dates for a sample.

## Refresh and storage

Logs stream immediately to the terminal: report, date, URL, rate-limit waits,
received bytes, CSV staging, Parquet writes, skips, failures, and final counts.
Every outgoing request, redirect, and retry is spaced at least five seconds apart.
HTTP 429/503 and GroupZip processing responses receive bounded exponential retries.
No API key is required. Separate processes sharing an egress IP must coordinate
requests; the shared request gate applies within a process.

`run()` checks saved report/filter checkpoints, fills missing days, and fetches
new dates after the last complete day. Missing or unreadable output partitions
are repaired. Full existing LMP days can be adopted without another download;
other datasets need checkpoints to establish which filters were ingested.
No-data and failed dates are retried on subsequent runs. Partial price days are
saved but remain eligible for refresh. Completed historical days are skipped;
CAISO revisions to those days require removing their completion checkpoints
(and existing price partitions if adoption would otherwise skip them).

```text
data/raw/caiso/{queryname}/...csv
data/state/caiso_oasis/{dataset}/{filter_fingerprint}/{query_date}.json
data/lake/power_gas/napg/caiso/{dataset}/year=YYYY/month=MM/day=DD/data.parquet
```

The existing `dam_lmp` dataset path is preserved. Interval tables partition by
UTC interval-start date; date-only tables use operating date. Auction, inventory,
and public-bid snapshots have `as_of_date` and partition by query/trading date;
this is not necessarily their effective or delivery date.

Polars standardizes column names, parses UTC timestamps, types measurements, and
preserves identifiers as strings. Writes atomically replace each partition,
merge existing rows and nodes, and remove exact duplicates. Changed values remain
separate rows. A run lock prevents simultaneous writers using the same state root.

The pipeline always ends with `_clear_raw()` on CAISO staging. Successfully
persisted inputs disappear; failed inputs remain for recovery and can resume
without another download. Unknown pre-existing files are preserved. Other data
providers' raw files are untouched. `RunSummary` returns downloaded, resumed,
skipped, adopted, no-data, incomplete, failed, and failure details; CLI exits with
status 1 for failures.

## Data fields

Schemas vary by report and version. Original fields are retained using lowercase
snake_case; fields absent from a report are not invented. GMT variants such as
`INTERVALSTARTTIME_GMT`, `STARTTIME_GMT`, and `START_DATE_TIME_GMT` map to the
canonical interval columns below. Unknown columns remain strings until their
meaning/type is explicitly added to a report definition.

| Fields | Meaning / type |
| --- | --- |
| `interval_start_time_gmt`, `interval_end_time_gmt` | UTC start inclusive / end exclusive, `Datetime(us, UTC)`. |
| Other `*_gmt` fields | Publication, submission, update, or effective timestamps in UTC. |
| `opr_dt`, `opr_hr`, `opr_interval` | Pacific operating date (`Date`), hour ending and interval (`Int64`); DST can repeat an hour. These need not equal the UTC partition date. |
| `starttime`, `stoptime`, other local time fields | Original local-time labels retained as strings, distinct from GMT timestamps. |
| `as_of_date` | Query/trading date added to snapshot tables (`Date`). |
| `market_run_id`, `market_type` | Market/process: DAM day-ahead; RTM/RTD five-minute dispatch; RTPD fifteen-minute; HASP hour-ahead; RUC reliability commitment; 2DA/7DA forecasts; ACTUAL observations. |
| `node`, `node_id`, `pnode_resmrid`, `resource_id` | Pricing node/resource identifiers; strings preserve leading zeroes. |
| `xml_data_item`, `data_item`, `label`, `grp_type`, `group`, `pos` | CAISO series/component labels, grouping and display metadata; interpret with the report. |
| `mw`, `value`, `price`, `prc`, `lmp_prc`, `quantity`, `*_mw`, `*_mwh`, `*_prc`, `*_price`, `*_cost` | Numeric measurements (`Float64`). Units depend on the report: **LMP `mw`/`value` is $/MWh**, load/reserves/capacity are MW, energy is MWh. Do not infer units from the name alone. |
| `lmp_type` | LMP total; MCE energy; MCC congestion; MCL losses; MGHG greenhouse gas component, when published. Values are $/MWh. |
| `constraint_id`, `constraint_name`, `nomogram_id`, `ti_id`, `direction`, `ti_direction` | Transmission constraint/interface and flow direction. Shadow prices describe marginal constraint cost. |
| `tr_type`, `horizon`, `outage_*` | Transmission metric, planning horizon, and outage characteristics/effective period. |
| `pwt_available_capacity`, `priority_wheel_through_award`, `avail_trans_capacity`, `total_trans_capacity`, `native_load_need_capacity`, `trans_reliability_margin` | Priority wheeling capability/awards, ATC, TTC, native-load needs and reliability margin; MW. |
| `purchaser_sc_id`, `seller_sc_id`, `resale_mw`, `resale_price`, `transaction_date`, `operating_month` | PWT counterparties, resold capacity, stated transaction price, transaction date and delivery month. |
| `load_type`, `tac_area_name`, `tac_area`, `trading_hub`, `renewable_type` | Demand/renewable series and geographical region or renewable technology. |
| `baa_id`, `baa_grp_id`, `tac_zone`, `slrs_type`, `schedule_type` | Balancing authority, transmission access charge zone, and load/resource schedule classifications. |
| `adjustment_mw`, `reason` | Operator demand adjustment (MW) and reason. |
| `anc_type`, `as_region`, `as_region_id` | Ancillary service and region: RU/RD regulation up/down; SR spinning; NR non-spinning; RMU/RMD regulation mileage. Requirements/results/reserves are MW; prices depend on capacity/mileage product. |
| `market_product_type`, `marketproduct_type`, `input_parameter`, `test_results`, `status` | Energy, flexible ramp, imbalance reserve/reliability capacity or EDAM product; parameter, sufficiency-test result and tagging status. |
| `insufficient_mw`, `requirement_amount`, `supply`, `total_transfer`, `transfer_*` | EDAM sufficiency shortfall, requirement, supply and transfers; interpret MW and direction using the report/product. |
| `he01` … `he25` | Hour-ending values in wide reports (`Float64`); an extra hour can appear on fall DST days. Units follow the selected metric, e.g. MW or bid-cap $/MWh. |
| `market_name`, `market_term`, `time_of_use` | CRR auction/allocation, annual/seasonal/monthly term and on/off-peak period. |
| `source`, `sink`, `source_node`, `sink_node`, `crr_type` | CRR path endpoints and obligation/option classification. |
| `on_prc`, `off_prc`, `lt_off_prc`, `on_mw`, `off_mw`, `crr_price`, `mwquantity` | CRR time-of-use prices and capacity; price convention is auction-specific. |
| `sc_id`, `lse_id`, `lse_name`, `intertie`, `jurisdiction` | Scheduling coordinator, load-serving entity and import interface/jurisdiction. Public bid participants/resources may be anonymized. |
| `ra_period_start_date`, `month`, `submittal_type`, `contact_information` | RA reporting period/date, submission classification and allocation contact. |
| `total_imp_allocation`, `reserved_capability`, `tradable_capability`, `ra_showing_mw` | Allocated imports, reserved/tradable capability and capacity shown in RA plans; MW. |
| Bid sequence/segment identifiers, `productdesc`, `curvetype` | Product, bid curve, segment and ordering metadata. |
| `xaxisdata`, `y1axisdata`, `y2axisdata`, prefixed variants | Bid curve coordinates; commonly quantity (MW) on X and price ($/MWh) on Y, depending on curve type. |
| `selfschedmw`, `mineohstateofcharge`, `maxeohstateofcharge`, opportunity-cost fields | Self-scheduled MW, end-of-hour storage bounds and opportunity-cost inputs; units follow CAISO's product definition. |
| CSP offer fields, CPM designation fields, commitment fields | Resource/offer identifiers, capacity, offer price, award/designation period and reason; snapshot/effective dates describe different things. |

Use `pl.read_parquet(path, hive_partitioning=False).schema` to inspect the exact
columns/types for a downloaded report. The [report catalog](caiso_oasis_reports.md)
records query identifiers, versions, filters and sources. Source definitions from
2019 are a dated fallback; they can be superseded by CAISO. The runner logs rejected
API configurations and continues other reports.

## Sources and verification

- [Current OASIS portal](https://oasis.caiso.com/mrioasis/logon.do), CSV download actions inspected on 2026-10-09 for PWT, load adjustments, EDAM, CRR names, bid caps and import/CPM reports.
- [CAISO OASIS FAQ](https://www.caiso.com/documents/oasis-frequently-asked-questions.pdf), rate limits and publication delays.
- [GridStatus CAISO definitions](https://github.com/gridstatus/gridstatus/blob/main/gridstatus/caiso/caiso_constants.py), maintained endpoint versions/filters.
- [CAISO interface specification, 2019 mirror](https://github.com/energy-analytics-project/energy-dashboard/blob/master/docs/caiso/OASIS-InterfaceSpecification_v5_1_8Clean_Independent2019Release.pdf), legacy report definitions and error codes.

Run offline verification with `.venv/bin/python -m unittest discover -s tests -v`.
Live checks use bounded samples, not a full retention backfill. The current CAISO
developer portal also provides newer technical specifications after registration.
