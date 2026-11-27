"""Object storage operations for the Garage data lake."""

import os
from collections.abc import Iterator
from contextlib import closing
from typing import BinaryIO

import boto3
from botocore.config import Config

from ..transform.schema import DATA_PREFIXES


def s3_mapper(data: str) -> str:
    """Return the raw S3 prefix for a dataset name."""
    try:
        return DATA_PREFIXES[data]
    except KeyError:
        raise ValueError(f"Unknown dataset {data!r}. Available: {', '.join(DATA_PREFIXES)}") from None


def list_available_data() -> None:
    """Print the dataset names accepted by s3_mapper."""
    print(*DATA_PREFIXES, sep="\n")


class S3Client:
    """Read, write, and delete objects in the Garage data lake."""

    def __init__(self) -> None:
        self._client = boto3.client(
            "s3",
            endpoint_url=os.environ.get("GARAGE_ENDPOINT", "http://localhost:3900"),
            region_name=os.environ.get("GARAGE_REGION", "garage"),
            aws_access_key_id=os.environ["GARAGE_ACCESS_KEY"],
            aws_secret_access_key=os.environ["GARAGE_SECRET_KEY"],
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
            ),
        )

    def list_keys(self, bucket: str, prefix: str) -> Iterator[str]:
        """Yield all object keys under a prefix across listing pages."""
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for entry in page.get("Contents", ()):
                yield entry["Key"]

    def read_from_lake(
        self,
        bucket: str,
        key: str,
        destination: str | os.PathLike[str] | BinaryIO | None = None,
    ) -> bytes | None:
        """Return the object as bytes, or download it to a path or binary stream."""
        if destination is None:
            with closing(self._client.get_object(Bucket=bucket, Key=key)["Body"]) as body:
                return body.read()

        if isinstance(destination, (str, os.PathLike)):
            self._client.download_file(Bucket=bucket, Key=key, Filename=os.fspath(destination))
        else:
            self._client.download_fileobj(Bucket=bucket, Key=key, Fileobj=destination)
        return None

    def write_to_lake(
        self,
        filename: str | os.PathLike[str] | bytes | BinaryIO,
        bucket: str,
        key: str,
    ) -> None:
        """Upload a local file, bytes, or a readable binary stream to an object key."""
        if isinstance(filename, (str, os.PathLike)):
            self._client.upload_file(Filename=os.fspath(filename), Bucket=bucket, Key=key)
        elif isinstance(filename, bytes):
            self._client.put_object(Body=filename, Bucket=bucket, Key=key)
        else:
            self._client.upload_fileobj(Fileobj=filename, Bucket=bucket, Key=key)

    def delete_from_lake(self, bucket: str, key: str) -> None:
        """Delete the object at the given key."""
        self._client.delete_object(Bucket=bucket, Key=key)

    def update_lake(
        self,
        filename: str | os.PathLike[str] | bytes | BinaryIO,
        bucket: str,
        key: str,
    ) -> None:
        """Replace the entire object at the key, creating it if absent."""
        self.write_to_lake(filename, bucket, key)
