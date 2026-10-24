"""估值指标的历史分位对照。"""

from __future__ import annotations

import sqlite3
from typing import Any

from .analytics import _age_days, _pct_rank
from .db import get_metric_series

VALUATION_LOOKBACK = 1250
VALUATION_MIN_SAMPLES = 250

VALUATION_METRICS: tuple[dict[str, str], ...] = (
    {
        "id": "pe_ttm",
        "name": "市盈率 TTM",
        "unit": "倍",
        "note": "股价除以近十二个月每股收益，盈利为负时分位参考意义有限",
    },
    {
        "id": "pb_mrq",
        "name": "市净率",
        "unit": "倍",
        "note": "股价除以最新一期每股净资产",
    },
    {
        "id": "ps_ttm",
        "name": "市销率 TTM",
        "unit": "倍",
        "note": "总市值除以近十二个月营业收入",
    },
    {
        "id": "pcf_ncf_ttm",
        "name": "市现率 TTM",
        "unit": "倍",
        "note": "总市值除以近十二个月经营现金流净额",
    },
)


def _fmt(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.2f}".rstrip("0").rstrip(".")


def valuation_snapshot(
    conn: sqlite3.Connection, symbol: str
) -> list[dict[str, Any]]:
    """返回各估值指标的最新值、近五年分位与样本信息。"""
    rows: list[dict[str, Any]] = []
    for metric in VALUATION_METRICS:
        series = get_metric_series(
            conn, symbol, metric["id"], limit=VALUATION_LOOKBACK
        )
        latest = series[-1] if series else None
        pct_rank, pct_label = _pct_rank(series, min_samples=VALUATION_MIN_SAMPLES)
        if latest is None:
            pct_label = "无数据"
        rows.append(
            {
                "id": metric["id"],
                "name": metric["name"],
                "unit": metric["unit"],
                "note": metric["note"],
                "value": latest[0] if latest else None,
                "value_text": _fmt(latest[0] if latest else None),
                "metric_date": latest[1] if latest else None,
                "pct_rank": pct_rank,
                "pct_label": pct_label,
                "samples": len(series),
                "age_days": _age_days(latest[1] if latest else None),
            }
        )
    return rows
