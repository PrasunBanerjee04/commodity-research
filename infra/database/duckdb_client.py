"""DuckDB operations for the research database."""

import os
from collections.abc import Mapping, Sequence
from pathlib import Path

import duckdb
from pandas import DataFrame


DEFAULT_DB_PATH = Path.home() / "Desktop/research-data/duckdb/research-data.duckdb"


def _quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


class DuckDBClient:
    """Read and modify a DuckDB file from a pipeline task or notebook."""

    def __init__(self, db_path: str | os.PathLike[str] | None = None) -> None:
        if db_path is None:
            db_path = os.environ.get("DUCKDB_PATH", DEFAULT_DB_PATH)
        self.db_path = Path(db_path).expanduser()

    def init_client(self) -> duckdb.DuckDBPyConnection:
        """Open a connection. Use it in a with block to close it automatically."""
        return duckdb.connect(str(self.db_path))

    def write_to_db(self, table: str, data: DataFrame) -> None:
        """Append a DataFrame to an existing table, matching columns by name."""
        source = f"_incoming_{table}"
        with self.init_client() as connection:
            connection.from_df(data).query(
                source,
                f"INSERT INTO {_quote_identifier(table)} BY NAME "
                f"SELECT * FROM {_quote_identifier(source)}",
            )

    def read_from_db(self, table: str) -> DataFrame:
        """Return all rows from a table as a pandas DataFrame."""
        return self.query(f"SELECT * FROM {_quote_identifier(table)}")

    def delete_from_db(
        self, table: str, where: str, parameters: Sequence[object] = ()
    ) -> None:
        """Delete rows matching a SQL predicate, with optional bound values."""
        self.query(f"DELETE FROM {_quote_identifier(table)} WHERE {where}", parameters)

    def update_db(
        self,
        table: str,
        values: Mapping[str, object],
        where: str,
        parameters: Sequence[object] = (),
    ) -> None:
        """Set the given column values on rows matching a SQL predicate."""
        if not values:
            raise ValueError("Provide at least one column to update")
        assignments = ", ".join(f"{_quote_identifier(column)} = ?" for column in values)
        self.query(
            f"UPDATE {_quote_identifier(table)} SET {assignments} WHERE {where}",
            [*values.values(), *parameters],
        )

    def query(
        self, q: str, parameters: Sequence[object] | dict[str, object] | None = None
    ) -> DataFrame:
        """Execute SQL with optional bound values and return the result as a DataFrame."""
        with self.init_client() as connection:
            return connection.execute(q, parameters).df()
