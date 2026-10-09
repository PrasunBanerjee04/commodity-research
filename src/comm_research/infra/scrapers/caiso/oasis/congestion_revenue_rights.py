"""Congestion Revenue Rights reports from the OASIS menu. See docs/caiso_oasis.md for sources."""

from .models import CategoryScraper, Report

REPORTS = (
    Report(
        "CRR_CLEARING",
        None,
        1,
        1,
        "crr_clearing_prices",
        menu="CRR Clearing Prices",
        description="CRR auction clearing prices",
        source="spec_2019",
        snapshot=True,
        numeric_columns=("on_prc", "lt_off_prc"),
        parameters=(
            ("market_name", "ALL"),
            ("market_term", "ALL"),
            ("time_of_use", "ALL"),
        ),
    ),
    Report(
        "CRR_INVENTORY",
        None,
        1,
        1,
        "crr_inventory",
        menu="CRR Inventory",
        description="CRR capacity holdings; requires an explicit market name",
        required_parameters=("market_name",),
        source="spec_2019",
        snapshot=True,
        numeric_columns=("on_mw", "off_mw"),
        parameters=(
            ("market_name", ""),
            ("market_term", "ALL"),
            ("time_of_use", "ALL"),
        ),
    ),
    Report(
        "CRR_AGG_REV_ADJ",
        None,
        7,
        1,
        "crr_revenue_adjustments",
        menu="CRR Aggregated Revenue Adjustment Data",
        description="Constraint-level CRR revenue adjustment results",
        source="spec_2019",
        snapshot=True,
        parameters=(("trans_cnstr_id", "ALL"),),
    ),
    Report(
        "CRR_MRKT_NAMES",
        None,
        1,
        1,
        "crr_market_names",
        menu="CRR Market Names",
        description="crr market names",
        source="portal",
        snapshot=True,
    ),
)

SCRAPER = CategoryScraper("congestion_revenue_rights", REPORTS)
