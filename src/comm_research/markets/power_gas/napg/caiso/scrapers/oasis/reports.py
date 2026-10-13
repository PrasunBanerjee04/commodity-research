"""Declarative OASIS report definitions shared by the category scrapers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .client import CAISOOASISClient


@dataclass(frozen=True)
class Report:
    queryname: str
    market_run_id: str | None
    version: int
    max_days: int
    dataset_name: str
    requires_node: bool = False
    endpoint: str = "SingleZip"
    parameters: tuple[tuple[str, str], ...] = ()
    publication_lag_days: int = 0
    publication_lag_quarters: int = 0
    retention_months: int = 39
    available_from: date | None = None
    menu: str = ""
    description: str = ""
    snapshot: bool = False
    numeric_columns: tuple[str, ...] = ()
    date_columns: tuple[str, ...] = ()
    source: str = "gridstatus"
    interval_minutes: int | None = None
    required_components: tuple[str, ...] = ()
    required_parameters: tuple[str, ...] = ()
    latest_only: bool = False
    same_day_end: bool = False

    def __post_init__(self) -> None:
        import re

        if self.endpoint not in ("SingleZip", "GroupZip"):
            raise ValueError("Unsupported OASIS endpoint")
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9_]*", self.queryname):
            raise ValueError("Invalid OASIS report identifier")
        if not re.fullmatch(r"[a-z0-9][a-z0-9_]*", self.dataset_name):
            raise ValueError("Invalid dataset identifier")
        if not 1 <= self.max_days <= 15 or self.retention_months < 1:
            raise ValueError("Invalid report window or retention")
        if self.version < 1 or self.publication_lag_days < 0:
            raise ValueError("Invalid version or publication lag")
        reserved = {
            "queryname",
            "groupid",
            "version",
            "resultformat",
            "startdatetime",
            "enddatetime",
            "market_run_id",
            "node",
        }
        if reserved & dict(self.parameters).keys():
            raise ValueError("Report parameters cannot override protocol fields")

    def request_parameters(
        self, overrides: dict[str, str] | None = None, *, node: str | None = None
    ) -> dict[str, str]:
        values = dict(self.parameters)
        unknown = set(overrides or {}) - set(values)
        if unknown:
            raise ValueError(
                f"Unsupported parameters for {self.dataset_name}: {sorted(unknown)}"
            )
        values.update(overrides or {})
        missing = [key for key in self.required_parameters if not values.get(key)]
        if missing:
            raise ValueError(
                f"Configure {self.dataset_name} parameters: {', '.join(missing)}"
            )
        if self.market_run_id:
            values["market_run_id"] = self.market_run_id
        if self.requires_node:
            if not node or not node.strip():
                raise ValueError("An explicit node is required")
            values["node"] = node
        elif node is not None:
            raise ValueError("This report does not accept a node")
        return values


@dataclass(frozen=True)
class CategoryScraper:
    """A category's executable reports and menu entries awaiting API evidence."""

    name: str
    reports: tuple[Report, ...]
    unavailable: tuple[str, ...] = ()

    def download(
        self,
        client: CAISOOASISClient,
        report: Report,
        start: datetime,
        end: datetime,
        *,
        node: str | None = None,
        parameters: dict[str, str] | None = None,
    ) -> list[Path]:
        if report not in self.reports:
            raise ValueError(f"{report.dataset_name} does not belong to {self.name}")
        return client.download(report, start, end, node=node, parameters=parameters)
