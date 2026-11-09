"""Unified ECMWF S2S week-4 reforecast entry point for scripts and notebooks."""
from __future__ import annotations

import argparse
from datetime import date
import logging
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from comm_research.setup.paths import PROJECT_ROOT
from comm_research.markets.weather.ingestion.ecmwf_s2s.pipeline import run as _run
from comm_research.markets.weather.ingestion.ecmwf_s2s.catalogue import setup_iri_auth

TARGETS = PROJECT_ROOT / "data/clean/weather/era5/t_2m/socal"
OUTPUT = PROJECT_ROOT / "data/lake/weather/ecmwf/s2s/t_2m"
RAW = PROJECT_ROOT / "data/raw/weather/ecmwf/s2s"


def run(*, reference_dates=None, **overrides):
    """Discover archive issuances and select one reforecast per target week.

    reference_dates=None discovers IRI's published control/perturbed issuance
    axes. An explicit list bypasses discovery. dry_run=True returns a detailed
    plan without downloading forecast fields. as_of caps associated issuance
    dates; selection is retrospective (newest eligible model vintage).


    Requires .[ecmwf], an ECMWF account with S2S terms accepted, and Web API
    authentication configured externally (e.g. ~/.ecmwfapirc). Never pass secrets
    through notebook globals. Outputs retain all eleven members and model vintage
    via reference_date. The default lead is 26 elapsed days, over seven UTC days.
    """
    settings = dict(targets=TARGETS, output=OUTPUT, raw=RAW, lead_days=26, workers=2)
    settings.update(overrides)
    return _run(reference_dates=reference_dates, **settings)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verbose", action="store_true", help="Include detailed diagnostic logging")
    for name, default in (("targets", TARGETS), ("output", OUTPUT), ("raw", RAW)):
        parser.add_argument("--" + name, type=Path, default=default)
    parser.add_argument("--reference-dates", nargs="+", type=date.fromisoformat)
    parser.add_argument("--start", type=date.fromisoformat)
    parser.add_argument("--end", type=date.fromisoformat)
    parser.add_argument("--as-of", type=date.fromisoformat)
    parser.add_argument("--lead-days", type=int, default=26)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--download", action="store_true", help="Download fields; default is a metadata-only plan")
    args = vars(parser.parse_args(argv))
    verbose = args.pop("verbose")
    args["dry_run"] = not args.pop("download")
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logger = logging.getLogger(__name__)
    logger.info("ECMWF configuration: %s", args)
    try:
        logger.info("ECMWF result: %s", run(**args))
    except KeyboardInterrupt:
        logger.warning("Interrupted; completed files remain available for resume")
        return 130
    except (ImportError, OSError, RuntimeError, ValueError) as error:
        logger.exception("ECMWF stopped: %s", error)
        return 1
    return 0


if __name__ == "__main__":
    # Edit TARGETS, OUTPUT, and RAW above, or supply command-line overrides.
    raise SystemExit(main())
