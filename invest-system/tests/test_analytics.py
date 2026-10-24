"""历史分位、连续异常期数与数据元信息。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from src.analytics import _abnormal_streak, _age_days, _pct_rank, indicator_stats
from src.db import init_db, upsert_metric
from src.models import ChainConfig, Trigger


@pytest.fixture()
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = init_db(str(tmp_path / "invest.db"))
    yield connection
    connection.close()


@pytest.fixture()
def t05(valid_payload: dict[str, Any]) -> Trigger:
    """T05：above，normal [0,800] / warn [800,1000] / crit [1000,1500]。"""
    return ChainConfig.model_validate(valid_payload).triggers[4]


def _series(values: list[float]) -> list[tuple[float, str]]:
    return [(value, f"2026-01-{index + 1:02d}") for index, value in enumerate(values)]


def test_pct_rank_needs_enough_samples() -> None:
    assert _pct_rank(_series([float(i) for i in range(1, 20)])) == (None, "样本不足")


def test_pct_rank_labels() -> None:
    values = [float(i) for i in range(1, 22)]
    assert _pct_rank(_series(values)) == (100, "高位")

    low = [float(i) for i in range(1, 21)] + [0.0]
    assert _pct_rank(_series(low)) == (0, "低位")

    mid = [float(i) for i in range(1, 21)] + [10.0]
    assert _pct_rank(_series(mid)) == (45, "中位")


def test_abnormal_streak_counts_backwards(t05: Trigger) -> None:
    assert _abnormal_streak(_series([400, 900, 1200]), t05) == 2
    assert _abnormal_streak(_series([400, 400, 1200]), t05) == 1
    assert _abnormal_streak(_series([400, 400, 400]), t05) == 0
    assert _abnormal_streak([], t05) == 0


def test_age_days_handles_bad_dates() -> None:
    assert _age_days(None) is None
    assert _age_days("not-a-date") is None
    assert _age_days("2026-13-01") is None
    assert _age_days(date.today().isoformat()) == 0
    older = (date.today() - timedelta(days=7)).isoformat()
    assert _age_days(older) == 7


def test_indicator_stats_combines_value_rank_and_meta(
    conn: sqlite3.Connection, t05: Trigger
) -> None:
    start = date.today() - timedelta(days=24)
    for offset in range(25):
        day = (start + timedelta(days=offset)).isoformat()
        upsert_metric(conn, "600519", day, t05.id, float(offset + 1))

    stats = indicator_stats(conn, "600519", t05)

    assert stats["value"] == 25.0
    assert stats["metric_date"] == date.today().isoformat()
    assert stats["samples"] == 25
    assert stats["pct_rank"] == 100
    assert stats["pct_label"] == "高位"
    assert stats["streak"] == 0
    assert stats["meta"]["source"] == t05.src
    assert stats["meta"]["freq"] == t05.freq
    assert stats["meta"]["lag_days"] == 0
    assert stats["meta"]["age_days"] == 0


def test_indicator_stats_without_data(conn: sqlite3.Connection, t05: Trigger) -> None:
    stats = indicator_stats(conn, "600519", t05)

    assert stats["value"] is None
    assert stats["metric_date"] is None
    assert stats["samples"] == 0
    assert stats["pct_rank"] is None
    assert stats["pct_label"] == "样本不足"
    assert stats["meta"]["age_days"] is None
