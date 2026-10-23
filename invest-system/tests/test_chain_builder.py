"""产业链配置生成、校验与落盘。"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from src.chain_builder import ChainBuildError, ChainExistsError, create_chain, save_chain
from src.config_loader import load_chain_config
from src.models import ChainConfig


def _payload(valid_payload: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(valid_payload)


def test_create_chain_writes_yaml(
    tmp_path: Path, valid_payload: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _payload(valid_payload)
    monkeypatch.setattr("src.chain_builder.call_llm", lambda system, body: payload)

    config, path = create_chain("白酒", tmp_path)

    assert path.exists()
    assert config.chain_name == "白酒"
    loaded = load_chain_config(str(path))
    assert loaded.chain_name == "白酒"
    assert len(loaded.triggers) == 8


def test_chain_name_is_forced_to_user_input(
    tmp_path: Path, valid_payload: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _payload(valid_payload)
    payload["chain_name"] = "模型自己编的名字"
    monkeypatch.setattr("src.chain_builder.call_llm", lambda system, body: payload)

    config, _ = create_chain("半导体", tmp_path)

    assert config.chain_name == "半导体"


def test_duplicate_chain_rejected(
    tmp_path: Path, valid_payload: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _payload(valid_payload)
    monkeypatch.setattr("src.chain_builder.call_llm", lambda system, body: payload)
    save_chain(ChainConfig.model_validate(payload), tmp_path)

    with pytest.raises(ChainExistsError):
        create_chain("白酒", tmp_path)


def test_invalid_output_retries_then_succeeds(
    tmp_path: Path, valid_payload: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = _payload(valid_payload)
    broken["triggers"] = broken["triggers"][:3]
    calls: list[dict] = []

    def fake(system: str, body: dict) -> dict:
        calls.append(body)
        return broken if len(calls) == 1 else _payload(valid_payload)

    monkeypatch.setattr("src.chain_builder.call_llm", fake)
    config, path = create_chain("白酒", tmp_path)

    assert len(calls) == 2
    assert calls[1]["previous_errors"]
    assert path.exists()
    assert len(config.triggers) == 8


def test_invalid_output_fails_without_writing(
    tmp_path: Path, valid_payload: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = _payload(valid_payload)
    broken["rules"] = []
    monkeypatch.setattr("src.chain_builder.call_llm", lambda system, body: broken)

    with pytest.raises(ChainBuildError):
        create_chain("白酒", tmp_path)

    assert not list(tmp_path.glob("*.yaml"))


@pytest.mark.parametrize("name", ["", "   ", "白酒/茅台", "a" * 21])
def test_invalid_name_rejected(tmp_path: Path, name: str) -> None:
    with pytest.raises(ChainBuildError):
        create_chain(name, tmp_path)
