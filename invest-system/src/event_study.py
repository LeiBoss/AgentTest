"""信号事件研究：状态迁移后等权链指数的前瞻收益。"""

from __future__ import annotations

import sqlite3
from bisect import bisect_left
from collections import defaultdict
from math import ceil
from typing import Any

from .db import get_metric_series
from .models import ChainConfig

RETURN_WINDOWS = (5, 20, 60)
MIN_EVENTS = 5
MIN_PRICE_DAYS = 30
PRICE_SERIES_LIMIT = 1600
MIN_COMPANY_RATIO = 0.5
EVENT_STATUSES = ("watch", "alert")


def _price_index(
    conn: sqlite3.Connection, config: ChainConfig
) -> tuple[list[str], list[float], int]:
    """等权链指数：各公司按首个收盘价归一化，再按日取可用公司均值。"""
    normalized: dict[str, dict[str, float]] = {}
    for company in config.companies:
        ticker = company.ticker.strip()
        if not ticker:
            continue
        series = get_metric_series(conn, ticker, "close", limit=PRICE_SERIES_LIMIT)
        if len(series) < MIN_PRICE_DAYS or series[0][0] <= 0:
            continue
        base = series[0][0]
        normalized[ticker] = {day: value / base for value, day in series}

    if not normalized:
        return [], [], 0

    min_count = max(1, ceil(len(normalized) * MIN_COMPANY_RATIO))
    dates = sorted({day for values in normalized.values() for day in values})
    index_dates: list[str] = []
    index_values: list[float] = []
    for day in dates:
        members = [values[day] for values in normalized.values() if day in values]
        if len(members) < min_count:
            continue
        index_dates.append(day)
        index_values.append(sum(members) / len(members))
    return index_dates, index_values, len(normalized)


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 2) if values else None


def _forward_returns(values: list[float], periods: int) -> list[float]:
    """全样本滚动前瞻收益，作为事件收益的对照基准。"""
    returns = [
        (values[index + periods] / values[index] - 1) * 100
        for index in range(len(values) - periods)
        if values[index] != 0
    ]
    return [round(item, 2) for item in returns]


def _event_dates(conn: sqlite3.Connection, chain: str) -> dict[str, list[str]]:
    """按触发器归集进入 watch/alert 的日期，同触发器同日只保留一次。"""
    rows = conn.execute(
        "SELECT trigger_id, ts FROM signal_log"
        " WHERE chain = ? AND to_status IN (?, ?) ORDER BY ts, id",
        (chain, *EVENT_STATUSES),
    ).fetchall()
    grouped: dict[str, list[str]] = defaultdict(list)
    seen: set[tuple[str, str]] = set()
    for row in rows:
        day = str(row["ts"])[:10]
        key = (str(row["trigger_id"]), day)
        if key in seen:
            continue
        seen.add(key)
        grouped[str(row["trigger_id"])].append(day)
    return grouped


def _event_window(
    dates: list[str], values: list[float], event_days: list[str], periods: int
) -> tuple[list[float], int]:
    """事件日收盘进入，持有 periods 个交易日；数据不足记为待观察。"""
    returns: list[float] = []
    pending = 0
    for day in event_days:
        position = bisect_left(dates, day)
        if position >= len(dates) or position + periods >= len(dates):
            pending += 1
            continue
        base = values[position]
        if base == 0:
            pending += 1
            continue
        returns.append(round((values[position + periods] / base - 1) * 100, 2))
    return returns, pending


def signal_study(conn: sqlite3.Connection, config: ChainConfig) -> dict[str, Any]:
    """按触发器统计状态迁移后的前瞻收益、胜率与相对基准超额。"""
    dates, values, covered = _price_index(conn, config)
    baseline = {window: _mean(_forward_returns(values, window)) for window in RETURN_WINDOWS}
    grouped = _event_dates(conn, config.chain_name)

    rows: list[dict[str, Any]] = []
    for trigger in config.triggers:
        event_days = grouped.get(trigger.id, [])
        if not event_days:
            continue
        windows = {
            window: _event_window(dates, values, event_days, window)
            for window in RETURN_WINDOWS
        }
        returns_20 = windows[20][0]
        avg_20 = _mean(returns_20)
        rows.append(
            {
                "id": trigger.id,
                "name": trigger.name,
                "events": len(event_days),
                "samples": len(returns_20),
                "pending": windows[20][1],
                "avg_5": _mean(windows[5][0]),
                "avg_20": avg_20,
                "avg_60": _mean(windows[60][0]),
                "win_20": (
                    round(sum(1 for value in returns_20 if value > 0) / len(returns_20) * 100, 1)
                    if returns_20
                    else None
                ),
                "excess_20": (
                    round(avg_20 - baseline[20], 2)
                    if avg_20 is not None and baseline[20] is not None
                    else None
                ),
                "sample_label": "样本充足" if len(returns_20) >= MIN_EVENTS else "样本不足",
            }
        )

    return {
        "label": "等权链指数",
        "windows": list(RETURN_WINDOWS),
        "baseline": {str(window): baseline[window] for window in RETURN_WINDOWS},
        "triggers": rows,
        "events": sum(row["events"] for row in rows),
        "samples": sum(row["samples"] for row in rows),
        "price_coverage": f"{covered}/{len(config.companies)}",
        "index_days": len(dates),
        "date": dates[-1] if dates else None,
        "min_events": MIN_EVENTS,
    }
