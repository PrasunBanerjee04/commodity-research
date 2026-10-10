"""Coordinate all CAISO lake writers, including standalone backfills."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def writer_lock(lake: Path) -> Iterator[None]:
    import fcntl

    lake.mkdir(parents=True, exist_ok=True)
    with (lake / ".writer.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("Another CAISO writer is using this lake") from error
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
