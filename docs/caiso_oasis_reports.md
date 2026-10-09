# OASIS report catalog

Dataset names are the `DATASETS` selectors and lake directory names. Each category
is a separate module under `infra/scrapers/caiso/oasis/`. Sources are linked in the
[usage guide](caiso_oasis.md): `portal` = current CSV action or observed API redirect;
`gridstatus` = maintained definitions; `spec_2019` = dated interface specification.
Only bounded representative samples have been exercised live, not every report/date.

## prices

| Dataset / menu | API identifier (version) | Market / filters | Availability / partition | Source |
| --- | --- | --- | --- | --- |
| `dam_lmp` — Energy Prices | SingleZip: `PRC_LMP` (v12) | DAM; node=NODES | 39 months; interval/operating date | gridstatus |
| `rtm_lmp` — Energy Prices | SingleZip: `PRC_INTVL_LMP` (v3) | RTM; node=NODES | 39 months; interval/operating date | gridstatus |
| `fmm_lmp` — Energy Prices | SingleZip: `PRC_RTPD_LMP` (v3) | RTPD; node=NODES | 39 months; interval/operating date | gridstatus |
| `hasp_lmp` — Energy Prices | SingleZip: `PRC_HASP_LMP` (v3) | node=NODES | 39 months; interval/operating date | gridstatus |
| `dam_scheduling_point_lmp` — Energy Prices | SingleZip: `PRC_SPTIE_LMP` (v5) | DAM; node=NODES | 39 months; interval/operating date | gridstatus |
| `rtd_scheduling_point_lmp` — Energy Prices | SingleZip: `PRC_SPTIE_LMP` (v5) | RTD; node=NODES | 39 months; interval/operating date | gridstatus |
| `rtpd_scheduling_point_lmp` — Energy Prices | SingleZip: `PRC_SPTIE_LMP` (v5) | RTPD; node=NODES | 39 months; interval/operating date | gridstatus |
| `dam_intertie_shadow_prices` — Shadow Prices | SingleZip: `PRC_CNSTR` (v1) | DAM; ti_id=ALL | 39 months; interval/operating date | spec_2019 |
| `dam_nomogram_shadow_prices` — Shadow Prices | SingleZip: `PRC_NOMOGRAM` (v1) | DAM; nomogram_id=ALL | 39 months; interval/operating date | spec_2019 |
| `rtm_nomogram_shadow_prices` — Shadow Prices | SingleZip: `PRC_RTM_NOMOGRAM` (v1) | — | 39 months; interval/operating date | spec_2019 |
| `rtm_intertie_shadow_prices` — Shadow Prices | SingleZip: `PRC_RTM_FLOWGATE` (v1) | — | 39 months; interval/operating date | spec_2019 |
| `dam_scheduling_constraint_prices` — Shadow Prices | SingleZip: `PRC_DAM_SCH_CNSTR` (v8) | DAM | 39 months; interval/operating date | spec_2019 |
| `rtd_scheduling_constraint_prices` — Shadow Prices | SingleZip: `PRC_RTM_SCH_CNSTR` (v4) | RTD | 39 months; interval/operating date | spec_2019 |
| `dam_as_prices` — Ancillary Services Prices | SingleZip: `PRC_AS` (v12) | DAM; anc_type=ALL, anc_region=ALL | 39 months; interval/operating date | gridstatus |
| `hasp_as_prices` — Ancillary Services Prices | SingleZip: `PRC_AS` (v12) | HASP; anc_type=ALL, anc_region=ALL | 39 months; interval/operating date | gridstatus |
| `rtm_as_prices` — Ancillary Services Prices | SingleZip: `PRC_INTVL_AS` (v1) | RTM; anc_type=ALL, anc_region=ALL | 39 months; interval/operating date | gridstatus |
| `fuel_prices` — Index Prices | SingleZip: `PRC_FUEL` (v1) | fuel_region_id=ALL | 39 months; interval/operating date | gridstatus |
| `ghg_allowance_prices` — Index Prices | SingleZip: `PRC_GHG_ALLOWANCE` (v1) | — | 39 months; interval/operating date | gridstatus |
| `dam_mpm_lmp` — Market Power Mitigation | SingleZip: `PRC_MPM_LMP` (v1) | DAM; grp_type=ALL_APNODES | 39 months; interval/operating date | spec_2019 |
| `dam_mpm_intertie_prices` — Market Power Mitigation | SingleZip: `PRC_MPM_CNSTR` (v1) | DAM; ti_id=ALL | 39 months; interval/operating date | spec_2019 |
| `dam_mpm_nomogram_prices` — Market Power Mitigation | SingleZip: `PRC_MPM_NOMOGRAM` (v1) | DAM; nomogram_id=ALL | 39 months; interval/operating date | spec_2019 |
| `dam_mpm_constraint_comparison` — Market Power Mitigation | SingleZip: `PRC_MPM_CNSTR_CMP` (v1) | DAM | 39 months; interval/operating date | spec_2019 |
| `dam_mpm_nomogram_comparison` — Market Power Mitigation | SingleZip: `PRC_MPM_NOMOGRAM_CMP` (v1) | DAM | 39 months; interval/operating date | spec_2019 |
| `dam_mpm_default_comparison` — Market Power Mitigation | SingleZip: `PRC_MPM_DEFAULT_CMP` (v5) | DAM | 39 months; interval/operating date | spec_2019 |
| `dam_mpm_reference_prices` — Market Power Mitigation | SingleZip: `PRC_MPM_REF_BUS` (v1) | DAM | 39 months; interval/operating date | spec_2019 |

