"""Run the global temperature backfill with the existing weather CLI settings."""

from comm_research.markets.weather.era5 import main

if __name__ == "__main__":
    raise SystemExit(main())
