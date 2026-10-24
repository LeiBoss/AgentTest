"""情景推演：沿传导链的方向、弹性区间与累计滞后。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest
from pydantic import ValidationError

from src.models import ChainConfig
from src.scenario import ScenarioError, run_scenario


def _config(valid_payload: dict[str, Any], lags: list[dict[str, Any]]) -> ChainConfig:
    payload = deepcopy(valid_payload)
    payload["transmission"]["lags"] = lags
    return ChainConfig.model_validate(payload)


@pytest.fixture()
def full_lags() -> list[dict[str, Any]]:
    return [
        {
            "src": "批价",
            "dst": "渠道利润",
            "days": 14,
            "sign": "same",
            "elasticity": 1.2,
            "elasticity_range": [1.0, 1.5],
        },
        {
            "src": "渠道利润",
            "dst": "打款意愿",
            "days": 30,
            "sign": "same",
            "elasticity": 0.8,
            "elasticity_range": [0.5, 1.0],
        },
        {
            "src": "打款意愿",
            "dst": "合同负债",
            "days": 90,
            "sign": "same",
            "elasticity": 0.9,
            "elasticity_range": [0.7, 1.0],
        },
        {
            "src": "合同负债",
            "dst": "酒企报表",
            "days": 120,
            "sign": "same",
            "elasticity": 1.0,
            "elasticity_range": [0.8, 1.2],
        },
    ]


def test_run_scenario_propagates_direction_and_days(
    valid_payload: dict[str, Any], full_lags: list[dict[str, Any]]
) -> None:
    config = _config(valid_payload, full_lags)

    result = run_scenario(config, "批价", -10)

    nodes = result["nodes"]
    assert [item["node"] for item in nodes] == ["批价", "渠道利润", "打款意愿", "合同负债", "酒企报表"]
    assert [item["direction"] for item in nodes] == ["down"] * 5
    assert [item["cumulative_days"] for item in nodes] == [0, 14, 44, 134, 254]
    assert [item["impact_pct"] for item in nodes] == [-10.0, -12.0, -9.6, -8.64, -8.64]
    assert nodes[0]["edge"] is None
    assert nodes[1]["edge"]["sign"] == "same"
    assert nodes[1]["edge"]["elasticity"] == 1.2
    assert nodes[1]["impact_low"] == -15.0
    assert nodes[1]["impact_high"] == -10.0
    assert nodes[4]["impact_low"] == -18.0
    assert nodes[4]["impact_high"] == -2.8
    assert result["warnings"] == []
    assert "若批价-10%" in result["headline"]
    assert "累计滞后 254 天" in result["headline"]


def test_run_scenario_supports_inverse_edge(valid_payload: dict[str, Any]) -> None:
    config = _config(
        valid_payload,
        [
            {
                "src": "批价",
                "dst": "渠道利润",
                "days": 7,
                "sign": "inverse",
                "elasticity": 0.5,
                "elasticity_range": [0.4, 0.6],
            }
        ],
    )

    result = run_scenario(config, "批价", -10)

    edge_node = result["nodes"][1]
    assert edge_node["direction"] == "up"
    assert edge_node["direction_label"] == "上行"
    assert edge_node["impact_pct"] == 5.0
    assert edge_node["impact_low"] == 4.0
    assert edge_node["impact_high"] == 6.0
    assert edge_node["edge"]["sign_label"] == "反向"
    assert edge_node["cumulative_days"] == 7


def test_run_scenario_marks_missing_edges(valid_payload: dict[str, Any]) -> None:
    config = _config(valid_payload, [])

    result = run_scenario(config, "批价", -10)

    assert result["nodes"][1]["impact_pct"] == -10.0
    assert result["nodes"][1]["edge"]["assumed"] is True
    assert len(result["warnings"]) == 4
    assert "未配置传导参数" in result["warnings"][0]
    assert result["nodes"][4]["cumulative_days"] == 0


def test_run_scenario_skips_upstream_nodes(valid_payload: dict[str, Any], full_lags) -> None:
    config = _config(valid_payload, full_lags)

    result = run_scenario(config, "打款意愿", -10)

    upstream = result["nodes"][:2]
    assert [item["direction"] for item in upstream] == ["none", "none"]
    assert [item["impact_pct"] for item in upstream] == [None, None]
    assert [item["cumulative_days"] for item in upstream] == [None, None]
    assert result["nodes"][2]["impact_pct"] == -10.0
    assert result["nodes"][4]["cumulative_days"] == 210


def test_run_scenario_rejects_unknown_node(valid_payload: dict[str, Any]) -> None:
    config = _config(valid_payload, [])

    with pytest.raises(ScenarioError, match="不在传导链中"):
        run_scenario(config, "终端动销", -10)


def test_run_scenario_rejects_out_of_range_shock(valid_payload: dict[str, Any]) -> None:
    config = _config(valid_payload, [])

    with pytest.raises(ScenarioError, match="冲击幅度需在"):
        run_scenario(config, "批价", 120)


def test_elasticity_range_must_be_positive_and_ordered(valid_payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError, match="elasticity_range"):
        _config(
            valid_payload,
            [
                {
                    "src": "批价",
                    "dst": "渠道利润",
                    "days": 14,
                    "elasticity_range": [1.5, 1.0],
                }
            ],
        )
