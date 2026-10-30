"""Unified ECMWF S2S week-4 reforecast entry point for scripts and notebooks."""
from __future__ import annotations

from comm_research.infra.paths import PROJECT_ROOT
from comm_research.markets.weather.scrapers.ecmwf_s2s.pipeline import run as _run

TARGETS = PROJECT_ROOT / "data/clean/weather/era5/t_2m/socal"
OUTPUT = PROJECT_ROOT / "data/lake/weather/ecmwf/s2s/t_2m"
RAW = PROJECT_ROOT / "data/raw/weather/ecmwf/s2s"


def run(*, reference_dates, **overrides):
    """Pull selected associated issuances; use dry_run=True to inspect coverage.

    Requires .[ecmwf], an ECMWF account with S2S terms accepted, and Web API
    authentication configured externally (e.g. ~/.ecmwfapirc). Never pass secrets
    through notebook globals. Outputs retain all eleven members and model vintage
    via reference_date. The default lead is 26 elapsed days, over seven UTC days.
    """
    settings = dict(targets=TARGETS, output=OUTPUT, raw=RAW, lead_days=26, workers=2)
    settings.update(overrides)
    return _run(reference_dates=reference_dates, **settings)
