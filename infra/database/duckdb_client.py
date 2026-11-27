"""DuckDB operations for the research database."""

import os
from collections.abc import Mapping, Sequence
from contextlib import nullcontext
from pathlib import Path
from typing import Literal

import duckdb
import pandas as pd
import polars as pl


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

    def write_to_db(
        self,
        table: str,
        data: pl.DataFrame | pd.DataFrame,
        *,
        connection: duckdb.DuckDBPyConnection | None = None,
    ) -> None:
        """Append by column name, optionally using the caller's transaction."""
        source = f"_incoming_{table}"
        connection_context = self.init_client() if connection is None else nullcontext(connection)
        with connection_context as conn:
            relation = (
                conn.from_arrow(data.to_arrow()) if isinstance(data, pl.DataFrame) else conn.from_df(data)
            )
            relation.query(
                source,
                f"INSERT INTO {_quote_identifier(table)} BY NAME "
                f"SELECT * FROM {_quote_identifier(source)}",
            )

    def read_from_db(
        self, table: str, *, type: Literal["polars", "pandas"] = "polars"
    ) -> pl.DataFrame | pd.DataFrame:
        """Return all rows from a table in the requested DataFrame format."""
        return self.query(f"SELECT * FROM {_quote_identifier(table)}", type=type)

    def get_data(
        self, data: str, *, type: Literal["polars", "pandas"] = "polars"
    ) -> pl.DataFrame | pd.DataFrame:
        """Return a dataset as Polars by default or pandas with type='pandas'."""
        return self.read_from_db(data, type=type)

    def drop_tables(self, tables_to_drop: Sequence[str] | None = None) -> None:
        """List available main-schema tables or drop the selected tables."""
        selected = list(dict.fromkeys(tables_to_drop or ()))
        with self.init_client() as connection:
            connection.begin()
            catalog, = connection.execute("SELECT current_database()").fetchone()
            available = [
                name
                for name, in connection.execute(
                    """
                    SELECT table_name FROM duckdb_tables()
                    WHERE database_name = ? AND schema_name = 'main'
                      AND NOT internal AND NOT temporary
                    ORDER BY table_name
                    """,
                    [catalog],
                ).fetchall()
            ]
            if not selected:
                print(f"Tables in {self.db_path}: {available}")
                return

            missing = sorted(set(selected).difference(available))
            if missing:
                raise ValueError(f"Tables not found in {self.db_path}: {missing}")

            for name in selected:
                qualified = ".".join(_quote_identifier(part) for part in (catalog, "main", name))
                connection.execute(f"DROP TABLE {qualified}")
            connection.commit()
        print(f"Dropped from {self.db_path}: {selected}")

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
        self,
        q: str,
        parameters: Sequence[object] | dict[str, object] | None = None,
        *,
        type: Literal["polars", "pandas"] = "polars",
    ) -> pl.DataFrame | pd.DataFrame:
        """Execute SQL with optional bound values and return the result as a DataFrame."""
        if type not in ("polars", "pandas"):
            raise ValueError(f"Unknown DataFrame type: {type!r}")
        with self.init_client() as connection:
            result = connection.execute(q, parameters)
            return result.df() if type == "pandas" else result.pl()
