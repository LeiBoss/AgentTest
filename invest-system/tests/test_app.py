"""Web API 触发器状态统计。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from app import CHOKE_TYPE_LABELS, _trigger_payload
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
