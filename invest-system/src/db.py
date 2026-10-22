"""SQLite 初始化。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

_DDL = """
CREATE TABLE IF NOT EXISTS metrics (
    chain_name  TEXT NOT NULL,
    trigger_id  TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    value       REAL NOT NULL,
    unit        TEXT,
    source      TEXT,
    PRIMARY KEY (chain_name, trigger_id, observed_at)
);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chain_name  TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    status      TEXT NOT NULL,
    message     TEXT
);

CREATE TABLE IF NOT EXISTS llm_outputs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER NOT NULL REFERENCES runs(id),
    content     TEXT NOT NULL,
    evidence    TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
"""


def init_db(path: str = "invest.db") -> sqlite3.Connection:
    """建库建表并返回连接。"""
    file = Path(path)
    if file.parent != Path(""):
        file.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(file)
    conn.executescript(_DDL)
    conn.commit()
    return conn
