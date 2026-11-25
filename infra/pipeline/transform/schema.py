SCHEMAS = {
    'ecmwf/source=cds/system=51/product=seasonal-monthly-mean/param=2t': {
        'filter_columns': [
            'dataDate',
            'validityDate',
            'stepRange',
            'message',
            'latitude',
            'longitude',
            'value', 
            'units', 
            'perturbationNumber'
        ], 
        'rename_columns': None
    }
}