## transmission

| Dataset / menu | API identifier (version) | Market / filters | Availability / partition | Source |
| --- | --- | --- | --- | --- |
| `current_transmission_usage` — Current Transmission Usage | SingleZip: `TRNS_CURR_USAGE` (v1) | ti_id=ALL, ti_direction=ALL | current only; snapshot date | spec_2019 |
| `transmission_outages` — Transmission Outages | SingleZip: `TRNS_OUTAGE` (v1) | ti_id=ALL, ti_direction=ALL | 39 months; interval/operating date | spec_2019 |
| `dam_transmission_interface_usage` — Transmission Interface Usage | SingleZip: `TRNS_USAGE` (v1) | DAM; ti_id=ALL, ti_direction=ALL | 39 months; interval/operating date | spec_2019 |
| `dam_available_transmission_capacity` — Market Available Transmission Capacity | SingleZip: `TRNS_ATC` (v1) | DAM; ti_id=ALL, ti_direction=ALL | 39 months; interval/operating date | spec_2019 |
| `pwt_available_transfer_capacity` — ATC for PWT Requests | GroupZip: `DAILY_ATC_PWT_REQ_GRP` (v1) | — | 39 months; interval/operating date | portal |
| `pwt_resale_transactions` — PWT Resale Transactions | GroupZip: `PWT_RESALE_TRANS_GRP` (v1) | — | 39 months; snapshot date | portal |

## system_demand

| Dataset / menu | API identifier (version) | Market / filters | Availability / partition | Source |
| --- | --- | --- | --- | --- |
| `dam_load_forecast` — CAISO Demand Forecast | SingleZip: `SLD_FCST` (v1) | DAM | 39 months; interval/operating date | gridstatus |
| `2da_load_forecast` — CAISO Demand Forecast | SingleZip: `SLD_FCST` (v1) | 2DA | 39 months; interval/operating date | gridstatus |
| `7da_load_forecast` — CAISO Demand Forecast | SingleZip: `SLD_FCST` (v1) | 7DA | 39 months; interval/operating date | gridstatus |
| `actual_load_forecast` — CAISO Demand Forecast | SingleZip: `SLD_FCST` (v1) | ACTUAL | 39 months; interval/operating date | gridstatus |
| `peak_demand_forecast` — CAISO Peak Demand Forecast | SingleZip: `SLD_FCST_PEAK` (v1) | — | 39 months; interval/operating date | spec_2019 |
| `dam_wind_solar_forecast` — Wind and Solar Forecast | SingleZip: `SLD_REN_FCST` (v1) | DAM | 39 months; interval/operating date | gridstatus |
| `actual_wind_solar_forecast` — Wind and Solar Forecast | SingleZip: `SLD_REN_FCST` (v1) | ACTUAL | 39 months; interval/operating date | gridstatus |
| `hasp_wind_solar_forecast` — Wind and Solar Forecast | SingleZip: `SLD_REN_FCST` (v1) | HASP | 39 months; interval/operating date | gridstatus |
| `rtd_wind_solar_forecast` — Wind and Solar Forecast | SingleZip: `SLD_REN_FCST` (v1) | RTD | 39 months; interval/operating date | gridstatus |
| `rtpd_wind_solar_forecast` — Wind and Solar Forecast | SingleZip: `SLD_REN_FCST` (v1) | RTPD | 39 months; interval/operating date | gridstatus |
| `rtd_advisory_load_forecast` — Advisory CAISO Demand Forecast | SingleZip: `SLD_ADV_FCST` (v4) | RTD | 39 months; interval/operating date | spec_2019 |
| `rtpd_advisory_load_forecast` — Advisory CAISO Demand Forecast | SingleZip: `SLD_ADV_FCST` (v4) | RTPD | 39 months; interval/operating date | spec_2019 |
| `sufficiency_demand_forecast_hourly` — Sufficiency Evaluation Demand Forecast | SingleZip: `SLD_SF_EVAL_DMD_FCST` (v4) | granularity=HOURLY | 39 months; interval/operating date | spec_2019 |
| `sufficiency_demand_forecast_15min` — Sufficiency Evaluation Demand Forecast | SingleZip: `SLD_SF_EVAL_DMD_FCST` (v4) | granularity=15MIN | 39 months; interval/operating date | spec_2019 |
| `hasp_load_adjustments` — Load Adjustments | GroupZip: `HASP_LOAD_ADJUSTMENT_GRP` (v13) | — | 39 months; interval/operating date | portal |

