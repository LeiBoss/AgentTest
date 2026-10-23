"""研报数据组装、LLM 解读与合规拦截。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from src.db import (
    get_latest_report,
    init_db,
    insert_report,
    upsert_metric,
)
from src.models import ChainConfig
from src.report import DISCLAIMER_TEXT, ReportError, build_report_data, generate_report

NARRATIVE = {
    "summary": "渠道指标整体处于正常区间，报表端仍需观察。",
    "transmission_reading": "批价与渠道利润未越过阈值，传导停留在渠道环节。",
    "signals_reading": "统计区间内没有出现新的状态迁移。",
    "rules_reading": "两条组合规则当前均未同时满足。",
    "choke_reading": "高端基酒仍是利润集中的卡点，状态为收紧。",
    "watch_next": "后续关注渠道库存与合同负债的同步变化。",
    "risks": ["渠道库存抬升可能压缩单瓶利润"],
    "evidence": ["T05 最新值 1200 元，状态正常", "T01 暂无数据", "T06 最新值 45 天"],
}


@pytest.fixture()
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = init_db(str(tmp_path / "inv.db"))
    yield connection
    connection.close()


@pytest.fixture()
def config(valid_payload: dict[str, Any]) -> ChainConfig:
    return ChainConfig.model_validate(valid_payload)


@pytest.fixture()
def seeded(conn: sqlite3.Connection, config: ChainConfig) -> sqlite3.Connection:
    upsert_metric(conn, "600519", "2026-10-20", "T05", 1200)
    upsert_metric(conn, "600519", "2026-10-19", "T05", 1100)
    upsert_metric(conn, "600519", "2026-10-20", "T06", 45)
    return conn


def test_build_report_data(seeded: sqlite3.Connection, config: ChainConfig) -> None:
    data = build_report_data(config, seeded, "600519")

    assert data["chain"] == "白酒"
    assert data["data_date"] == "2026-10-20"
    assert data["stats"] == {"normal": 2, "watch": 0, "alert": 0, "no_data": 6}
    assert [item["value_text"] for item in data["triggers"] if item["id"] == "T05"] == ["1200"]
    assert len(data["triggers"]) == 8
    assert data["current_stage"]["t"] == "去库存"


def test_generate_report_ok(
    seeded: sqlite3.Connection, config: ChainConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("src.report.call_llm", lambda system, body: dict(NARRATIVE))

    report = generate_report(config, seeded, "600519")

    assert report["summary"] == NARRATIVE["summary"]
    assert report["risks"] == NARRATIVE["risks"]
    assert report["disclaimer"] == DISCLAIMER_TEXT
    assert report["stats"]["normal"] == 2
    assert report["generated_at"]


def test_generate_report_rejects_banned_words(
    seeded: sqlite3.Connection, config: ChainConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad = dict(NARRATIVE)
    bad["summary"] = "建议买入并长期持有"
    calls: list[int] = []

    def fake(system: str, body: dict) -> dict:
        calls.append(1)
        return bad

    monkeypatch.setattr("src.report.call_llm", fake)
    with pytest.raises(ReportError):
        generate_report(config, seeded, "600519")
    assert len(calls) == 2


def test_generate_report_requires_evidence(
    seeded: sqlite3.Connection, config: ChainConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad = dict(NARRATIVE)
    bad["evidence"] = ["行业景气度回升"]
    monkeypatch.setattr("src.report.call_llm", lambda system, body: bad)

    with pytest.raises(ReportError):
        generate_report(config, seeded, "600519")


def test_report_log_roundtrip(seeded: sqlite3.Connection) -> None:
    payload = {"chain": "白酒", "summary": "示例"}
    insert_report(seeded, "白酒", payload, "2026-10-23T10:00:00")

    latest = get_latest_report(seeded, "白酒")

    assert latest is not None
    assert latest["summary"] == "示例"
    assert latest["generated_at"] == "2026-10-23T10:00:00"
    assert get_latest_report(seeded, "半导体") is None
