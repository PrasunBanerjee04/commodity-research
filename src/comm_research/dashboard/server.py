"""FastAPI workstation with a startup-only, in-memory CAISO lake snapshot."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import logging
import os
from collections import OrderedDict
from contextlib import asynccontextmanager
from pathlib import Path
from threading import RLock
from time import perf_counter
from typing import Any

import duckdb
import polars as pl
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware

from comm_research.dashboard.app import (
    DEFAULT_LAKE_ROOT,
    PROJECT_ROOT,
    WEB_ROOT,
)
from comm_research.dashboard.data.loader import (
    clear_caches,
    discover_lake,
    inspect_dataset,
)
from comm_research.dashboard.lake_api import LakeAPI, _dataset_json
from comm_research.dashboard.research_lake import ResearchMemoryLake

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


def create_app(
    lake_root: Path = DEFAULT_LAKE_ROOT, memory_limit: str = "2GB"
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.lake = CaisoMemoryLake(lake_root, memory_limit)
        try:
            yield
        finally:
            app.state.lake.close()

    app = FastAPI(title="CAISO Commodities Workstation", lifespan=lifespan)
    app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=3)

    @app.exception_handler(ValueError)
    @app.exception_handler(TypeError)
    async def invalid_query(_request: Request, error: ValueError):
        return JSONResponse({"error": str(error)}, status_code=400)

    @app.exception_handler(KeyError)
    async def missing_feed(_request: Request, error: KeyError):
        return JSONResponse({"error": str(error.args[0])}, status_code=404)

    @app.exception_handler(pl.exceptions.PolarsError)
    async def invalid_lake(_request: Request, error: Exception):
        return JSONResponse({"error": str(error)}, status_code=422)

    @app.get("/api/health")
    def health(request: Request):
        lake = request.app.state.lake
        return {
            "status": "ok",
            "revision": lake.revision,
            "nodes": {market: len(nodes) for market, nodes in lake.nodes.items()},
        }

    @app.get("/api/data")
    def data(request: Request, feed: str, node: str | None = None):
        raw, compressed = request.app.state.lake.series(feed, node)
        headers = {
            "Cache-Control": "no-cache",
            "ETag": '"' + hashlib.sha256(raw).hexdigest() + '"',
            "Vary": "Accept-Encoding",
        }
        if request.headers.get("if-none-match") == headers["ETag"]:
            return Response(status_code=304, headers=headers)
        if "gzip" in request.headers.get("accept-encoding", ""):
            headers["Content-Encoding"] = "gzip"
            return Response(compressed, media_type="application/json", headers=headers)
        return Response(raw, media_type="application/json", headers=headers)

    @app.get("/api/nodes")
    def nodes(request: Request, feed: str):
        lake = request.app.state.lake
        with lake.lock:
            return {
                "nodes": lake.nodes[resolve_market(feed)],
                "revision": lake.revision,
            }

    @app.get("/api/research/metadata")
    def research_metadata(request: Request, feed: str):
        lake = request.app.state.lake
        with lake.lock:
            specification = lake.research.resolve(feed)
            return lake.research.metadata[specification.name]

    @app.get("/api/research/data")
    def research_data(
        request: Request,
        feed: str,
        entity: str | None = None,
        start: str | None = None,
        end: str | None = None,
        filters: str = "{}",
        max_points: int = 1500,
    ):
        if len(filters) > 16_000:
            raise ValueError("Filters are too large")
        decoded = json.loads(filters)
        if not isinstance(decoded, dict):
            raise TypeError("Filters must be a JSON object")
        lake = request.app.state.lake
        with lake.lock:
            return lake.research.series(feed, entity, start, end, decoded, max_points)

    @app.get("/api/datasets")
    def datasets(request: Request):
        return request.app.state.lake.datasets

    @app.post("/api/rescan")
    def rescan(request: Request):
        request.app.state.lake.preload()
        clear_caches()
        return request.app.state.lake.datasets

    @app.get("/api/metadata")
    def metadata(key: str):
        api = LakeAPI(str(lake_root))
        api._metadata(key)
        return api.result

    @app.get("/api/options")
    def options(key: str, dimension: str):
        api = LakeAPI(str(lake_root))
        api._options(key, dimension)
        return api.result

    @app.post("/api/series")
    @app.post("/api/rows")
    def generic_series(request: Request, payload: dict[str, Any]):
        # Keep the generic workstation API; CAISO full-history panels never use it.
        if int(request.headers.get("content-length", "0")) > 64_000:
            raise HTTPException(413, "Request too large")
        if not isinstance(payload, dict):
            raise TypeError("Request body must be an object")
        api = LakeAPI(str(lake_root))
        dataset = api._dataset(payload.get("key"))
        meta = inspect_dataset(dataset)
        controls = api._query_controls(payload, dataset, meta)
        if request.url.path == "/api/rows":
            api._rows(payload, dataset, *controls, meta)
        else:
            api._series(payload, dataset, *controls)
        return api.result

    # Vendor files are pinned locally and loaded before panel initialization.
    for folder in ("src", "styles", "vendor"):
        app.mount("/" + folder, StaticFiles(directory=WEB_ROOT / folder), name=folder)
    static_root = PROJECT_ROOT / "static"

    @app.get("/popout.html")
    def popout():
        return FileResponse(WEB_ROOT / "popout.html")

    @app.get("/")
    def index():
        return FileResponse(static_root / "index.html")

    return app


app = create_app(
    Path(os.environ.get("CAISO_LAKE_ROOT", DEFAULT_LAKE_ROOT)),
    os.environ.get("CAISO_MEMORY_LIMIT", "2GB"),
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Preload the CAISO lake and launch the workstation"
    )
    parser.add_argument(
        "--lake",
        type=Path,
        default=Path(os.environ.get("CAISO_LAKE_ROOT", DEFAULT_LAKE_ROOT)),
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--memory-limit", default=os.environ.get("CAISO_MEMORY_LIMIT", "2GB")
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    uvicorn.run(
        create_app(args.lake, args.memory_limit), host=args.host, port=args.port
    )
