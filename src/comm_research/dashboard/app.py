"""Local HTTP server for the commodities HTML dashboard."""

from __future__ import annotations

import argparse
import json
import os
from functools import partial
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import polars as pl

from comm_research.dashboard.data.loader import (
    LakeError,
    clear_caches,
    discover_lake,
    inspect_dataset,
)
from comm_research.dashboard.lake_api import LakeAPI, _dataset_json

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_LAKE_ROOT = PROJECT_ROOT / "data/lake"
WEB_ROOT = Path(__file__).resolve().parent / "ui"
MAX_REQUEST_BYTES = 64_000
TIMEZONES = {"UTC", "America/Los_Angeles", "America/New_York"}
FREQUENCIES = {"native", "5m", "1h", "1d"}
PAGE_SIZES = {100, 250, 500}


class DashboardHandler(BaseHTTPRequestHandler, LakeAPI):
    """Serve the dashboard assets and local lake analytics endpoints."""

    server_version = "CommodityResearch"
    sys_version = ""

    def __init__(self, *args: Any, lake_root: str, **kwargs: Any) -> None:
        self.lake_root = lake_root
        super().__init__(*args, **kwargs)

    def do_GET(self) -> None:
        request = urlparse(self.path)
        try:
            if request.path == "/api/health":
                self._json({"status": "ok"})
            elif request.path == "/api/datasets":
                self._json(
                    [
                        _dataset_json(dataset)
                        for dataset in discover_lake(self.lake_root)
                    ]
                )
            elif request.path == "/api/metadata":
                self._metadata(self._query_value(request.query, "key"))
            elif request.path == "/api/options":
                self._options(
                    self._query_value(request.query, "key"),
                    self._query_value(request.query, "dimension"),
                )
            elif request.path == "/":
                self._asset("index.html", "text/html; charset=utf-8")
            elif request.path.startswith(("/vendor/", "/styles/", "/src/")):
                self._static_asset(request.path)
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        except KeyError as error:
            self._json(
                {"error": str(error.args[0]) or "Unknown dataset."},
                HTTPStatus.NOT_FOUND,
            )
        except (ValueError, LakeError, TypeError) as error:
            self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        except (OSError, pl.exceptions.PolarsError) as error:
            self._json({"error": str(error)}, HTTPStatus.UNPROCESSABLE_ENTITY)

    def do_POST(self) -> None:
        request = urlparse(self.path)
        if request.path == "/api/rescan":
            try:
                clear_caches()
                self._json(
                    [
                        _dataset_json(dataset)
                        for dataset in discover_lake(self.lake_root)
                    ]
                )
            except (OSError, pl.exceptions.PolarsError) as error:
                self._json({"error": str(error)}, HTTPStatus.UNPROCESSABLE_ENTITY)
            return
        if request.path not in ("/api/series", "/api/rows"):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            payload = self._read_json()
            dataset = self._dataset(payload.get("key"))
            metadata = inspect_dataset(dataset)
            start, end, zone, filters = self._query_controls(payload, dataset, metadata)
            if request.path == "/api/series":
                self._series(payload, dataset, start, end, zone, filters)
            else:
                self._rows(payload, dataset, start, end, zone, filters, metadata)
        except KeyError as error:
            self._json(
                {"error": str(error.args[0]) or "Unknown dataset."},
                HTTPStatus.NOT_FOUND,
            )
        except (ValueError, LakeError, TypeError) as error:
            self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        except (OSError, pl.exceptions.PolarsError) as error:
            self._json({"error": str(error)}, HTTPStatus.UNPROCESSABLE_ENTITY)

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ValueError("Invalid request length.") from error
        if length < 0 or length > MAX_REQUEST_BYTES:
            raise ValueError("Request body is too large.")
        try:
            payload = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ValueError("Request body must be valid JSON.") from error
        if not isinstance(payload, dict):
            raise TypeError("Request body must be a JSON object.")
        return payload

    @staticmethod
    def _query_value(query: str, name: str) -> str:
        values = parse_qs(query).get(name, [])
        if len(values) != 1 or not values[0]:
            raise ValueError(f"Expected one {name} parameter.")
        return values[0]

    def _asset(self, name: str, content_type: str) -> None:
        content = (WEB_ROOT / name).read_bytes()
        self._send(content, content_type)

    def _static_asset(self, request_path: str) -> None:
        relative_path = unquote(request_path).lstrip("/")
        asset_path = (WEB_ROOT / relative_path).resolve()
        try:
            asset_path.relative_to(WEB_ROOT.resolve())
        except ValueError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if asset_path.suffix not in {".css", ".js"} or not asset_path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content_type = (
            "text/javascript; charset=utf-8"
            if asset_path.suffix == ".js"
            else "text/css; charset=utf-8"
        )
        self._send(asset_path.read_bytes(), content_type)

    def _json(self, value: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        content = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self._send(content, "application/json; charset=utf-8", status)

    def _send(
        self,
        content: bytes,
        content_type: str,
        status: HTTPStatus = HTTPStatus.OK,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; "
            "img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format: str, *args: Any) -> None:
        super().log_message(format, *args)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local commodities dashboard.")
    parser.add_argument(
        "--lake",
        default=os.environ.get("COMMODITY_LAKE_ROOT", str(DEFAULT_LAKE_ROOT)),
        help="Local directory containing Parquet or Arrow lake files.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind address.")
    parser.add_argument("--port", type=int, default=8502, help="HTTP port.")
    args = parser.parse_args()
    lake_root = str(Path(args.lake).expanduser().resolve())
    handler = partial(DashboardHandler, lake_root=lake_root)
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"Commodities dashboard: http://{args.host}:{args.port}/")
    print(f"Local data lake: {lake_root}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping dashboard.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
