"""CAISO OASIS category scrapers and the shared rate-limited client."""

from . import (
    ancillary_services,
    congestion_revenue_rights,
    energy,
    prices,
    public_bids,
    resource_adequacy,
    system_demand,
    transmission,
)
from .client import CAISOOASISClient, NoDataError, OASISError
from .models import CategoryScraper, Report

__all__ = [
    "SCRAPERS",
    "CAISOOASISClient",
    "CategoryScraper",
    "NoDataError",
    "OASISError",
    "Report",
]

SCRAPERS = {
    module.SCRAPER.name: module.SCRAPER
    for module in (
        prices,
        transmission,
        system_demand,
        energy,
        ancillary_services,
        congestion_revenue_rights,
        public_bids,
        resource_adequacy,
    )
}
