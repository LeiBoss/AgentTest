"""collector.py 的取数与落库测试。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from copy import deepcopy
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from src import collector
from src.collector import collect_chain
from src.db import get_latest_metric, get_metric_history, init_db, upsert_metric
from src.models import ChainConfig


@pytest.fixture()
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = init_db(str(tmp_path / "invest.db"))
    yield connection
    connection.close()


def _config(payload: dict[str, Any]) -> ChainConfig:
    return ChainConfig.model_validate(payload)


def _all_local(payload: dict[str, Any]) -> dict[str, Any]:
    data = deepcopy(payload)
    for trigger in data["triggers"]:
        trigger["src"] = "渠道调研"
    return data


def _frame() -> pd.DataFrame:
    return pd.DataFrame({"date": ["2026-01-02", "2026-01-09"], "value": [100.0, 101.5]})


def test_collect_writes_metrics(
    conn: sqlite3.Connection,
    valid_payload: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _all_local(valid_payload)
    payload["triggers"][1]["src"] = "财报"
    monkeypatch.setitem(collector._FETCHERS, "财报", lambda trigger, symbol: _frame())

    result = collect_chain(conn, _config(payload), "600519")

    assert result["T02"] == pytest.approx(101.5)
    assert get_latest_metric(conn, "600519", "T02") == (101.5, "2026-01-09")
    assert len(get_metric_history(conn, "600519", "T02")) == 2
    assert result["_missing_T01"] is None


def test_single_failure_does_not_block_others(
    conn: sqlite3.Connection,
    valid_payload: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _all_local(valid_payload)
    payload["triggers"][1]["src"] = "财报"
    payload["triggers"][2]["src"] = "财报"
    calls: list[str] = []

    def fake(trigger: Any, symbol: str) -> pd.DataFrame:
        calls.append("call")
        if len(calls) == 1:
            raise RuntimeError("接口超时")
        return _frame()

    monkeypatch.setitem(collector._FETCHERS, "财报", fake)

    result = collect_chain(conn, _config(payload), "600519")

    assert "T02" not in result
    assert result["T03"] == pytest.approx(101.5)
    assert get_latest_metric(conn, "600519", "T02") is None
    assert get_latest_metric(conn, "600519", "T03") == (101.5, "2026-01-09")


def test_local_source_without_history_is_missing(
    conn: sqlite3.Connection, valid_payload: dict[str, Any]
) -> None:
    payload = _all_local(valid_payload)

    result = collect_chain(conn, _config(payload), "600519")

    assert result["_missing_T01"] is None
    assert "T01" not in result
    assert get_latest_metric(conn, "600519", "T01") is None


def test_local_source_uses_history(
    conn: sqlite3.Connection, valid_payload: dict[str, Any]
) -> None:
    payload = _all_local(valid_payload)
    upsert_metric(conn, "600519", "2026-01-05", "T01", 2300.0)

    result = collect_chain(conn, _config(payload), "600519")

    assert result["T01"] == pytest.approx(2300.0)


def test_unsupported_financial_metric_does_not_block_others(
    conn: sqlite3.Connection, valid_payload: dict[str, Any]
) -> None:
    payload = _all_local(valid_payload)
    payload["triggers"][0]["src"] = "财报"
    upsert_metric(conn, "600519", "2026-01-05", "T02", 1200.0)

    result = collect_chain(conn, _config(payload), "600519")

    assert result["_missing_T01"] is None
    assert result["T02"] == pytest.approx(1200.0)


def test_unknown_source_raises(
    conn: sqlite3.Connection,
    valid_payload: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _all_local(valid_payload)
    payload["triggers"][0]["src"] = "未接入来源"

    with pytest.raises(NotImplementedError, match="未支持的数据源"):
        collect_chain(conn, _config(payload), "600519")
