"""链内公司横截面指标、相对打分与等权链指数。"""

from __future__ import annotations

import sqlite3
from statistics import median, pstdev
from typing import Any

from .db import get_company_fin_history, get_metric_series
from .models import ChainConfig, Company
from .valuation import valuation_snapshot

RETURN_WINDOWS = (20, 60, 120)
PRICE_SERIES_LIMIT = 140
MIN_CROSS_SECTION = 3
MIN_COVERAGE = 0.5

SCORE_WEIGHTS = {"growth": 0.30, "quality": 0.30, "valuation": 0.25, "momentum": 0.15}
SCORE_LABELS = ((70, "领先"), (40, "中性"))
SCORE_FALLBACK = "落后"
DATA_GAP_LABEL = "数据不足"


def _window_return(series: list[tuple[float, str]], periods: int) -> float | None:
    """按交易日间隔计算区间涨跌幅。"""
    if len(series) <= periods:
        return None
    latest = series[-1][0]
    base = series[-1 - periods][0]
    if base == 0:
        return None
    return round((latest / base - 1) * 100, 2)


def _rank(
    pool: list[float], value: float | None, higher_better: bool = True
) -> float | None:
    """链内截面百分位，0 表示最差、100 表示最好。"""
    if value is None or len(pool) < MIN_CROSS_SECTION:
        return None
    better = (
        sum(1 for item in pool if item < value)
        if higher_better
        else sum(1 for item in pool if item > value)
    )
    ties = sum(1 for item in pool if item == value) - 1
    return round((better + ties / 2) / (len(pool) - 1) * 100, 1)


def _average(values: list[float | None]) -> float | None:
    valid = [value for value in values if value is not None]
    return round(sum(valid) / len(valid), 1) if valid else None


def _company_row(conn: sqlite3.Connection, company: Company) -> dict[str, Any]:
    """单家公司的价格、估值与最新财务指标。"""
    ticker = company.ticker.strip()
    series: list[tuple[float, str]] = []
    if ticker:
        series = get_metric_series(conn, ticker, "close", limit=PRICE_SERIES_LIMIT)

    returns = {
        f"ret_{periods}": _window_return(series, periods) for periods in RETURN_WINDOWS
    }
    valuation = {
        item["id"]: item for item in valuation_snapshot(conn, ticker)
    } if ticker else {}
    pe = valuation.get("pe_ttm", {})
    pb = valuation.get("pb_mrq", {})

    history = get_company_fin_history(conn, ticker, limit=2) if ticker else []
    latest = history[0] if history else {}
    prior = history[1] if len(history) > 1 else {}
    yoy_ni = latest.get("yoy_ni")
    prior_yoy_ni = prior.get("yoy_ni")

    return {
        "name": company.name,
        "ticker": ticker,
        "link": company.link,
        "stage": company.stage,
        "role": company.role,
        "note": company.note,
        "price": series[-1][0] if series else None,
        "price_date": series[-1][1] if series else None,
        **returns,
        "pe_ttm": pe.get("value"),
        "pe_pct": pe.get("pct_rank"),
        "pe_label": pe.get("pct_label"),
        "pb_mrq": pb.get("value"),
        "pb_pct": pb.get("pct_rank"),
        "pb_label": pb.get("pct_label"),
        "yoy_ni": yoy_ni,
        "yoy_ni_prev": prior_yoy_ni,
        "yoy_ni_delta": (
            round(yoy_ni - prior_yoy_ni, 1)
            if yoy_ni is not None and prior_yoy_ni is not None
            else None
        ),
        "yoy_eps": latest.get("yoy_eps"),
        "gp_margin": latest.get("gp_margin"),
        "np_margin": latest.get("np_margin"),
        "roe": latest.get("roe"),
        "cfo_to_np": latest.get("cfo_to_np"),
        "liability_to_asset": latest.get("liability_to_asset"),
        "asset_turn": latest.get("asset_turn"),
        "fin_date": latest.get("stat_date"),
        "fin_pub_date": latest.get("pub_date"),
        "scores": {},
        "score": None,
        "score_label": DATA_GAP_LABEL,
        "coverage": 0.0,
    }


