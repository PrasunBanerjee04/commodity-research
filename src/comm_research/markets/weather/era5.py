"""Run global hourly ERA5 temperature ingestion; edit defaults here or use flags."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

# Support `python src/comm_research/markets/weather/era5.py` from any directory.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from comm_research.markets.weather.scrapers.era5_global.pipeline import (
    DEFAULT_LAKE_ROOT,
    run as _run,
)

START_DATE = date(2000, 1, 1)
END_DATE: date | None = None  # Exclusive; None uses the latest finalized UTC day.
LAKE_ROOT = DEFAULT_LAKE_ROOT
WORKERS = 4
MIN_FREE_GB = 2.0


def run(**overrides):
    """Run the configured backfill; keyword arguments override these defaults."""
    settings = dict(start=START_DATE, end=END_DATE, lake=LAKE_ROOT,
                    workers=WORKERS, min_free_gb=MIN_FREE_GB)
    settings.update(overrides)
    return _run(**settings)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=date.fromisoformat, default=START_DATE,
                        help="First UTC day, inclusive (default: 2000-01-01)")
    parser.add_argument("--end", type=date.fromisoformat, default=END_DATE,
                        help="Exclusive UTC end date (default: latest finalized complete day)")
    parser.add_argument("--lake", type=Path, default=LAKE_ROOT,
                        help="Lake root; appends weather/era5/single_levels/year=…")
    parser.add_argument("--state", type=Path, help="Override progress-manifest directory")
    parser.add_argument("--workers", type=int, default=WORKERS)
    parser.add_argument("--retries", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--min-free-gb", type=float, default=MIN_FREE_GB,
                        help="Free-space reserve in GiB (default: 2)")
    parser.add_argument("--refresh", action="store_true", help="Replace existing requested days")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        run(**vars(args))
    except KeyboardInterrupt:
        logging.getLogger(__name__).warning("Interrupted; completed days will resume on next run")
        return 130
    except (ImportError, OSError, RuntimeError, ValueError) as error:
        logging.getLogger(__name__).error("ERA5 ingestion stopped: %s", error)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
