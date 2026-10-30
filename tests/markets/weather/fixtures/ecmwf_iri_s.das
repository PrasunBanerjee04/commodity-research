Attributes {
    LA {
        String long_name "Lead";
        String standard_name "forecast_period";
        Float32 pointwidth 1.0;
        Int32 gridtype 0;
        String units "days";
    }
    Y {
        String long_name "latitude";
        String standard_name "latitude";
        Float32 pointwidth 1.5;
        Int32 gridtype 0;
        String units "degree_north";
    }
    hdate {
        Float32 pointwidth 12.0;
        String calendar "360";
        Int32 gridtype 0;
        String units "months since 1960-01-01";
    }
    S {
        String long_name "forecast start time";
        String standard_name "forecast_reference_time";
        Int32 pointwidth 0;
        String calendar "standard";
        Int32 gridtype 0;
        String units "days since 1960-01-01";
    }
    X {
        String long_name "longitude";
        String standard_name "longitude";
        Float32 pointwidth 1.5;
        Int32 gridtype 1;
        String units "degree_east";
    }
    2t {
        Int32 pointwidth 0;
        String gribPDSpattern "04XXXX003D0000";
        Float32 missing_value 9.99900026E20;
        String standard_name "air_temperature";
        String long_name "2-meter Temperature";
        String units "K";
    }
NC_GLOBAL {
    String Conventions "IRIDL";
}
