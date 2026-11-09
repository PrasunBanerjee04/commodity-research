"""Small public input aliases; tables infer their schema from local files."""

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Literal, TypeAlias, TypedDict

TimestampInput: TypeAlias = datetime | date | str
Scalar: TypeAlias = str | int | float | bool | None
Filters: TypeAlias = Mapping[str, Scalar | Sequence[Scalar]]
WriteMode: TypeAlias = Literal["create", "append", "upsert", "replace"]
Area: TypeAlias = Literal["lake", "clean", "raw"]


class DatasetInfo(TypedDict):
    name: str
    format: str
    files: int
    size_bytes: int
    columns: dict[str, str]
    rows: int | None
