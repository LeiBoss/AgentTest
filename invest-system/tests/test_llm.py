"""llm.py 的调用与校验测试。"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from src import llm
from src.llm import LLMError, call_llm, interpret_transition, validate_evidence
from src.models import Trigger


class _FakeClient:
    """替身 OpenAI 客户端，按顺序返回预设内容。"""

    def __init__(self, contents: list[str]) -> None:
        self.contents = contents
        self.calls = 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs: Any) -> Any:
        index = min(self.calls, len(self.contents) - 1)
        self.calls += 1
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.contents[index]))],
            usage=SimpleNamespace(prompt_tokens=12, completion_tokens=34),
        )


def _trigger() -> Trigger:
    return Trigger(
        id="T01",
        name="飞天散瓶批价",
        layer="price",
        lead="sync",
        freq="周",
        src="渠道调研",
        unit="元/瓶",
        normal_range=(2400, 3000),
        warn_range=(2200, 2400),
        crit_range=(0, 2200),
        rule="below",
        meaning="批价跌破后传导至合同负债",
    )


def _transition() -> dict:
    return {
        "trigger_id": "T01",
        "from_status": "normal",
        "to_status": "watch",
        "value": 2300.0,
        "unit": "元/瓶",
    }


def _history() -> list[tuple[float, str]]:
    return [(2350.0, "2026-01-02"), (2300.0, "2026-01-09")]


def _patch(monkeypatch: pytest.MonkeyPatch, contents: list[str]) -> _FakeClient:
    client = _FakeClient(contents)
    monkeypatch.setattr(llm, "OpenAI", lambda **kwargs: client)
    return client


def test_interpret_transition_returns_valid_result(monkeypatch: pytest.MonkeyPatch) -> None:
    content = json.dumps(
        {
            "trigger_id": "T01",
            "interpretation": "批价进入观察区间，渠道利润压缩。",
            "evidence": ["T01"],
            "watch_next": "channel_inv",
        },
        ensure_ascii=False,
    )
    _patch(monkeypatch, [content])

    result = interpret_transition(_trigger(), _transition(), _history())

    assert result is not None
    assert validate_evidence(result) is True
    assert result["trigger_id"] == "T01"


def test_interpret_transition_without_evidence_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = json.dumps({"trigger_id": "T01", "interpretation": "x"}, ensure_ascii=False)
    _patch(monkeypatch, [content])

    assert interpret_transition(_trigger(), _transition(), _history()) is None


def test_interpret_transition_trigger_id_mismatch_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = json.dumps(
        {"trigger_id": "T99", "interpretation": "x", "evidence": ["T99"]},
        ensure_ascii=False,
    )
    _patch(monkeypatch, [content])

    assert interpret_transition(_trigger(), _transition(), _history()) is None


def test_call_llm_raises_after_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _patch(monkeypatch, ["这不是 JSON"])

    with pytest.raises(LLMError):
        call_llm("system", {"trigger_id": "T01"})

    assert client.calls == 3


def test_validate_evidence() -> None:
    assert validate_evidence({"evidence": ["T01"]}) is True
    assert validate_evidence({"evidence": []}) is False
    assert validate_evidence({"evidence": "T01"}) is False
    assert validate_evidence({}) is False
