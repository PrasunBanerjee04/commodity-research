"""Run a focused CAISO price backfill or the incremental OASIS refresh."""
from __future__ import annotations
import logging
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from comm_research.markets.power.oasis.pipelines import backfill, oasis


def run(*, mode="backfill", **settings):
    """Backfill accepts market/nodes/start/end; refresh accepts OASIS run options."""
    logger = logging.getLogger(__name__)
    logger.info("CAISO %s configuration: %s", mode, settings)
    if mode == "backfill":
        result = backfill.fetch(**settings)
    elif mode == "refresh":
        result = oasis.run(**settings)
    else:
        raise ValueError("mode must be backfill or refresh")
    logger.info("CAISO %s finished: %s", mode, result)
    return result


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if args and args[0] == "refresh":
        # The legacy refresh parser reads sys.argv; restore it after parsing.
        previous = sys.argv
        try:
            sys.argv = [previous[0], *args[1:]]
            oasis.main()
        finally:
            sys.argv = previous
        return 0
    return backfill.main(args)


if __name__ == "__main__":
    # To run configured parameters directly, populate this mapping, for example:
    # from datetime import date
    # PARAMETERS = dict(market="DAM", nodes=["SP15"],
    #                   start=date(2026, 1, 1), end=date(2026, 1, 2))
    PARAMETERS = {}
    if PARAMETERS:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
        summary = run(**PARAMETERS)
        raise SystemExit(1 if summary.failed else 2 if getattr(summary, "no_data", 0) or getattr(summary, "incomplete", 0) else 0)
    raise SystemExit(main())
