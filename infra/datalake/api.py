"""Direct access to the repository's existing Parquet and CSV tables."""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4
from zoneinfo import ZoneInfo

import polars as pl

from .config import default_root
from .errors import (
    DatasetNotFoundError,
    DataValidationError,
    InvalidQueryError,
    InvalidTimeError,
    QueryLimitExceededError,
)
from .query import columns_exist, positive_limit, predicate, query_bounds, time_expression
from .storage.local_store import write_file, writer_lock
from .types import Area, DatasetInfo, Filters, TimestampInput, WriteMode

if TYPE_CHECKING:
    import pandas as pd
    import pyarrow as pa

LOG = logging.getLogger("datalake")
FORMATS = {".parquet": "parquet", ".csv": "csv"}


def _walk(root: Path) -> list[Path]:
    """Skip audit, state, temporary and symlink directories by default."""
    if not root.is_dir():
        return []
    files = []
    for parent, directories, names in os.walk(root, followlinks=False):
        directories[:] = sorted(
            name
            for name in directories
            if not name.startswith((".", "_")) and not (Path(parent) / name).is_symlink()
        )
        for name in sorted(names):
            path = Path(parent) / name
            if (
                not name.startswith((".", "_"))
                and path.suffix.lower() in FORMATS
                and not path.is_symlink()
            ):
                files.append(path)
    return sorted(files)


