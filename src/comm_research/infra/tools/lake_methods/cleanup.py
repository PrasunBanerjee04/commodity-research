"""Raw staging cleanup after lake persistence."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from pathlib import Path

logger = logging.getLogger(__name__)
DEFAULT_RAW_ROOT = Path(__file__).resolve().parents[5] / "data/raw"


def _clear_raw(
    raw_root: Path | str = DEFAULT_RAW_ROOT, *, keep: Iterable[Path] = ()
) -> int:
    """Empty raw_root, preserving failed inputs; never follow symlinks.

    The root directory remains. OASIS passes its own staging directory so that
    refreshing CAISO does not delete other providers' inputs.
    """
    root = Path(raw_root)
    if root.is_symlink():
        raise ValueError("Raw root must not be a symlink")
    if not root.exists():
        return 0
    protected = {Path(path).absolute() for path in keep}
    removed = 0

    def clear(folder: Path) -> None:
        nonlocal removed
        for path in folder.iterdir():
            if path.absolute() in protected:
                continue
            if path.is_dir() and not path.is_symlink():
                clear(path)
                if not any(path.iterdir()):
                    path.rmdir()
            else:
                path.unlink()
                removed += 1

    clear(root)
    logger.info(
        "Raw cleanup: removed %d file(s); retained %d protected input(s)",
        removed,
        len(protected),
    )
    return removed
