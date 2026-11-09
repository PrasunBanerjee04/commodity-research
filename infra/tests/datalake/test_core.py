"""Offline tests for native local files; no metadata database or schema declarations."""

from __future__ import annotations

import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import polars as pl
import pytest
from datalake import (
    ConcurrentWriteError,
    DataLake,
    DatasetNotFoundError,
    DataValidationError,
    InvalidQueryError,
    InvalidTimeError,
    QueryLimitExceededError,
)
from datalake.storage.local_store import writer_lock

T0 = datetime(2025, 1, 1, tzinfo=timezone.utc)


def frame(hours=(0, 1, 2), values=None):
    return pl.DataFrame(
        {
            "timestamp": [T0 + timedelta(hours=h) for h in hours],
            "site": ["A"] * len(hours),
            "value": values if values is not None else [float(h) for h in hours],
        },
        schema={"timestamp": pl.Datetime("us", "UTC"), "site": pl.String, "value": pl.Float64},
    )


@pytest.fixture
def lake(tmp_path):
    return DataLake(tmp_path / "lake")


@pytest.fixture
def partitioned(lake):
    for day, hours in ((1, (0, 1)), (2, (24, 25))):
        path = lake.root / f"provider/observations/year=2025/month=01/day={day:02d}/data.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        frame(hours).write_parquet(path)
    return lake


def test_discovery_existing_partitions_no_registration(partitioned):
    lake = partitioned
    audit = lake.root / "provider/observations/_audit/coverage.csv"
    audit.parent.mkdir()
    frame().write_csv(audit)
    assert lake.list_datasets() == ["provider/observations"]
    assert lake.search("PROVIDER obs") == ["provider/observations"]
    assert lake.search("absent") == []
    assert lake.list_datasets(prefix="provider") == ["provider/observations"]
    info = lake.describe("provider/observations", count_rows=True)
    assert info["rows"] == 4 and info["files"] == 2
    assert info["columns"]["value"] == "Float64"
    assert info["format"] == "parquet" and info["size_bytes"] > 0
    assert lake.describe("provider/observations")["rows"] is None
    assert not list(lake.root.rglob("*.sqlite"))


def test_default_paths_and_clean_area(tmp_path, monkeypatch):
    monkeypatch.setenv("COMM_RESEARCH_ROOT", str(tmp_path))
    assert DataLake().root == tmp_path / "data/lake"
    assert DataLake(area="clean").root == tmp_path / "data/clean"
    assert DataLake.open(area="raw").root == tmp_path / "data/raw"
    assert not (tmp_path / "data").exists()  # opening/discovery are read-only
    assert DataLake().list_datasets() == []


def test_native_csv_and_audit_exclusion(tmp_path):
    folder = tmp_path / "clean/measurements"
    folder.mkdir(parents=True)
    frame((0, 1)).write_csv(folder / "part-0.csv")
    frame((2, 3)).write_csv(folder / "part-1.csv")
    audit = folder / "_audit"
    audit.mkdir()
    pl.DataFrame({"status": ["complete"]}).write_csv(audit / "coverage.csv")
    lake = DataLake(tmp_path / "clean")
    assert lake.list_datasets() == ["measurements"]
    result = lake.read("measurements", start="2025-01-01", end="2025-01-02")
    assert result.height == 4
    assert result.schema["timestamp"] == pl.Datetime("us", "UTC")
    assert lake.read("measurements/part-0.csv").height == 2


def test_root_files_discovery_and_mixed_formats(tmp_path):
    frame().write_parquet(tmp_path / "table.parquet")
    assert DataLake(tmp_path).list_datasets() == ["table.parquet"]
    folder = tmp_path / "mixed"
    folder.mkdir()
    frame().write_parquet(folder / "one.parquet")
    frame().write_csv(folder / "two.csv")
    with pytest.raises(InvalidQueryError):
        DataLake(tmp_path).read("mixed")


