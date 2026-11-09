# Status

| Available | Not included |
| --- | --- |
| Automatic discovery of existing Parquet/CSV tables | Registration, DatasetSpec, schema declarations |
| Search, list, describe | Metadata database, catalog |
| Filtered Polars read/scan, half-open time bounds | SQL, automatic network fetching |
| Atomic single-file Parquet writes, append/upsert/replace | Writing ingestion-managed partitions |
| CSV/Parquet export, writer locking | Snapshot history, historical `asof` |

- Defaults to existing `data/lake/`; `area="clean"` and `area="raw"` select sibling areas.
- No files or directories are created by opening, discovery, or reads.
- Keys are optional for create/append/replace; upsert requires explicit keys.
- Append/upsert load the existing single-file output in memory and require matching dtypes.
- Lazy scans reference current file paths; later ingestion/writes can change their contents.
- No coverage or completeness claims are inferred from files or timestamp ranges.
- Read limits bound returned rows/bytes; timeout cancellation is cooperative. No hard RSS limit.
- Local files only; original specification is reference material, superseded by this local API.
