"""因果链情景推演：给定节点冲击，沿传导链推演方向与幅度区间。"""

from __future__ import annotations

from typing import Any

from .models import ChainConfig, TransmissionLag

DEFAULT_SHOCK_PCT = -10.0
MAX_SHOCK_PCT = 100.0
FLAT_THRESHOLD = 0.05
DIRECTION_LABELS = {"up": "上行", "down": "下行", "flat": "持平", "none": "未推演"}
SIGN_LABELS = {"same": "同向", "inverse": "反向"}
DISCLAIMER_TEXT = "情景推演基于配置中的传导方向与弹性假设，属于条件推演，不构成投资建议。"


class ScenarioError(ValueError):
    """情景参数不合法。"""


def _edge(config: ChainConfig, src: str, dst: str) -> TransmissionLag | None:
    for lag in config.transmission.lags:
        if lag.src == src and lag.dst == dst:
            return lag
    return None


def _bounds(lag: TransmissionLag | None) -> tuple[float, float]:
    if lag is None:
        return 1.0, 1.0
    if lag.elasticity_range is not None:
        return lag.elasticity_range
    return lag.elasticity, lag.elasticity


def _round(value: float) -> float:
    rounded = round(value, 2)
    return 0.0 if rounded == 0 else rounded


def _direction(pct: float | None) -> str:
    if pct is None:
        return "none"
    if abs(pct) < FLAT_THRESHOLD:
        return "flat"
    return "up" if pct > 0 else "down"


def _edge_payload(lag: TransmissionLag | None) -> dict[str, Any] | None:
    if lag is None:
        return None
    low, high = _bounds(lag)
    return {
        "src": lag.src,
        "dst": lag.dst,
        "days": lag.days,
        "sign": lag.sign,
        "sign_label": SIGN_LABELS[lag.sign],
        "elasticity": lag.elasticity,
        "elasticity_low": low,
        "elasticity_high": high,
        "note": lag.note,
        "assumed": False,
    }


def _assumed_edge(src: str, dst: str) -> dict[str, Any]:
    return {
        "src": src,
        "dst": dst,
        "days": 0,
        "sign": "same",
        "sign_label": SIGN_LABELS["same"],
        "elasticity": 1.0,
        "elasticity_low": 1.0,
        "elasticity_high": 1.0,
        "note": "",
        "assumed": True,
    }


def _spread(central: float, low: float, high: float, lag: TransmissionLag | None):
    """按边的方向与弹性区间推进一步，返回新的中心值与区间。"""
    factor = -1.0 if lag is not None and lag.sign == "inverse" else 1.0
    e_low, e_high = _bounds(lag)
    elasticity = lag.elasticity if lag is not None else 1.0
    next_central = central * factor * elasticity
    candidates = [
        low * factor * e_low,
        low * factor * e_high,
        high * factor * e_low,
        high * factor * e_high,
    ]
    return next_central, min(candidates), max(candidates)


def run_scenario(
    config: ChainConfig, node: str, shock_pct: float = DEFAULT_SHOCK_PCT
) -> dict[str, Any]:
    """以 node 为起点，按 transmission 的方向、弹性与滞后逐段推演。"""
    chain = list(config.transmission.chain)
    if node not in chain:
        raise ScenarioError(f"冲击节点 {node} 不在传导链中，可选：{'、'.join(chain)}")
    try:
        shock = float(shock_pct)
    except (TypeError, ValueError) as exc:
        raise ScenarioError("冲击幅度必须是数字") from exc
    if not -MAX_SHOCK_PCT <= shock <= MAX_SHOCK_PCT:
        raise ScenarioError(
            f"冲击幅度需在 -{MAX_SHOCK_PCT:.0f}% 到 {MAX_SHOCK_PCT:.0f}% 之间"
        )

    start = chain.index(node)
    nodes: list[dict[str, Any]] = []
    warnings: list[str] = []
    central = low = high = float(shock)
    days = 0

    for index, name in enumerate(chain):
        if index < start:
            nodes.append(
                {
                    "node": name,
                    "index": index,
                    "direction": "none",
                    "direction_label": DIRECTION_LABELS["none"],
                    "impact_pct": None,
                    "impact_low": None,
                    "impact_high": None,
                    "cumulative_days": None,
                    "edge": None,
                }
            )
            continue

        edge = None
        if index > start:
            prev = chain[index - 1]
            lag = _edge(config, prev, name)
            if lag is None:
                warnings.append(f"{prev}→{name} 未配置传导参数，按同向、弹性 1.0 推演")
                edge = _assumed_edge(prev, name)
            else:
                edge = _edge_payload(lag)
                days += lag.days
            central, low, high = _spread(central, low, high, lag)

        direction = _direction(central)
        nodes.append(
            {
                "node": name,
                "index": index,
                "direction": direction,
                "direction_label": DIRECTION_LABELS[direction],
                "impact_pct": _round(central),
                "impact_low": _round(low),
                "impact_high": _round(high),
                "cumulative_days": days,
                "edge": edge,
            }
        )

    last = nodes[-1]
    spread = (
        f"{_round(last['impact_low']):+g}% ~ {_round(last['impact_high']):+g}%"
        if last["impact_low"] != last["impact_high"]
        else f"{_round(last['impact_pct']):+g}%"
    )
    headline = (
        f"若{node}{shock:+g}%，{last['node']}约{_round(last['impact_pct']):+g}%"
        f"（区间 {spread}），累计滞后 {last['cumulative_days']} 天。"
    )

    return {
        "node": node,
        "shock_pct": _round(shock),
        "headline": headline,
        "chain": chain,
        "nodes": nodes,
        "warnings": warnings,
        "disclaimer": DISCLAIMER_TEXT,
    }
