"""Unified entry point for public GEFSv12 extended-range temperature pulls."""
from __future__ import annotations

import argparse
from datetime import date
import logging
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from comm_research.infra.paths import PROJECT_ROOT
from comm_research.markets.weather.scrapers.gefs.pipeline import run as _run

TARGETS = PROJECT_ROOT / "data/clean/weather/era5/t_2m/socal"
OUTPUT = PROJECT_ROOT / "data/lake/weather/gefs/v12/t_2m"
RAW = PROJECT_ROOT / "data/raw/weather/gefs/v12"
STATE = PROJECT_ROOT / "data/state/weather/gefs/v12/t_2m"


def run(**overrides):
    """Use dry_run=True for an archive-backed plan; False downloads all members.

    No credentials are needed. START/END refer to target-week starts (end is
    exclusive), whereas archive coverage refers to initializations in 2000-2019.
    Source samples and weekly Parquet are both retained; raw global GRIB slices
    are removed after successful processing unless keep_raw=True.
    """
    settings = dict(targets=TARGETS, output=OUTPUT, raw=RAW, state=STATE,
                    lead_days=26, workers=4, dry_run=True)
    settings.update(overrides)
    return _run(**settings)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name, default in (("targets", TARGETS), ("output", OUTPUT), ("raw", RAW), ("state", STATE)):
        parser.add_argument(f"--{name}", type=Path, default=default)
    parser.add_argument("--start", type=date.fromisoformat)
    parser.add_argument("--end", type=date.fromisoformat)
    parser.add_argument("--lead-days", type=int, default=26)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--min-free-gb", type=float, default=2)
    parser.add_argument("--download", action="store_true", help="Download fields; default is a metadata-only plan")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--refresh-catalogue", action="store_true")
    parser.add_argument("--keep-raw", action="store_true")
    parser.add_argument("--on-incomplete", choices=("raise", "record"), default="raise",
                        help="Stop on incomplete source data, or record the whole week as unavailable and continue")
    args = vars(parser.parse_args(argv))
    args["dry_run"] = not args.pop("download")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        print(run(**args))
    except (OSError, ValueError, RuntimeError, ImportError) as error:
        logging.getLogger(__name__).error("GEFS stopped: %s", error)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
