# Focused CAISO LMP backfills

From the checkout root, with Polars installed (`pip install -e .`):

```bash
python scripts/fetch_caiso.py --market RTM \
  --nodes TH_NP15_GEN-APND TH_SP15_GEN-APND \
  --start 2026-09-01 --end 2026-10-09
```

`--start` and `--end` are **inclusive Pacific trading dates**; `--end` defaults to
today in Pacific time. CAISO's rolling history is generally 39 months. Choose
published dates within that limit. RTM maps to five-minute `PRC_INTVL_LMP`, FMM to
15-minute `PRC_RTPD_LMP` (market RTPD), and DAM to hourly `PRC_LMP`. The CLI makes
one node/day request at a time, including 23/25-hour DST days, through the shared
SingleZip client with `resultformat=6`, five-second pacing and 429/503 backoff.

Results merge atomically into the existing UTC-day structure:

```text
data/lake/power_gas/napg/caiso/{dam_lmp,rtm_lmp,fmm_lmp}/
  year=YYYY/month=MM/day=DD/data.parquet
```

One Pacific day normally touches two UTC partitions. Existing nodes remain in
place; new observations replace matching timestamp/node/market/component prices.
Typed `timestamp` and standardized `component` fields accompany the source fields.
The original `lmp_type` is retained for the full pipeline's coverage checks.

The script skips a day only when every expected interval and required price
component is present for that node. Missing or partial data is fetched again.
Use `--force` to refresh completed days and apply corrected prices. Current-day
partial data remains eligible on the next run. Logs stream requests, pacing,
writing, skipping, failures and the final summary to the terminal.

`--lake PATH` changes the **CAISO root** (the directory containing `rtm_lmp`, not
`data/lake`); `--raw PATH` changes CSV staging. Successfully ingested CSVs are
removed; failures retain raw files. Re-running retries failed queries. Unrelated
raw files are preserved. The standalone CLI and full pipeline share a lake writer
lock to prevent conflicting writes.

Exit status: `0` complete/skipped, `1` failures, `2` unpublished/partial data,
`130` interrupted. An HTTP access denial stops remaining requests. No bulk fetch
runs on import. The existing category-wide pipeline remains available for reports
beyond LMPs; this command is its small ad-hoc counterpart.
