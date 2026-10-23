"""SQLite 初始化与触发器状态读写。"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path

_DDL = """
CREATE TABLE IF NOT EXISTS daily_metric (
    symbol      TEXT NOT NULL,
    metric_date TEXT NOT NULL,
    metric_id   TEXT NOT NULL,
    value       REAL NOT NULL,
    PRIMARY KEY (symbol, metric_date, metric_id)
);

CREATE TABLE IF NOT EXISTS signal_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chain       TEXT NOT NULL,
    trigger_id  TEXT NOT NULL,
    from_status TEXT NOT NULL,
    to_status   TEXT NOT NULL,
    value       REAL NOT NULL,
    unit        TEXT NOT NULL,
    ts          TEXT NOT NULL,
    payload_json TEXT
);

CREATE TABLE IF NOT EXISTS judgment_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    chain        TEXT NOT NULL,
    rule_cond    TEXT NOT NULL,
    judgment     TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    ts           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS choke_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    chain        TEXT NOT NULL,
    node         TEXT NOT NULL,
    score        INTEGER NOT NULL,
    state        TEXT NOT NULL,
    paradox      INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    ts           TEXT NOT NULL
);
"""


def now_iso() -> str:
    """当前时间的 ISO 8601 秒级字符串。"""
    return datetime.now().isoformat(timespec="seconds")


def init_db(path: str | None = None) -> sqlite3.Connection:
    """建库建表并返回连接，可重复调用。"""
    file = Path(path or os.getenv("INVEST_DB", "invest.db"))
    if file.parent != Path(""):
        file.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(file, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    conn.executescript(_DDL)
    conn.commit()
    return conn


def get_latest_metric(
    conn: sqlite3.Connection, symbol: str, metric_id: str
) -> tuple[float, str] | None:
    """返回该指标最新一期 (value, metric_date)，无数据返回 None。"""
    row = conn.execute(
        "SELECT value, metric_date FROM daily_metric"
        " WHERE symbol = ? AND metric_id = ?"
        " ORDER BY metric_date DESC LIMIT 1",
        (symbol, metric_id),
    ).fetchone()
    if row is None:
        return None
    return float(row["value"]), str(row["metric_date"])


def get_metric_history(
    conn: sqlite3.Connection, symbol: str, metric_id: str, limit: int = 8
) -> list[tuple[float, str]]:
    """按日期倒序返回最近 limit 期 (value, metric_date)。"""
    rows = conn.execute(
        "SELECT value, metric_date FROM daily_metric"
        " WHERE symbol = ? AND metric_id = ?"
        " ORDER BY metric_date DESC LIMIT ?",
        (symbol, metric_id, limit),
    ).fetchall()
    return [(float(row["value"]), str(row["metric_date"])) for row in rows]


def upsert_metric(
    conn: sqlite3.Connection,
    symbol: str,
    metric_date: str,
    metric_id: str,
    value: float,
) -> None:
    """写入或覆盖一条日频指标。"""
    conn.execute(
        "INSERT OR REPLACE INTO daily_metric (symbol, metric_date, metric_id, value)"
        " VALUES (?, ?, ?, ?)",
        (symbol, metric_date, metric_id, value),
    )
    conn.commit()


def insert_signal(
    conn: sqlite3.Connection,
    chain: str,
    trigger_id: str,
    from_status: str,
    to_status: str,
    value: float,
    unit: str,
    ts: str,
) -> int:
    """记录一次状态变化并返回记录 id。"""
    cursor = conn.execute(
        "INSERT INTO signal_log"
        " (chain, trigger_id, from_status, to_status, value, unit, ts)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (chain, trigger_id, from_status, to_status, value, unit, ts),
    )
    conn.commit()
    return int(cursor.lastrowid)


def set_signal_payload(
    conn: sqlite3.Connection, signal_id: int, payload: dict
) -> None:
    """将 LLM 解读结果挂到对应状态迁移记录上。"""
    conn.execute(
        "UPDATE signal_log SET payload_json = ? WHERE id = ?",
        (json.dumps(payload, ensure_ascii=False), signal_id),
    )
    conn.commit()


def insert_judgment(
    conn: sqlite3.Connection,
    chain: str,
    rule_cond: str,
    judgment: str,
    payload: dict,
    ts: str,
) -> None:
    """记录一条规则判定结果。"""
    conn.execute(
        "INSERT INTO judgment_log (chain, rule_cond, judgment, payload_json, ts)"
        " VALUES (?, ?, ?, ?, ?)",
        (chain, rule_cond, judgment, json.dumps(payload, ensure_ascii=False), ts),
    )
    conn.commit()


def insert_choke(
    conn: sqlite3.Connection,
    chain: str,
    node: str,
    score: int,
    state: str,
    paradox: bool,
    payload: dict,
    ts: str,
) -> None:
    """记录一次卡点评分快照。"""
    conn.execute(
        "INSERT INTO choke_log"
        " (chain, node, score, state, paradox, payload_json, ts)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (chain, node, score, state, int(paradox), json.dumps(payload, ensure_ascii=False), ts),
    )
    conn.commit()


def get_last_status(conn: sqlite3.Connection, chain: str, trigger_id: str) -> str | None:
    """返回该触发器最近一次 to_status，无记录返回 None。"""
    row = conn.execute(
        "SELECT to_status FROM signal_log"
        " WHERE chain = ? AND trigger_id = ?"
        " ORDER BY id DESC LIMIT 1",
        (chain, trigger_id),
    ).fetchone()
    if row is None:
        return None
    return str(row["to_status"])
