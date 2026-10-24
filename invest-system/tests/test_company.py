"""链内公司横截面打分与等权链指数。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from copy import deepcopy
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from src.company import (
    _apply_scores,
    _rank,
    _window_return,
    chain_index,
    company_scorecards,
)
from src.db import init_db, upsert_company_fin, upsert_metric
from src.models import ChainConfig

ROW_FIELDS = (
    "yoy_ni",
    "yoy_eps",
    "gp_margin",
    "np_margin",
    "roe",
    "cfo_to_np",
    "liability_to_asset",
    "asset_turn",
    "ret_20",
    "ret_60",
    "pe_pct",
    "pb_pct",
)


@pytest.fixture()
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = init_db(str(tmp_path / "invest.db"))
    yield connection
    connection.close()


def _row(**values: Any) -> dict[str, Any]:
    row: dict[str, Any] = {field: None for field in ROW_FIELDS}
    row.update(values)
    row.update({"score": None, "score_label": "数据不足", "coverage": 0.0, "scores": {}})
    return row


def _payload_with_companies(payload: dict[str, Any]) -> dict[str, Any]:
    data = deepcopy(payload)
    data["companies"] = [
        {
            "name": "甲公司",
            "ticker": "600001",
            "link": "高端酒企",
            "stage": "中游酒企",
            "role": "龙头",
            "note": "高端价格带",
        },
        {
            "name": "乙公司",
            "ticker": "600002",
            "link": "区域酒企",
            "stage": "中游酒企",
            "role": "区域",
            "note": "",
        },
        {
            "name": "丙公司",
            "ticker": "600003",
            "link": "经销商",
            "stage": "下游渠道与终端",
            "role": "渠道",
            "note": "",
        },
    ]
    return data


def _seed_prices(
    conn: sqlite3.Connection, symbol: str, base: float, step: float
) -> None:
    start = date(2026, 1, 1)
    rows = [
        (symbol, (start + timedelta(days=index)).isoformat(), "close", base + step * index)
        for index in range(130)
    ]
    conn.executemany(
        "INSERT OR REPLACE INTO daily_metric"
        " (symbol, metric_date, metric_id, value) VALUES (?, ?, ?, ?)",
        rows,
    )
    conn.commit()


def _seed_valuation(
    conn: sqlite3.Connection, symbol: str, values: list[float]
) -> None:
    start = date(2025, 1, 1)
    rows: list[tuple[str, str, str, float]] = []
    for index, value in enumerate(values):
        day = (start + timedelta(days=index)).isoformat()
        rows.append((symbol, day, "pe_ttm", value))
        rows.append((symbol, day, "pb_mrq", round(value / 5, 2)))
    conn.executemany(
        "INSERT OR REPLACE INTO daily_metric"
        " (symbol, metric_date, metric_id, value) VALUES (?, ?, ?, ?)",
        rows,
    )
    conn.commit()


def _seed_fin(conn: sqlite3.Connection, symbol: str, **overrides: float) -> None:
    latest = {
        "yoy_ni": 30.0,
        "yoy_eps": 28.0,
        "gp_margin": 90.0,
        "np_margin": 50.0,
        "roe": 20.0,
        "cfo_to_np": 1.5,
        "liability_to_asset": 20.0,
        "asset_turn": 1.2,
    }
    latest.update(overrides)
    upsert_company_fin(conn, symbol, "2026-06-30", "2026-08-20", latest)
    prior = {
        "yoy_ni": latest["yoy_ni"] - 5,
        "yoy_eps": latest["yoy_eps"] - 5,
    }
    upsert_company_fin(conn, symbol, "2026-03-31", "2026-04-28", prior)


def test_window_return_uses_trading_days() -> None:
    series = [(100.0, "d1"), (105.0, "d2"), (110.0, "d3")]

    assert _window_return(series, 2) == pytest.approx(10.0)
    assert _window_return(series, 3) is None
    assert _window_return([(0.0, "d1"), (1.0, "d2")], 1) is None


def test_rank_handles_ties_and_small_pool() -> None:
    assert _rank([1.0, 2.0, 3.0], 3.0) == 100.0
    assert _rank([1.0, 2.0, 3.0], 1.0) == 0.0
    assert _rank([1.0, 2.0, 3.0], 3.0, higher_better=False) == 0.0
    assert _rank([1.0, 1.0, 1.0], 1.0) == 50.0
    assert _rank([1.0, 2.0], 2.0) is None
    assert _rank([1.0, 2.0, 3.0], None) is None


def test_apply_scores_marks_low_coverage_as_data_gap() -> None:
    rows = [_row(yoy_ni=value) for value in (30.0, 10.0, 20.0)]

    _apply_scores(rows)

    assert rows[0]["scores"]["growth"] == 100.0
    assert rows[0]["coverage"] == 0.3
    assert rows[0]["score"] is None
    assert rows[0]["score_label"] == "数据不足"


def test_apply_scores_weights_all_dimensions() -> None:
    rows = [
        _row(
            yoy_ni=30.0, yoy_eps=30.0, gp_margin=90.0, np_margin=50.0, roe=20.0,
            cfo_to_np=1.5, liability_to_asset=20.0, asset_turn=1.2,
            ret_20=10.0, ret_60=20.0, pe_pct=0.0, pb_pct=0.0,
        ),
        _row(
            yoy_ni=20.0, yoy_eps=20.0, gp_margin=80.0, np_margin=40.0, roe=15.0,
            cfo_to_np=1.2, liability_to_asset=30.0, asset_turn=1.0,
            ret_20=0.0, ret_60=0.0, pe_pct=50.0, pb_pct=50.0,
        ),
        _row(
            yoy_ni=10.0, yoy_eps=10.0, gp_margin=70.0, np_margin=30.0, roe=10.0,
            cfo_to_np=1.0, liability_to_asset=40.0, asset_turn=0.8,
            ret_20=-10.0, ret_60=-20.0, pe_pct=100.0, pb_pct=100.0,
        ),
    ]

    _apply_scores(rows)

    assert rows[0]["coverage"] == 1.0
    assert rows[0]["score"] == 100
    assert rows[0]["score_label"] == "领先"
    assert rows[2]["score"] == 0
    assert rows[2]["score_label"] == "落后"


def test_chain_index_averages_and_breadth() -> None:
    rows = [
        {
            "price": 10.0, "price_date": "2026-10-20",
            "ret_20": 5.0, "ret_60": 10.0, "ret_120": 20.0,
            "yoy_ni": 30.0, "pe_ttm": 10.0, "pb_mrq": 2.0,
        },
        {
            "price": 20.0, "price_date": "2026-10-21",
            "ret_20": -5.0, "ret_60": 0.0, "ret_120": -10.0,
            "yoy_ni": -10.0, "pe_ttm": 20.0, "pb_mrq": 3.0,
        },
        {
            "price": None, "price_date": None,
            "ret_20": None, "ret_60": None, "ret_120": None,
            "yoy_ni": 10.0, "pe_ttm": None, "pb_mrq": None,
        },
    ]

    index = chain_index(rows)

    assert index["ret_20"] == 0.0
    assert index["ret_60"] == 5.0
    assert index["breadth_20"] == 50.0
    assert index["dispersion_60"] == 5.0
    assert index["growth_breadth"] == pytest.approx(66.7)
    assert index["median_pe"] == 15.0
    assert index["median_pb"] == 2.5
    assert index["price_coverage"] == "2/3"
    assert index["date"] == "2026-10-21"


def test_company_scorecards_rank_within_chain(
    conn: sqlite3.Connection, valid_payload: dict[str, Any]
) -> None:
    _seed_prices(conn, "600001", base=100.0, step=1.0)
    _seed_prices(conn, "600002", base=100.0, step=-0.5)
    _seed_prices(conn, "600003", base=100.0, step=0.0)
    _seed_valuation(conn, "600001", [30.0 - 20.0 * index / 259 for index in range(260)])
    _seed_valuation(conn, "600002", [10.0 + 20.0 * index / 259 for index in range(260)])
    _seed_valuation(
        conn, "600003", [15.0 if index % 2 else 25.0 for index in range(259)] + [20.0]
    )
    _seed_fin(conn, "600001")
    _seed_fin(
        conn, "600002",
        yoy_ni=5.0, yoy_eps=4.0, gp_margin=70.0, np_margin=25.0, roe=8.0,
        cfo_to_np=0.8, liability_to_asset=45.0, asset_turn=0.7,
    )
    _seed_fin(
        conn, "600003",
        yoy_ni=15.0, yoy_eps=14.0, gp_margin=80.0, np_margin=35.0, roe=12.0,
        cfo_to_np=1.1, liability_to_asset=32.0, asset_turn=0.9,
    )
    config = ChainConfig.model_validate(_payload_with_companies(valid_payload))

    rows = company_scorecards(conn, config)

    assert [row["ticker"] for row in rows] == ["600001", "600002", "600003"]
    assert rows[0]["score"] >= 70
    assert rows[0]["score_label"] == "领先"
    assert rows[1]["score"] < 40
    assert rows[1]["score_label"] == "落后"
    assert rows[0]["yoy_ni_delta"] == 5.0
    assert rows[0]["fin_date"] == "2026-06-30"
    assert rows[0]["ret_20"] == pytest.approx(9.57, abs=0.01)
    assert rows[0]["pe_label"] == "低位"
    assert rows[1]["pe_label"] == "高位"


def test_company_scorecards_without_ticker_marks_gap(
    conn: sqlite3.Connection, valid_payload: dict[str, Any]
) -> None:
    payload = _payload_with_companies(valid_payload)
    payload["companies"][0]["ticker"] = ""
    config = ChainConfig.model_validate(payload)

    rows = company_scorecards(conn, config)

    assert rows[0]["ticker"] == ""
    assert rows[0]["score"] is None
    assert rows[0]["score_label"] == "数据不足"
    assert rows[0]["price"] is None


def test_company_scorecards_reads_latest_fin(
    conn: sqlite3.Connection, valid_payload: dict[str, Any]
) -> None:
    config = ChainConfig.model_validate(_payload_with_companies(valid_payload))
    _seed_fin(conn, "600001")
    upsert_metric(conn, "600001", "2026-10-20", "close", 120.0)

    rows = company_scorecards(conn, config)

    assert rows[0]["price"] == 120.0
    assert rows[0]["price_date"] == "2026-10-20"
    assert rows[1]["fin_date"] is None
