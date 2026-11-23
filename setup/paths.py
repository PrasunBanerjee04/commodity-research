"""Canonical paths for the CommodityResearch repository."""

import os
from pathlib import Path

SETUP_DIR = Path(__file__).resolve().parent
ROOT_DIR = SETUP_DIR.parent

DATA_DIR = Path(os.environ.get("DATA_DIR", ROOT_DIR / "data")).expanduser().resolve()
INFRA_DIR = ROOT_DIR / "infra"
DAGS_DIR = INFRA_DIR / "dags"
GAS_DAGS_DIR = DAGS_DIR / "gas"
WEATHER_DAGS_DIR = DAGS_DIR / "weather"
DATABASE_DIR = INFRA_DIR / "database"
PIPELINE_DIR = INFRA_DIR / "pipeline"
INGESTION_DIR = PIPELINE_DIR / "ingestion"
STORAGE_DIR = PIPELINE_DIR / "storage"
TRANSFORM_DIR = PIPELINE_DIR / "transform"

NOTEBOOKS_DIR = ROOT_DIR / "notebooks"
SRC_DIR = ROOT_DIR / "src"
COMM_RESEARCH_DIR = SRC_DIR / "comm_research"

ASSETS_DIR = SETUP_DIR / "assets"
MARKET_ASSETS_DIR = ASSETS_DIR / "markets"
CONFIG_DIR = SETUP_DIR / "config"
DOCS_DIR = SETUP_DIR / "docs"
ENV_DIR = SETUP_DIR / "env"
