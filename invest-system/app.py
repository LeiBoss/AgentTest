"""产业链跟踪 Web API 与页面。"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from src.auth import (
    SESSION_COOKIE,
    SESSION_TTL,
    auth_enabled,
    check_credentials,
    make_token,
    verify_token,
)
from src.chain_builder import ChainBuildError, ChainExistsError, create_chain
from src.config_loader import load_chain_config
from src.db import (
    get_last_status,
    get_latest_report,
    get_metric_history,
    init_db,
    insert_report,
    now_iso,
)
from src.models import ChainConfig
from src.report import ReportError, generate_report

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
CONFIG_DIR = BASE_DIR / "config" / "chains"
STATIC_DIR = BASE_DIR / "static"
DB_PATH = Path(os.getenv("INVEST_DB", str(BASE_DIR / "invest.db")))
SYMBOL = os.getenv("SYMBOL", "600519")

STATUS_LABELS = {"normal": "正常", "watch": "关注", "alert": "触发"}
CHOKE_TYPE_LABELS = {"ecosystem": "生态位", "resource": "资源", "crowded": "拥挤"}
CHOKE_STATE_LABELS = {
    "tight": "收紧",
    "balanced": "平衡",
    "stable": "稳定",
    "loosening": "放松",
}

app = FastAPI(title="产业链跟踪", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

_PUBLIC_PATHS = frozenset({"/login", "/api/login", "/api/health", "/favicon.ico"})
_PUBLIC_PREFIXES = ("/static/",)


def _is_public(path: str) -> bool:
    return path in _PUBLIC_PATHS or path.startswith(_PUBLIC_PREFIXES)


@app.middleware("http")
async def _require_login(request: Request, call_next):
    """未登录时页面跳转登录页，接口返回 401。"""
    if not auth_enabled() or _is_public(request.url.path):
        return await call_next(request)
    if verify_token(request.cookies.get(SESSION_COOKIE)):
        return await call_next(request)
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "未登录"}, status_code=401)
    target = request.url.path
    if request.url.query:
        target += "?" + request.url.query
    return RedirectResponse(url="/login?next=" + quote(target, safe=""), status_code=302)


@app.on_event("startup")
def _ensure_schema() -> None:
    """建表，确保 Web 先于采集任务启动时也能正常查询。"""
    init_db(str(DB_PATH)).close()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _chain_paths() -> dict[str, Path]:
    """按配置内的 chain_name 建立索引。"""
    paths: dict[str, Path] = {}
    for path in sorted(CONFIG_DIR.glob("*.yaml")):
        try:
            paths[load_chain_config(str(path)).chain_name] = path
        except Exception as exc:  # noqa: BLE001 - 单个坏配置不影响其他链
            logger.warning("[app] skip config %s: %s", path.name, exc)
    return paths


def _load_chain(name: str) -> ChainConfig:
    path = _chain_paths().get(name)
    if path is None:
        raise HTTPException(status_code=404, detail=f"未知产业链: {name}")
    return load_chain_config(str(path))


def _status(conn: sqlite3.Connection, chain: str, trigger_id: str) -> str:
    return get_last_status(conn, chain, trigger_id) or "normal"


def _fmt_value(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _change_text(history: list[tuple[float, str]]) -> str:
    if len(history) < 2:
        return ""
    delta = history[0][0] - history[1][0]
    return f"较上期 {delta:+.2f}"


def _latest_signal_ts(conn: sqlite3.Connection, chain: str) -> str | None:
    row = conn.execute(
        "SELECT MAX(ts) AS ts FROM signal_log WHERE chain = ?", (chain,)
    ).fetchone()
    return row["ts"] if row is not None else None


def _trigger_payload(conn: sqlite3.Connection, config: ChainConfig) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    counts = {"normal": 0, "watch": 0, "alert": 0}
    for trigger in config.triggers:
        history = get_metric_history(conn, SYMBOL, trigger.id, limit=2)
        value = history[0][0] if history else None
        status = _status(conn, config.chain_name, trigger.id)
        if status not in counts:
            status = "normal"
        counts[status] += 1
        items.append(
            {
                "id": trigger.id,
                "name": trigger.name,
                "layer": trigger.layer,
                "value": value,
                "value_text": _fmt_value(value),
                "unit": trigger.unit,
                "status": status,
                "status_label": STATUS_LABELS[status],
                "change_text": _change_text(history),
                "metric_date": history[0][1] if history else None,
            }
        )
    return {
        "chain": config.chain_name,
        "symbol": SYMBOL,
        "stats": {**counts, "latest_signal_ts": _latest_signal_ts(conn, config.chain_name)},
        "triggers": items,
    }


def _signal_payload(conn: sqlite3.Connection, config: ChainConfig, limit: int) -> list[dict[str, Any]]:
    names = {trigger.id: trigger.name for trigger in config.triggers}
    rows = conn.execute(
        """
        SELECT id, trigger_id, from_status, to_status, value, unit, ts, payload_json
        FROM signal_log WHERE chain = ? ORDER BY id DESC LIMIT ?
        """,
        (config.chain_name, limit),
    ).fetchall()
    signals: list[dict[str, Any]] = []
    for row in rows:
        interpretation = ""
        if row["payload_json"]:
            try:
                interpretation = str(json.loads(row["payload_json"]).get("interpretation", ""))
            except (json.JSONDecodeError, AttributeError):
                interpretation = ""
        signals.append(
            {
                "id": row["id"],
                "trigger_id": row["trigger_id"],
                "name": names.get(row["trigger_id"], row["trigger_id"]),
                "from_status": row["from_status"],
                "to_status": row["to_status"],
                "from_label": STATUS_LABELS.get(row["from_status"], row["from_status"]),
                "to_label": STATUS_LABELS.get(row["to_status"], row["to_status"]),
                "value": row["value"],
                "value_text": _fmt_value(row["value"]),
                "unit": row["unit"],
                "interpretation": interpretation,
                "ts": row["ts"],
            }
        )
    return signals


def _judgment_payload(conn: sqlite3.Connection, chain: str, limit: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, rule_cond, judgment, ts FROM judgment_log
        WHERE chain = ? ORDER BY id DESC LIMIT ?
        """,
        (chain, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def _choke_payload(conn: sqlite3.Connection, config: ChainConfig) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT node, state, paradox, score FROM choke_log
        WHERE chain = ? ORDER BY id DESC
        """,
        (config.chain_name,),
    ).fetchall()
    latest: dict[str, sqlite3.Row] = {}
    for row in rows:
        latest.setdefault(row["node"], row)

    chokes: list[dict[str, Any]] = []
    for point in config.choke_points:
        log = latest.get(point.node)
        state = log["state"] if log is not None else point.current_state
        paradox = bool(log["paradox"]) if log is not None else point.paradox_flag
        chokes.append(
            {
                "node": point.node,
                "type": point.type,
                "type_label": CHOKE_TYPE_LABELS.get(point.type, point.type),
                "score": point.score.total,
                "level": point.level,
                "state": state,
                "state_label": CHOKE_STATE_LABELS.get(state, state),
                "profit_share_pct": point.profit_share_pct,
                "profit_share_text": f"{point.profit_share_pct:g}%",
                "paradox": paradox,
                "tracking_triggers": point.tracking_triggers,
            }
        )
    return chokes


def _health_payload() -> dict[str, Any]:
    sources: list[dict[str, Any]] = []
    try:
        with closing(_connect()) as conn:
            conn.execute("SELECT 1").fetchone()
        sqlite_ok = True
    except sqlite3.Error:
        sqlite_ok = False
    sources.append({"name": "sqlite", "ok": sqlite_ok})
    for name in ("akshare", "adata"):
        sources.append({"name": name, "ok": importlib.util.find_spec(name) is not None})
    return {"status": "ok" if sqlite_ok else "degraded", "sources": sources}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/login")
def login_page(request: Request):
    if auth_enabled() and verify_token(request.cookies.get(SESSION_COOKIE)):
        return RedirectResponse(url="/", status_code=302)
    return FileResponse(STATIC_DIR / "login.html")


@app.post("/api/login")
async def login(request: Request) -> JSONResponse:
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001 - 非 JSON 输入统一按空处理
        data = {}
    if not isinstance(data, dict):
        data = {}
    user = str(data.get("username", ""))
    password = str(data.get("password", ""))
    if not check_credentials(user, password):
        raise HTTPException(status_code=401, detail="账号或密码错误")
    response = JSONResponse({"ok": True, "user": user})
    response.set_cookie(
        SESSION_COOKIE,
        make_token(user),
        max_age=SESSION_TTL,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return response


@app.post("/api/logout")
def logout() -> JSONResponse:
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@app.get("/api/health")
def health() -> dict[str, Any]:
    return _health_payload()


def _chain_item(name: str, path: Path, config: ChainConfig, conn: sqlite3.Connection) -> dict[str, Any]:
    ids = [trigger.id for trigger in config.triggers]
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT metric_id, MAX(metric_date) AS d FROM daily_metric"
        f" WHERE symbol = ? AND metric_id IN ({placeholders}) GROUP BY metric_id",
        (SYMBOL, *ids),
    ).fetchall()
    dates = [row["d"] for row in rows if row["d"]]
    signal = conn.execute(
        "SELECT MAX(ts) AS ts FROM signal_log WHERE chain = ?", (name,)
    ).fetchone()
    report = conn.execute(
        "SELECT MAX(ts) AS ts FROM report_log WHERE chain = ?", (name,)
    ).fetchone()
    return {
        "name": name,
        "file": path.name,
        "trigger_count": len(config.triggers),
        "layer_count": len({trigger.layer for trigger in config.triggers}),
        "rule_count": len(config.rules),
        "choke_count": len(config.choke_points),
        "data_count": len(rows),
        "data_date": max(dates) if dates else None,
        "latest_signal_ts": signal["ts"] if signal is not None else None,
        "report_ts": report["ts"] if report is not None else None,
    }


@app.get("/api/chains")
def chains() -> dict[str, Any]:
    paths = _chain_paths()
    items: list[dict[str, Any]] = []
    with closing(_connect()) as conn:
        for name, path in sorted(paths.items()):
            try:
                config = load_chain_config(str(path))
                items.append(_chain_item(name, path, config, conn))
            except Exception as exc:  # noqa: BLE001 - 单条链异常不影响整个列表
                logger.warning("[app] chain item %s failed: %s", name, exc)
    return {"chains": sorted(paths), "items": items}


@app.post("/api/chains")
def add_chain(payload: dict[str, Any]) -> dict[str, Any]:
    name = str((payload or {}).get("name", "")).strip()
    try:
        config, path = create_chain(name, CONFIG_DIR)
    except ChainExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ChainBuildError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    logger.info("[app] 生成产业链 %s -> %s", config.chain_name, path.name)
    return {
        "chain": config.chain_name,
        "file": path.name,
        "trigger_count": len(config.triggers),
        "rule_count": len(config.rules),
        "choke_count": len(config.choke_points),
    }


@app.get("/report")
def report_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "report.html")


@app.get("/api/report/{name}")
def get_report(name: str) -> dict[str, Any]:
    config = _load_chain(name)
    with closing(_connect()) as conn:
        payload = get_latest_report(conn, config.chain_name)
    if payload is None:
        raise HTTPException(status_code=404, detail="暂无研报，请先生成")
    return {"report": payload}


@app.post("/api/report/{name}")
def create_report(name: str) -> dict[str, Any]:
    config = _load_chain(name)
    with closing(_connect()) as conn:
        try:
            payload = generate_report(config, conn, SYMBOL)
        except ReportError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        insert_report(conn, config.chain_name, payload, payload["generated_at"])
    return {"report": payload}


@app.get("/api/chain/{name}/triggers")
def chain_triggers(name: str) -> dict[str, Any]:
    config = _load_chain(name)
    with closing(_connect()) as conn:
        return _trigger_payload(conn, config)


@app.get("/api/chain/{name}/signals")
def chain_signals(name: str, limit: int = 10) -> dict[str, Any]:
    config = _load_chain(name)
    limit = max(1, min(limit, 50))
    with closing(_connect()) as conn:
        return {"signals": _signal_payload(conn, config, limit)}


@app.get("/api/chain/{name}/judgments")
def chain_judgments(name: str, limit: int = 5) -> dict[str, Any]:
    config = _load_chain(name)
    limit = max(1, min(limit, 50))
    with closing(_connect()) as conn:
        return {"judgments": _judgment_payload(conn, config.chain_name, limit)}


@app.get("/api/chain/{name}/chokes")
def chain_chokes(name: str) -> dict[str, Any]:
    config = _load_chain(name)
    with closing(_connect()) as conn:
        return {"chokes": _choke_payload(conn, config)}
