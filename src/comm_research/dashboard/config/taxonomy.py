"""Readable labels for directory tokens, source columns and price components."""

DIRECTORY_NAMES: dict[str, str] = {
    "power_gas": "Power & Natural Gas",
    "napg": "North America Power & Gas",
    "eupg": "Europe Power & Gas",
    "ags": "Agricultural Commodities",
    "oil": "Crude & Refined Products",
    "weather": "Meteorology & Numerical Models",
    "dam_lmp": "Day-Ahead LMP",
    "da_lmp": "Day-Ahead LMP",
    "rtm_lmp": "Real-Time LMP",
    "rt_lmp": "Real-Time LMP",
    "fmm_lmp": "Fifteen-Minute LMP",
    "dam_load_forecast": "Day-Ahead Load Forecast",
    "actual_load_forecast": "Actual Load",
    "dam_wind_solar_forecast": "Day-Ahead Wind & Solar Forecast",
}
ACRONYMS = {
    "caiso",
    "aeso",
    "ercot",
    "pjm",
    "miso",
    "nyiso",
    "isone",
    "spp",
    "ieso",
    "bpa",
    "eim",
    "edam",
    "crr",
    "ghg",
    "lmp",
    "mw",
    "mwh",
    "lng",
    "noaa",
    "ecmwf",
    "gfs",
    "hrrr",
    "cme",
    "ice",
    "wti",
}
COMPONENT_NAMES = {
    "LMP": "LMP",
    "ENERGY": "Energy",
    "CONG": "Congestion",
    "LOSS": "Loss",
    "GHG": "GHG",
}
COMPONENT_ORDER = {name: index for index, name in enumerate(COMPONENT_NAMES)}
NON_MEASUREMENTS = {
    "opr_hr",
    "opr_interval",
    "interval_num",
    "interval_number",
    "year",
    "month",
    "day",
    "hour",
    "minute",
    "pos",
    "group",
}
DIMENSION_PRIORITY = (
    "node",
    "symbol",
    "ticker",
    "contract",
    "ti_id",
    "ti_constraint_id",
    "ti_direction",
    "resource_id",
    "resource_name",
    "resourcebid_seq",
    "resource_type",
    "tac_area_name",
    "tac_area",
    "tac_zone_name",
    "baa_id",
    "baa_grp_id",
    "as_region",
    "as_region_id",
    "anc_region",
    "anc_type",
    "load_type",
    "renewable_type",
    "schedule",
    "slrs_type",
    "ra_mlc_type",
    "region",
    "location",
    "station",
    "product",
    "marketproducttype",
    "productbid_desc",
    "sch_bid_curvetype",
    "market_run_id",
    "market_type",
    "tr_type",
    "scenario",
)
TIME_COLUMNS = (
    "timestamp",
    "interval_start_time_gmt",
    "intervalstarttime_gmt",
    "interval_start_gmt",
    "datetime",
    "time",
    "date",
    "opr_dt",
    "as_of_date",
)


def display_name(token: str) -> str:
    if token in DIRECTORY_NAMES:
        return DIRECTORY_NAMES[token]
    return " ".join(
        word.upper() if word.lower() in ACRONYMS else word.capitalize()
        for word in token.replace("-", "_").split("_")
        if word
    )


def metric_unit(dataset_key: str, column: str) -> str:
    if "lmp" in dataset_key.lower() or column.endswith(("_price", "_prc")):
        return "$/MWh" if "lmp" in dataset_key.lower() else "source units"
    if column == "mw" or column.endswith("_mw"):
        return "MW"
    if column == "mwh" or column.endswith("_mwh"):
        return "MWh"
    return "source units"
