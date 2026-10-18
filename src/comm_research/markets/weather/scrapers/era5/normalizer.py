"""Validate one global hour and turn it into a columnar research table."""

from __future__ import annotations

import json

SCHEMA_VERSION = 1


def parquet_schema(info, fingerprint: str):
    import pyarrow as pa

    return pa.schema(
        [
            pa.field("timestamp", pa.timestamp("us", tz="UTC"), nullable=False),
            pa.field("latitude", pa.float32(), nullable=False,
                     metadata={b"units": b"degrees_north"}),
            pa.field("longitude", pa.float32(), nullable=False,
                     metadata={b"units": b"degrees_east", b"range": b"[-180, 180)"}),
            pa.field("temperature_2m", pa.float32(), nullable=False,
                     metadata={b"units": b"K", b"standard_name": b"air_temperature",
                               b"height": b"2 m"}),
        ],
        metadata={
            b"dataset": b"era5_single_levels", b"variable": b"2m_temperature",
            b"schema_version": str(SCHEMA_VERSION).encode(),
            b"fingerprint": fingerprint.encode(), b"source_url": info.url.encode(),
            b"source_status": b"finalized",
            b"source_valid_time_stop": info.valid_time_stop.encode(),
            b"source_last_updated": info.last_updated.encode(),
            b"dimensions": json.dumps({"latitude": len(info.latitude),
                                       "longitude": len(info.longitude)}).encode(),
        },
    )


class HourNormalizer:
    """Reuse coordinates; preserve float32 source temperatures in kelvin."""

    def __init__(self, info, schema):
        import numpy as np
        import pyarrow as pa

        self.shape = (len(info.latitude), len(info.longitude))
        self.cells = info.cells
        self.schema = schema
        longitude = (np.asarray(info.longitude, dtype=np.float32) + 180) % 360 - 180
        self.longitude_order = np.argsort(longitude)
        self.latitude = pa.array(np.repeat(np.asarray(info.latitude, dtype=np.float32),
                                          self.shape[1]))
        self.longitude = pa.array(np.tile(longitude[self.longitude_order], self.shape[0]))

    def table(self, timestamp, values):
        import numpy as np
        import pyarrow as pa

        values = np.asarray(values)
        if values.shape != self.shape or values.dtype != np.dtype("float32"):
            raise ValueError(f"Unexpected temperature array: {values.shape}, {values.dtype}")
        if not np.isfinite(values).all():
            raise ValueError(f"Missing/nonfinite temperature values at {timestamp}")
        values = np.ascontiguousarray(values[:, self.longitude_order]).reshape(-1)
        timestamps = np.full(self.cells, timestamp, dtype="datetime64[us]")
        return pa.Table.from_arrays(
            [pa.array(timestamps, type=self.schema.field("timestamp").type),
             self.latitude, self.longitude, pa.array(values)],
            schema=self.schema,
        )
