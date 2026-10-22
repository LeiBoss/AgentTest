"""组合规则的条件解析与布尔判断。"""

from __future__ import annotations

import re
import sqlite3

from .models import ChainConfig, Rule

_TERM_RE = re.compile(r"^(?P<id>\S+)\s+(?P<mode>触发|非正常)$")


def parse_cond(cond: str) -> list[tuple[str, str]]:
    """解析 "id 触发 AND id 非正常" 为 (trigger_id, expected_state) 列表。"""
    parsed: list[tuple[str, str]] = []
    for term in cond.split(" AND "):
        match = _TERM_RE.match(term.strip())
        if match is None:
            raise ValueError(f"无法解析条件项: {term}")
        expected = "alert" if match.group("mode") == "触发" else "not_normal"
        parsed.append((match.group("id"), expected))
    return parsed


def evaluate_rule(rule: Rule, statuses: dict[str, str]) -> bool:
    """所有条件项都满足才返回 True，缺少状态视为不满足。"""
    for trigger_id, expected in parse_cond(rule.cond):
        status = statuses.get(trigger_id)
        if expected == "alert":
            matched = status == "alert"
        else:
            matched = status in ("watch", "alert")
        if not matched:
            return False
    return True


def run_rules(
    conn: sqlite3.Connection, config: ChainConfig, statuses: dict[str, str]
) -> list[dict]:
    """返回全部被触发的规则及其参与判断的触发器状态。"""
    triggered: list[dict] = []

    for rule in config.rules:
        if not evaluate_rule(rule, statuses):
            continue
        active_triggers = [
            {"trigger_id": trigger_id, "status": statuses.get(trigger_id)}
            for trigger_id, _ in parse_cond(rule.cond)
        ]
        triggered.append(
            {
                "rule_cond": rule.cond,
                "res": rule.res,
                "rationale": rule.rationale,
                "active_triggers": active_triggers,
            }
        )

    return triggered
