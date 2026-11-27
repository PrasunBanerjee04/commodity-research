from argparse import ArgumentParser
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from functools import cached_property
from hashlib import sha256
from re import search, sub
from typing import cast

import polars as pl
from duckdb import DuckDBPyConnection

from comm_research.markets.weather import ZONAL_MAP
from infra.database.duckdb_client import DuckDBClient, _quote_identifier
from infra.pipeline.storage.s3_client import S3Client, s3_mapper
from infra.pipeline.transform.ecmwf_transform import Frame, FrameType, schema_filter
from infra.pipeline.transform.ecmwf_transform import transform as decode_grib
from infra.pipeline.transform.schema import DATA_PREFIXES


FCMONTH_PATTERN = r"(?:^|/)fcmonth=([0-9]+)(?:/|$)"
SEASONAL_PREFIX = s3_mapper("ecmwf_seasonal")


def extract(s3: S3Client, bucket: str, key: str) -> bytes:
    """Return the raw GRIB bytes for the selected data lake object."""
    return cast(bytes, s3.read_from_lake(bucket, key))


def extract_prefix(s3: S3Client, bucket: str, prefix: str) -> Iterator[tuple[str, bytes]]:
    """Download GRIB objects one at a time as the iterator is consumed."""
    prefix = prefix.rstrip("/")
    if not prefix:
        raise ValueError("Provide an ECMWF folder prefix")
    for key in s3.list_keys(bucket, prefix + "/"):
        if key.lower().endswith((".grib", ".grib2")):
            yield key, extract(s3, bucket, key)


def _assign(df: Frame, **values: int | str) -> Frame:
    if isinstance(df, pl.DataFrame):
        return df.with_columns(**{key: pl.lit(value) for key, value in values.items()})
    return df.assign(**values)


def transform(raw: bytes, key: str, *, region: str, type: FrameType = "polars") -> Frame:
    df = decode_grib(raw, region=region, type=type)
    if key.startswith(SEASONAL_PREFIX + "/"):
        match = search(FCMONTH_PATTERN, key)
        if match is None:
            raise ValueError(f"Missing fcmonth in seasonal forecast key: {key}")
        df = _assign(df, fcmonth=int(match.group(1)))
    return schema_filter(df, path=key, type=type)


@dataclass(frozen=True)
class ForecastObject:
    key: str
    raw: bytes
    bucket: str
    region: str
    type: FrameType = field(default="polars", kw_only=True)

    @property
    def source_uri(self) -> str:
        return f"s3://{self.bucket}/{self.key}"

    @cached_property
    def db_path(self) -> str:
        return sub(r"(/param=[^/]+)(/)", rf"\1/region={self.region}\2", self.source_uri, count=1)

    @cached_property
    def frame(self) -> Frame:
        df = transform(self.raw, self.key, region=self.region, type=self.type)
        return _assign(df, region=self.region, source_uri=self.source_uri, db_path=self.db_path)


def transform_objects(
    objects: Iterable[tuple[str, bytes]], *, bucket: str, region: str, type: FrameType = "polars"
) -> Iterator[ForecastObject]:
    """Prepare lazy forecast objects whose frames use the requested format."""
    if region not in ZONAL_MAP:
        raise ValueError(f"Unknown weather region: {region}")
    return (ForecastObject(key, raw, bucket, region, type=type) for key, raw in objects)


def _ensure_table(connection: DuckDBPyConnection, table: str) -> None:
    table = _quote_identifier(table)
    connection.execute(f"""
        CREATE TABLE IF NOT EXISTS {table} (
            dataDate INTEGER,
            validityDate INTEGER,
            fcmonth INTEGER,
            stepRange VARCHAR,
            message INTEGER,
            latitude DOUBLE,
            longitude DOUBLE,
            value DOUBLE,
            units VARCHAR,
            perturbationNumber INTEGER
        )
    """)
    for column in ("region", "source_uri", "db_path"):
        connection.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} VARCHAR")
    for column in ("fcmonth", "dataTime", "validityTime"):
        connection.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} INTEGER")


def load(
    cleaned: Frame,
    db: DuckDBClient | None = None,
    *,
    region: str,
    data: str,
) -> None:
    """Append cleaned rows to the dataset's table when their source object is unknown."""
    s3_mapper(data)
    if db is None:
        db = DuckDBClient()

    with db.init_client() as connection:
        _ensure_table(connection, data)
        db.write_to_db(data, _assign(cleaned, region=region), connection=connection)


