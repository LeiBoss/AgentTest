"""研报数据组装、LLM 解读与合规校验。"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime
from typing import Any

from .analytics import indicator_stats
from .compliance import ComplianceError, filter_or_raise
from .db import get_last_status
from .llm import LLMError, call_llm
from .models import ChainConfig
from .prompts import P6_SYSTEM
from .rules import evaluate_rule

logger = logging.getLogger(__name__)

STATUS_LABELS = {"normal": "正常", "watch": "关注", "alert": "触发", "no_data": "无数据"}
RULE_TYPE_LABELS = {"condition": "一般组合", "price_volume_inv": "量价库存组合"}
CHOKE_STATE_LABELS = {
    "tight": "收紧",
    "balanced": "平衡",
    "loose": "放松",
}
CHOKE_TYPE_LABELS = {
    "technology": "技术",
    "resource": "资源",
    "capacity": "产能",
    "channel": "渠道",
    "regulation": "监管",
    "brand": "品牌",
    "ecosystem": "生态位",
    "crowded": "拥挤",
}

DISCLAIMER_TEXT = "本报告仅供产业链研究跟踪使用，数据存在口径与时效限制，不构成投资建议。"
NARRATIVE_FIELDS = (
    "summary",
    "transmission_reading",
    "signals_reading",
    "rules_reading",
    "choke_reading",
    "watch_next",
)


class ReportError(RuntimeError):
    """研报生成失败。"""


def _fmt(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _change_text(series: list[tuple[float, str]]) -> str:
    """series 按日期升序，取末两期计算较上期差值。"""
    if len(series) < 2:
        return ""
    return f"较上期 {series[-1][0] - series[-2][0]:+.2f}"


def _trigger_rows(conn: sqlite3.Connection, config: ChainConfig, symbol: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for trigger in config.triggers:
        stats = indicator_stats(conn, symbol, trigger)
        value = stats["value"]
        status = "no_data" if value is None else (get_last_status(conn, config.chain_name, trigger.id) or "normal")
        rows.append(
            {
                "id": trigger.id,
                "name": trigger.name,
                "layer": trigger.layer,
                "value": value,
                "value_text": _fmt(value),
                "unit": trigger.unit,
                "rule": trigger.rule,
                "normal_range": list(trigger.normal_range),
                "warn_range": list(trigger.warn_range),
                "crit_range": list(trigger.crit_range),
                "status": status,
                "status_label": STATUS_LABELS.get(status, status),
                "change_text": _change_text(stats["series"]),
                "metric_date": stats["metric_date"],
                "pct_rank": stats["pct_rank"],
                "pct_label": stats["pct_label"],
                "samples": stats["samples"],
                "streak": stats["streak"],
                "meta": stats["meta"],
                "meaning": trigger.meaning,
            }
        )
    return rows


def _signal_rows(conn: sqlite3.Connection, config: ChainConfig, limit: int = 10) -> list[dict[str, Any]]:
    names = {trigger.id: trigger.name for trigger in config.triggers}
    rows = conn.execute(
        """
        SELECT id, trigger_id, from_status, to_status, value, unit, ts, payload_json
        FROM signal_log WHERE chain = ? ORDER BY id DESC LIMIT ?
        """,
        (config.chain_name, limit),
    ).fetchall()
    signals: list[dict[str, Any]] = []
    for row in rows:
        interpretation = ""
        if row["payload_json"]:
            try:
                interpretation = str(json.loads(row["payload_json"]).get("interpretation", ""))
            except (json.JSONDecodeError, AttributeError):
                interpretation = ""
        signals.append(
            {
                "trigger_id": row["trigger_id"],
                "name": names.get(row["trigger_id"], row["trigger_id"]),
                "from_label": STATUS_LABELS.get(row["from_status"], row["from_status"]),
                "to_label": STATUS_LABELS.get(row["to_status"], row["to_status"]),
                "value_text": _fmt(row["value"]),
                "unit": row["unit"],
                "ts": row["ts"],
                "interpretation": interpretation,
            }
        )
    return signals


def _rule_rows(
    conn: sqlite3.Connection, config: ChainConfig, statuses: dict[str, str]
) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT rule_cond, judgment, ts FROM judgment_log WHERE chain = ? ORDER BY id DESC LIMIT 20",
        (config.chain_name,),
    ).fetchall()
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        latest.setdefault(row["rule_cond"], {"judgment": row["judgment"], "ts": row["ts"]})

    result: list[dict[str, Any]] = []
    for rule in config.rules:
        last = latest.get(rule.cond)
        result.append(
            {
                "cond": rule.cond,
                "res": rule.res,
                "type": rule.type,
                "type_label": RULE_TYPE_LABELS.get(rule.type, rule.type),
                "rationale": rule.rationale,
                "current_match": evaluate_rule(rule, statuses),
                "last_judgment": last["judgment"] if last else None,
                "last_ts": last["ts"] if last else None,
            }
        )
    return result


def _choke_rows(conn: sqlite3.Connection, config: ChainConfig) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT node, state, paradox, score FROM choke_log WHERE chain = ? ORDER BY id DESC",
        (config.chain_name,),
    ).fetchall()
    latest: dict[str, sqlite3.Row] = {}
    for row in rows:
        latest.setdefault(row["node"], row)

    result: list[dict[str, Any]] = []
    for point in config.choke_points:
        log = latest.get(point.node)
        state = log["state"] if log is not None else point.current_state
        paradox = bool(log["paradox"]) if log is not None else point.paradox_flag
        result.append(
            {
                "node": point.node,
                "type_label": CHOKE_TYPE_LABELS.get(point.type, point.type),
                "score": point.score.total,
                "level": point.level,
                "state": state,
                "state_label": CHOKE_STATE_LABELS.get(state, state),
                "profit_share_pct": point.profit_share_pct,
                "tracking_triggers": point.tracking_triggers,
                "paradox": paradox,
                "paradox_note": point.paradox_note,
            }
        )
    return result


def build_report_data(config: ChainConfig, conn: sqlite3.Connection, symbol: str) -> dict[str, Any]:
    """汇总一份研报所需的全部静态与动态数据（不做任何预测）。"""
    triggers = _trigger_rows(conn, config, symbol)
    statuses = {item["id"]: item["status"] for item in triggers}
    stats = {"normal": 0, "watch": 0, "alert": 0, "no_data": 0}
    for status in statuses.values():
        stats[status] = stats.get(status, 0) + 1

    dates = [item["metric_date"] for item in triggers if item["metric_date"]]
    current_stage = next(
        ({k: v for k, v in stage.model_dump().items() if k != "current"} for stage in reversed(config.stages) if stage.current),
        None,
    )
    return {
        "chain": config.chain_name,
        "data_date": max(dates) if dates else None,
        "stats": stats,
        "current_stage": current_stage,
        "wealth_logic": config.wealth_logic,
        "skeleton": [node.model_dump() for node in config.skeleton],
        "profit_pool": [item.model_dump() for item in config.profit_pool],
        "transmission": config.transmission.model_dump(),
        "stages": [stage.model_dump() for stage in config.stages],
        "triggers": triggers,
        "signals": _signal_rows(conn, config),
        "rules": _rule_rows(conn, config, statuses),
        "chokes": _choke_rows(conn, config),
        "companies": [company.model_dump() for company in config.companies],
        "data_gaps": list(config.data_gaps),
    }


def _narrative_input(data: dict[str, Any]) -> dict[str, Any]:
    """给 LLM 的输入只保留描述所需字段，避免传入多余噪声。"""
    return {
        "chain_name": data["chain"],
        "data_date": data["data_date"],
        "wealth_logic": data["wealth_logic"],
        "current_stage": data["current_stage"],
        "transmission": data["transmission"],
        "stats": data["stats"],
        "triggers": [
            {
                "id": item["id"],
                "name": item["name"],
                "layer": item["layer"],
                "value": item["value_text"],
                "unit": item["unit"],
                "status": item["status_label"],
                "metric_date": item["metric_date"],
                "pct_label": item["pct_label"],
                "streak": item["streak"],
            }
            for item in data["triggers"]
        ],
        "recent_signals": [
            {
                "trigger_id": item["trigger_id"],
                "name": item["name"],
                "from": item["from_label"],
                "to": item["to_label"],
                "value": item["value_text"],
                "unit": item["unit"],
                "ts": item["ts"],
            }
            for item in data["signals"]
        ],
        "rules": [
            {
                "cond": item["cond"],
                "res": item["res"],
                "type": item["type_label"],
                "rationale": item["rationale"],
                "current_match": item["current_match"],
                "last_judgment": item["last_judgment"],
            }
            for item in data["rules"]
        ],
        "chokes": [
            {
                "node": item["node"],
                "type": item["type_label"],
                "state": item["state_label"],
                "score": item["score"],
                "profit_share_pct": item["profit_share_pct"],
                "paradox": item["paradox"],
            }
            for item in data["chokes"]
        ],
    }


_PUNCT_RE = re.compile(r"[\s，。、；：！？,.;:!?（）()【】\[\]“”\"'《》<>—\-/\\]+")


def _norm_text(text: str) -> str:
    return _PUNCT_RE.sub("", text).lower()


def _cites_trigger(item: str, ids: list[str], names: list[str]) -> bool:
    """evidence 只要包含某个 trigger id 或指标名称（忽略标点与大小写）即视为可验证。"""
    lower = item.lower()
    for trigger_id in ids:
        pattern = rf"(?<![a-z0-9_]){re.escape(trigger_id.lower())}(?![a-z0-9_])"
        if re.search(pattern, lower):
            return True
    normalized = _norm_text(item)
    return any(name in normalized for name in names)


def _validate_narrative(result: Any, config: ChainConfig) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise ReportError("LLM 返回的不是 JSON 对象")
    for field in NARRATIVE_FIELDS:
        value = result.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ReportError(f"LLM 输出缺少字段或为空：{field}")

    risks = result.get("risks")
    if not isinstance(risks, list) or not risks or not all(isinstance(item, str) and item.strip() for item in risks):
        raise ReportError("LLM 输出 risks 必须是非空字符串数组")

    evidence = result.get("evidence")
    if not isinstance(evidence, list) or len(evidence) < 3:
        raise ReportError("LLM 输出 evidence 至少需要 3 条")
    ids = [trigger.id for trigger in config.triggers]
    names = [_norm_text(trigger.name) for trigger in config.triggers]
    valid = [
        item
        for item in evidence
        if isinstance(item, str) and item.strip() and _cites_trigger(item, ids, names)
    ]
    if len(valid) < 3:
        raise ReportError(
            "LLM 输出 evidence 未引用真实指标，每条须以 trigger id 开头。可用 id："
            + "、".join(ids)
        )
    return {**result, "evidence": valid}


def _narrative_text(result: dict[str, Any]) -> str:
    parts = [result[field] for field in NARRATIVE_FIELDS]
    parts.extend(result.get("risks", []))
    parts.append(DISCLAIMER_TEXT)
    return "\n".join(parts)


def generate_report(config: ChainConfig, conn: sqlite3.Connection, symbol: str) -> dict[str, Any]:
    """组装数据并生成 LLM 解读；合规不过关时带提示重试一次。"""
    data = build_report_data(config, conn, symbol)
    payload = _narrative_input(data)
    system_prompt = P6_SYSTEM
    last_error: Exception | None = None

    for attempt in range(2):
        try:
            result = call_llm(system_prompt, payload)
        except LLMError as exc:
            raise ReportError(f"LLM 调用失败：{exc}") from exc
        try:
            narrative = _validate_narrative(result, config)
            filter_or_raise(_narrative_text(narrative), "research_report")
        except (ReportError, ComplianceError) as exc:
            last_error = exc
            logger.warning("[report] 第 %s 次生成未通过: %s", attempt + 1, exc)
            system_prompt = (
                P6_SYSTEM
                + f"\n上次输出未通过审查：{exc}。请修正后重新输出完整 JSON，避免任何违规表述。"
            )
            continue

        report = {
            **data,
            "summary": narrative["summary"],
            "transmission_reading": narrative["transmission_reading"],
            "signals_reading": narrative["signals_reading"],
            "rules_reading": narrative["rules_reading"],
            "choke_reading": narrative["choke_reading"],
            "watch_next": narrative["watch_next"],
            "risks": narrative["risks"],
            "evidence": narrative["evidence"],
            "disclaimer": DISCLAIMER_TEXT,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
        }
        return report

    raise ReportError(f"研报生成未通过校验：{last_error}")
