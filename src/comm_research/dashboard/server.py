"""FastAPI workstation with a startup-only, in-memory CAISO lake snapshot."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import polars as pl
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware

from comm_research.dashboard.app import (
    DEFAULT_LAKE_ROOT,
    PROJECT_ROOT,
    WEB_ROOT,
)
from comm_research.dashboard.data.loader import clear_caches, inspect_dataset
from comm_research.dashboard.lake_api import LakeAPI
from comm_research.markets.power_gas.napg.caiso.dashboard.prices import (
    CaisoMemoryLake,
    resolve_market,
)

__all__ = ["CaisoMemoryLake", "create_app", "frontend_document", "resolve_market"]

LOG = logging.getLogger("caiso.workstation")


def frontend_document(path: Path) -> tuple[str, str]:
    """Version the entire ES module graph, including relative child imports.

    A version on main.js alone leaves its relative imports cached at their old
    URLs. An import map assigns every application module a common content
    version, so a page cannot mix an old parser with a new panel.
    """
    modules = sorted((WEB_ROOT / "src").rglob("*.js"))
    styles = sorted((WEB_ROOT / "styles").glob("*.css"))
    digest = hashlib.sha256()
    for asset in [*modules, *styles]:
        digest.update(asset.relative_to(WEB_ROOT).as_posix().encode())
        digest.update(asset.read_bytes())
    version = digest.hexdigest()[:16]
    urls = ["/" + asset.relative_to(WEB_ROOT).as_posix() for asset in modules]
    imports = {url: f"{url}?v={version}" for url in urls}
    html = path.read_text()
    import_map = (
        '<script type="importmap">' + json.dumps({"imports": imports}) + "</script>"
    )
    html = html.replace(
        '<script type="module"', import_map + '\n    <script type="module"', 1
    )
    for asset in [*modules, *styles]:
        url = "/" + asset.relative_to(WEB_ROOT).as_posix()
        html = html.replace('src="' + url + '"', 'src="' + url + "?v=" + version + '"')
        html = html.replace(
            'href="' + url + '"', 'href="' + url + "?v=" + version + '"'
        )
    return html, version


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

    @app.middleware("http")
    async def revalidate_frontend(request: Request, call_next):
        response = await call_next(request)
        if request.url.path in {"/", "/popout.html"} or request.url.path.startswith(
            ("/src/", "/styles/")
        ):
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
        elif request.url.path.startswith("/api/research/"):
            response.headers["Cache-Control"] = "no-store"
        return response

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
        template = static_root / "index.html"
        if not template.is_file():
            template = WEB_ROOT / "workstation.html"
        html, version = frontend_document(template)
        return HTMLResponse(html, headers={"X-Frontend-Version": version})

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
