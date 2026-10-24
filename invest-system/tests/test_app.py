"""Web API 触发器状态统计。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException

from app import (
    CHOKE_TYPE_LABELS,
    _trigger_payload,
    add_catalyst,
    add_thesis,
    chain_scenario,
    chain_thesis,
    change_catalyst_status,
    change_thesis_status,
)
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


@pytest.fixture()
def chain_app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, valid_payload: dict[str, Any]) -> ChainConfig:
    config = ChainConfig.model_validate(valid_payload)
    db_path = str(tmp_path / "app.db")
    init_db(db_path).close()
    monkeypatch.setattr("app._load_chain", lambda name: config)
    monkeypatch.setattr("app.DB_PATH", Path(db_path))
    return config


def test_thesis_endpoints_round_trip(chain_app: ChainConfig) -> None:
    board = add_thesis(
        chain_app.chain_name,
        {
            "title": "批价下行传导至报表",
            "direction": "down",
            "horizon": "两个季度",
            "core_logic": "渠道利润压缩后打款意愿下降",
            "confirm_condition": "合同负债连续两期为负",
            "invalidate_condition": "批价回升",
            "confidence": 55,
        },
    )
    thesis_id = board["theses"][0]["id"]
    assert board["stats"]["active"] == 1

    board = change_thesis_status(chain_app.chain_name, thesis_id, {"status": "confirmed"})
    assert board["stats"]["confirmed"] == 1
    assert chain_thesis(chain_app.chain_name)["theses"][0]["status_label"] == "成立"


def test_add_thesis_returns_400_for_missing_conditions(chain_app: ChainConfig) -> None:
    with pytest.raises(HTTPException) as exc_info:
        add_thesis(chain_app.chain_name, {"title": "只有标题", "direction": "down"})

    assert exc_info.value.status_code == 400
    assert "核心逻辑不能为空" in str(exc_info.value.detail)


def test_catalyst_endpoints_round_trip(chain_app: ChainConfig) -> None:
    board = add_catalyst(
        chain_app.chain_name,
        {"event_date": "2026-10-28", "title": "三季报披露", "kind": "earnings"},
    )
    catalyst_id = board["catalysts"][0]["id"]
    assert board["catalysts"][0]["kind_label"] == "财报日"

    board = change_catalyst_status(chain_app.chain_name, catalyst_id, {"status": "done"})
    assert board["catalyst_stats"]["done"] == 1


def test_add_catalyst_returns_400_for_bad_date(chain_app: ChainConfig) -> None:
    with pytest.raises(HTTPException) as exc_info:
        add_catalyst(chain_app.chain_name, {"event_date": "2026/10/28", "title": "三季报"})

    assert exc_info.value.status_code == 400
    assert "YYYY-MM-DD" in str(exc_info.value.detail)