def test_half_open_read_projection_filters_and_lazy_scan(partitioned):
    lake = partitioned
    selected = lake.read(
        "provider/observations",
        start=T0,
        end=T0 + timedelta(hours=24),
        columns=["value"],
        filters={"site": ["A", "B"], "year": 2025},
    )
    assert selected["value"].to_list() == [0.0, 1.0]
    assert lake.read("provider/observations", filters={"site": []}).is_empty()
    plan = lake.scan("provider/observations", columns=["value"], filters={"site": "A"}).explain()
    assert "SELECTION" in plan and "PROJECT" in plan
    assert lake.read("provider/observations", start="2026-01-01").is_empty()
    assert lake.read("provider/observations", limit=1).height == 1
    assert lake.read("provider/observations", order_by=["value"])["value"].to_list() == [
        0.0,
        1.0,
        24.0,
        25.0,
    ]


def test_null_filters(lake):
    lake.write("research/values", frame(values=[0.0, None, 2.0]))
    assert lake.read("research/values", filters={"value": None}).height == 1
    assert lake.read("research/values", filters={"value": [None, 2.0]}).height == 2


@pytest.mark.parametrize(
    "kwargs",
    [
        {"columns": ["absent"]},
        {"columns": []},
        {"filters": {"site": 1}},
        {"filters": {"unknown": 1}},
        {"filters": {"value": True}},
        {"order_by": ["unknown"]},
        {"limit": 0},
        {"limit": True},
        {"start": "2025-01-02", "end": "2025-01-01"},
        {"start": datetime(2025, 1, 1)},
        {"timezone": "Not/AZone"},
    ],
)
def test_invalid_queries(lake, kwargs):
    lake.write("values", frame())
    with pytest.raises(InvalidQueryError):
        lake.read("values", **kwargs)


def test_infer_time_or_require_explicit_column(lake):
    lake.write(
        "multiple",
        frame().rename({"timestamp": "event"}).with_columns(pl.col("event").alias("published")),
    )
    with pytest.raises(InvalidQueryError):
        lake.read("multiple", start="2025-01-01")
    assert lake.read("multiple", start="2025-01-01", time_column="event").height == 3
    lake.write("no-time", pl.DataFrame({"value": [1, 2]}))
    assert lake.read("no-time").height == 2
    with pytest.raises(InvalidQueryError):
        lake.read("no-time", start="2025-01-01")


def test_time_filters_and_dst(lake):
    hours = [
        datetime.fromisoformat("2025-11-02T01:00:00-04:00").astimezone(timezone.utc),
        datetime.fromisoformat("2025-11-02T01:00:00-05:00").astimezone(timezone.utc),
    ]
    data = frame((0, 1)).with_columns(pl.Series("timestamp", hours))
    lake.write("dst", data)
    assert lake.read("dst", start="2025-11-02", end="2025-11-03")["timestamp"].n_unique() == 2
    assert lake.read("dst", filters={"timestamp": "2025-11-02T01:00:00-04:00"}).height == 1
    with pytest.raises(InvalidTimeError):
        lake.read("dst", start="2025-11-02T01:30:00", timezone="America/New_York")
    naive = frame().with_columns(pl.col("timestamp").dt.replace_time_zone(None))
    lake.write("naive", naive)
    with pytest.raises(InvalidTimeError):
        lake.read("naive", start="2025-01-01")
    assert lake.read("naive", start="2025-01-01", timezone="UTC").height == 3


def test_schema_inferred_and_all_write_modes(lake):
    original = frame((0,))
    target = lake.write("research/features", original)
    assert target == lake.root / "research/features/data.parquet"
    assert pl.read_parquet(target).equals(original)
    with pytest.raises(FileExistsError):
        lake.write("research/features", original)
    lake.write("research/features", frame((1,)), mode="append", keys=["timestamp", "site"])
    with pytest.raises(DataValidationError):
        lake.write("research/features", frame((0,)), mode="append", keys=["timestamp", "site"])
    lake.write("research/features", frame((0,), [9.0]), mode="upsert", keys=["timestamp", "site"])
    assert lake.read("research/features")["value"].to_list() == [9.0, 1.0]
    lake.write("research/features", frame((2,)), mode="replace")
    assert lake.read("research/features").equals(frame((2,)))
    assert DataLake(lake.root).read("research/features").equals(frame((2,)))
    assert not list(lake.root.rglob("*.json")) and not list(lake.root.rglob("*.sqlite"))


