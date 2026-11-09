"""Ordinary local files, with a writer lock and atomic file replacement."""

import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import polars as pl

from ..errors import ConcurrentWriteError


def sync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def writer_lock(root: Path) -> Iterator[None]:
    import fcntl

    root.mkdir(parents=True, exist_ok=True)
    # Same lock name as existing ingestion tools, so writers coordinate.
    with (root / ".writer.lock").open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ConcurrentWriteError("Another writer is using this lake; retry later") from exc
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def write_file(frame: pl.DataFrame, target: Path, *, format: str, overwrite: bool) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=target.parent, prefix=".write-", suffix=".tmp", delete=False
    ) as tmp:
        temporary = Path(tmp.name)
    try:
        if format == "parquet":
            frame.write_parquet(temporary, compression="zstd", statistics=True)
            pl.read_parquet_schema(temporary)
        else:
            frame.write_csv(temporary)
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        if overwrite:
            os.replace(temporary, target)
        else:
            os.link(temporary, target)
        sync_directory(target.parent)
    finally:
        temporary.unlink(missing_ok=True)
