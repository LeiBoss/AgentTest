"""端到端链路测试：YAML 配置 → 判定 → 解读 → 规则 → 推送。"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

import main as main_module
import src.llm as llm_module
from src.db import init_db

CHAIN_PATH = str(
    Path(__file__).resolve().parents[1] / "config" / "chains" / "baijiu.yaml"
)

VALUES = {
    "wholesale_price": 2100.0,  # below 2200 → alert
    "channel_profit": 120.0,  # 50-150 warn
    "channel_inv": 2.0,  # 1.5-2.5 warn
    "contract_liab": 5.0,
    "advance_receipt": 20.0,
    "shipment": 5.0,
    "banquet": 90.0,
    "ecom_price": 50.0,
}


def _fake_llm(system_prompt: str, user_payload: dict, max_retries: int = 2) -> dict:
    trigger_id = user_payload["trigger"]["id"]
    return {
        "trigger_id": trigger_id,
        "interpretation": f"{trigger_id} 状态变化已传导至下游",
        "evidence": [trigger_id],
    }


@pytest.fixture()
def pushed(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> list[tuple]:
    """临时库 + mock 采集/LLM/推送，返回推送调用记录。"""
    conn = init_db(str(tmp_path / "invest.db"))
    monkeypatch.setattr(main_module, "init_db", lambda: conn)
    monkeypatch.setattr(main_module, "collect_chain", lambda *args, **kwargs: dict(VALUES))
    monkeypatch.setattr(main_module, "collect_valuation", lambda *args, **kwargs: 0)
    monkeypatch.setattr(llm_module, "call_llm", _fake_llm)

    calls: list[tuple] = []

    def fake_push(title: str, body: str, tail: str = "信息整理，不构成投资建议。") -> bool:
        calls.append((title, body, tail))
        return True

    monkeypatch.setattr(main_module, "push_feishu", fake_push)
    return calls


def test_e2e_full_chain(pushed: list[tuple], tmp_path: Any) -> None:
    result = main_module.run_chain(CHAIN_PATH, "600519.SH")

    assert result["transitions"]
    assert result["rules_triggered"]
    assert result["pushed"] is True

    with sqlite3.connect(str(tmp_path / "invest.db")) as check:
        rows = check.execute("SELECT trigger_id, payload_json FROM signal_log").fetchall()

    assert len(rows) == len(result["transitions"])
    payload = dict(rows)
    assert payload["wholesale_price"] is not None

    assert len(pushed) == 1
    assert "不构成投资建议" in pushed[0][2]


def test_e2e_drops_output_without_evidence(
    pushed: list[tuple], tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_llm_missing(system_prompt: str, user_payload: dict, max_retries: int = 2) -> dict:
        trigger_id = user_payload["trigger"]["id"]
        if trigger_id == "channel_profit":
            return {"trigger_id": trigger_id, "interpretation": "缺少证据"}
        return _fake_llm(system_prompt, user_payload, max_retries)

    monkeypatch.setattr(llm_module, "call_llm", fake_llm_missing)

    result = main_module.run_chain(CHAIN_PATH, "600519.SH")

    assert result["transitions"]
    assert result["pushed"] is True

    with sqlite3.connect(str(tmp_path / "invest.db")) as check:
        rows = dict(
            check.execute("SELECT trigger_id, payload_json FROM signal_log").fetchall()
        )

    assert rows["channel_profit"] is None
    assert rows["wholesale_price"] is not None
    assert len(pushed) == 1
