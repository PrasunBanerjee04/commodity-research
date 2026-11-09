"""Errors for local table access."""

from typing import Any


class DataLakeError(Exception):
    def __init__(self, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


class DatasetNotFoundError(DataLakeError, FileNotFoundError):
    pass


class InvalidQueryError(DataLakeError, ValueError):
    pass


class InvalidTimeError(InvalidQueryError):
    pass


class DataValidationError(DataLakeError, ValueError):
    pass


class QueryLimitExceededError(DataLakeError):
    pass


class ConcurrentWriteError(DataLakeError):
    pass
