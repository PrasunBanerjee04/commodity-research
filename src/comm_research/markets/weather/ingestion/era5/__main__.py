"""Run the global temperature backfill with the existing weather CLI settings."""

from comm_research.markets.weather.scripts.era5 import main

if __name__ == "__main__":
    raise SystemExit(main())