## energy

| Dataset / menu | API identifier (version) | Market / filters | Availability / partition | Source |
| --- | --- | --- | --- | --- |
| `dam_load_resource_schedules` — Schedule | SingleZip: `ENE_SLRS` (v1) | DAM; tac_zone_name=ALL, schedule=ALL | 39 months; interval/operating date | gridstatus |
| `hasp_load_resource_schedules` — Schedule | SingleZip: `ENE_SLRS` (v1) | HASP; tac_zone_name=ALL, schedule=ALL | 39 months; interval/operating date | gridstatus |
| `rtm_load_resource_schedules` — Schedule | SingleZip: `ENE_SLRS` (v1) | RTM; tac_zone_name=ALL, schedule=ALL | 39 months; interval/operating date | gridstatus |
| `ruc_load_resource_schedules` — Schedule | SingleZip: `ENE_SLRS` (v1) | RUC; tac_zone_name=ALL, schedule=ALL | 39 months; interval/operating date | gridstatus |
| `system_dispatch` — System | SingleZip: `ENE_DISP` (v1) | — | 39 months; interval/operating date | spec_2019 |
| `energy_available` — System | SingleZip: `ENE_EA` (v2) | energy_type=ALL, opr_interval=ALL | 39 months; interval/operating date | spec_2019 |
| `dam_system_losses` — System | SingleZip: `ENE_LOSS` (v1) | DAM | 39 months; interval/operating date | spec_2019 |
| `rtd_flexible_ramp_requirements` — Flexible Ramping | SingleZip: `ENE_FLEX_RAMP_REQT` (v4) | RTD; baa_grp_id=ALL | 39 months; interval/operating date | spec_2019 |
| `rtd_aggregate_flexible_ramp` — Flexible Ramping | SingleZip: `ENE_AGGR_FLEX_RAMP` (v4) | RTD; baa_grp_id=ALL | 39 months; interval/operating date | spec_2019 |
| `rtd_flexible_ramp_credits` — Flexible Ramping | SingleZip: `ENE_FLEX_RAMP_DC` (v4) | RTD; baa_grp_id=ALL | 39 months; interval/operating date | spec_2019 |
| `rtd_uncertainty_movement` — Flexible Ramping | SingleZip: `ENE_UNCERTAINTY_MV` (v4) | RTD; baa_grp_id=ALL | 39 months; interval/operating date | spec_2019 |
| `convergence_bid_awards` — Convergence Bidding | SingleZip: `ENE_CB_AWARDS` (v1) | — | 39 months; interval/operating date | spec_2019 |
| `convergence_bid_cleared_awards` — Convergence Bidding | SingleZip: `ENE_CB_CLR_AWARDS` (v1) | — | 39 months; interval/operating date | spec_2019 |
| `convergence_bid_market_summary` — Convergence Bidding | SingleZip: `ENE_CB_MKT_SUM` (v1) | — | 39 months; interval/operating date | spec_2019 |
| `rtd_eim_tie_transfers` — Energy Imbalance Market | SingleZip: `ENE_EIM_TRANSFER_TIE` (v4) | RTD; baa_grp_id=ALL | 39 months; interval/operating date | spec_2019 |
| `rtd_eim_tie_transfer_limits` — Energy Imbalance Market | SingleZip: `ENE_EIM_TRANSFER_LIMITS_TIE` (v5) | RTD; baa_grp_id=ALL | 39 months; interval/operating date | spec_2019 |
| `eim_transfers` — Energy Imbalance Market | SingleZip: `ENE_EIM_TRANSFER` (v2) | ALL; baa_grp_id=ALL | 39 months; interval/operating date | spec_2019 |
| `eim_transfer_limits` — Energy Imbalance Market | SingleZip: `ENE_EIM_TRANSFER_LIMITS` (v2) | ALL; baa_grp_id=ALL | 39 months; interval/operating date | spec_2019 |
| `eim_flexible_ramp_inputs` — Energy Imbalance Market | SingleZip: `ENE_EIM_FLEX_RAMP_INPUT` (v6) | RTM; baa_grp_id=ALL, snapshot_indicator=ALL | 39 months; interval/operating date | spec_2019 |
| `resource_uplift` — Uplift | GroupZip: `ENE_RES_UPL_GRP` (v8) | — | 39 months; interval/operating date | spec_2019 |
| `zonal_uplift` — Uplift | GroupZip: `ENE_ZNL_UPL_GRP` (v8) | — | 39 months; interval/operating date | spec_2019 |
| `operator_instruction_costs` — Uplift | GroupZip: `ENE_OIC_GRP` (v8) | — | 39 months; interval/operating date | spec_2019 |
| `dam_imbalance_reserve_prices` — Imbalance Reserve | GroupZip: `DAM_CAP_PRC_GRP` (v1) | — | 39 months; interval/operating date | gridstatus |
| `dam_imbalance_reserve_awards` — Imbalance Reserve | GroupZip: `DAM_CAP_REQ_AWRD_GRP` (v1) | — | 39 months; interval/operating date | gridstatus |
| `2da_imbalance_reserve_awards` — Imbalance Reserve | GroupZip: `2DA_CAP_REQ_AWRD_GRP` (v1) | — | 39 months; interval/operating date | gridstatus |
| `3da_imbalance_reserve_awards` — Imbalance Reserve | GroupZip: `3DA_CAP_REQ_AWRD_GRP` (v1) | — | 39 months; interval/operating date | gridstatus |
| `edam_ghg_import_transfers` — EDAM | GroupZip: `DAM_TOTAL_NET_GHG_IMP_TRNSFR_GRP` (v1) | — | 39 months; interval/operating date | portal |
| `edam_baa_rse_tests` — EDAM | GroupZip: `DAM_AGG_BAA_LVL_FINAL_BIND_GRP` (v1) | — | 39 months; interval/operating date | portal |
| `edam_net_export_constraints` — EDAM | GroupZip: `EDAM_NET_EXP_TRNS_CNSTR_INP_PRM_GRP` (v1) | — | 39 months; interval/operating date | portal |
| `edam_tagged_energy_profiles` — EDAM | GroupZip: `DAM_TAG_TRANS_ENE_STATUS_GRP` (v1) | — | 39 months; interval/operating date | portal |
| `edam_market_ba_composition` — EDAM | GroupZip: `DAM_MKT_BA_COMP_GRP` (v1) | — | 39 months; interval/operating date | portal |
| `edam_baa_aggregated_transfers` — EDAM | GroupZip: `DAM_BAA_AGG_TRNSFR_GRP` (v1) | — | 39 months; interval/operating date | portal |

