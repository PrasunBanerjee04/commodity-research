#!/usr/bin/env python3
"""Checkout entrypoint for the market-owned CAISO backfill implementation."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from comm_research.markets.power_gas.napg.caiso.pipelines import backfill

if __name__ == "__main__":
    raise SystemExit(backfill.main())
else:
    sys.modules[__name__] = backfill