def test_no_key_guessing_or_silent_schema_cast(lake):
    lake.write("values", frame())
    with pytest.raises(InvalidQueryError):
        lake.write("values", frame(), mode="upsert")
    with pytest.raises(DataValidationError):
        lake.write("values", frame().with_columns(pl.col("value").cast(pl.Int64)), mode="append")
    with pytest.raises(DataValidationError):
        lake.write("duplicate", pl.concat([frame(), frame()]), keys=["timestamp"])
    with pytest.raises(InvalidQueryError):
        lake.write("values", frame(), keys=[])
    with pytest.raises(DataValidationError):
        lake.write(
            "nulls",
            frame().with_columns(pl.lit(None, dtype=pl.String).alias("site")),
            keys=["site"],
        )
    lake.write("empty", frame(()))
    assert lake.read("empty").is_empty()


def test_partitioned_writes_refused_and_files_preserved(partitioned):
    before = {p: p.read_bytes() for p in partitioned.root.rglob("*.parquet")}
    for name in (
        "provider/observations",
        "provider/observations/new",
        "provider/observations/year=2025/month=01/day=01/data.parquet",
    ):
        with pytest.raises(InvalidQueryError):
            partitioned.write(name, frame(), mode="replace")
    assert all(path.read_bytes() == data for path, data in before.items())


def test_nested_outputs_and_single_file_writes(lake):
    lake.write("output", frame())
    with pytest.raises(InvalidQueryError):
        lake.write("output/nested", frame())
    target = lake.write("standalone.parquet", frame())
    assert target == lake.root / "standalone.parquet"
    lake.write("standalone.parquet", frame((5,)), mode="replace")
    assert lake.read("standalone.parquet").equals(frame((5,)))


def test_atomic_write_failure_preserves_existing_file(lake):
    target = lake.write("values", frame())
    previous = target.read_bytes()
    with patch("datalake.storage.local_store.os.replace", side_effect=OSError("disk failure")):
        with pytest.raises(OSError):
            lake.write("values", frame((9,)), mode="replace")
    assert target.read_bytes() == previous
    assert not list(target.parent.glob(".write-*"))


def test_writer_lock_and_concurrent_appends(lake):
    lake.write("values", frame((0,)))
    with writer_lock(lake.root):
        with pytest.raises(ConcurrentWriteError):
            lake.write("values", frame((1,)), mode="append")

    def append(value):
        for _ in range(100):
            try:
                lake.write("values", frame((value,)), mode="append", keys=["timestamp"])
                return
            except ConcurrentWriteError:
                import time

                time.sleep(0.001)
        raise AssertionError("Writer never acquired the lock")

    with ThreadPoolExecutor(2) as pool:
        list(pool.map(append, [1, 2]))
    assert lake.read("values").height == 3


@pytest.mark.parametrize("format", ["parquet", "csv"])
def test_export_and_no_overwrite(lake, tmp_path, format):
    lake.write("values", frame(values=[1.0, None, 3.0]))
    destination = tmp_path / f"export.{format}"
    assert lake.export("values", destination=destination) == destination
    restored = (
        pl.read_parquet(destination)
        if format == "parquet"
        else pl.read_csv(destination, try_parse_dates=True)
    )
    assert restored.equals(frame(values=[1.0, None, 3.0]))
    with pytest.raises(FileExistsError):
        lake.export("values", destination=destination)
    with pytest.raises(InvalidQueryError):
        lake.export("values", destination=lake.root / "bad.parquet")
    with pytest.raises(InvalidQueryError):
        lake.export("values", destination=tmp_path / "output.xlsx")


def test_limits_timeout_and_closed_instance(lake):
    lake.write("values", frame())
    with pytest.raises(QueryLimitExceededError):
        DataLake(lake.root, max_rows=2).read("values")
    assert DataLake(lake.root, max_rows=2).read("values", limit=1).height == 1
    with pytest.raises(QueryLimitExceededError):
        DataLake(lake.root, max_bytes=1).read("values")
    with patch("datalake.api.pl.LazyFrame.collect") as collect:
        collect.return_value.fetch.return_value = None
        with patch("datalake.api.time.monotonic", side_effect=[0.0, 100.0]):
            with pytest.raises(QueryLimitExceededError):
                lake.read("values")
        collect.return_value.cancel.assert_called_once()
    with DataLake.open(lake.root) as scoped:
        assert scoped.read("values").height == 3
    with pytest.raises(InvalidQueryError):
        scoped.read("values")


