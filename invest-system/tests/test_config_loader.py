"""config_loader.py 的加载与语义校验测试。"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
import yaml

from src.config_loader import load_chain_config, validate_chain_config


def _write(tmp_path: Path, content: Any) -> str:
    path = tmp_path / "chain.yaml"
    if isinstance(content, str):
        path.write_text(content, encoding="utf-8")
    else:
        path.write_text(yaml.safe_dump(content, allow_unicode=True), encoding="utf-8")
    return str(path)


def test_load_valid_config(tmp_path: Path, valid_payload: dict[str, Any]) -> None:
    config = load_chain_config(_write(tmp_path, valid_payload))
    assert config.chain_name == "白酒"
    assert validate_chain_config(config) == []


def test_missing_file_reports_path(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="配置文件不存在"):
        load_chain_config(str(tmp_path / "nope.yaml"))


def test_non_mapping_yaml_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="顶层必须是映射对象"):
        load_chain_config(_write(tmp_path, "- 1\n- 2\n"))


def test_broken_yaml_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="YAML 解析失败"):
        load_chain_config(_write(tmp_path, "chain_name: [unclosed\n"))


def test_overlapping_ranges_rejected(tmp_path: Path, valid_payload: dict[str, Any]) -> None:
    payload = deepcopy(valid_payload)
    payload["triggers"][0]["warn_range"] = [6, 15]
    with pytest.raises(ValueError, match="normal_range"):
        load_chain_config(_write(tmp_path, payload))


def test_unknown_rule_reference_rejected(tmp_path: Path, valid_payload: dict[str, Any]) -> None:
    payload = deepcopy(valid_payload)
    payload["rules"][0]["cond"] = "T09 >= 15 AND T05 >= 1000"
    with pytest.raises(ValueError, match="T09"):
        load_chain_config(_write(tmp_path, payload))


def test_wrong_score_total_rejected(tmp_path: Path, valid_payload: dict[str, Any]) -> None:
    payload = deepcopy(valid_payload)
    payload["choke_points"][0]["score"]["total"] = 9
    with pytest.raises(ValueError, match="total"):
        load_chain_config(_write(tmp_path, payload))


def test_rule_without_and_reported(
    tmp_path: Path, valid_payload: dict[str, Any]
) -> None:
    payload = deepcopy(valid_payload)
    payload["rules"][1]["cond"] = "T03 >= 50"
    config = load_chain_config(_write(tmp_path, payload))
    issues = validate_chain_config(config)
    assert any("rules[1].cond" in issue and "AND" in issue for issue in issues)


def test_missing_paradox_point_reported(
    tmp_path: Path, valid_payload: dict[str, Any]
) -> None:
    payload = deepcopy(valid_payload)
    for choke_point in payload["choke_points"]:
        choke_point["paradox_flag"] = False
    config = load_chain_config(_write(tmp_path, payload))
    assert any("paradox_flag" in issue for issue in validate_chain_config(config))


def test_last_stage_must_be_current(
    tmp_path: Path, valid_payload: dict[str, Any]
) -> None:
    payload = deepcopy(valid_payload)
    payload["stages"][-1]["current"] = False
    config = load_chain_config(_write(tmp_path, payload))
    assert any("current=true" in issue for issue in validate_chain_config(config))


def test_missing_margin_layer_reported(
    tmp_path: Path, valid_payload: dict[str, Any]
) -> None:
    payload = deepcopy(valid_payload)
    for trigger in payload["triggers"]:
        if trigger["layer"] == "margin":
            trigger["layer"] = "price"
    config = load_chain_config(_write(tmp_path, payload))
    issues = validate_chain_config(config)
    assert any("layer=margin" in issue for issue in issues)


def test_trigger_count_out_of_range_reported(
    tmp_path: Path, valid_payload: dict[str, Any]
) -> None:
    payload = deepcopy(valid_payload)
    payload["triggers"] = [
        trigger for trigger in payload["triggers"] if trigger["id"] not in {"T06", "T07"}
    ]
    payload["choke_points"][0]["tracking_triggers"] = ["T05"]
    config = load_chain_config(_write(tmp_path, payload))
    issues = validate_chain_config(config)
    assert any("数量应为 8-10" in issue for issue in issues)


def _with_composite(valid_payload: dict[str, Any], cond: str) -> dict[str, Any]:
    payload = deepcopy(valid_payload)
    payload["rules"].append(
        {
            "cond": cond,
            "res": "量价库存共振走弱",
            "rationale": "价格、库存与量能同向走弱",
            "type": "price_volume_inv",
        }
    )
    return payload


def test_composite_rule_passes(
    tmp_path: Path, valid_payload: dict[str, Any]
) -> None:
    payload = _with_composite(
        valid_payload, "T05 非正常 AND T06 非正常 AND T01 非正常"
    )
    config = load_chain_config(_write(tmp_path, payload))
    assert config.rules[-1].type == "price_volume_inv"
    assert validate_chain_config(config) == []


def test_composite_rule_requires_price_and_inv(
    tmp_path: Path, valid_payload: dict[str, Any]
) -> None:
    payload = _with_composite(
        valid_payload, "T01 非正常 AND T02 非正常 AND T03 非正常"
    )
    config = load_chain_config(_write(tmp_path, payload))
    issues = validate_chain_config(config)
    assert any("必须包含 price 与 inv" in issue for issue in issues)


def test_composite_rule_requires_volume_layer(
    tmp_path: Path, valid_payload: dict[str, Any]
) -> None:
    payload = _with_composite(
        valid_payload, "T05 非正常 AND T06 非正常 AND T03 非正常"
    )
    config = load_chain_config(_write(tmp_path, payload))
    issues = validate_chain_config(config)
    assert any("必须包含 supply 或 demand" in issue for issue in issues)


def test_composite_rule_requires_three_terms(
    tmp_path: Path, valid_payload: dict[str, Any]
) -> None:
    payload = _with_composite(valid_payload, "T05 非正常 AND T06 非正常")
    config = load_chain_config(_write(tmp_path, payload))
    issues = validate_chain_config(config)
    assert any("至少需要 3 个条件项" in issue for issue in issues)


def test_plain_rule_skips_composite_checks(
    tmp_path: Path, valid_payload: dict[str, Any]
) -> None:
    payload = deepcopy(valid_payload)
    payload["rules"][0]["cond"] = "T01 非正常 AND T05 非正常"
    config = load_chain_config(_write(tmp_path, payload))
    assert validate_chain_config(config) == []
