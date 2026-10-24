"""评测集：研究链路与研报质量的确定性检查。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from src.db import init_db, insert_report
from src.evaluate import evaluate_chain, evaluate_chains
from src.models import ChainConfig
from src.report import DISCLAIMER_TEXT


@pytest.fixture()
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = init_db(str(tmp_path / "invest.db"))
    yield connection
    connection.close()


@pytest.fixture()
def config(valid_payload: dict[str, Any]) -> ChainConfig:
    return ChainConfig.model_validate(valid_payload)


def report_payload(**overrides: Any) -> dict[str, Any]:
    payload = {
        "summary": "批价与库存仍处正常区间。",
        "transmission_reading": "传导仍停留在渠道利润一环。",
        "signals_reading": "最近没有状态迁移。",
        "rules_reading": "组合规则当前未成立。",
        "choke_reading": "卡点状态保持平衡。",
        "watch_next": "继续观察批价。",
        "risks": ["数据覆盖不足"],
        "evidence": ["T01：暂无数据", "T02：暂无数据", "T03：暂无数据"],
        "disclaimer": DISCLAIMER_TEXT,
    }
    payload.update(overrides)
    return payload


def test_evaluate_chain_without_report(conn: sqlite3.Connection, config: ChainConfig) -> None:
    case = evaluate_chain(config, conn, "600519")

    statuses = {item["key"]: item["status"] for item in case["checks"]}
    assert statuses["pipeline"] == "pass"
    assert statuses["evidence"] == "pass"
    assert statuses["scenario"] == "pass"
    assert statuses["ledger"] == "pass"
    assert statuses["report_evidence"] == "skip"
    assert statuses["report_compliance"] == "skip"
    assert case["passed"] == 4
    assert case["failed"] == 0


def test_evaluate_chain_flags_noncompliant_report(
    conn: sqlite3.Connection, config: ChainConfig
) -> None:
    insert_report(conn, config.chain_name, report_payload(summary="建议买入渠道环节。"), "2026-01-06T09:00:00")

    case = evaluate_chain(config, conn, "600519")
    checks = {item["key"]: item for item in case["checks"]}

    assert checks["report_compliance"]["status"] == "fail"
    assert "买入" in checks["report_compliance"]["detail"]
    assert checks["report_evidence"]["status"] == "pass"


def test_evaluate_chain_flags_unverifiable_evidence(
    conn: sqlite3.Connection, config: ChainConfig
) -> None:
    insert_report(
        conn,
        config.chain_name,
        report_payload(evidence=["宏观向好", "渠道反馈积极", "库存偏低"]),
        "2026-01-06T09:00:00",
    )

    case = evaluate_chain(config, conn, "600519")
    checks = {item["key"]: item for item in case["checks"]}

    assert checks["report_evidence"]["status"] == "fail"
    assert "0/3" in checks["report_evidence"]["detail"]


def test_evaluate_chains_scores_all(conn: sqlite3.Connection, config: ChainConfig) -> None:
    result = evaluate_chains([config], conn, "600519")

    assert result["chain_count"] == 1
    assert result["passed"] == 4
    assert result["failed"] == 0
    assert result["total"] == 4
    assert result["score"] == 100.0


def test_evaluate_chains_reports_runtime_failure(
    conn: sqlite3.Connection, valid_payload: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    config = ChainConfig.model_validate(valid_payload)

    def boom(config, conn, symbol, **kwargs):
        raise RuntimeError("数据读取失败")

    monkeypatch.setattr("src.evaluate.build_report_data", boom)

    result = evaluate_chains([config], conn, "600519")

    assert result["failed"] == 1
    assert result["score"] == 0.0
    assert result["cases"][0]["checks"][0]["key"] == "runtime"
