"""渠道指标导入的校验与落库测试。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from src.db import get_latest_metric, init_db
from src.metrics_import import import_metrics
from src.models import ChainConfig

HEADER = "metric_date,metric_id,value\n"


@pytest.fixture()
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = init_db(str(tmp_path / "inv.db"))
    yield connection
    connection.close()


@pytest.fixture()
def config(valid_payload: dict[str, Any]) -> ChainConfig:
    return ChainConfig.model_validate(valid_payload)


def _write_csv(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "manual.csv"
    path.write_text(HEADER + body, encoding="utf-8")
    return path


def _count(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM daily_metric").fetchone()
    return int(row["n"])


def test_import_writes_metrics(
    tmp_path: Path, conn: sqlite3.Connection, config: ChainConfig
) -> None:
    path = _write_csv(
        tmp_path,
        "2026-10-20,T05,1200\n2026-10-20,T06,45\n",
    )

    report = import_metrics(conn, config, "600519", path)

    assert report.imported == 2
    assert report.metric_ids == ["T05", "T06"]
    assert get_latest_metric(conn, "600519", "T05") == (1200.0, "2026-10-20")
    assert "T01" in report.missing
    assert "T05" not in report.missing


def test_unknown_metric_id_reports_line(
    tmp_path: Path, conn: sqlite3.Connection, config: ChainConfig
) -> None:
    path = _write_csv(tmp_path, "2026-10-20,T99,1\n")

    with pytest.raises(ValueError) as exc:
        import_metrics(conn, config, "600519", path)

    message = str(exc.value)
    assert "第 2 行 metric_id" in message
    assert "T99" in message
    assert _count(conn) == 0


def test_bad_date_reports_field(
    tmp_path: Path, conn: sqlite3.Connection, config: ChainConfig
) -> None:
    path = _write_csv(tmp_path, "2026/10/20,T05,1200\n")

    with pytest.raises(ValueError) as exc:
        import_metrics(conn, config, "600519", path)

    assert "第 2 行 metric_date" in str(exc.value)


def test_future_date_rejected(
    tmp_path: Path, conn: sqlite3.Connection, config: ChainConfig
) -> None:
    path = _write_csv(tmp_path, "2999-01-01,T05,1200\n")

    with pytest.raises(ValueError) as exc:
        import_metrics(conn, config, "600519", path)

    assert "不能晚于今天" in str(exc.value)


def test_duplicate_row_rejected(
    tmp_path: Path, conn: sqlite3.Connection, config: ChainConfig
) -> None:
    path = _write_csv(
        tmp_path,
        "2026-10-20,T05,1200\n2026-10-20,T05,1300\n",
    )

    with pytest.raises(ValueError) as exc:
        import_metrics(conn, config, "600519", path)

    assert "重复出现" in str(exc.value)
    assert _count(conn) == 0


def test_non_numeric_value_rejected(
    tmp_path: Path, conn: sqlite3.Connection, config: ChainConfig
) -> None:
    path = _write_csv(tmp_path, "2026-10-20,T05,待补\n")

    with pytest.raises(ValueError) as exc:
        import_metrics(conn, config, "600519", path)

    assert "第 2 行 value" in str(exc.value)


def test_missing_column_rejected(
    tmp_path: Path, conn: sqlite3.Connection, config: ChainConfig
) -> None:
    path = tmp_path / "manual.csv"
    path.write_text("metric_id,value\nT05,1200\n", encoding="utf-8")

    with pytest.raises(ValueError) as exc:
        import_metrics(conn, config, "600519", path)

    assert "CSV 缺少字段: metric_date" in str(exc.value)


def test_reimport_is_idempotent(
    tmp_path: Path, conn: sqlite3.Connection, config: ChainConfig
) -> None:
    path = _write_csv(tmp_path, "2026-10-20,T05,1200\n")

    import_metrics(conn, config, "600519", path)
    import_metrics(conn, config, "600519", path)

    assert _count(conn) == 1
