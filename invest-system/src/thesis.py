"""Thesis 账本与催化剂日历：把核心判断写成可证伪条目。"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from typing import Any

from .db import (
    get_catalysts,
    get_theses,
    insert_catalyst,
    insert_thesis,
    update_catalyst_status,
    update_thesis_status,
)

THESIS_STATUSES = {"active": "待验证", "confirmed": "成立", "invalidated": "已证伪"}
CATALYST_STATUSES = {"pending": "待发生", "done": "已发生", "missed": "未发生"}
DIRECTION_LABELS = {"up": "上行", "down": "下行", "neutral": "中性"}
CATALYST_KIND_LABELS = {
    "earnings": "财报日",
    "price": "批价窗口",
    "policy": "政策节点",
    "other": "其他",
}

DUE_SOON_DAYS = 7
MAX_TITLE = 80
MAX_HORIZON = 40
MAX_TEXT = 400
MAX_CONFIDENCE = 100


class ThesisError(ValueError):
    """Thesis 或催化剂输入不合法。"""


def _text(payload: dict, key: str, label: str, required: bool, limit: int = MAX_TEXT) -> str:
    raw = payload.get(key, "")
    text = str(raw).strip() if raw is not None else ""
    if not text:
        if required:
            raise ThesisError(f"{label}不能为空")
        return ""
    if len(text) > limit:
        raise ThesisError(f"{label}不能超过 {limit} 字")
    return text


def _choice(payload: dict, key: str, label: str, options: dict[str, str], default: str | None = None) -> str:
    raw = payload.get(key, "")
    value = str(raw).strip() if raw is not None else ""
    if not value:
        if default is not None:
            return default
        raise ThesisError(f"{label}必须是：{'、'.join(options)}")
    if value not in options:
        raise ThesisError(f"{label}必须是：{'、'.join(options)}")
    return value


def _confidence(payload: dict) -> int:
    raw = payload.get("confidence")
    try:
        value = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ThesisError("置信度必须是 0 到 100 的整数") from exc
    if value < 0 or value > MAX_CONFIDENCE:
        raise ThesisError("置信度必须是 0 到 100 的整数")
    return value


def _event_date(payload: dict) -> str:
    raw = str(payload.get("event_date", "") or "").strip()
    if not raw:
        raise ThesisError("事件日期不能为空")
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise ThesisError("事件日期格式应为 YYYY-MM-DD") from exc


def validate_thesis(payload: dict) -> dict[str, Any]:
    """校验并归一化一条 Thesis，确认与证伪条件都必填。"""
    if not isinstance(payload, dict):
        raise ThesisError("请求体必须是 JSON 对象")
    return {
        "title": _text(payload, "title", "标题", True, MAX_TITLE),
        "direction": _choice(payload, "direction", "方向", DIRECTION_LABELS),
        "horizon": _text(payload, "horizon", "时间窗口", False, MAX_HORIZON),
        "core_logic": _text(payload, "core_logic", "核心逻辑", True),
        "confirm_condition": _text(payload, "confirm_condition", "确认条件", True),
        "invalidate_condition": _text(payload, "invalidate_condition", "证伪条件", True),
        "confidence": _confidence(payload),
    }


def validate_catalyst(payload: dict) -> dict[str, Any]:
    """校验并归一化一条催化剂。"""
    if not isinstance(payload, dict):
        raise ThesisError("请求体必须是 JSON 对象")
    return {
        "event_date": _event_date(payload),
        "title": _text(payload, "title", "事件名称", True, MAX_TITLE),
        "kind": _choice(payload, "kind", "类型", CATALYST_KIND_LABELS, "other"),
        "related": _text(payload, "related", "关联环节", False, 60),
        "expectation": _text(payload, "expectation", "届时看什么", False, 200),
    }


def _day(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _age_days(created_at: str | None, today: date) -> int | None:
    created = _day(created_at)
    return None if created is None else (today - created).days


def _timing(status: str, days_left: int) -> tuple[str, str]:
    if status == "done":
        return "done", "已完成"
    if status == "missed":
        return "missed", "未发生"
    if days_left < 0:
        return "overdue", f"逾期 {abs(days_left)} 天"
    if days_left == 0:
        return "today", "今天"
    if days_left <= DUE_SOON_DAYS:
        return "soon", f"{days_left} 天后"
    return "later", f"{days_left} 天后"


def _catalyst_rows(
    conn: sqlite3.Connection, chain: str, today: date
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    stats = {"pending": 0, "done": 0, "missed": 0, "soon": 0, "overdue": 0}
    pending: list[dict[str, Any]] = []
    closed: list[dict[str, Any]] = []
    for row in get_catalysts(conn, chain):
        event = _day(row["event_date"])
        days_left = (event - today).days if event else 0
        phase, timing_label = _timing(row["status"], days_left)
        stats[row["status"]] = stats.get(row["status"], 0) + 1
        if phase in ("soon", "overdue"):
            stats[phase] += 1
        item = {
            "id": row["id"],
            "event_date": row["event_date"],
            "title": row["title"],
            "kind": row["kind"],
            "kind_label": CATALYST_KIND_LABELS.get(row["kind"], row["kind"]),
            "related": row["related"],
            "expectation": row["expectation"],
            "status": row["status"],
            "status_label": CATALYST_STATUSES.get(row["status"], row["status"]),
            "days_left": days_left if event else None,
            "phase": phase,
            "timing_label": timing_label,
            "created_at": row["created_at"],
        }
        (pending if row["status"] == "pending" else closed).append(item)
    closed.sort(key=lambda item: item["event_date"], reverse=True)
    return pending + closed, stats


def thesis_board(conn: sqlite3.Connection, chain: str, today: date | None = None) -> dict[str, Any]:
    """汇总某条产业链的 Thesis 与催化剂日历。"""
    current = today or date.today()
    stats = {key: 0 for key in THESIS_STATUSES}
    theses: list[dict[str, Any]] = []
    for row in get_theses(conn, chain):
        stats[row["status"]] = stats.get(row["status"], 0) + 1
        theses.append(
            {
                "id": row["id"],
                "title": row["title"],
                "direction": row["direction"],
                "direction_label": DIRECTION_LABELS.get(row["direction"], row["direction"]),
                "horizon": row["horizon"],
                "core_logic": row["core_logic"],
                "confirm_condition": row["confirm_condition"],
                "invalidate_condition": row["invalidate_condition"],
                "confidence": row["confidence"],
                "status": row["status"],
                "status_label": THESIS_STATUSES.get(row["status"], row["status"]),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "age_days": _age_days(row["created_at"], current),
            }
        )
    catalysts, catalyst_stats = _catalyst_rows(conn, chain, current)
    return {
        "theses": theses,
        "stats": stats,
        "catalysts": catalysts,
        "catalyst_stats": catalyst_stats,
    }


def create_thesis(
    conn: sqlite3.Connection, chain: str, payload: dict, today: date | None = None
) -> dict[str, Any]:
    """新增 Thesis 后返回最新账本。"""
    insert_thesis(conn, chain, validate_thesis(payload))
    return thesis_board(conn, chain, today)


def set_thesis_status(
    conn: sqlite3.Connection, chain: str, thesis_id: int, payload: dict, today: date | None = None
) -> dict[str, Any]:
    status = _choice(payload, "status", "状态", THESIS_STATUSES)
    if update_thesis_status(conn, chain, thesis_id, status) is None:
        raise ThesisError(f"未找到 Thesis #{thesis_id}")
    return thesis_board(conn, chain, today)


def create_catalyst(
    conn: sqlite3.Connection, chain: str, payload: dict, today: date | None = None
) -> dict[str, Any]:
    """新增催化剂后返回最新账本。"""
    insert_catalyst(conn, chain, validate_catalyst(payload))
    return thesis_board(conn, chain, today)


def set_catalyst_status(
    conn: sqlite3.Connection, chain: str, catalyst_id: int, payload: dict, today: date | None = None
) -> dict[str, Any]:
    status = _choice(payload, "status", "状态", CATALYST_STATUSES)
    if update_catalyst_status(conn, chain, catalyst_id, status) is None:
        raise ThesisError(f"未找到催化剂 #{catalyst_id}")
    return thesis_board(conn, chain, today)
