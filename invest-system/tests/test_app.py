"""Web API 触发器状态统计。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException

from app import CHOKE_TYPE_LABELS, _trigger_payload, chain_scenario
from src.db import init_db, upsert_metric
from src.models import ChainConfig


@pytest.fixture()
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = init_db(str(tmp_path / "invest.db"))
    yield connection
    connection.close()


def test_triggers_without_data_are_no_data(
    conn: sqlite3.Connection, valid_payload: dict[str, Any]
) -> None:
    config = ChainConfig.model_validate(valid_payload)
    upsert_metric(conn, "600519", "2026-01-05", "T02", 1000.0)

    payload = _trigger_payload(conn, config)

    assert payload["stats"]["normal"] == 1
    assert payload["stats"]["no_data"] == 7
    statuses = {item["id"]: item["status"] for item in payload["triggers"]}
    assert statuses["T01"] == "no_data"
    assert statuses["T02"] == "normal"
    labels = {item["id"]: item["status_label"] for item in payload["triggers"]}
    assert labels["T01"] == "无数据"


def test_choke_type_labels_cover_generated_types() -> None:
    assert CHOKE_TYPE_LABELS["technology"] == "技术"
    assert CHOKE_TYPE_LABELS["resource"] == "资源"


def test_chain_scenario_defaults_to_first_node(
    monkeypatch: pytest.MonkeyPatch, valid_payload: dict[str, Any]
) -> None:
    config = ChainConfig.model_validate(valid_payload)
    monkeypatch.setattr("app._load_chain", lambda name: config)

    result = chain_scenario(config.chain_name, "", -10)

    assert result["node"] == "批价"
    assert result["nodes"][0]["impact_pct"] == -10.0
    assert len(result["warnings"]) == 4


def test_chain_scenario_returns_400_for_unknown_node(
    monkeypatch: pytest.MonkeyPatch, valid_payload: dict[str, Any]
) -> None:
    config = ChainConfig.model_validate(valid_payload)
    monkeypatch.setattr("app._load_chain", lambda name: config)

    with pytest.raises(HTTPException) as exc_info:
        chain_scenario(config.chain_name, "终端动销", -10)

    assert exc_info.value.status_code == 400
    assert "不在传导链中" in str(exc_info.value.detail)
