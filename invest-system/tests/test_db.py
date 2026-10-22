"""db.py 的读写往返测试。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest

from src.db import (
    get_last_status,
    get_latest_metric,
    get_metric_history,
    init_db,
    insert_choke,
    insert_judgment,
    insert_signal,
    now_iso,
    upsert_metric,
)


@pytest.fixture()
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = init_db(str(tmp_path / "invest.db"))
    yield connection
    connection.close()


def test_init_db_is_idempotent(tmp_path: Path) -> None:
    path = str(tmp_path / "invest.db")
    init_db(path).close()
    conn = init_db(path)
    tables = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert {"daily_metric", "signal_log", "judgment_log", "choke_log"} <= tables
    assert conn.row_factory is sqlite3.Row
    conn.close()


def test_upsert_and_read_metric(conn: sqlite3.Connection) -> None:
    upsert_metric(conn, "600519", "2026-01-02", "wholesale_price", 2300.0)
    upsert_metric(conn, "600519", "2026-01-09", "wholesale_price", 2280.0)

    assert get_latest_metric(conn, "600519", "wholesale_price") == (2280.0, "2026-01-09")
    assert get_metric_history(conn, "600519", "wholesale_price") == [
        (2280.0, "2026-01-09"),
        (2300.0, "2026-01-02"),
    ]
    assert get_latest_metric(conn, "600519", "missing") is None
    assert get_metric_history(conn, "600519", "missing") == []


def test_upsert_same_key_overwrites(conn: sqlite3.Connection) -> None:
    upsert_metric(conn, "600519", "2026-01-09", "wholesale_price", 2280.0)
    upsert_metric(conn, "600519", "2026-01-09", "wholesale_price", 2250.0)

    count = conn.execute("SELECT COUNT(*) AS n FROM daily_metric").fetchone()["n"]
    assert count == 1
    assert get_latest_metric(conn, "600519", "wholesale_price") == (2250.0, "2026-01-09")


def test_history_respects_limit(conn: sqlite3.Connection) -> None:
    for day in range(1, 11):
        upsert_metric(conn, "600519", f"2026-01-{day:02d}", "wholesale_price", 2000.0 + day)
    history = get_metric_history(conn, "600519", "wholesale_price", limit=3)
    assert [date for _, date in history] == ["2026-01-10", "2026-01-09", "2026-01-08"]


def test_signal_and_last_status(conn: sqlite3.Connection) -> None:
    assert get_last_status(conn, "白酒", "wholesale_price") is None

    ts = now_iso()
    insert_signal(conn, "白酒", "wholesale_price", "normal", "warn", 2280.0, "元/瓶", ts)
    insert_signal(conn, "白酒", "wholesale_price", "warn", "crit", 2100.0, "元/瓶", ts)

    assert get_last_status(conn, "白酒", "wholesale_price") == "crit"
    assert get_last_status(conn, "白酒", "channel_inv") is None
    row = conn.execute("SELECT * FROM signal_log ORDER BY id LIMIT 1").fetchone()
    assert row["from_status"] == "normal"
    assert row["value"] == pytest.approx(2280.0)


def test_insert_judgment(conn: sqlite3.Connection) -> None:
    payload = {"wholesale_price": 2100.0, "channel_inv": 2.8}
    insert_judgment(
        conn, "白酒", "wholesale_price < 2200 AND channel_inv > 2.5", "被动去库存", payload, now_iso()
    )
    row = conn.execute("SELECT * FROM judgment_log").fetchone()
    assert row["judgment"] == "被动去库存"
    assert json.loads(row["payload_json"]) == payload


def test_insert_choke(conn: sqlite3.Connection) -> None:
    payload = {"node": "赤水河产区基酒储能", "paradox": True}
    insert_choke(conn, "白酒", "赤水河产区基酒储能", 8, "tight", True, payload, now_iso())
    row = conn.execute("SELECT * FROM choke_log").fetchone()
    assert row["score"] == 8
    assert row["state"] == "tight"
    assert row["paradox"] == 1
    assert json.loads(row["payload_json"]) == payload


def test_now_iso_is_second_precision() -> None:
    parsed = datetime.fromisoformat(now_iso())
    assert parsed.microsecond == 0
