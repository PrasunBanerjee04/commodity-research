"""Stable checkout paths, independent of market module nesting."""

import os
from pathlib import Path


def project_root() -> Path:
    override = os.environ.get("COMM_RESEARCH_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    # Editable installs resolve to the checkout. Wheels can locate a checkout
    # from the working directory; explicit CLI --lake/--raw options still win.
    here = Path(__file__).resolve()
    cwd = Path.cwd().resolve()
    for candidate in (*here.parents, cwd, *cwd.parents):
        if (candidate / "pyproject.toml").is_file() and (
            candidate / "src/comm_research"
        ).is_dir():
            return candidate
    return cwd


PROJECT_ROOT = project_root()
