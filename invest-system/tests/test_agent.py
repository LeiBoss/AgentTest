"""研究 Agent 的固定工具序列与追问校验。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from src.agent import AgentError, ask, research_run
from src.db import init_db, upsert_metric
from src.models import ChainConfig
from src.thesis import create_catalyst, create_thesis

TOOL_ORDER = ["data", "triggers", "rules", "cross_section", "event_study", "scenario", "ledger"]


@pytest.fixture()
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = init_db(str(tmp_path / "invest.db"))
    yield connection
    connection.close()


@pytest.fixture()
def config(valid_payload: dict[str, Any]) -> ChainConfig:
    return ChainConfig.model_validate(valid_payload)


def test_research_run_covers_all_tools(conn: sqlite3.Connection, config: ChainConfig) -> None:
    run = research_run(config, conn, "600519")

    assert [step["tool"] for step in run["steps"]] == TOOL_ORDER
    assert all(step["status"] == "ok" for step in run["steps"])
    assert run["findings"]
    assert all(item["evidence"] for item in run["findings"])
    assert run["chain"] == "白酒"
    assert "不构成投资建议" in run["disclaimer"]


def test_research_run_reads_seeded_metric(conn: sqlite3.Connection, config: ChainConfig) -> None:
    upsert_metric(conn, "600519", "2026-01-05", "T02", 150.0)

    run = research_run(config, conn, "600519")
    data_step = next(step for step in run["steps"] if step["tool"] == "data")
    findings = " ".join(item["text"] for item in run["findings"])

    assert "1/8" in data_step["detail"]
    assert "2026-01-05" in data_step["detail"]
    assert "仍有 7 项指标没有数据" in findings


def test_research_run_watch_includes_ledger(conn: sqlite3.Connection, config: ChainConfig) -> None:
    create_thesis(
        conn,
        "白酒",
        {
            "title": "批价下行传导",
            "direction": "down",
            "core_logic": "渠道利润压缩",
            "confirm_condition": "合同负债转负",
            "invalidate_condition": "批价回升",
            "confidence": 60,
        },
    )
    soon = (date.today() + timedelta(days=3)).isoformat()
    create_catalyst(conn, "白酒", {"event_date": soon, "title": "三季报披露", "kind": "earnings"})

    run = research_run(config, conn, "600519")

    assert any("验证 Thesis：批价下行传导" in item for item in run["watch"])
    assert any("三季报披露" in item for item in run["watch"])
    ledger = next(step for step in run["steps"] if step["tool"] == "ledger")
    assert "待验证 1" in ledger["detail"]


def test_ask_requires_real_evidence(conn: sqlite3.Connection, config: ChainConfig) -> None:
    with pytest.raises(AgentError):
        ask(config, conn, "600519", "  ")

    with pytest.raises(AgentError):
        ask(config, conn, "600519", "问" * 201)


def test_ask_returns_validated_answer(
    conn: sqlite3.Connection, config: ChainConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "src.agent.call_llm",
        lambda system, payload, max_retries=1: {
            "answer": "当前只有 T02 有数据，其余指标尚未覆盖。",
            "evidence": ["T02：最新值 150 万箱，状态正常", "T01：暂无数据"],
            "unknown": ["渠道库存数据尚未接入"],
            "watch_next": "渠道库存",
        },
    )

    result = ask(config, conn, "600519", "现在能看什么？")

    assert result["evidence"] == ["T02：最新值 150 万箱，状态正常", "T01：暂无数据"]
    assert result["unknown"] == ["渠道库存数据尚未接入"]
    assert result["watch_next"] == "渠道库存"


@pytest.mark.parametrize(
    "result",
    [
        {"answer": "结论", "evidence": ["T02：150", "编造的指标：1"], "unknown": []},
        {"answer": "", "evidence": ["T02：150", "T01：暂无"], "unknown": []},
        {"answer": "结论", "evidence": ["T02：150"], "unknown": []},
        {"answer": "结论", "evidence": ["T02：150", "T01：暂无"], "unknown": "无"},
    ],
)
def test_ask_rejects_invalid_output(
    conn: sqlite3.Connection,
    config: ChainConfig,
    monkeypatch: pytest.MonkeyPatch,
    result: dict[str, Any],
) -> None:
    monkeypatch.setattr("src.agent.call_llm", lambda system, payload, max_retries=1: result)

    with pytest.raises(AgentError):
        ask(config, conn, "600519", "现在能看什么？")


def test_ask_rejects_banned_words(
    conn: sqlite3.Connection, config: ChainConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "src.agent.call_llm",
        lambda system, payload, max_retries=1: {
            "answer": "建议买入渠道环节。",
            "evidence": ["T02：150", "T01：暂无"],
            "unknown": [],
        },
    )

    with pytest.raises(AgentError) as exc_info:
        ask(config, conn, "600519", "现在能看什么？")

    assert "合规" in str(exc_info.value)