def _apply_scores(rows: list[dict[str, Any]]) -> None:
    """按链内截面相对位置给成长、质量、估值、动量打分。"""
    pools = {
        "yoy_ni": [row["yoy_ni"] for row in rows if row["yoy_ni"] is not None],
        "yoy_eps": [row["yoy_eps"] for row in rows if row["yoy_eps"] is not None],
        "gp_margin": [row["gp_margin"] for row in rows if row["gp_margin"] is not None],
        "np_margin": [row["np_margin"] for row in rows if row["np_margin"] is not None],
        "roe": [row["roe"] for row in rows if row["roe"] is not None],
        "cfo_to_np": [row["cfo_to_np"] for row in rows if row["cfo_to_np"] is not None],
        "liability_to_asset": [
            row["liability_to_asset"]
            for row in rows
            if row["liability_to_asset"] is not None
        ],
        "asset_turn": [row["asset_turn"] for row in rows if row["asset_turn"] is not None],
        "ret_20": [row["ret_20"] for row in rows if row["ret_20"] is not None],
        "ret_60": [row["ret_60"] for row in rows if row["ret_60"] is not None],
    }

    for row in rows:
        growth = _average(
            [
                _rank(pools["yoy_ni"], row["yoy_ni"]),
                _rank(pools["yoy_eps"], row["yoy_eps"]),
            ]
        )
        quality = _average(
            [
                _rank(pools["gp_margin"], row["gp_margin"]),
                _rank(pools["np_margin"], row["np_margin"]),
                _rank(pools["roe"], row["roe"]),
                _rank(pools["cfo_to_np"], row["cfo_to_np"]),
                _rank(pools["liability_to_asset"], row["liability_to_asset"], False),
                _rank(pools["asset_turn"], row["asset_turn"]),
            ]
        )
        pct_values = [
            value for value in (row["pe_pct"], row["pb_pct"]) if value is not None
        ]
        valuation = (
            round(100 - sum(pct_values) / len(pct_values), 1) if pct_values else None
        )
        momentum = _average(
            [
                _rank(pools["ret_20"], row["ret_20"]),
                _rank(pools["ret_60"], row["ret_60"]),
            ]
        )

        scores = {
            "growth": growth,
            "quality": quality,
            "valuation": valuation,
            "momentum": momentum,
        }
        eligible = {
            key: value for key, value in scores.items() if value is not None
        }
        weight = sum(SCORE_WEIGHTS[key] for key in eligible)
        if weight >= MIN_COVERAGE:
            row["score"] = round(
                sum(SCORE_WEIGHTS[key] * value for key, value in eligible.items())
                / weight
            )
            row["score_label"] = next(
                (label for threshold, label in SCORE_LABELS if row["score"] >= threshold),
                SCORE_FALLBACK,
            )
        row["coverage"] = round(weight, 2)
        row["scores"] = scores


def _percent(value: float | None) -> float | None:
    return round(value, 2) if value is not None else None


def chain_index(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """等权链指数收益、上涨占比、离散度与基本面广度。"""
    total = len(rows)
    price_rows = [row for row in rows if row["price"] is not None]
    returns = {
        key: _average([row[key] for row in rows])
        for key in ("ret_20", "ret_60", "ret_120")
    }
    breadth_pool = [row["ret_20"] for row in rows if row["ret_20"] is not None]
    breadth = (
        round(sum(1 for value in breadth_pool if value > 0) / len(breadth_pool) * 100, 1)
        if breadth_pool
        else None
    )
    dispersion_pool = [row["ret_60"] for row in rows if row["ret_60"] is not None]
    dispersion = (
        round(pstdev(dispersion_pool), 2) if len(dispersion_pool) >= 2 else None
    )
    growth_pool = [row["yoy_ni"] for row in rows if row["yoy_ni"] is not None]
    growth_breadth = (
        round(sum(1 for value in growth_pool if value > 0) / len(growth_pool) * 100, 1)
        if growth_pool
        else None
    )
    pe_pool = [row["pe_ttm"] for row in rows if row["pe_ttm"] is not None]
    pb_pool = [row["pb_mrq"] for row in rows if row["pb_mrq"] is not None]
    prices = [row["price_date"] for row in rows if row["price_date"]]

    return {
        "label": "等权链指数",
        **returns,
        "breadth_20": breadth,
        "dispersion_60": dispersion,
        "growth_breadth": growth_breadth,
        "median_pe": _percent(median(pe_pool)) if pe_pool else None,
        "median_pb": _percent(median(pb_pool)) if pb_pool else None,
        "price_coverage": f"{len(price_rows)}/{total}" if total else "0/0",
        "date": max(prices) if prices else None,
    }


def company_scorecards(
    conn: sqlite3.Connection, config: ChainConfig
) -> list[dict[str, Any]]:
    """返回链内公司指标与相对打分，顺序与配置一致。"""
    rows = [_company_row(conn, company) for company in config.companies]
    _apply_scores(rows)
    return rows
