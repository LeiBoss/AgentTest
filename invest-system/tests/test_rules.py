"""rules.py 的条件解析与触发判断测试。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from src.db import init_db
from src.models import ChainConfig, Rule
from src.rules import evaluate_rule, parse_cond, run_rules


@pytest.fixture()
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = init_db(str(tmp_path / "invest.db"))
    yield connection
    connection.close()


def _rule(cond: str) -> Rule:
    return Rule(cond=cond, res="结果", rationale="理由")


def test_parse_cond() -> None:
    assert parse_cond("a 触发 AND b 非正常") == [("a", "alert"), ("b", "not_normal")]


def test_evaluate_rule_true() -> None:
    rule = _rule("a 触发 AND b 非正常")
    assert evaluate_rule(rule, {"a": "alert", "b": "watch"}) is True


def test_evaluate_rule_false() -> None:
    rule = _rule("a 触发 AND b 非正常")
    assert evaluate_rule(rule, {"a": "watch", "b": "watch"}) is False


def test_evaluate_rule_missing_status_is_false() -> None:
    rule = _rule("a 触发 AND b 非正常")
    assert evaluate_rule(rule, {"a": "alert"}) is False


def test_run_rules_returns_only_triggered(
    conn: sqlite3.Connection, valid_payload: dict[str, Any]
) -> None:
    payload = deepcopy(valid_payload)
    payload["rules"] = [
        {"cond": "T01 触发 AND T05 非正常", "res": "组合A", "rationale": "理由A"},
        {"cond": "T03 触发 AND T04 非正常", "res": "组合B", "rationale": "理由B"},
    ]
    config = ChainConfig.model_validate(payload)
    statuses = {"T01": "alert", "T05": "watch", "T03": "normal", "T04": "watch"}

    triggered = run_rules(conn, config, statuses)

    assert len(triggered) == 1
    assert triggered[0]["rule_cond"] == "T01 触发 AND T05 非正常"
    assert triggered[0]["res"] == "组合A"
    assert triggered[0]["active_triggers"] == [
        {"trigger_id": "T01", "status": "alert"},
        {"trigger_id": "T05", "status": "watch"},
    ]


def test_run_rules_missing_status_returns_empty(
    conn: sqlite3.Connection, valid_payload: dict[str, Any]
) -> None:
    payload = deepcopy(valid_payload)
    payload["rules"] = [
        {"cond": "T01 触发 AND T05 非正常", "res": "组合A", "rationale": "理由A"},
    ]
    config = ChainConfig.model_validate(payload)

    assert run_rules(conn, config, {"T01": "alert"}) == []
