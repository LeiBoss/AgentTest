"""估值分位对照与增量采集。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from copy import deepcopy
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from src import collector
from src.collector import collect_valuation
from src.db import get_metric_series, init_db, upsert_metric
from src.models import ChainConfig
from src.valuation import VALUATION_MIN_SAMPLES, valuation_context, valuation_snapshot


@pytest.fixture()
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = init_db(str(tmp_path / "invest.db"))
    yield connection
    connection.close()


def _seed(conn: sqlite3.Connection, metric_id: str, values: list[float]) -> None:
    start = date(2025, 1, 1)
    for index, value in enumerate(values):
        day = start + timedelta(days=index)
        upsert_metric(conn, "600519", day.isoformat(), metric_id, value)


def test_snapshot_without_data(conn: sqlite3.Connection) -> None:
    rows = valuation_snapshot(conn, "600519")

    assert [item["id"] for item in rows] == ["pe_ttm", "pb_mrq", "ps_ttm", "pcf_ncf_ttm"]
    assert all(item["value"] is None for item in rows)
    assert all(item["value_text"] == "—" for item in rows)
    assert all(item["pct_label"] == "无数据" for item in rows)
    assert all(item["samples"] == 0 for item in rows)


def test_snapshot_needs_enough_samples(conn: sqlite3.Connection) -> None:
    _seed(conn, "pe_ttm", [float(i) for i in range(1, 20)])

    row = valuation_snapshot(conn, "600519")[0]

    assert row["samples"] == 19
    assert row["pct_rank"] is None
    assert row["pct_label"] == "样本不足"
    assert row["value_text"] == "19"


def test_snapshot_ranks_latest_value(conn: sqlite3.Connection) -> None:
    values = [float(i) for i in range(1, VALUATION_MIN_SAMPLES + 1)]
    values[-1] = 1000.0
    _seed(conn, "pb_mrq", values)

    row = valuation_snapshot(conn, "600519")[1]

    assert row["samples"] == VALUATION_MIN_SAMPLES
    assert row["pct_rank"] == 100
    assert row["pct_label"] == "高位"
    assert row["metric_date"] == (date(2025, 1, 1) + timedelta(days=VALUATION_MIN_SAMPLES - 1)).isoformat()


def test_context_links_company_and_profit_pool(
    valid_payload: dict[str, Any],
) -> None:
    payload = deepcopy(valid_payload)
    payload["companies"] = [
        {
            "name": "贵州茅台",
            "ticker": "600519.SH",
            "link": "高端酒企",
            "stage": "中游酒企",
            "role": "龙头",
        }
    ]
    config = ChainConfig.model_validate(payload)

    context = valuation_context(config, "600519")

    assert context["company"] == "贵州茅台"
    assert context["ticker"] == "600519.SH"
    assert context["link"] == "高端酒企"
    assert context["share_pct"] == 55.0
    assert context["stage"] == "中游酒企"
    assert context["role"] == "龙头"


def test_context_without_matching_company(valid_payload: dict[str, Any]) -> None:
    config = ChainConfig.model_validate(valid_payload)

    context = valuation_context(config, "000001.SZ")

    assert context["symbol"] == "000001.SZ"
    assert context["company"] is None
    assert context["link"] is None
    assert context["share_pct"] is None


def test_collect_valuation_full_history_range(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, str] = {}

    def fake(symbol: str, start_date: str, end_date: str) -> list[tuple[str, str, str, float]]:
        captured["start"] = start_date
        captured["end"] = end_date
        return [
            ("600519", "2026-10-20", "pe_ttm", 22.5),
            ("600519", "2026-10-20", "pb_mrq", 7.2),
        ]

    monkeypatch.setattr(collector, "_query_valuation_rows", fake)

    written = collect_valuation(conn, "600519")

    today = date.today()
    assert written == 2
    assert captured["end"] == today.isoformat()
    assert captured["start"] == (today - timedelta(days=365 * 5 + 30)).isoformat()
    assert get_metric_series(conn, "600519", "pe_ttm") == [(22.5, "2026-10-20")]


def test_collect_valuation_incremental_from_last_date(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    upsert_metric(conn, "600519", "2026-10-20", "pe_ttm", 22.5)
    captured: dict[str, str] = {}

    def fake(symbol: str, start_date: str, end_date: str) -> list[tuple[str, str, str, float]]:
        captured["start"] = start_date
        return []

    monkeypatch.setattr(collector, "_query_valuation_rows", fake)

    assert collect_valuation(conn, "600519") == 0
    assert captured["start"] == "2026-10-10"


def test_collect_valuation_ignores_bad_last_date(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO daily_metric (symbol, metric_date, metric_id, value)"
        " VALUES ('600519', 'bad-date', 'pe_ttm', 1.0)"
    )
    conn.commit()
    captured: dict[str, str] = {}

    def fake(symbol: str, start_date: str, end_date: str) -> list[tuple[str, str, str, float]]:
        captured["start"] = start_date
        return []

    monkeypatch.setattr(collector, "_query_valuation_rows", fake)

    collect_valuation(conn, "600519")

    today = date.today()
    assert captured["start"] == (today - timedelta(days=365 * 5 + 30)).isoformat()
