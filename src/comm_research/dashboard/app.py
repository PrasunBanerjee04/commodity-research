"""Local HTTP server for the commodities HTML dashboard."""

from __future__ import annotations

import argparse
import json
import math
import os
from datetime import date, datetime
from functools import partial
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse
from zoneinfo import ZoneInfo

import polars as pl

from comm_research.dashboard.data.loader import (
    Dataset,
    LakeError,
    clear_caches,
    dimension_options,
    discover_lake,
    inspect_dataset,
    load_raw_page,
    load_series,
    raw_row_count,
)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_LAKE_ROOT = PROJECT_ROOT / "data/lake"
WEB_ROOT = Path(__file__).resolve().parent / "ui"
MAX_REQUEST_BYTES = 64_000
TIMEZONES = {"UTC", "America/Los_Angeles", "America/New_York"}
FREQUENCIES = {"native", "5m", "1h", "1d"}
PAGE_SIZES = {100, 250, 500}


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _dataset_json(dataset: Dataset) -> dict[str, Any]:
    return {
        "key": dataset.key,
        "title": dataset.title,
        "name": dataset.name,
        "venue": dataset.venue,
        "tokens": list(dataset.tokens),
        "files": len(dataset.files),
    }


class DashboardHandler(BaseHTTPRequestHandler):
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

    def _metadata(self, key: str) -> None:
        dataset = self._dataset(key)
        metadata = inspect_dataset(dataset)
        earliest = (
            metadata.earliest.astimezone(ZoneInfo("UTC")).date()
            if metadata.earliest
            else None
        )
        latest = (
            metadata.latest.astimezone(ZoneInfo("UTC")).date()
            if metadata.latest
            else None
        )
        self._json(
            {
                "dataset": _dataset_json(dataset),
                "schema": [
                    {"field": field, "type": dtype} for field, dtype in metadata.schema
                ],
                "timeColumn": metadata.time_column,
                "earliest": earliest.isoformat() if earliest else None,
                "latest": latest.isoformat() if latest else None,
                "signals": [
                    {
                        "key": signal.key,
                        "label": signal.label,
                        "unit": signal.unit,
                        "component": signal.component,
                    }
                    for signal in metadata.signals
                ],
                "dimensions": list(metadata.dimensions),
            }
        )

    def _options(self, key: str, dimension: str) -> None:
        dataset = self._dataset(key)
        if dimension not in inspect_dataset(dataset).dimensions:
            raise LakeError("Unsupported dimension.")
        self._json({"values": dimension_options(dataset, dimension)})

    def _series(
        self,
        payload: dict[str, Any],
        dataset: Dataset,
        start: date | None,
        end: date | None,
        zone: str,
        filters: tuple[tuple[str, tuple[str, ...]], ...],
    ) -> None:
        if start is None or end is None:
            raise LakeError("This dataset has no recognized time field.")
        signals = payload.get("signals", [])
        if (
            not isinstance(signals, list)
            or len(signals) > 8
            or any(not isinstance(signal, str) for signal in signals)
        ):
            raise ValueError("Choose up to eight numeric signals.")
        frequency = payload.get("frequency", "native")
        aggregation = payload.get("aggregation", "Mean")
        if (
            not isinstance(frequency, str)
            or frequency not in FREQUENCIES
            or not isinstance(aggregation, str)
            or aggregation not in {"Mean", "Last"}
        ):
            raise ValueError("Invalid frequency or aggregation.")
        bundle = load_series(
            dataset,
            start,
            end,
            zone,
            tuple(signals),
            filters,
            frequency,
            aggregation,
            payload.get("fallbackToLatest") is True,
        )
        self._json(
            {
                "observations": bundle.observations,
                "points": bundle.points,
                "downsampled": bundle.downsampled,
                "fallbackHorizon": [day.isoformat() for day in bundle.fallback_horizon]
                if bundle.fallback_horizon
                else None,
                "plot": [
                    {
                        "timestamp": row["timestamp"]
                        .isoformat()
                        .replace("+00:00", "Z"),
                        "series": row["series"],
                        "value": _json_value(row["value"]),
                        "unit": row["unit"],
                        "component": row["component"],
                        "node": row["node"],
                    }
                    for row in bundle.plot.iter_rows(named=True)
                ],
                "statistics": [
                    {key: _json_value(value) for key, value in row.items()}
                    for row in bundle.statistics.iter_rows(named=True)
                ],
            }
        )

    def _rows(
        self,
        payload: dict[str, Any],
        dataset: Dataset,
        start: date | None,
        end: date | None,
        zone: str,
        filters: tuple[tuple[str, tuple[str, ...]], ...],
        metadata: Any,
    ) -> None:
        page = payload.get("page", 0)
        page_size = payload.get("pageSize", 250)
        if not isinstance(page, int) or page < 0:
            raise ValueError("Page must be a non-negative integer.")
        if not isinstance(page_size, int) or page_size not in PAGE_SIZES:
            raise ValueError("Page size must be 100, 250 or 500.")
        if metadata.time_column is None:
            start = end = None
        count = raw_row_count(dataset, start, end, zone, filters)
        frame = load_raw_page(dataset, start, end, zone, filters, page, page_size)
        self._json(
            {
                "count": count,
                "page": page,
                "pageSize": page_size,
                "rows": [
                    {key: _json_value(value) for key, value in row.items()}
                    for row in frame.iter_rows(named=True)
                ],
            }
        )

    def _query_controls(
        self, payload: dict[str, Any], dataset: Dataset, metadata: Any
    ) -> tuple[
        date | None,
        date | None,
        str,
        tuple[tuple[str, tuple[str, ...]], ...],
    ]:
        zone = payload.get("zone", "UTC")
        if not isinstance(zone, str) or zone not in TIMEZONES:
            raise ValueError("Choose a supported date zone.")
        start, end = (
            self._parse_date(payload.get("start")),
            self._parse_date(payload.get("end")),
        )
        if (start is None) != (end is None):
            raise ValueError("Start and end dates must be provided together.")
        if start and end and start > end:
            raise ValueError("Start date must be on or before end date.")
        if (start or end) and metadata.time_column is None:
            raise LakeError("This dataset has no recognized time field.")
        raw_filters = payload.get("filters", {})
        if not isinstance(raw_filters, dict):
            raise TypeError("Filters must be an object.")
        normalized = []
        for column, values in raw_filters.items():
            if column not in metadata.dimensions:
                raise LakeError(f"Unsupported dimension: {column}")
            if (
                not isinstance(values, list)
                or len(values) > 8
                or any(
                    not isinstance(value, str) or len(value) > 256 for value in values
                )
            ):
                raise ValueError("Choose up to eight valid values for each filter.")
            normalized.append((column, tuple(values)))
        return start, end, zone, tuple(normalized)

    def _dataset(self, key: Any) -> Dataset:
        if not isinstance(key, str) or not key:
            raise ValueError("A dataset key is required.")
        dataset = next(
            (item for item in discover_lake(self.lake_root) if item.key == key), None
        )
        if dataset is None:
            raise KeyError("Dataset is no longer available. Rescan the lake.")
        return dataset

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
    def _parse_date(value: Any) -> date | None:
        if value in (None, ""):
            return None
        if not isinstance(value, str):
            raise TypeError("Dates must use YYYY-MM-DD format.")
        return date.fromisoformat(value)

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
