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

CREATE TABLE IF NOT EXISTS report_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    chain        TEXT NOT NULL,
    ts           TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS company_fin (
    symbol             TEXT NOT NULL,
    stat_date          TEXT NOT NULL,
    pub_date           TEXT NOT NULL,
    yoy_ni             REAL,
    yoy_eps            REAL,
    gp_margin          REAL,
    np_margin          REAL,
    roe                REAL,
    cfo_to_np          REAL,
    liability_to_asset REAL,
    asset_turn         REAL,
    PRIMARY KEY (symbol, stat_date)
);

CREATE TABLE IF NOT EXISTS thesis_log (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    chain                TEXT NOT NULL,
    title                TEXT NOT NULL,
    direction            TEXT NOT NULL,
    horizon              TEXT NOT NULL DEFAULT '',
    core_logic           TEXT NOT NULL DEFAULT '',
    confirm_condition    TEXT NOT NULL DEFAULT '',
    invalidate_condition TEXT NOT NULL DEFAULT '',
    confidence           INTEGER NOT NULL,
    status               TEXT NOT NULL,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS catalyst_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chain       TEXT NOT NULL,
    event_date  TEXT NOT NULL,
    title       TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT '',
    related     TEXT NOT NULL DEFAULT '',
    expectation TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
"""

COMPANY_FIN_FIELDS = (
    "yoy_ni",
    "yoy_eps",
    "gp_margin",
    "np_margin",
    "roe",
    "cfo_to_np",
    "liability_to_asset",
    "asset_turn",
)


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


def get_metric_series(
    conn: sqlite3.Connection, symbol: str, metric_id: str, limit: int = 250
) -> list[tuple[float, str]]:
    """按日期升序返回最近 limit 期 (value, metric_date)，用于分位计算。"""
    rows = conn.execute(
        "SELECT value, metric_date FROM daily_metric"
        " WHERE symbol = ? AND metric_id = ?"
        " ORDER BY metric_date DESC LIMIT ?",
        (symbol, metric_id, limit),
    ).fetchall()
    return [(float(row["value"]), str(row["metric_date"])) for row in reversed(rows)]


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


def upsert_company_fin(
    conn: sqlite3.Connection,
    symbol: str,
    stat_date: str,
    pub_date: str,
    values: dict[str, float | None],
) -> None:
    """写入或覆盖一家公司某个报告期的财务指标。"""
    placeholders = ", ".join("?" for _ in COMPANY_FIN_FIELDS)
    conn.execute(
        "INSERT OR REPLACE INTO company_fin"
        f" (symbol, stat_date, pub_date, {', '.join(COMPANY_FIN_FIELDS)})"
        f" VALUES (?, ?, ?, {placeholders})",
        (
            symbol,
            stat_date,
            pub_date,
            *(values.get(field) for field in COMPANY_FIN_FIELDS),
        ),
    )
    conn.commit()


def get_company_fin_history(
    conn: sqlite3.Connection, symbol: str, limit: int = 2
) -> list[dict]:
    """按报告期倒序返回最近 limit 期财务指标。"""
    rows = conn.execute(
        "SELECT * FROM company_fin WHERE symbol = ?"
        " ORDER BY stat_date DESC LIMIT ?",
        (symbol, limit),
    ).fetchall()
    return [dict(row) for row in rows]


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


def insert_report(
    conn: sqlite3.Connection, chain: str, payload: dict, ts: str
) -> int:
    """保存一份研报快照并返回记录 id。"""
    cursor = conn.execute(
        "INSERT INTO report_log (chain, ts, payload_json) VALUES (?, ?, ?)",
        (chain, ts, json.dumps(payload, ensure_ascii=False)),
    )
    conn.commit()
    return int(cursor.lastrowid)


def get_latest_report(conn: sqlite3.Connection, chain: str) -> dict | None:
    """返回该链最近一份研报，没有则返回 None。"""
    row = conn.execute(
        "SELECT ts, payload_json FROM report_log"
        " WHERE chain = ? ORDER BY id DESC LIMIT 1",
        (chain,),
    ).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(row["payload_json"])
    except json.JSONDecodeError:
        return None
    payload["generated_at"] = row["ts"]
    return payload


def _thesis_row(conn: sqlite3.Connection, thesis_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM thesis_log WHERE id = ?", (thesis_id,)).fetchone()
    return dict(row) if row is not None else None


def insert_thesis(conn: sqlite3.Connection, chain: str, data: dict, ts: str | None = None) -> dict:
    """新增一条 Thesis，初始状态 active。"""
    now = ts or now_iso()
    cursor = conn.execute(
        "INSERT INTO thesis_log"
        " (chain, title, direction, horizon, core_logic, confirm_condition,"
        " invalidate_condition, confidence, status, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            chain,
            data["title"],
            data["direction"],
            data.get("horizon", ""),
            data.get("core_logic", ""),
            data.get("confirm_condition", ""),
            data.get("invalidate_condition", ""),
            data["confidence"],
            "active",
            now,
            now,
        ),
    )
    conn.commit()
    row = _thesis_row(conn, int(cursor.lastrowid))
    assert row is not None
    return row


def get_theses(conn: sqlite3.Connection, chain: str) -> list[dict]:
    """跟踪中的 Thesis 在前，其余按更新时间倒序。"""
    rows = conn.execute(
        "SELECT * FROM thesis_log WHERE chain = ?"
        " ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END, id DESC",
        (chain,),
    ).fetchall()
    return [dict(row) for row in rows]


def update_thesis_status(
    conn: sqlite3.Connection, chain: str, thesis_id: int, status: str, ts: str | None = None
) -> dict | None:
    """更新 Thesis 状态，返回更新后的记录；不存在返回 None。"""
    now = ts or now_iso()
    cursor = conn.execute(
        "UPDATE thesis_log SET status = ?, updated_at = ? WHERE id = ? AND chain = ?",
        (status, now, thesis_id, chain),
    )
    conn.commit()
    if cursor.rowcount == 0:
        return None
    return _thesis_row(conn, thesis_id)


def _catalyst_row(conn: sqlite3.Connection, catalyst_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM catalyst_log WHERE id = ?", (catalyst_id,)
    ).fetchone()
    return dict(row) if row is not None else None


def insert_catalyst(
    conn: sqlite3.Connection, chain: str, data: dict, ts: str | None = None
) -> dict:
    """新增一条催化剂，初始状态 pending。"""
    now = ts or now_iso()
    cursor = conn.execute(
        "INSERT INTO catalyst_log"
        " (chain, event_date, title, kind, related, expectation, status, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            chain,
            data["event_date"],
            data["title"],
            data.get("kind", ""),
            data.get("related", ""),
            data.get("expectation", ""),
            "pending",
            now,
        ),
    )
    conn.commit()
    row = _catalyst_row(conn, int(cursor.lastrowid))
    assert row is not None
    return row


def get_catalysts(conn: sqlite3.Connection, chain: str, limit: int = 100) -> list[dict]:
    """按事件日期升序返回催化剂。"""
    rows = conn.execute(
        "SELECT * FROM catalyst_log WHERE chain = ? ORDER BY event_date ASC, id ASC LIMIT ?",
        (chain, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def update_catalyst_status(
    conn: sqlite3.Connection, chain: str, catalyst_id: int, status: str
) -> dict | None:
    """更新催化剂状态，返回更新后的记录；不存在返回 None。"""
    cursor = conn.execute(
        "UPDATE catalyst_log SET status = ? WHERE id = ? AND chain = ?",
        (status, catalyst_id, chain),
    )
    conn.commit()
    if cursor.rowcount == 0:
        return None
    return _catalyst_row(conn, catalyst_id)
