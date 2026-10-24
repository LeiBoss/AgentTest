"""main.run_chain 的编排测试。"""

from __future__ import annotations

import copy
import sqlite3
import sys
from typing import Any

import pytest
import yaml

import main as main_module
from main import CollectError, ConfigError, run_chain
from src.compliance import ComplianceError
from src.db import init_db


def _write_chain(tmp_path: Any, payload: dict[str, Any]) -> str:
    path = tmp_path / "chain.yaml"
    path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return str(path)


def _payload(valid_payload: dict[str, Any]) -> dict[str, Any]:
    payload = copy.deepcopy(valid_payload)
    payload["rules"] = [
        {
            "cond": "T01 触发 AND T05 非正常",
            "res": "需求与批价共振",
            "rationale": "需求放量叠加批价走强",
        },
        {
            "cond": "T04 非正常 AND T03 触发",
            "res": "费用率下行放大利润",
            "rationale": "规模效应释放",
        },
    ]
    return payload


def _temp_db(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    conn = init_db(str(tmp_path / "inv.db"))
    monkeypatch.setattr(main_module, "init_db", lambda: conn)
    return conn


def _patch_flow(
    monkeypatch: pytest.MonkeyPatch, values: dict[str, float], transitions: list[dict]
) -> None:
    monkeypatch.setattr(main_module, "collect_chain", lambda *args, **kwargs: values)
    monkeypatch.setattr(main_module, "collect_valuation", lambda *args, **kwargs: 0)
    monkeypatch.setattr(main_module, "run_judge", lambda *args, **kwargs: transitions)
    monkeypatch.setattr(
        main_module,
        "interpret_transition",
        lambda *args, **kwargs: {
            "trigger_id": "T01",
            "interpretation": "批价走强",
            "evidence": ["T01"],
        },
    )
    monkeypatch.setattr(main_module, "push_feishu", lambda *args, **kwargs: True)


def _transition() -> dict:
    return {
        "trigger_id": "T01",
        "from_status": "normal",
        "to_status": "alert",
        "value": 20.0,
        "unit": "%",
        "signal_id": 1,
    }


def test_run_chain_happy_path(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, valid_payload: dict[str, Any]
) -> None:
    _temp_db(tmp_path, monkeypatch)
    _patch_flow(monkeypatch, {"T01": 20.0, "T05": 1200.0}, [_transition()])

    result = run_chain(_write_chain(tmp_path, _payload(valid_payload)), "600519.SH")

    assert len(result["transitions"]) == 1
    assert result["rules_triggered"][0]["res"] == "需求与批价共振"
    assert result["pushed"] is True


def test_run_chain_validation_error(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, valid_payload: dict[str, Any]
) -> None:
    payload = _payload(valid_payload)
    payload["rules"][0]["cond"] = "T01 >= 15"

    with pytest.raises(ConfigError, match="配置校验失败"):
        run_chain(_write_chain(tmp_path, payload), "600519.SH")


def test_run_chain_compliance_failure_keeps_flow(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, valid_payload: dict[str, Any]
) -> None:
    _temp_db(tmp_path, monkeypatch)
    _patch_flow(monkeypatch, {"T01": 20.0, "T05": 1200.0}, [_transition()])
    monkeypatch.setattr(
        main_module, "filter_or_raise", lambda *args, **kwargs: (_ for _ in ()).throw(
            ComplianceError("测试拦截")
        )
    )

    result = run_chain(_write_chain(tmp_path, _payload(valid_payload)), "600519.SH")

    assert result["pushed"] is False
    assert len(result["transitions"]) == 1


def test_run_chain_without_any_data_raises(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, valid_payload: dict[str, Any]
) -> None:
    _temp_db(tmp_path, monkeypatch)
    monkeypatch.setattr(
        main_module,
        "collect_chain",
        lambda *args, **kwargs: {"_missing_T01": None, "_missing_T02": None},
    )

    with pytest.raises(CollectError, match="无任何可用数据"):
        run_chain(_write_chain(tmp_path, _payload(valid_payload)), "600519.SH")


def test_backfill_valuation_closes_connection(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = init_db(str(tmp_path / "inv.db"))
    seen: list[str] = []
    monkeypatch.setattr(main_module, "init_db", lambda: conn)
    monkeypatch.setattr(
        main_module,
        "collect_valuation",
        lambda connection, symbol: seen.append(symbol) or 12,
    )

    assert main_module.backfill_valuation("600519") == 12
    assert seen == ["600519"]
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")


def test_main_valuation_only_uses_cli_symbol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", ["main.py", "--valuation-only", "600519"])
    seen: list[str] = []
    monkeypatch.setattr(
        main_module, "backfill_valuation", lambda symbol: seen.append(symbol) or 7
    )

    main_module.main()

    assert seen == ["600519"]


def test_main_valuation_only_falls_back_to_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", ["main.py", "--valuation-only"])
    monkeypatch.setenv("SYMBOL", "000858")
    seen: list[str] = []
    monkeypatch.setattr(
        main_module, "backfill_valuation", lambda symbol: seen.append(symbol) or 7
    )

    main_module.main()

    assert seen == ["000858"]


def test_main_valuation_only_requires_symbol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", ["main.py", "--valuation-only"])
    monkeypatch.delenv("SYMBOL", raising=False)

    with pytest.raises(SystemExit) as excinfo:
        main_module.main()

    assert excinfo.value.code == 1