## ancillary_services

| Dataset / menu | API identifier (version) | Market / filters | Availability / partition | Source |
| --- | --- | --- | --- | --- |
| `dam_as_requirements` — AS Requirements | SingleZip: `AS_REQ` (v1) | DAM; anc_type=ALL, anc_region=ALL | 39 months; interval/operating date | gridstatus |
| `hasp_as_requirements` — AS Requirements | SingleZip: `AS_REQ` (v1) | HASP; anc_type=ALL, anc_region=ALL | 39 months; interval/operating date | gridstatus |
| `rtm_as_requirements` — AS Requirements | SingleZip: `AS_REQ` (v1) | RTM; anc_type=ALL, anc_region=ALL | 39 months; interval/operating date | gridstatus |
| `dam_as_results` — AS Results | SingleZip: `AS_RESULTS` (v1) | DAM; anc_type=ALL, anc_region=ALL | 39 months; interval/operating date | gridstatus |
| `hasp_as_results` — AS Results | SingleZip: `AS_RESULTS` (v1) | HASP; anc_type=ALL, anc_region=ALL | 39 months; interval/operating date | gridstatus |
| `rtm_as_results` — AS Results | SingleZip: `AS_RESULTS` (v1) | RTM; anc_type=ALL, anc_region=ALL | 39 months; interval/operating date | gridstatus |
| `actual_operating_reserves` — Actual Operating Reserves | SingleZip: `AS_OP_RSRV` (v1) | — | 39 months; interval/operating date | spec_2019 |
| `as_mileage_components` — Mileage Calculation Components | SingleZip: `AS_MILEAGE_CALC` (v1) | anc_type=ALL | 39 months; interval/operating date | spec_2019 |