def _prepare_object_loads(db: DuckDBClient, table: str, loads_table: str) -> None:
    target = _quote_identifier(table)
    tracking = _quote_identifier(loads_table)
    with db.init_client() as connection:
        connection.begin()
        target_exists = connection.execute(
            """
            SELECT 1 FROM duckdb_tables()
            WHERE database_name = current_database() AND schema_name = current_schema()
              AND lower(table_name) = lower(?)
            """,
            [table],
        ).fetchone()
        _ensure_table(connection, table)
        connection.execute(f"""
            CREATE TABLE IF NOT EXISTS {tracking} (
                db_path VARCHAR PRIMARY KEY,
                checksum VARCHAR NOT NULL
            )
        """)
        if target_exists is None:
            connection.execute(f"DELETE FROM {tracking}")
        columns = connection.execute(f"SELECT * FROM {tracking} LIMIT 0").description
        if any(column[0] == "source_uri" for column in columns):
            # The previous loader always filtered to us48.
            legacy_region = "us48"
            replacement = rf"\1/region={legacy_region}\2"
            connection.execute(f"ALTER TABLE {tracking} RENAME COLUMN source_uri TO db_path")
            connection.execute(
                f"UPDATE {tracking} SET db_path = regexp_replace(db_path, ?, ?)",
                [r"(/param=[^/]+)(/)", replacement],
            )
            connection.execute(
                f"""
                    UPDATE {target}
                    SET region = ?, db_path = regexp_replace(source_uri, ?, ?)
                    WHERE source_uri IS NOT NULL AND db_path IS NULL
                """,
                [legacy_region, r"(/param=[^/]+)(/)", replacement],
            )
        # Backfill before checking object checksums so unchanged objects are covered.
        connection.execute(
            f"""
                UPDATE {target}
                SET fcmonth = COALESCE(
                    TRY_CAST(regexp_extract(source_uri, ?, 1) AS INTEGER),
                    TRY_CAST(regexp_extract(db_path, ?, 1) AS INTEGER)
                )
                WHERE fcmonth IS NULL
                  AND (regexp_matches(source_uri, ?) OR regexp_matches(db_path, ?))
            """,
            [FCMONTH_PATTERN] * 4,
        )
        connection.commit()


def _load_object(
    forecast: ForecastObject,
    db: DuckDBClient,
    *,
    table: str,
    loads_table: str,
) -> bool:
    tracking = _quote_identifier(loads_table)
    checksum = sha256(forecast.raw).hexdigest()

    with db.init_client() as connection:
        connection.begin()
        changed = connection.execute(
            f"""
                INSERT INTO {tracking} VALUES (?, ?)
                ON CONFLICT (db_path) DO UPDATE SET checksum = excluded.checksum
                WHERE {tracking}.checksum <> excluded.checksum
                RETURNING db_path
            """,
            [forecast.db_path, checksum],
        ).fetchone()
        if changed is None:
            return False

        connection.execute(
            f"DELETE FROM {_quote_identifier(table)} WHERE db_path = ?", [forecast.db_path]
        )
        db.write_to_db(table, forecast.frame, connection=connection)
        connection.commit()
    return True


def load_objects(
    objects: Iterable[ForecastObject],
    *,
    data: str,
    db: DuckDBClient | None = None,
) -> int:
    """Load new or changed objects into the table named for the dataset."""
    prefix = s3_mapper(data) + "/"
    if db is None:
        db = DuckDBClient()
    loads_table = f"{data}_loaded_objects"
    _prepare_object_loads(db, data, loads_table)

    loaded = 0
    for obj in objects:
        if not obj.key.startswith(prefix):
            raise ValueError(f"Object {obj.key!r} is outside the prefix for {data!r}: {prefix}")
        loaded += _load_object(obj, db, table=data, loads_table=loads_table)
    return loaded


def load_prefix(
    data: str,
    bucket: str,
    *,
    region: str,
    db: DuckDBClient | None = None,
) -> int:
    """Load new or changed GRIB objects for a dataset and return their count."""
    prefix = s3_mapper(data)
    if region not in ZONAL_MAP:
        raise ValueError(f"Unknown weather region: {region}")
    objects = extract_prefix(S3Client(), bucket, prefix)
    forecasts = transform_objects(objects, bucket=bucket, region=region)
    return load_objects(forecasts, data=data, db=db)


def main(
    key: str,
    bucket: str,
    *,
    region: str,
    data: str,
) -> None:
    prefix = s3_mapper(data) + "/"
    if not key.startswith(prefix):
        raise ValueError(f"Object {key!r} is outside the prefix for {data!r}: {prefix}")
    objects = [(key, extract(S3Client(), bucket, key))]
    load_objects(transform_objects(objects, bucket=bucket, region=region), data=data)


if __name__ == "__main__":
    parser = ArgumentParser()
    parser.add_argument("data", choices=DATA_PREFIXES, help="ECMWF dataset and destination table")
    parser.add_argument("--bucket", default="commodities-raw")
    parser.add_argument("--region", choices=ZONAL_MAP, default="us48")
    args = parser.parse_args()
    loaded = load_prefix(
        args.data,
        args.bucket,
        region=args.region,
    )
    print(f"Loaded {loaded} new or changed GRIB objects.")
