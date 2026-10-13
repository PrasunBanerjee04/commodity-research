"""CAISO price snapshot and query adapters; shared routing lives in dashboard."""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
from collections import OrderedDict
from pathlib import Path
from threading import RLock
from time import perf_counter
from typing import Any

import duckdb

from comm_research.dashboard.data.loader import discover_lake
from comm_research.dashboard.lake_api import _dataset_json

from .research import ResearchMemoryLake

LOG = logging.getLogger("caiso.workstation")
FEEDS = {"DAM": ("dam_lmp", "da_lmp"), "RTM": ("rtm_lmp", "rt_lmp")}
COMPONENTS = ("lmp", "energy", "congestion", "loss")


def resolve_market(feed: str) -> str:
    """Only accept explicit market aliases or canonical CAISO dataset paths."""
    key = feed.strip().lower()
    for market, aliases in FEEDS.items():
        if (
            key == market.lower()
            or key in aliases
            or key in {f"power_gas/napg/caiso/{alias}" for alias in aliases}
        ):
            return market
    raise ValueError("Unknown CAISO feed. Choose DAM/dam_lmp or RTM/rtm_lmp.")


def identifier(column: str) -> str:
    return '"' + column.replace('"', '""') + '"'


class CaisoMemoryLake:
    """Read parquet at boot/rescan only; serialize each requested node once.

    A single lock protects DuckDB's shared connection and response LRU. No SQL
    provided by a browser is interpolated. Disabling spill prevents interactive
    queries from silently becoming disk scans on memory-constrained machines.
    """

    def __init__(
        self, root: Path, memory_limit: str = "2GB", response_bytes: int = 256 * 1024**2
    ):
        self.root = Path(root)
        self.lock = RLock()
        self.con = duckdb.connect(":memory:")
        self.con.execute("SET TimeZone='UTC'")
        self.con.execute("SET memory_limit = ?", [memory_limit])
        self.con.execute("SET temp_directory = ''")
        self.con.execute("SET threads=4")
        self.responses: OrderedDict[tuple[str, str], tuple[bytes, bytes]] = (
            OrderedDict()
        )
        self.response_bytes = response_bytes
        self.cached_bytes = 0
        self.nodes: dict[str, list[str]] = {}
        self.datasets: list[dict[str, Any]] = []
        self.preload()

    def preload(self) -> None:
        """Build the next snapshot transactionally; rescan is an explicit action."""
        started = perf_counter()
        with self.lock:
            discover_lake.cache_clear()
            catalog = [_dataset_json(item) for item in discover_lake(str(self.root))]
            next_nodes = {}
            previous = (
                self.nodes,
                self.datasets,
                self.responses,
                self.cached_bytes,
                getattr(self, "revision", ""),
            )
            self.con.execute("BEGIN TRANSACTION")
            try:
                for market, aliases in FEEDS.items():
                    table = "caiso_" + market.lower()
                    sources = sorted(
                        {
                            str(path)
                            for alias in aliases
                            for path in (
                                self.root / "power_gas/napg/caiso" / alias
                            ).rglob("*.parquet")
                        }
                    )
                    LOG.info("PRELOAD %s: %d parquet files → RAM", market, len(sources))
                    self.con.execute(f"DROP TABLE IF EXISTS {table}")
                    self.con.execute(
                        f"CREATE TABLE {table} (timestamp TIMESTAMPTZ, node VARCHAR, lmp_type VARCHAR, price DOUBLE)"
                    )
                    if sources:
                        schema = self.con.execute(
                            "DESCRIBE SELECT * FROM read_parquet(?, union_by_name=true, hive_partitioning=false)",
                            [sources],
                        ).fetchall()
                        columns = {row[0].lower(): row[0] for row in schema}

                        def field(name: str, columns=columns, market=market) -> str:
                            if name not in columns:
                                raise ValueError(
                                    f"{market}: missing required CAISO column {name}"
                                )
                            return identifier(columns[name])

                        prices = [
                            f"TRY_CAST({identifier(columns[name])} AS DOUBLE)"
                            for name in ("mw", "value", "price")
                            if name in columns
                        ]
                        if not prices:
                            raise ValueError(
                                f"{market}: missing price column mw/value/price"
                            )
                        price = (
                            prices[0]
                            if len(prices) == 1
                            else "COALESCE(" + ",".join(prices) + ")"
                        )
                        component = (
                            field("lmp_type")
                            if "lmp_type" in columns
                            else field("xml_data")
                        )
                        market_filter = (
                            f"AND ({field('market_run_id')} IS NULL OR upper(trim(CAST({field('market_run_id')} AS VARCHAR))) = ?)"
                            if "market_run_id" in columns
                            else ""
                        )
                        params = [sources, market] if market_filter else [sources]
                        self.con.execute(
                            f"""
                            INSERT INTO {table}
                            SELECT timestamp, node, lmp_type, CASE WHEN isfinite(price) THEN price END
                            FROM (
                                SELECT TRY_CAST({field("interval_start_time_gmt")} AS TIMESTAMPTZ) AS timestamp,
                                    CAST({field("node")} AS VARCHAR) AS node,
                                    CASE upper(trim(CAST({component} AS VARCHAR)))
                                        WHEN 'MCE' THEN 'ENERGY' WHEN 'MCC' THEN 'CONG'
                                        WHEN 'MCL' THEN 'LOSS' WHEN 'CONGESTION' THEN 'CONG'
                                        ELSE upper(trim(CAST({component} AS VARCHAR))) END AS lmp_type,
                                    {price} AS price
                                FROM read_parquet(?, union_by_name=true, hive_partitioning=false)
                                WHERE true {market_filter}
                            ) WHERE timestamp IS NOT NULL AND node IS NOT NULL
                                AND lmp_type IN ('LMP', 'ENERGY', 'CONG', 'LOSS')
                        """,
                            params,
                        )
                    self.con.execute(
                        f"CREATE INDEX idx_{market.lower()}_node ON {table}(node, timestamp)"
                    )
                    nodes = [
                        row[0]
                        for row in self.con.execute(
                            f"SELECT DISTINCT node FROM {table} ORDER BY node"
                        ).fetchall()
                    ]
                    next_nodes[market] = nodes
                    count, earliest, latest = self.con.execute(
                        f"SELECT count(*), strftime(min(timestamp), '%Y-%m-%dT%H:%M:%SZ'), strftime(max(timestamp), '%Y-%m-%dT%H:%M:%SZ') FROM {table}"
                    ).fetchone()
                    matching = [
                        item
                        for item in catalog
                        if item["key"]
                        in {f"power_gas/napg/caiso/{alias}" for alias in aliases}
                    ]
                    if not matching:
                        matching = [
                            {
                                "key": f"power_gas/napg/caiso/{aliases[0]}",
                                "name": "Day-Ahead LMP"
                                if market == "DAM"
                                else "Real-Time LMP",
                                "title": f"CAISO / {'Day-Ahead' if market == 'DAM' else 'Real-Time'} LMP",
                                "tokens": ["power_gas", "napg", "caiso", aliases[0]],
                                "venue": "CAISO",
                                "files": 0,
                                "revision": "empty",
                            }
                        ]
                        catalog.extend(matching)
                    for item in matching:
                        item.update(
                            transport="node-history",
                            market=market,
                            observations=count,
                            earliest=earliest,
                            latest=latest,
                        )
                    LOG.info(
                        "READY %s: %s component rows, %d nodes, %s → %s",
                        market,
                        f"{count:,}",
                        len(nodes),
                        earliest,
                        latest,
                    )
                next_research = ResearchMemoryLake(self.con, self.root)
                catalog = next_research.preload(catalog)
                self.nodes = next_nodes
                self.datasets = catalog
                self.responses = OrderedDict()
                self.cached_bytes = 0
                self.revision = hashlib.sha256(
                    json.dumps(catalog, sort_keys=True).encode()
                ).hexdigest()[:16]
                for item in self.datasets:
                    if item.get("transport") == "node-history":
                        item["revision"] = self.revision
                # Prepare a bounded first-paint overview and the default node's full
                # response at startup. The browser paints the overview immediately,
                # then replaces it with the exact full-history cache in background.
                for market in FEEDS:
                    if not self.nodes[market]:
                        continue
                    full = json.loads(self.series(market, None)[0])
                    count = len(full["timestamps"])
                    stride = max(1, (count + 139) // 140)
                    indices = set()
                    for start in range(0, count, stride):
                        end = min(count, start + stride)
                        indices.update((start, end - 1))
                        for component in COMPONENTS:
                            valid = [
                                i
                                for i in range(start, end)
                                if full[component][i] is not None
                            ]
                            if valid:
                                indices.add(
                                    min(
                                        valid,
                                        key=lambda i, component=component: full[
                                            component
                                        ][i],
                                    )
                                )
                                indices.add(
                                    max(
                                        valid,
                                        key=lambda i, component=component: full[
                                            component
                                        ][i],
                                    )
                                )
                    ordered = sorted(indices)
                    preview = {
                        "status": "ok",
                        "node": full["node"],
                        "preview": True,
                        "sourceCount": count,
                        "revision": self.revision,
                    }
                    preview.update(
                        {
                            column: [full[column][i] for i in ordered]
                            for column in ("timestamps", *COMPONENTS)
                        }
                    )
                    for item in self.datasets:
                        if item.get("market") == market:
                            item["preview"] = preview
                self.con.execute("COMMIT")
                self.research = next_research
            except Exception:
                self.con.execute("ROLLBACK")
                (
                    self.nodes,
                    self.datasets,
                    self.responses,
                    self.cached_bytes,
                    self.revision,
                ) = previous
                LOG.exception(
                    "CAISO preload failed; check schema or --memory-limit. No spill to disk is enabled."
                )
                raise
        LOG.info(
            "STARTUP snapshot ready in %.3fs. Chart queries now read RAM only.",
            perf_counter() - started,
        )

    def series(self, feed: str, node: str | None) -> tuple[bytes, bytes]:
        market = resolve_market(feed)
        with self.lock:
            nodes = self.nodes[market]
            active = (
                node
                if node in nodes
                else (
                    "TH_NP15_GEN-APND"
                    if "TH_NP15_GEN-APND" in nodes
                    else nodes[0]
                    if nodes
                    else node or "TH_NP15_GEN-APND"
                )
            )
            key = (market, active)
            if key in self.responses:
                self.responses.move_to_end(key)
                return self.responses[key]
            table = (
                "caiso_" + market.lower()
            )  # The resolved market is an internal constant.
            rows = self.con.execute(
                f"""
                SELECT strftime(timestamp, '%Y-%m-%dT%H:%M:%SZ'),
                    first(price) FILTER (WHERE lmp_type='LMP'),
                    first(price) FILTER (WHERE lmp_type='ENERGY'),
                    first(price) FILTER (WHERE lmp_type='CONG'),
                    first(price) FILTER (WHERE lmp_type='LOSS')
                FROM {table} WHERE node = ? GROUP BY timestamp ORDER BY timestamp
            """,
                [active],
            ).fetchall()
            result: dict[str, Any] = {
                "status": "ok" if rows else "empty",
                "node": active,
                "timestamps": [row[0] for row in rows],
                "revision": self.revision,
            }
            result.update(
                {
                    name: [row[index + 1] for row in rows]
                    for index, name in enumerate(COMPONENTS)
                }
            )
            raw = json.dumps(result, separators=(",", ":"), allow_nan=False).encode()
            compressed = gzip.compress(raw, compresslevel=3, mtime=0)
            size = len(raw) + len(compressed)
            if size <= self.response_bytes:
                while self.responses and self.cached_bytes + size > self.response_bytes:
                    _, removed = self.responses.popitem(last=False)
                    self.cached_bytes -= sum(map(len, removed))
                self.responses[key] = (raw, compressed)
                self.cached_bytes += size
            LOG.info(
                "HISTORY %s %s: %s timestamps, %.2f MiB compressed",
                market,
                active,
                f"{len(rows):,}",
                len(compressed) / 1024**2,
            )
            return raw, compressed

    def close(self) -> None:
        with self.lock:
            self.responses.clear()
            self.con.close()
