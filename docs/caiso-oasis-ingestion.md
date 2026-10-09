# CAISO OASIS Ingestion

Install the package in a virtual environment, then run the executable scraper
from the project root to resume DAM LMP ingestion for the SP15 and NP15 nodes:

```bash
./src/comm_research/infra/scrapers/caiso_oasis.py
```

With no arguments, it resumes each node independently from its latest saved
trade date (re-fetching that date), or starts at 2025-01-01 if that node has no
data, through today. Existing dates are skipped, missing dates are filled, and
the latest stored date is refreshed. Supplying `--start YYYY-MM-DD` and
optionally `--end YYYY-MM-DD` runs the same gap-aware behavior over that
inclusive date range; for example, `--start 2025-01-01 --end 2025-01-03`
requests January 1 through January 3. Dates are CAISO Pacific trade dates; the
API boundaries are converted to UTC using the applicable daylight-saving
offset. SingleZip supports a rolling 39-month history window; older dates
require CAISO's bulk archive rather than this endpoint. Use `--node NODE` to
select one node, or repeat `--node` to select several. The raw client queries
the SingleZip endpoint with report-specific versions (`PRC_LMP` version 12) and
`resultformat=6`, validates the in-memory ZIP, and stages CSVs under
`data/raw/caiso/{queryname}/`. The Polars normalizer writes UTC
interval-start-date partitions under
`data/lake/power_gas/napg/caiso/dam_lmp/year=YYYY/month=MM/day=DD/data.parquet`.
Raw CSVs are deleted only after all requested partitions are written.

For other reports, use `CAISOOASISClient.download()` with `DAM_LMP`, `RTM_LMP`, or
`DAM_LOAD_FORECAST`, then pass its returned paths and `report.dataset_name` to
`normalize_csvs()`. Query boundaries must be timezone-aware with minute
precision; the end is exclusive. RTM requests use one-day chunks; DAM uses at
most 15 days. LMP downloads require an explicit node. Timestamp columns become
`interval_start_time_gmt` and `interval_end_time_gmt`, typed as UTC datetimes;
known numeric fields are explicitly typed, and identifiers stay strings.

Requests, including retries and redirects, are serialized and spaced at least
five seconds apart across clients in one process. HTTP 429/503 responses receive
bounded exponential retries. Separate processes sharing an IP must coordinate
requests externally. Network access to `oasis.caiso.com` is required; no API key
is used. CAISO's explicit `ERR_CODE=1000` no-data response is logged and that
date window is split into smaller queries until any available daily data is
found. Truly empty daily windows are logged and skipped; malformed reports and
other API errors stop the run.

Use a single normalizer writer per dataset. Existing rows are preserved and
exact duplicates removed on repeat ingestion; changed values are retained as
distinct rows. Partition replacement is atomic, but a multi-day batch is not a
transaction. On a failure, raw files remain available for retry; earlier
successful download chunks also remain staged if a later request fails.

`run_backfill()` implements the default resume behavior and can also be called
from Python code.

Run the offline tests (including simulated ZIP-to-Parquet ingestion) with:

```bash
.venv/bin/python -m unittest discover -s tests -v
```
