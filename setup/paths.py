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
        if (candidate / "setup/pyproject.toml").is_file() and (
            candidate / "src/comm_research"
        ).is_dir():
            return candidate
    return cwd


PROJECT_ROOT = project_root()

SETUP = PROJECT_ROOT / "setup"
CONFIG = SETUP / "config"
DOCS = SETUP / "docs"
ASSETS = SETUP / "assets"
ENV = SETUP / "env"
INFRA = PROJECT_ROOT / "infra"
SRC = PROJECT_ROOT / "src"
MARKETS = SRC / "comm_research/markets"
NOTEBOOKS = PROJECT_ROOT / "notebooks"
DATA = PROJECT_ROOT / "data"
RAW = DATA / "raw"
LAKE = DATA / "lake"
CLEAN = DATA / "clean"
STATE = DATA / "state"


def data_path(*parts: str) -> Path:
    """Resolve a dataset relative to data/, without changing its layout."""
    return DATA.joinpath(*parts)