class DataLake:
    """Read existing data by its relative name, e.g. research/features."""

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        area: Area = "lake",
        max_rows: int = 1_000_000,
        max_bytes: int = 250_000_000,
        timeout: int = 60,
    ):
        if area not in ("lake", "clean", "raw"):
            raise InvalidQueryError("area must be lake, clean, or raw")
        for name, value in (("max_rows", max_rows), ("max_bytes", max_bytes), ("timeout", timeout)):
            positive_limit(value, name)
        self.root = (Path(root).expanduser() if root is not None else default_root(area)).resolve()
        if self.root.exists() and not self.root.is_dir():
            raise InvalidQueryError("root must be a directory")
        self.max_rows, self.max_bytes, self.timeout = max_rows, max_bytes, timeout
        self._closed = False

    @classmethod
    def open(
        cls,
        root: str | Path | None = None,
        *,
        area: Area = "lake",
        max_rows: int = 1_000_000,
        max_bytes: int = 250_000_000,
        timeout: int = 60,
    ) -> DataLake:
        return cls(root, area=area, max_rows=max_rows, max_bytes=max_bytes, timeout=timeout)

    def __enter__(self) -> DataLake:
        self._check_open()
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        self._closed = True

    def _check_open(self) -> None:
        if self._closed:
            raise InvalidQueryError("DataLake is closed; create a new DataLake() to reopen")

    def _path(self, name: str) -> Path:
        self._check_open()
        if not isinstance(name, str) or not name.strip() or "\\" in name or "\x00" in name:
            raise InvalidQueryError("Use a relative dataset name, e.g. research/features")
        relative = Path(name)
        if relative.is_absolute() or any(part in (".", "..") for part in relative.parts):
            raise InvalidQueryError("Dataset names must stay inside the data root")
        target = (self.root / relative).resolve()
        if target == self.root or not target.is_relative_to(self.root):
            raise InvalidQueryError("Dataset names must identify a table inside the data root")
        return target

    def _files(self, name: str) -> list[Path]:
        path = self._path(name)
        files = [path] if path.is_file() and path.suffix.lower() in FORMATS else _walk(path)
        if not files:
            raise DatasetNotFoundError(
                f"No Parquet or CSV files for {name!r}", details={"dataset": name}
            )
        formats = {p.suffix.lower() for p in files}
        if len(formats) != 1:
            raise InvalidQueryError(
                f"{name!r} contains both CSV and Parquet; select a narrower dataset or file"
            )
        return files

    def list_datasets(
        self, *, prefix: str | None = None, limit: int = 100, offset: int = 0
    ) -> list[str]:
        self._check_open()
        positive_limit(limit)
        if type(offset) is not int or offset < 0:
            raise InvalidQueryError("offset must be a nonnegative integer")
        if prefix is not None:
            self._path(prefix)
        found = set()
        for file in _walk(self.root):
            relative = file.relative_to(self.root)
            parent = relative.parts[:-1]
            boundary = next((i for i, part in enumerate(parent) if "=" in part), len(parent))
            name = "/".join(parent[:boundary]) or relative.as_posix()
            if prefix is None or name == prefix or name.startswith(prefix.rstrip("/") + "/"):
                found.add(name)
        return sorted(found)[offset : offset + limit]

    def search(
        self, text: str = "", *, prefix: str | None = None, limit: int = 100, offset: int = 0
    ) -> list[str]:
        positive_limit(limit)
        if type(offset) is not int or offset < 0:
            raise InvalidQueryError("offset must be a nonnegative integer")
        # Discovery needs only directory entries, never table data or a catalog.
        names = self.list_datasets(prefix=prefix, limit=2**63 - 1)
        tokens = text.casefold().replace("/", " ").split()
        return [name for name in names if all(token in name.casefold() for token in tokens)][
            offset : offset + limit
        ]

    @staticmethod
    def _scan_files(files: list[Path]) -> pl.LazyFrame:
        if files[0].suffix.lower() == ".parquet":
            return pl.scan_parquet(files, hive_partitioning=True)
        return pl.scan_csv(files, try_parse_dates=True, infer_schema_length=10_000)

    def describe(self, dataset: str, *, count_rows: bool = False) -> DatasetInfo:
        files = self._files(dataset)
        lazy = self._scan_files(files)
        schema = lazy.collect_schema()
        rows = int(lazy.select(pl.len()).collect().item()) if count_rows else None
        return DatasetInfo(
            name=dataset,
            format=FORMATS[files[0].suffix.lower()],
            files=len(files),
            size_bytes=sum(path.stat().st_size for path in files),
            columns={name: str(dtype) for name, dtype in schema.items()},
            rows=rows,
        )

    def scan(
        self,
        dataset: str,
        *,
        start: TimestampInput | None = None,
        end: TimestampInput | None = None,
        columns: Sequence[str] | None = None,
        filters: Filters | None = None,
        time_column: str | None = None,
        timezone: str | None = None,
        order_by: Sequence[str] | None = None,
    ) -> pl.LazyFrame:
        # Alias keeps the public time_column argument concise.
        from .query import time_column as infer_time_column

        if timezone is not None:
            try:
                ZoneInfo(timezone)
            except (KeyError, ValueError) as exc:
                raise InvalidTimeError(f"Unknown timezone {timezone!r}") from exc
        left, right = query_bounds(start, end, timezone)
        lazy = self._scan_files(self._files(dataset))
        schema = lazy.collect_schema()
        columns_exist(schema, columns, "columns")
        columns_exist(schema, order_by, "order_by")
        temporal = infer_time_column(
            schema, time_column, required=start is not None or end is not None
        )
        expression = predicate(schema, filters, timezone)
        if temporal is not None and (left is not None or right is not None):
            event_time = time_expression(schema, temporal, timezone)
            if left is not None:
                expression &= event_time >= pl.lit(left)
            if right is not None:
                expression &= event_time < pl.lit(right)
        lazy = lazy.filter(expression)
        if order_by is not None:
            lazy = lazy.sort(list(order_by))
        elif temporal is not None:
            lazy = lazy.sort(temporal, maintain_order=True)
        if columns is not None:
            lazy = lazy.select(list(columns))
        return lazy

    def _collect(self, lazy: pl.LazyFrame, limit: int | None) -> pl.DataFrame:
        cap = min(self.max_rows, limit) if limit is not None else self.max_rows
        handle = lazy.limit(cap + 1).collect(background=True)
        deadline = time.monotonic() + self.timeout
        try:
            while True:
                result = handle.fetch()
                if result is not None:
                    break
                if time.monotonic() >= deadline:
                    handle.cancel()
                    raise QueryLimitExceededError(
                        "Read timed out; use scan() for a caller-managed lazy query"
                    )
                time.sleep(0.005)
        except pl.exceptions.ComputeError as exc:
            if "ambiguous" in str(exc).lower() or "non-existent" in str(exc).lower():
                raise InvalidTimeError(
                    "Ambiguous/nonexistent local timestamps; normalize them explicitly"
                ) from exc
            raise
        if result.height > cap:
            if limit is None or limit > self.max_rows:
                raise QueryLimitExceededError(
                    "Read exceeds max_rows; narrow the filters, pass limit=..., or use scan()"
                )
            result = result.head(limit)
        if result.estimated_size() > self.max_bytes:
            raise QueryLimitExceededError(
                "Read exceeds max_bytes; narrow the columns/filters or use scan()"
            )
        return result

    def read(
        self,
        dataset: str,
        *,
        start: TimestampInput | None = None,
        end: TimestampInput | None = None,
        columns: Sequence[str] | None = None,
        filters: Filters | None = None,
        time_column: str | None = None,
        timezone: str | None = None,
        order_by: Sequence[str] | None = None,
        limit: int | None = None,
    ) -> pl.DataFrame:
        positive_limit(limit)
        began = time.perf_counter()
        query_id = uuid4().hex
        try:
            lazy = self.scan(
                dataset,
                start=start,
                end=end,
                columns=columns,
                filters=filters,
                time_column=time_column,
                timezone=timezone,
                order_by=order_by,
            )
            result = self._collect(lazy, limit)
            LOG.info(
                "read dataset=%s query=%s rows=%d elapsed_ms=%.2f",
                dataset,
                query_id,
                result.height,
                (time.perf_counter() - began) * 1000,
            )
            return result
        except Exception as exc:
            LOG.warning(
                "read failed dataset=%s query=%s error=%s", dataset, query_id, type(exc).__name__
            )
            raise

    def write(
        self,
        dataset: str,
        frame: pl.DataFrame | pa.Table | pd.DataFrame,
        *,
        mode: WriteMode = "create",
        keys: Sequence[str] | None = None,
    ) -> Path:
        """Write ordinary single-file Parquet outputs; never flatten source partitions."""
        path = self._path(dataset)
        if mode not in ("create", "append", "upsert", "replace"):
            raise InvalidQueryError("mode must be create, append, upsert, or replace")
        if any("=" in part or part.startswith((".", "_")) for part in Path(dataset).parts):
            raise InvalidQueryError(
                "Write research outputs to a new dataset; source partitions belong to ingestion scripts"
            )
        if path.suffix.lower() in FORMATS:
            if path.suffix.lower() != ".parquet":
                raise InvalidQueryError(
                    "write() stores Parquet; use export(..., format='csv') for CSV"
                )
            target = path
        elif path.suffix:
            raise InvalidQueryError("Use a dataset directory or a .parquet filename")
        else:
            target = path / "data.parquet"
        if not isinstance(frame, pl.DataFrame):
            try:
                import pyarrow as arrow
            except ImportError:
                arrow = None
            if arrow is not None and isinstance(frame, arrow.Table):
                frame = pl.from_arrow(frame)
            else:
                try:
                    import pandas as pandas_runtime
                except ImportError as exc:
                    raise DataValidationError(
                        "Pass a Polars DataFrame, or install datalake[pandas] for pandas input"
                    ) from exc
                if not isinstance(frame, pandas_runtime.DataFrame):
                    raise DataValidationError("write() expects a Polars, Arrow, or pandas table")
                frame = pl.from_pandas(frame)
        assert isinstance(frame, pl.DataFrame)
        if not frame.columns:
            raise DataValidationError("Cannot write a table without columns")
        if keys is not None:
            columns_exist(frame.schema, keys, "keys")
            if (
                any(frame[key].null_count() for key in keys)
                or frame.select(list(keys)).is_duplicated().any()
            ):
                raise DataValidationError(
                    "Key columns must be non-null and unique in the incoming table"
                )
        if mode == "upsert" and keys is None:
            raise InvalidQueryError("upsert requires keys=[...] for matching existing rows")
        if target.is_symlink() or not target.resolve().is_relative_to(self.root):
            raise InvalidQueryError(
                "Write target must stay inside the lake and cannot be a symlink"
            )
        with writer_lock(self.root):
            # Refuse parent/child table overlaps and partitioned-table replacement.
            for parent in path.parents:
                if parent == self.root:
                    break
                if parent.exists() and any(
                    p.suffix.lower() in FORMATS and p != path
                    for p in parent.iterdir()
                    if p.is_file()
                ):
                    raise InvalidQueryError("Cannot nest a dataset inside another table")
                if parent.exists() and any(p.is_dir() and "=" in p.name for p in parent.iterdir()):
                    raise InvalidQueryError(
                        "Cannot write inside an ingestion-managed partitioned dataset"
                    )
            files = _walk(path) if path.is_dir() else ([path] if path.is_file() else [])
            if files:
                if (
                    path.is_dir()
                    and len(files) == 1
                    and files[0].parent == path
                    and files[0].suffix.lower() == ".parquet"
                ):
                    target = files[0]
                elif len(files) != 1 or files[0] != target or target.suffix.lower() != ".parquet":
                    raise InvalidQueryError(
                        "Writes support single-file Parquet outputs; use ingestion scripts for partitioned or multi-file datasets"
                    )
            if target.is_symlink() or not target.resolve().is_relative_to(self.root):
                raise InvalidQueryError("Write target must stay inside the lake")
            exists = target.exists()
            if mode == "create" and exists:
                raise FileExistsError(
                    f"Dataset {dataset!r} already exists; choose append, upsert, or replace"
                )
            if exists and mode in ("append", "upsert"):
                old = pl.read_parquet(target)
                if old.schema != frame.schema:
                    raise DataValidationError(
                        "Append/upsert columns and dtypes must match existing data"
                    )
                combined = pl.concat([old, frame])
                if keys is not None:
                    if (
                        any(old[key].null_count() for key in keys)
                        or old.select(list(keys)).is_duplicated().any()
                    ):
                        raise DataValidationError("Existing table has null/duplicate keys")
                    if mode == "append" and combined.select(list(keys)).is_duplicated().any():
                        raise DataValidationError(
                            "Append would duplicate existing keys; use upsert"
                        )
                    if mode == "upsert":
                        combined = combined.unique(
                            subset=list(keys), keep="last", maintain_order=True
                        )
                frame = combined
            write_file(frame, target, format="parquet", overwrite=exists)
        LOG.info("write dataset=%s mode=%s rows=%d", dataset, mode, frame.height)
        return target

    def export(
        self,
        dataset: str,
        *,
        destination: str | Path,
        format: str | None = None,
        start: TimestampInput | None = None,
        end: TimestampInput | None = None,
        columns: Sequence[str] | None = None,
        filters: Filters | None = None,
        time_column: str | None = None,
        timezone: str | None = None,
        order_by: Sequence[str] | None = None,
        limit: int | None = None,
    ) -> Path:
        self._check_open()
        target = Path(destination).expanduser().absolute()
        fmt = format or FORMATS.get(target.suffix.lower())
        if fmt not in ("parquet", "csv"):
            raise InvalidQueryError(
                "Choose format='parquet' or 'csv', or use a matching filename extension"
            )
        if target.exists() or target.is_symlink():
            raise FileExistsError(f"Export destination already exists: {target}")
        if target.resolve().is_relative_to(self.root):
            raise InvalidQueryError("Use write() for lake outputs; export outside the lake root")
        result = self.read(
            dataset,
            start=start,
            end=end,
            columns=columns,
            filters=filters,
            time_column=time_column,
            timezone=timezone,
            order_by=order_by,
            limit=limit,
        )
        write_file(result, target, format=fmt, overwrite=False)
        return target
