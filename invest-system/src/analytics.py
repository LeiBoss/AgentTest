"""指标历史分位、连续异常期数与数据元信息。"""

from __future__ import annotations

import sqlite3
from datetime import date

from .db import get_metric_series
from .judge import judge_status
from .models import Trigger

LOOKBACK = 250
MIN_SAMPLES = 20
HIGH_PCT = 80
LOW_PCT = 20


def _age_days(metric_date: str | None) -> int | None:
    """数据日期距今天数，日期格式非法时返回 None。"""
    if not metric_date:
        return None
    try:
        day = date.fromisoformat(metric_date[:10])
    except ValueError:
        return None
    return max((date.today() - day).days, 0)


def _pct_rank(
    series: list[tuple[float, str]], min_samples: int = MIN_SAMPLES
) -> tuple[int | None, str]:
    """当前值在历史序列（不含当期）中的百分位与位置标签。"""
    samples = len(series)
    if samples < min_samples:
        return None, "样本不足"
    latest = series[-1][0]
    prior = [value for value, _ in series[:-1]]
    rank = round(sum(1 for value in prior if value < latest) / len(prior) * 100)
    if rank >= HIGH_PCT:
        return rank, "高位"
    if rank <= LOW_PCT:
        return rank, "低位"
    return rank, "中位"


def _abnormal_streak(series: list[tuple[float, str]], trigger: Trigger) -> int:
    """从最新一期往前，连续处于关注或触发的期数。"""
    streak = 0
    for value, _ in reversed(series):
        if judge_status(trigger, value) == "normal":
            break
        streak += 1
    return streak


def metric_meta(trigger: Trigger, metric_date: str | None) -> dict:
    """指标的数据来源、频率、滞后与口径说明。"""
    return {
        "source": trigger.src,
        "source_note": trigger.source_note,
        "freq": trigger.freq,
        "lag_days": trigger.lag_days,
        "definition": trigger.definition,
        "metric_date": metric_date,
        "age_days": _age_days(metric_date),
    }


def indicator_stats(
    conn: sqlite3.Connection,
    symbol: str,
    trigger: Trigger,
    lookback: int = LOOKBACK,
) -> dict:
    """一次查询同时给出最新值、历史分位、连续异常期数与元信息。"""
    series = get_metric_series(conn, symbol, trigger.id, limit=lookback)
    latest = series[-1] if series else None
    pct_rank, pct_label = _pct_rank(series)
    return {
        "series": series,
        "value": latest[0] if latest else None,
        "metric_date": latest[1] if latest else None,
        "pct_rank": pct_rank,
        "pct_label": pct_label,
        "samples": len(series),
        "streak": _abnormal_streak(series, trigger),
        "meta": metric_meta(trigger, latest[1] if latest else None),
    }
