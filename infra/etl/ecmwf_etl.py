from argparse import ArgumentParser
from typing import cast

from pipeline.storage.s3_client import S3Client


RAW_BUCKET = "commodities-raw"


def extract(s3: S3Client, bucket: str, key: str) -> bytes:
    """Return the raw GRIB bytes for the selected data lake object."""
    return cast(bytes, s3.read_from_lake(bucket, key))


def transform(raw: bytes):
    ## calls from transform code, mapping data to correct schema
    pass


def load(cleaned):
    ## creates relevant table if needed, and pushes to database
    pass


def main(key: str, bucket: str = RAW_BUCKET):
    raw = extract(S3Client(), bucket, key)
    cleaned = transform(raw)
    load(cleaned)


if __name__ == "__main__":
    parser = ArgumentParser()
    parser.add_argument("key", help="ECMWF object key in the data lake")
    parser.add_argument("--bucket", default=RAW_BUCKET)
    args = parser.parse_args()
    main(args.key, args.bucket)
