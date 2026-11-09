"""Read and write the repository's existing local data without registration."""

from .api import DataLake
from .errors import (
    ConcurrentWriteError,
    DataLakeError,
    DatasetNotFoundError,
    DataValidationError,
    InvalidQueryError,
    InvalidTimeError,
    QueryLimitExceededError,
)

__version__ = "0.2.0"
__all__ = [
    "DataLake",
    "DataLakeError",
    "DatasetNotFoundError",
    "DataValidationError",
    "InvalidQueryError",
    "InvalidTimeError",
    "QueryLimitExceededError",
    "ConcurrentWriteError",
]