## congestion_revenue_rights

| Dataset / menu | API identifier (version) | Market / filters | Availability / partition | Source |
| --- | --- | --- | --- | --- |
| `crr_clearing_prices` — CRR Clearing Prices | SingleZip: `CRR_CLEARING` (v1) | market_name=ALL, market_term=ALL, time_of_use=ALL | 39 months; snapshot date | spec_2019 |
| `crr_inventory` — CRR Inventory | SingleZip: `CRR_INVENTORY` (v1) | market_name=**required**, market_term=ALL, time_of_use=ALL | 39 months; snapshot date | spec_2019 |
| `crr_revenue_adjustments` — CRR Aggregated Revenue Adjustment Data | SingleZip: `CRR_AGG_REV_ADJ` (v7) | trans_cnstr_id=ALL | 39 months; snapshot date | spec_2019 |
| `crr_market_names` — CRR Market Names | SingleZip: `CRR_MRKT_NAMES` (v1) | — | 39 months; snapshot date | portal |

## public_bids

| Dataset / menu | API identifier (version) | Market / filters | Availability / partition | Source |
| --- | --- | --- | --- | --- |
| `dam_public_bids` — Public Bids | GroupZip: `PUB_DAM_GRP` (v3) | — | 39 months; delay 90 days; snapshot date | gridstatus |
| `rtm_public_bids` — Public Bids | GroupZip: `PUB_RTM_GRP` (v3) | — | 39 months; delay 90 days; snapshot date | gridstatus |
| `convergence_public_bids` — Convergence Bidding Public Bids | GroupZip: `PUB_CB_DAM_GRP` (v2) | — | 39 months; delay 90 days; snapshot date | spec_2019 |
| `crr_seasonal_public_bids` — CRR Public Bids | GroupZip: `PUB_CRR_BID_SEASONAL_GRP` (v2) | — | 39 months; delay 90 days; snapshot date | spec_2019 |
| `crr_monthly_public_bids` — CRR Public Bids | GroupZip: `PUB_CRR_BID_MONTHLY_GRP` (v2) | — | 39 months; delay 90 days; snapshot date | spec_2019 |
| `market_bid_caps` — Market Bid Caps | GroupZip: `MKT_BID_CAPS_GRP` (v1) | — | 39 months; interval/operating date | portal |

## resource_adequacy

| Dataset / menu | API identifier (version) | Market / filters | Availability / partition | Source |
| --- | --- | --- | --- | --- |
| `dam_ra_minimum_load` — Resource Adequacy and Minimum Load | SingleZip: `CMMT_RA_MLC` (v1) | DAM | 39 months; interval/operating date | spec_2019 |
| `csp_annual_offers` — CSP Offer | GroupZip: `PUB_CSP_OFFER_SET_ANNUAL_GRP` (v1) | — | 39 months; delay 5 quarters; snapshot date | spec_2019 |
| `csp_monthly_offers` — CSP Offer | GroupZip: `PUB_CSP_OFFER_SET_MONTHLY_GRP` (v1) | — | 39 months; delay 5 quarters; snapshot date | spec_2019 |
| `csp_intramonthly_offers` — CSP Offer | GroupZip: `PUB_CSP_OFFER_SET_INTRAMONTHLY_GRP` (v1) | — | 39 months; delay 5 quarters; snapshot date | spec_2019 |
| `cpm_designations` — CPM Designations | SingleZip: `PUB_CPM_DESIGNATION` (v1) | — | 39 months; snapshot date | portal |
| `available_import_capability` — Available Import Capability | GroupZip: `AVAIL_IMP_CAP_GRP` (v1) | — | 39 months; snapshot date | portal |
| `import_capability_ra_plans` — Import Capability Used in RA Plan Data | GroupZip: `ANNUAL_IMP_CAP_USED_RA_PLAN_GRP` (v1) | — | 39 months; snapshot date | portal |
