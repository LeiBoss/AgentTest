"""信号事件研究：前瞻收益、胜率与基准超额。"""

from __future__ import annotations

import sqlite3
from copy import deepcopy
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from src.db import init_db, insert_signal
from src.event_study import signal_study
from src.models import ChainConfig


@pytest.fixture()
def conn(tmp_path: Path) -> sqlite3.Connection:
    connection = init_db(str(tmp_path / "invest.db"))
    yield connection
    connection.close()


def _config(payload: dict[str, Any], ticker: str = "600519") -> ChainConfig:
    data = deepcopy(payload)
    data["companies"] = [
        {"name": "测试公司", "ticker": ticker, "link": "高端酒企", "stage": "中游酒企"}
    ]
    return ChainConfig.model_validate(data)


def _day(offset: int) -> str:
    return (date(2026, 1, 1) + timedelta(days=offset)).isoformat()


def _seed_prices(
    conn: sqlite3.Connection, symbol: str = "600519", days: int = 100, rate: float = 1.01
) -> None:
    rows = [
        (symbol, _day(index), "close", round(100 * rate**index, 4))
        for index in range(days)
    ]
    conn.executemany(
        "INSERT OR REPLACE INTO daily_metric"
        " (symbol, metric_date, metric_id, value) VALUES (?, ?, ?, ?)",
        rows,
    )
    conn.commit()


def _signal(conn: sqlite3.Connection, chain: str, trigger_id: str, offset: int, to_status: str = "alert") -> None:
    insert_signal(conn, chain, trigger_id, "normal", to_status, 1.0, "元", f"{_day(offset)}T07:30:00")


def test_signal_study_reports_forward_returns(
    conn: sqlite3.Connection, valid_payload: dict[str, Any]
) -> None:
    config = _config(valid_payload)
    trigger_id = config.triggers[0].id
    _seed_prices(conn)
    for offset in (10, 20, 30, 40, 50):
        _signal(conn, config.chain_name, trigger_id, offset)

    study = signal_study(conn, config)

    assert study["label"] == "等权链指数"
    assert study["events"] == 5
    assert study["samples"] == 5
    assert study["price_coverage"] == "1/1"
    assert study["index_days"] == 100
    assert study["date"] == _day(99)
    assert study["baseline"]["20"] == pytest.approx(22.02, abs=0.03)
    row = study["triggers"][0]
    assert row["id"] == trigger_id
    assert row["events"] == 5
    assert row["samples"] == 5
    assert row["pending"] == 0
    assert row["avg_5"] == pytest.approx(5.1, abs=0.03)
    assert row["avg_20"] == pytest.approx(22.02, abs=0.03)
    assert row["avg_60"] == pytest.approx(81.67, abs=0.1)
    assert row["win_20"] == 100.0
    assert row["excess_20"] == pytest.approx(0.0, abs=0.05)
    assert row["sample_label"] == "样本充足"


def test_signal_study_marks_insufficient_samples_and_pending(
    conn: sqlite3.Connection, valid_payload: dict[str, Any]
) -> None:
    config = _config(valid_payload)
    trigger_id = config.triggers[0].id
    _seed_prices(conn)
    _signal(conn, config.chain_name, trigger_id, 10)
    _signal(conn, config.chain_name, trigger_id, 99)

    study = signal_study(conn, config)

    row = study["triggers"][0]
    assert row["events"] == 2
    assert row["samples"] == 1
    assert row["pending"] == 1
    assert row["sample_label"] == "样本不足"


def test_signal_study_dedupes_same_trigger_and_day(
    conn: sqlite3.Connection, valid_payload: dict[str, Any]
) -> None:
    config = _config(valid_payload)
    trigger_id = config.triggers[0].id
    _seed_prices(conn)
    _signal(conn, config.chain_name, trigger_id, 10, "watch")
    _signal(conn, config.chain_name, trigger_id, 10, "alert")

    study = signal_study(conn, config)

    assert study["events"] == 1
    assert study["triggers"][0]["events"] == 1


def test_signal_study_ignores_exit_to_normal(
    conn: sqlite3.Connection, valid_payload: dict[str, Any]
) -> None:
    config = _config(valid_payload)
    _seed_prices(conn)
    _signal(conn, config.chain_name, config.triggers[0].id, 10, "normal")

    study = signal_study(conn, config)

    assert study["events"] == 0
    assert study["triggers"] == []


def test_signal_study_without_prices_keeps_events_pending(
    conn: sqlite3.Connection, valid_payload: dict[str, Any]
) -> None:
    config = _config(valid_payload)
    _signal(conn, config.chain_name, config.triggers[0].id, 10)

    study = signal_study(conn, config)

    assert study["date"] is None
    assert study["index_days"] == 0
    assert study["price_coverage"] == "0/1"
    row = study["triggers"][0]
    assert row["samples"] == 0
    assert row["pending"] == 1
    assert row["avg_20"] is None
    assert row["win_20"] is None
    assert row["excess_20"] is None


def test_signal_study_equal_weights_companies(
    conn: sqlite3.Connection, valid_payload: dict[str, Any]
) -> None:
    data = deepcopy(valid_payload)
    data["companies"] = [
        {"name": "甲公司", "ticker": "600001", "link": "高端酒企"},
        {"name": "乙公司", "ticker": "000858", "link": "区域酒企"},
    ]
    config = ChainConfig.model_validate(data)
    _seed_prices(conn, "600001", rate=1.01)
    _seed_prices(conn, "000858", rate=1.01)
    _signal(conn, config.chain_name, config.triggers[0].id, 10)

    study = signal_study(conn, config)

    assert study["price_coverage"] == "2/2"
    assert study["triggers"][0]["avg_20"] == pytest.approx(22.02, abs=0.03)
