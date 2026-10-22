"""触发器状态判定与状态迁移检测。"""

from __future__ import annotations

import sqlite3
from typing import Literal

from .db import get_last_status, insert_signal, now_iso
from .models import ChainConfig, Trigger

Status = Literal["normal", "watch", "alert"]


def judge_status(trigger: Trigger, value: float) -> Status:
    """按 rule 方向与阈值端点判定状态。"""
    if trigger.rule == "above":
        if value >= trigger.crit_range[0]:
            return "alert"
        if value >= trigger.warn_range[0]:
            return "watch"
        return "normal"

    if value <= trigger.crit_range[1]:
        return "alert"
    if value <= trigger.warn_range[1]:
        return "watch"
    return "normal"


def detect_transition(
    conn: sqlite3.Connection, chain: str, trigger: Trigger, current_value: float
) -> dict | None:
    """与最近一次状态比较，有变化则返回迁移信息。"""
    last_status = get_last_status(conn, chain, trigger.id) or "normal"
    new_status = judge_status(trigger, current_value)
    if new_status == last_status:
        return None
    return {
        "trigger_id": trigger.id,
        "from_status": last_status,
        "to_status": new_status,
        "value": current_value,
        "unit": trigger.unit,
    }


def run_judge(
    conn: sqlite3.Connection, config: ChainConfig, values: dict[str, float]
) -> list[dict]:
    """批量判定并记录状态迁移。"""
    transitions: list[dict] = []

    for trigger in config.triggers:
        if f"_missing_{trigger.id}" in values:
            continue
        value = values.get(trigger.id)
        if value is None:
            continue

        transition = detect_transition(conn, config.chain_name, trigger, value)
        if transition is None:
            continue

        signal_id = insert_signal(
            conn,
            config.chain_name,
            trigger.id,
            transition["from_status"],
            transition["to_status"],
            value,
            trigger.unit,
            now_iso(),
        )
        transitions.append({**transition, "signal_id": signal_id})

    return transitions
