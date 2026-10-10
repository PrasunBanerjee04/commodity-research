"""Indexed RAM snapshots for CRR, transmission, and ancillary requirements."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LOG = logging.getLogger("caiso.workstation")
PREFIX = "power_gas/napg/caiso/"


@dataclass(frozen=True)
class Feed:
    name: str
    title: str
    aliases: tuple[str, ...]
    entity: str
    value: str
    dimensions: tuple[str, ...]
    unit: str


FEEDS = (
    Feed(
        "crr",
        "Congestion Revenue Rights",
        ("crr_bids", "crr_clearing_prices"),
        "apnode_id",
        "apnode_id_price",
        ("market_name", "market_term", "time_of_use", "xml_data_item"),
        "CAISO price",
    ),
    Feed(
        "transmission",
        "Transmission Usage",
        (
            "transmission_usage",
            "current_transmission_usage",
            "dam_transmission_interface_usage",
        ),
        "ti_id",
        "mw",
        ("ti_direction", "market_run_id", "ti_constraint_id", "measure"),
        "MW",
    ),
    Feed(
        "ancillary",
        "DAM Ancillary Services",
        ("as_req", "dam_as_requirements"),
        "anc_region",
        "mw",
        ("anc_type", "measure"),
        "MW",
    ),
)


def quote(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def boundary(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return (
        parsed.replace(tzinfo=timezone.utc)
        if parsed.tzinfo is None
        else parsed.astimezone(timezone.utc)
    )


class ResearchMemoryLake:
    """Uses the parent lake's locked connection and transaction; never scans on clicks."""

    def __init__(self, con, root: Path):
        self.con = con
        self.root = root
        self.metadata: dict[str, dict[str, Any]] = {}

    def resolve(self, key: str) -> Feed:
        key = key.strip().removesuffix(".parquet")
        for feed in FEEDS:
            if (
                key == feed.name
                or key in feed.aliases
                or key in {PREFIX + alias for alias in feed.aliases}
            ):
                return feed
        raise ValueError(
            "Unknown research feed. Choose crr, transmission, or ancillary."
        )

    def preload(self, catalog: list[dict]) -> list[dict]:
        for feed in FEEDS:
            sources = sorted(
                {
                    str(p)
                    for alias in feed.aliases
                    for p in (self.root / PREFIX / alias).rglob("*.parquet")
                }
                | {
                    str(directory / (alias + ".parquet"))
                    for alias in feed.aliases
                    for directory in (self.root, self.root / PREFIX)
                    if (directory / (alias + ".parquet")).is_file()
                }
            )
            table = "research_" + feed.name
            self.con.execute(f"DROP TABLE IF EXISTS {table}")
            definitions = ", ".join(
                f"{quote(name)} VARCHAR" for name in (feed.entity, *feed.dimensions)
            )
            self.con.execute(
                f"CREATE TABLE {table} (timestamp TIMESTAMPTZ, value DOUBLE, observed_at DATE, {definitions})"
            )
            if sources:
                schema = self.con.execute(
                    "DESCRIBE SELECT * FROM read_parquet(?,union_by_name=true,hive_partitioning=false)",
                    [sources],
                ).fetchall()
                columns = {row[0].lower(): row[0] for row in schema}

                def column(name, optional=False, columns=columns, feed=feed):
                    if name not in columns:
                        if optional:
                            return "NULL"
                        raise ValueError(f"{feed.name}: missing required column {name}")
                    return quote(columns[name])

                def text(name, optional=False, column=column):
                    return f"CAST({column(name, optional)} AS VARCHAR)"

                selections = [text(feed.entity)]
                for dimension in feed.dimensions:
                    if dimension != "measure":
                        selections.append(
                            text(dimension, dimension == "ti_constraint_id")
                        )
                    elif feed.name == "transmission":
                        item, kind = text("xml_data_item", True), text("tr_type", True)
                        selections.append(
                            f"CASE WHEN upper({item}) IN ('USEAGE_MW','USAGE_MW') OR upper({kind}) LIKE '%USEAGE%' OR upper({kind}) LIKE '%USAGE%' THEN 'USEAGE_MW' WHEN upper({item}) IN ('ATC_MW','RATING_ATC') OR upper({kind})='RATING_ATC' THEN 'RATING_ATC' ELSE coalesce({item},{kind},'UNKNOWN') END"
                        )
                    else:
                        item = text("xml_data_item")
                        selections.append(
                            f"CASE WHEN upper({item}) LIKE '%MIN%' THEN 'MINIMUM' WHEN upper({item}) LIKE '%MAX%' THEN 'MAXIMUM' ELSE {item} END"
                        )
                market = (
                    f"WHERE upper({text('market_run_id')})='DAM'"
                    if feed.name == "ancillary"
                    else ""
                )
                self.con.execute(
                    f"""INSERT INTO {table}
                    SELECT TRY_CAST({column("interval_start_time_gmt")} AS TIMESTAMPTZ),
                        TRY_CAST({column(feed.value)} AS DOUBLE), TRY_CAST({column("as_of_date", True)} AS DATE),
                        {",".join(selections)}
                    FROM read_parquet(?,union_by_name=true,hive_partitioning=false) {market}""",
                    [sources],
                )
                self.con.execute(
                    f"DELETE FROM {table} WHERE timestamp IS NULL OR {quote(feed.entity)} IS NULL"
                )
                self.con.execute(
                    f"UPDATE {table} SET value=NULL WHERE NOT isfinite(value)"
                )
            self.con.execute(
                f"CREATE INDEX idx_{table}_time ON {table}({quote(feed.entity)},timestamp)"
            )
            self.con.execute(
                f"CREATE INDEX idx_{table}_timestamp ON {table}(timestamp)"
            )
            count, earliest, latest = self.con.execute(
                f"SELECT count(*), strftime(min(timestamp), '%Y-%m-%dT%H:%M:%SZ'), strftime(max(timestamp), '%Y-%m-%dT%H:%M:%SZ') FROM {table}"
            ).fetchone()
            entities = [
                row[0]
                for row in self.con.execute(
                    f"SELECT DISTINCT {quote(feed.entity)} FROM {table} ORDER BY 1"
                ).fetchall()
            ]
            options = {
                dimension: [
                    row[0]
                    for row in self.con.execute(
                        f"SELECT DISTINCT {quote(dimension)} FROM {table} WHERE {quote(dimension)} IS NOT NULL ORDER BY 1"
                    ).fetchall()
                ]
                for dimension in feed.dimensions
            }
            key = PREFIX + feed.aliases[0]
            revision = hashlib.sha256(
                repr(
                    [
                        (p, Path(p).stat().st_mtime_ns, Path(p).stat().st_size)
                        for p in sources
                    ]
                ).encode()
            ).hexdigest()[:16]
            self.metadata[feed.name] = {
                "feed": feed.name,
                "key": key,
                "entity": feed.entity,
                "entities": entities,
                "options": options,
                "unit": feed.unit,
                "earliest": earliest,
                "latest": latest,
                "observations": count,
                "revision": revision,
            }
            matches = [
                item
                for item in catalog
                if item["key"] in {PREFIX + alias for alias in feed.aliases}
            ]
            if not matches:
                matches = [
                    {
                        "key": key,
                        "title": "CAISO / " + feed.title,
                        "name": feed.title,
                        "tokens": key.split("/"),
                        "venue": "CAISO",
                        "files": len(sources),
                    }
                ]
                catalog.extend(matches)
            for item in matches:
                item.update(
                    transport="research-history",
                    title="CAISO / " + feed.title,
                    name=feed.title,
                    researchFeed=feed.name,
                    revision=revision,
                )
            LOG.info(
                "PRELOAD %s: %d files, %d rows → indexed RAM",
                feed.name,
                len(sources),
                count,
            )
        return catalog

    def series(
        self,
        key: str,
        entity: str | None = None,
        start: str | None = None,
        end: str | None = None,
        filters: dict[str, list[str]] | None = None,
        max_points: int = 1500,
    ) -> dict:
        feed = self.resolve(key)
        meta = self.metadata[feed.name]
        if max_points != 0 and not 4 <= max_points <= 1500:
            raise ValueError(
                "max_points must be 4–1500, or 0 for a complete entity history"
            )
        lower, upper = boundary(start), boundary(end)
        if lower and upper and lower >= upper:
            raise ValueError("start must precede the exclusive end")
        active = entity if entity is not None else next(iter(meta["entities"]), None)
        clauses, parameters = [f"{quote(feed.entity)} = ?"], [active]
        if lower:
            clauses.append("timestamp >= ?")
            parameters.append(lower)
        if upper:
            clauses.append("timestamp < ?")
            parameters.append(upper)
        for dimension, values in (filters or {}).items():
            if (
                dimension not in feed.dimensions
                or not isinstance(values, list)
                or len(values) > 100
                or any(not isinstance(value, str) for value in values)
            ):
                raise ValueError("Invalid research filter")
            if not values:
                clauses.append("false")
            else:
                clauses.append(
                    f"{quote(dimension)} IN ({','.join('?' for _ in values)})"
                )
                parameters.extend(values)
        dimensions = ",".join(quote(dimension) for dimension in feed.dimensions)
        # Published snapshots repeat intervals: prefer latest as_of_date and never
        # sum separate limits, auctions, constraints, or time-of-use prices.
        base = f"""SELECT timestamp,{dimensions},first(value ORDER BY observed_at DESC NULLS LAST) AS value
            FROM research_{feed.name} WHERE {" AND ".join(clauses)} GROUP BY timestamp,{dimensions}"""
        query = f"SELECT *,count(*) OVER(PARTITION BY {dimensions}) AS source_count FROM ({base})"
        if max_points:
            buckets = max_points // 4
            query = f"""WITH source AS ({query}), numbered AS (
                SELECT *,row_number() OVER(PARTITION BY {dimensions} ORDER BY timestamp) AS n FROM source
            ), binned AS (SELECT *,floor((n-1)/ceil(source_count/{buckets}.0)) AS bucket FROM numbered),
            ranked AS (SELECT *,row_number() OVER(PARTITION BY {dimensions},bucket ORDER BY timestamp) AS first_n,
                row_number() OVER(PARTITION BY {dimensions},bucket ORDER BY timestamp DESC) AS last_n,
                row_number() OVER(PARTITION BY {dimensions},bucket ORDER BY value ASC NULLS LAST) AS low_n,
                row_number() OVER(PARTITION BY {dimensions},bucket ORDER BY value DESC NULLS LAST) AS high_n FROM binned)
            SELECT timestamp,{dimensions},value,source_count FROM ranked WHERE first_n=1 OR last_n=1 OR low_n=1 OR high_n=1"""
        rows = self.con.execute(
            f"SELECT strftime(timestamp, '%Y-%m-%dT%H:%M:%SZ'),{dimensions},value,source_count FROM ({query}) ORDER BY {dimensions},timestamp",
            parameters,
        ).fetchall()
        series = {}
        for row in rows:
            dimension_values = dict(
                zip(feed.dimensions, row[1 : 1 + len(feed.dimensions)])
            )
            identity = json.dumps(dimension_values, sort_keys=True)
            trace = series.setdefault(
                identity,
                {
                    "key": identity,
                    "dimensions": dimension_values,
                    "timestamps": [],
                    "values": [],
                    "sourceCount": row[-1],
                },
            )
            trace["timestamps"].append(row[0])
            trace["values"].append(row[-2])
        return {
            "status": "ok" if rows else "empty",
            "feed": feed.name,
            "entity": active,
            "unit": feed.unit,
            "revision": meta["revision"],
            "series": list(series.values()),
            "start": start,
            "end": end,
        }
