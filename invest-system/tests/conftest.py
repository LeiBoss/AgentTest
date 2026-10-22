"""测试共享数据：一份最小合法链路配置。"""

from __future__ import annotations

from typing import Any

import pytest


def _trigger(
    trigger_id: str,
    layer: str,
    rule: str,
    normal: tuple[float, float],
    warn: tuple[float, float],
    crit: tuple[float, float],
    unit: str = "%",
) -> dict[str, Any]:
    return {
        "id": trigger_id,
        "name": f"指标{trigger_id}",
        "layer": layer,
        "lead": "leading",
        "freq": "monthly",
        "src": "akshare",
        "unit": unit,
        "normal_range": list(normal),
        "warn_range": list(warn),
        "crit_range": list(crit),
        "rule": rule,
        "meaning": "一句话含义",
    }


@pytest.fixture()
def valid_payload() -> dict[str, Any]:
    """构造 8 个触发器、6 个 layer、2 条规则的最小合法配置。"""
    return {
        "chain_name": "白酒",
        "wealth_logic": "品牌溢价与渠道库存周期共同决定利润分配",
        "skeleton": "上游基酒 → 中游酒企 → 下游渠道 → 终端消费",
        "profit_pool": "利润集中在高端酒企",
        "transmission": "批价上行传导至渠道补库，再传导至酒企报表",
        "triggers": [
            _trigger("T01", "demand", "above", (0, 5), (5, 15), (15, 30)),
            _trigger("T02", "demand", "above", (0, 100), (100, 200), (200, 400), "万箱"),
            _trigger("T03", "margin", "above", (0, 30), (30, 50), (50, 80)),
            _trigger("T04", "margin", "below", (15, 30), (8, 15), (0, 8)),
            _trigger("T05", "price", "above", (0, 800), (800, 1000), (1000, 1500), "元"),
            _trigger("T06", "inventory", "above", (0, 30), (30, 60), (60, 120), "天"),
            _trigger("T07", "supply", "below", (60, 100), (40, 60), (0, 40)),
            _trigger("T08", "policy", "above", (0, 40), (40, 70), (70, 100), "指数"),
        ],
        "rules": [
            {
                "cond": "T01 >= 15 AND T05 >= 1000",
                "res": "高端酒企利润加速",
                "rationale": "需求放量叠加批价上行",
            },
            {
                "cond": "T04 <= 8 AND T03 >= 50",
                "res": "费用率下行放大利润弹性",
                "rationale": "规模效应释放",
            },
        ],
        "agents": [
            {"name": "供需分析师", "role": "跟踪需求与批价", "triggers": ["T01", "T05"]},
        ],
        "stages": [
            {"yr": 2021, "t": "酱酒热", "d": "渠道加库存", "current": False},
            {"yr": 2024, "t": "去库存", "d": "批价回落", "current": True},
        ],
        "choke_points": [
            {
                "node": "高端基酒",
                "type": "resource",
                "score": {
                    "switchability": 1,
                    "concentration": 3,
                    "replicability": 2,
                    "total": 6,
                },
                "level": "high",
                "current_state": "tight",
                "profit_share_pct": 65.0,
                "value_accumulation_trigger": "T05",
                "value_transfer_trigger": "T01",
                "tracking_triggers": ["T05", "T06"],
                "paradox_flag": True,
                "paradox_note": "卡点越紧，渠道库存越高",
            },
        ],
        "data_gaps": ["渠道库存口径缺失"],
    }
