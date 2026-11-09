"""Locate the repository's existing data tree; no catalog or configuration file."""

import os
from importlib import import_module
from pathlib import Path

from .types import Area


def default_root(area: Area) -> Path:
    override = os.environ.get("COMM_RESEARCH_ROOT")
    if override:
        return Path(override).expanduser().resolve() / "data" / area
    try:
        data = import_module("comm_research.setup.paths").DATA
    except ModuleNotFoundError:
        # A standalone wheel can still locate a checkout or work from cwd/data.
        for candidate in (*Path(__file__).resolve().parents, Path.cwd(), *Path.cwd().parents):
            if (candidate / "setup/paths.py").is_file() and (
                candidate / "src/comm_research"
            ).is_dir():
                return candidate / "data" / area
        return Path.cwd() / "data" / area
    return Path(data) / area
