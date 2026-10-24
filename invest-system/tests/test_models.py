"""models.py 的校验行为测试。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest
from pydantic import ValidationError

from src.models import (
    ChainConfig,
    ChokeScore,
    Rule,
    Stage,
    Transmission,
    TransmissionLag,
    Trigger,
)


def _trigger(**overrides: Any) -> Trigger:
    payload: dict[str, Any] = {
        "id": "T01",
        "name": "批价",
        "layer": "price",
        "lead": "lead",
        "freq": "weekly",
        "src": "akshare",
        "unit": "元",
        "normal_range": (0, 800),
        "warn_range": (800, 1000),
        "crit_range": (1000, 1500),
        "rule": "above",
        "meaning": "批价上行代表需求强",
    }
    payload.update(overrides)
    return Trigger.model_validate(payload)


def test_valid_config_loads(valid_payload: dict[str, Any]) -> None:
    config = ChainConfig.model_validate(valid_payload)
    assert config.chain_name == "白酒"
    assert len(config.triggers) == 8
    assert config.choke_points[0].score.total == 6


def test_above_ranges_must_be_continuous() -> None:
    with pytest.raises(ValidationError, match="normal_range"):
        _trigger(normal_range=(0, 5), warn_range=(6, 15))


def test_above_ranges_must_increase() -> None:
    with pytest.raises(ValidationError, match="连续且递增"):
        _trigger(normal_range=(10, 20), warn_range=(0, 10), crit_range=(-10, 0))


def test_below_ranges_valid() -> None:
    trigger = _trigger(
        rule="below",
        normal_range=(60, 100),
        warn_range=(40, 60),
        crit_range=(0, 40),
    )
    assert trigger.rule == "below"


def test_below_ranges_reject_above_layout() -> None:
    with pytest.raises(ValidationError, match="连续且递减"):
        _trigger(rule="below")


def test_range_lower_bound_must_be_smaller() -> None:
    with pytest.raises(ValidationError, match="下界必须小于上界"):
        _trigger(normal_range=(800, 800))


def test_choke_score_total_must_match_sum() -> None:
    with pytest.raises(ValidationError, match="total"):
        ChokeScore(switchability=1, concentration=2, replicability=3, total=5)


def test_rule_reference_must_exist(valid_payload: dict[str, Any]) -> None:
    payload = deepcopy(valid_payload)
    payload["rules"][0]["cond"] = "T09 >= 15 AND T05 >= 1000"
    with pytest.raises(ValidationError, match="T09"):
        ChainConfig.model_validate(payload)


def test_agent_reference_must_exist(valid_payload: dict[str, Any]) -> None:
    payload = deepcopy(valid_payload)
    payload["agents"][0]["triggers"] = ["T01", "T99"]
    with pytest.raises(ValidationError, match="T99"):
        ChainConfig.model_validate(payload)


def test_choke_point_reference_must_exist(valid_payload: dict[str, Any]) -> None:
    payload = deepcopy(valid_payload)
    payload["choke_points"][0]["tracking_triggers"] = ["T77"]
    with pytest.raises(ValidationError, match="T77"):
        ChainConfig.model_validate(payload)


def test_duplicate_trigger_ids_rejected(valid_payload: dict[str, Any]) -> None:
    payload = deepcopy(valid_payload)
    payload["triggers"][1]["id"] = "T01"
    with pytest.raises(ValidationError, match="重复 id"):
        ChainConfig.model_validate(payload)


def test_stage_current_defaults_false() -> None:
    stage = Stage(yr=2023, t="扩张", d="补库")
    assert stage.current is False


def test_rule_type_defaults_to_condition() -> None:
    rule = Rule(cond="T01 非正常 AND T05 非正常", res="利润加速", rationale="需求与价格共振")
    assert rule.type == "condition"


def test_transmission_lag_must_be_adjacent() -> None:
    payload = {
        "chain": ["批价", "渠道利润", "打款意愿"],
        "lags": [{"src": "批价", "dst": "打款意愿", "days": 30}],
        "note": "批价领先渠道利润",
    }
    with pytest.raises(ValidationError, match="不是 chain 中的相邻节点"):
        Transmission.model_validate(payload)


def test_transmission_lag_duplicate_rejected() -> None:
    payload = {
        "chain": ["批价", "渠道利润"],
        "lags": [
            {"src": "批价", "dst": "渠道利润", "days": 7},
            {"src": "批价", "dst": "渠道利润", "days": 14},
        ],
        "note": "批价领先渠道利润",
    }
    with pytest.raises(ValidationError, match="重复定义"):
        Transmission.model_validate(payload)


def test_transmission_lag_days_must_be_non_negative() -> None:
    with pytest.raises(ValidationError):
        TransmissionLag(src="批价", dst="渠道利润", days=-1)


def test_company_link_must_exist(valid_payload: dict[str, Any]) -> None:
    payload = deepcopy(valid_payload)
    payload["companies"] = [{"name": "示例酒企", "link": "不存在环节"}]
    with pytest.raises(ValidationError, match="profit_pool.link"):
        ChainConfig.model_validate(payload)


def test_company_stage_must_exist(valid_payload: dict[str, Any]) -> None:
    payload = deepcopy(valid_payload)
    payload["companies"] = [
        {"name": "示例酒企", "link": "高端酒企", "stage": "不存在的阶段"}
    ]
    with pytest.raises(ValidationError, match="skeleton.name"):
        ChainConfig.model_validate(payload)


def test_duplicate_company_ticker_rejected(valid_payload: dict[str, Any]) -> None:
    payload = deepcopy(valid_payload)
    payload["companies"] = [
        {"name": "甲酒企", "ticker": "600519", "link": "高端酒企"},
        {"name": "乙酒企", "ticker": "600519", "link": "区域酒企"},
    ]
    with pytest.raises(ValidationError, match="重复 ticker"):
        ChainConfig.model_validate(payload)


def test_company_stage_optional(valid_payload: dict[str, Any]) -> None:
    payload = deepcopy(valid_payload)
    payload["companies"] = [{"name": "示例酒企", "link": "高端酒企"}]
    config = ChainConfig.model_validate(payload)
    assert config.companies[0].stage == ""
