DATA_PREFIXES = {
    "ecmwf_seasonal": "ecmwf/source=cds/system=51/product=seasonal-monthly-mean/param=2t",
    "ecmwf_hourly": "ecmwf/source=opendata/model=aifs-ens/resolution=0p25/stream=enfo/type=pf/param=2t",
}


SCHEMAS = {
    DATA_PREFIXES["ecmwf_seasonal"]: {
        'filter_columns': [
            'dataDate',
            'validityDate',
            'fcmonth',
            'stepRange',
            'message',
            'latitude',
            'longitude',
            'value', 
            'units', 
            'perturbationNumber'
        ], 
        'rename_columns': None
    },
    DATA_PREFIXES["ecmwf_hourly"]: {
        "filter_columns": [
            "dataDate",
            "dataTime",
            "validityDate",
            "validityTime",
            "stepRange",
            "message",
            "latitude",
            "longitude",
            "value",
            "units",
            "perturbationNumber",
        ],
        "rename_columns": None,
    },
}