@pytest.mark.parametrize("name", ["../outside", "/tmp/outside", "a/../../outside", ".", "", "a\\b"])
def test_path_traversal_and_invalid_names(lake, name):
    with pytest.raises(InvalidQueryError):
        lake.read(name)
    with pytest.raises(InvalidQueryError):
        lake.write(name, frame())


def test_external_symlinks_and_missing_dataset(lake, tmp_path):
    lake.root.mkdir()
    external = tmp_path / "external.parquet"
    frame().write_parquet(external)
    (lake.root / "outside.parquet").symlink_to(external)
    assert lake.list_datasets() == []
    with pytest.raises(InvalidQueryError):
        lake.read("outside.parquet")
    folder = lake.root / "trap"
    folder.mkdir()
    (folder / "data.parquet").symlink_to(external)
    with pytest.raises(InvalidQueryError):
        lake.write("trap", frame(), mode="replace")
    with pytest.raises(DatasetNotFoundError):
        lake.read("unknown")


def test_quickstart_runs_outside_checkout(tmp_path):
    script = Path(__file__).resolve().parents[2] / "examples/datalake/quickstart.py"
    result = subprocess.run(
        [sys.executable, str(script)], cwd=tmp_path, capture_output=True, text=True, check=True
    )
    assert "Quickstart passed" in result.stdout


def test_documentation_examples_use_native_layout(tmp_path, monkeypatch):
    monkeypatch.setenv("COMM_RESEARCH_ROOT", str(tmp_path))
    root = tmp_path / "data/lake"
    weather = root / "weather/era5/t_2m/year=2025/month=01/day=01/data.parquet"
    weather.parent.mkdir(parents=True)
    frame().drop("site").with_columns(
        pl.lit(32.5).alias("latitude"), pl.lit(-121.0).alias("longitude")
    ).write_parquet(weather)
    prices = root / "power_gas/napg/caiso/dam_lmp/year=2025/month=01/day=01/data.parquet"
    prices.parent.mkdir(parents=True)
    frame().rename({"timestamp": "interval_start_time_gmt", "site": "node"}).with_columns(
        pl.lit("TH_SP15_GEN-APND").alias("node"),
        (pl.col("interval_start_time_gmt") + timedelta(hours=1)).alias("interval_end_time_gmt"),
    ).write_parquet(prices)
    guide = Path(__file__).resolve().parents[3] / "setup/docs/datalake.md"
    namespace = {"__name__": "documentation_examples"}
    for index, snippet in enumerate(re.findall(r"```python\n(.*?)```", guide.read_text(), re.S)):
        snippet = snippet.replace("/tmp/features.", str(tmp_path / "exports/features."))
        exec(compile(snippet, f"datalake.md:block-{index + 1}", "exec"), namespace)


def test_core_works_without_catalog_dependencies(tmp_path):
    code = f"""
import sys, importlib.abc
class NoCatalog(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {{'sqlite3', 'sqlalchemy', 'pydantic', 'yaml'}}:
            raise ModuleNotFoundError('Unavailable dependency: ' + fullname)
sys.meta_path.insert(0, NoCatalog())
import polars as pl
from datalake import DataLake
lake = DataLake({str(tmp_path / "lake")!r})
lake.write('example', pl.DataFrame({{'value': [1.0]}}))
assert lake.read('example')['value'].to_list() == [1.0]
assert not {{'sqlite3', 'sqlalchemy', 'pydantic', 'yaml'}} & sys.modules.keys()
"""
    subprocess.run(
        [sys.executable, "-c", code], cwd=tmp_path, check=True, capture_output=True, text=True
    )


def test_write_existing_single_file_without_renaming(lake):
    target = lake.root / "native/observations.parquet"
    target.parent.mkdir(parents=True)
    frame((0,)).write_parquet(target)
    assert lake.write("native", frame((1,)), mode="append", keys=["timestamp"]) == target
    assert lake.read("native").height == 2
    assert not (target.parent / "data.parquet").exists()
    lake.write("native/observations.parquet", frame((1,), [5.0]), mode="upsert", keys=["timestamp"])
    assert lake.read("native")["value"].to_list() == [0.0, 5.0]
