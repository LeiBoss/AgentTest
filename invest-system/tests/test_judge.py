"""judge.py 的状态判定与迁移测试。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from src.db import get_last_status, init_db, insert_signal, now_iso
from src.judge import detect_transition, judge_status, run_judge
from src.models import ChainConfig, Trigger


@pytest.fixture()
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = init_db(str(tmp_path / "invest.db"))
    yield connection
    connection.close()


def _trigger(rule: str, normal: tuple[float, float], warn: tuple[float, float],
             crit: tuple[float, float]) -> Trigger:
    return Trigger(
        id="T01",
        name="测试指标",
        layer="price",
        lead="sync",
        freq="周",
        src="渠道调研",
        unit="元",
        normal_range=normal,
        warn_range=warn,
        crit_range=crit,
        rule=rule,
        meaning="测试用",
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [(4.9, "normal"), (5.0, "watch"), (14.9, "watch"), (15.0, "alert"), (99.0, "alert")],
)
def test_judge_above(value: float, expected: str) -> None:
    trigger = _trigger("above", (0, 5), (5, 15), (15, 30))
    assert judge_status(trigger, value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [(15.1, "normal"), (15.0, "watch"), (8.1, "watch"), (8.0, "alert"), (0.0, "alert")],
)
def test_judge_below(value: float, expected: str) -> None:
    trigger = _trigger("below", (15, 30), (8, 15), (0, 8))
    assert judge_status(trigger, value) == expected


def test_detect_transition_without_history(conn: sqlite3.Connection) -> None:
    trigger = _trigger("above", (0, 5), (5, 15), (15, 30))

    transition = detect_transition(conn, "白酒", trigger, 10.0)

    assert transition == {
        "trigger_id": "T01",
        "from_status": "normal",
        "to_status": "watch",
        "value": 10.0,
        "unit": "元",
    }


def test_detect_transition_unchanged_returns_none(conn: sqlite3.Connection) -> None:
    trigger = _trigger("above", (0, 5), (5, 15), (15, 30))
    insert_signal(conn, "白酒", "T01", "normal", "watch", 10.0, "元", now_iso())

    assert detect_transition(conn, "白酒", trigger, 10.0) is None


def test_run_judge_skips_missing_values(
    conn: sqlite3.Connection, valid_payload: dict[str, Any]
) -> None:
    config = ChainConfig.model_validate(valid_payload)
    values: dict[str, Any] = {"T01": None, "_missing_T02": None, "T03": 60.0}

    transitions = run_judge(conn, config, values)

    assert [item["trigger_id"] for item in transitions] == ["T03"]
    assert transitions[0]["to_status"] == "alert"
    assert conn.execute("SELECT COUNT(*) AS n FROM signal_log").fetchone()["n"] == 1


def test_run_judge_twice_only_logs_once(
    conn: sqlite3.Connection, valid_payload: dict[str, Any]
) -> None:
    config = ChainConfig.model_validate(valid_payload)

    first = run_judge(conn, config, {"T01": 10.0})
    second = run_judge(conn, config, {"T01": 10.0})

    assert len(first) == 1
    assert second == []
    assert get_last_status(conn, "白酒", "T01") == "watch"
    assert conn.execute("SELECT COUNT(*) AS n FROM signal_log").fetchone()["n"] == 1
