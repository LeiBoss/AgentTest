"""按配置从 AkShare 拉取指标并落库。"""

from __future__ import annotations

import atexit
import logging
import random
import sqlite3
import time
from collections.abc import Callable
from typing import Any

import akshare as ak
import baostock as bs
import pandas as pd
from tenacity import retry, stop_after_attempt, wait_exponential

from .db import get_latest_metric, upsert_metric
from .models import ChainConfig, Trigger

logger = logging.getLogger(__name__)

HISTORY_LIMIT = 8
VALUATION_YEARS = 5
_VALUATION_FIELDS = {
    "peTTM": "pe_ttm",
    "pbMRQ": "pb_mrq",
    "psTTM": "ps_ttm",
    "pcfNcfTTM": "pcf_ncf_ttm",
}
_VALUATION_IDS = tuple(_VALUATION_FIELDS.values())
_LOCAL_SOURCES = frozenset({"渠道调研", "研报"})

_BS_LOGGED_IN = False

_CIRCUIT: dict[str, dict] = {}
_FAILURE_THRESHOLD = 3
_COOLDOWN_SECONDS = 1800


def _ensure_baostock_login() -> None:
    """进程级单例登录，避免重复 login 导致连接租约冲突。"""
    global _BS_LOGGED_IN
    if _BS_LOGGED_IN:
        return
    lg = bs.login()
    if lg is None or lg.error_code != "0":
        raise ConnectionError(
            f"Baostock 登录失败: {getattr(lg, 'error_msg', 'None')}"
        )
    _BS_LOGGED_IN = True


def _baostock_logout() -> None:
    global _BS_LOGGED_IN
    if _BS_LOGGED_IN:
        try:
            bs.logout()
        except Exception:
            pass
        _BS_LOGGED_IN = False


atexit.register(_baostock_logout)


def _to_bs_code(symbol: str) -> str:
    """6 位 A 股代码转 Baostock 格式。"""
    if symbol.startswith(("sh.", "sz.")):
        return symbol
    if symbol.startswith(("600", "601", "603", "605", "688")):
        return f"sh.{symbol}"
    return f"sz.{symbol}"


def _baostock_profit_history(
    symbol: str, quarters: int = HISTORY_LIMIT
) -> list[tuple[str, float]]:
    """拉取最近 N 个季度毛利率（百分数），复用单例会话，失败指数退避重试。"""
    from datetime import date
    from tenacity import retry, stop_after_attempt, wait_exponential

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=5, max=30),
        reraise=True,
    )
    def _do_query() -> list[tuple[str, float]]:
        _ensure_baostock_login()
        today = date.today()
        y, q = today.year, (today.month - 1) // 3 + 1
        results: list[tuple[str, float]] = []
        for _ in range(quarters + 4):
            rs = bs.query_profit_data(
                code=_to_bs_code(symbol), year=y, quarter=q
            )
            if rs.error_code == "0":
                while rs.next():
                    row = dict(zip(rs.fields, rs.get_row_data()))
                    stat_date = row.get("statDate", "")
                    gp = row.get("gpMargin", "")
                    if stat_date and gp not in ("", None):
                        try:
                            results.append((stat_date, float(gp) * 100))
                        except (ValueError, TypeError):
                            pass
            q -= 1
            if q == 0:
                q, y = 4, y - 1
            if len(results) >= quarters:
                break
        results.sort(key=lambda item: item[0])
        return results[-quarters:]

    return _do_query()


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    reraise=True,
)
def fetch_with_retry(func: Callable[..., pd.DataFrame], **kwargs: Any) -> pd.DataFrame:
    """限速调用 AkShare，空结果转 ConnectionError 触发重试。"""
    time.sleep(random.uniform(1.5, 3.0))
    frame = func(**kwargs)
    if frame is None or (isinstance(frame, pd.DataFrame) and frame.empty):
        raise ConnectionError(f"AkShare 返回空数据: {getattr(func, '__name__', func)}")
    return frame


def _circuit_open(name: str) -> bool:
    """判断某数据源是否处于熔断冷却期。"""
    state = _CIRCUIT.get(name)
    if not state:
        return False
    if time.time() >= state.get("open_until", 0):
        return False
    return True


def _record_source(name: str, ok: bool) -> None:
    """记录一次调用结果，连续失败达到阈值则开启熔断。"""
    state = _CIRCUIT.setdefault(name, {"failures": 0, "open_until": 0})
    if ok:
        state["failures"] = 0
        state["open_until"] = 0
        return
    state["failures"] += 1
    if state["failures"] >= _FAILURE_THRESHOLD:
        state["open_until"] = time.time() + _COOLDOWN_SECONDS
        state["failures"] = 0


def _try_sources(
    sources: list[tuple[str, Callable[[Trigger, str], pd.DataFrame]]],
    trigger: Trigger,
    symbol: str,
) -> pd.DataFrame:
    """按优先级依次尝试候选源，全部失败抛 ConnectionError。"""
    errors: list[str] = []
    for name, func in sources:
        if _circuit_open(name):
            errors.append(f"{name}: circuit_open")
            continue
        try:
            frame = func(trigger, symbol)
            _record_source(name, True)
            _log("source", name, "ok")
            return frame
        except Exception as exc:
            _record_source(name, False)
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
            _log("source", name, f"fail: {exc}")
    raise ConnectionError("; ".join(errors))


def _cl_from_akshare(trigger: Trigger, symbol: str) -> pd.DataFrame:
    """AKShare 资产负债表：合同负债同比。"""
    raw = fetch_with_retry(
        ak.stock_balance_sheet_by_report_em, symbol=f"SH{symbol}"
    )
    return pd.DataFrame(
        {
            "date": raw["REPORT_DATE"].astype(str).str[:10],
            "value": pd.to_numeric(
                raw["CONTRACT_LIAB_YOY"], errors="coerce"
            ),
        }
    ).dropna(subset=["value"])


def _fetch_contract_liab(trigger: Trigger, symbol: str) -> pd.DataFrame:
    sources = [
        ("akshare_cl", _cl_from_akshare),
        # 预留：Tushare 接入后在此追加 ("tushare_cl", _cl_from_tushare)
    ]
    return _try_sources(sources, trigger, symbol)


def _gm_from_adata(trigger: Trigger, symbol: str) -> pd.DataFrame:
    """adata 核心财务：毛利率（已是百分数）。"""
    import adata
    raw = adata.stock.finance.get_core_index(stock_code=symbol)
    if raw is None or raw.empty:
        raise ValueError("adata 毛利率无数据")
    df = pd.DataFrame(
        {
            "date": raw["report_date"].astype(str).str[:10],
            "value": pd.to_numeric(raw["gross_margin"], errors="coerce"),
        }
    ).dropna(subset=["value"])
    return df.sort_values("date").tail(HISTORY_LIMIT)


def _gm_from_akshare(trigger: Trigger, symbol: str) -> pd.DataFrame:
    """AKShare 利润表：毛利率（百分数）。字段名以实际验证结果为准。"""
    raw = fetch_with_retry(
        ak.stock_profit_sheet_by_report_em, symbol=f"SH{symbol}"
    )
    # 注意：以下列名需根据第 0 步验证结果调整
    # 可能是 GROSS_PROFIT_MARGIN、GROSS_PROFIT_RATIO 或其他
    raise NotImplementedError("待第 0 步验证后填入实际列名")


def _fetch_gross_margin(trigger: Trigger, symbol: str) -> pd.DataFrame:
    sources = [
        ("adata_gm", _gm_from_adata),
        # ("akshare_gm", _gm_from_akshare),  # 第 0 步验证通过后取消注释
    ]
    return _try_sources(sources, trigger, symbol)


class UnsupportedMetricError(NotImplementedError):
    """数据源已接入，但没有该指标的取数逻辑。"""


def _fetch_financial(trigger: Trigger, symbol: str) -> pd.DataFrame:
    if trigger.id == "contract_liab":
        return _fetch_contract_liab(trigger, symbol)
    if trigger.id == "gross_margin":
        return _fetch_gross_margin(trigger, symbol)
    raise UnsupportedMetricError(f"未支持的财报指标: {trigger.id}")


def _fetch_cpca(trigger: Trigger, symbol: str) -> pd.DataFrame:
    return fetch_with_retry(ak.car_market_total_cpca)


def _fetch_nbs(trigger: Trigger, symbol: str) -> pd.DataFrame:
    return fetch_with_retry(ak.macro_china_cpi_monthly)


_FETCHERS: dict[str, Callable[[Trigger, str], pd.DataFrame]] = {
    "财报": _fetch_financial,
    "乘联会": _fetch_cpca,
    "统计局": _fetch_nbs,
}


def _log(chain: str, trigger_id: str, status: str) -> None:
    logger.info("[collector] %s %s -> %s", chain, trigger_id, status)


def _standardize(frame: pd.DataFrame, trigger: Trigger) -> list[tuple[str, float]]:
    """把不同接口返回的表格统一成按日期升序的 (date, value) 序列。"""
    if not isinstance(frame, pd.DataFrame):
        raise ValueError(f"返回类型不是 DataFrame: {type(frame)!r}")

    if {"date", "value"}.issubset(frame.columns):
        data = frame.loc[:, ["date", "value"]]
    elif "指标" in frame.columns:
        matched = frame[frame["指标"].astype(str) == trigger.name]
        if matched.empty:
            raise ValueError(f"财报缺少指标字段: {trigger.name}")
        date_cols = [col for col in frame.columns if str(col).isdigit() and len(str(col)) == 8]
        if not date_cols:
            raise ValueError("财报缺少报告期列")
        row = matched.iloc[0]
        data = pd.DataFrame(
            {"date": [str(col) for col in date_cols], "value": [row[col] for col in date_cols]}
        )
    else:
        date_col = frame.columns[0]
        value_cols = [
            col for col in frame.columns[1:] if pd.api.types.is_numeric_dtype(frame[col])
        ]
        if not value_cols:
            raise ValueError(f"无法定位数值列: {list(frame.columns)}")
        data = pd.DataFrame(
            {"date": frame[date_col].astype(str), "value": frame[value_cols[-1]]}
        )

    data["date"] = data["date"].astype(str).str[:10]
    data = data.dropna(subset=["value"])
    data["value"] = data["value"].astype(float)
    data = data.sort_values("date").tail(HISTORY_LIMIT)
    if data.empty:
        raise ValueError("无可用数据")
    return [(str(date), float(value)) for date, value in zip(data["date"], data["value"])]


def collect_chain(
    conn: sqlite3.Connection, config: ChainConfig, symbol: str
) -> dict[str, float | None]:
    """按触发器配置取数落库，返回最新值与缺失标记。"""
    values: dict[str, float | None] = {}

    for trigger in config.triggers:
        try:
            if trigger.src in _LOCAL_SOURCES:
                latest = get_latest_metric(conn, symbol, trigger.id)
                if latest is None:
                    values[f"_missing_{trigger.id}"] = None
                    _log(config.chain_name, trigger.id, "missing")
                else:
                    values[trigger.id] = latest[0]
                    _log(config.chain_name, trigger.id, "local")
                continue

            fetcher = _FETCHERS.get(trigger.src)
            if fetcher is None:
                raise NotImplementedError(f"未支持的数据源: {trigger.src}")

            rows = _standardize(fetcher(trigger, symbol), trigger)
            for metric_date, value in rows:
                upsert_metric(conn, symbol, metric_date, trigger.id, value)
            values[trigger.id] = rows[-1][1]
            _log(config.chain_name, trigger.id, "ok")
        except UnsupportedMetricError as exc:
            _log(config.chain_name, trigger.id, f"unsupported: {exc}")
            latest = get_latest_metric(conn, symbol, trigger.id)
            if latest is not None:
                values[trigger.id] = latest[0]
            else:
                values[f"_missing_{trigger.id}"] = None
        except NotImplementedError as exc:
            _log(config.chain_name, trigger.id, f"unsupported: {exc}")
            raise
        except Exception as exc:  # noqa: BLE001 - 单个指标失败不阻断其余指标
            logger.warning(
                "[collector] %s %s -> error: %s", config.chain_name, trigger.id, exc
            )
            latest = get_latest_metric(conn, symbol, trigger.id)
            if latest is not None:
                values[trigger.id] = latest[0]
                _log(config.chain_name, trigger.id, "fallback_local")
            else:
                values[f"_missing_{trigger.id}"] = None

    return values


def _last_valuation_date(conn: sqlite3.Connection, symbol: str) -> str | None:
    """已落库的最新估值日期，用于增量拉取。"""
    marks = ", ".join("?" for _ in _VALUATION_IDS)
    row = conn.execute(
        "SELECT MAX(metric_date) FROM daily_metric"
        f" WHERE symbol = ? AND metric_id IN ({marks})",
        (symbol, *_VALUATION_IDS),
    ).fetchone()
    return str(row[0]) if row and row[0] else None


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=5, max=30),
    reraise=True,
)
def _query_valuation_rows(
    symbol: str, start_date: str, end_date: str
) -> list[tuple[str, str, str, float]]:
    """Baostock 日频估值字段，返回 (symbol, date, metric_id, value) 列表。"""
    _ensure_baostock_login()
    rs = bs.query_history_k_data_plus(
        _to_bs_code(symbol),
        "date," + ",".join(_VALUATION_FIELDS),
        start_date=start_date,
        end_date=end_date,
        frequency="d",
        adjustflag="3",
    )
    if rs.error_code != "0":
        raise ConnectionError(f"Baostock 估值查询失败: {rs.error_msg}")
    rows: list[tuple[str, str, str, float]] = []
    while rs.next():
        row = dict(zip(rs.fields, rs.get_row_data()))
        day = str(row.get("date", ""))[:10]
        if not day:
            continue
        for field, metric_id in _VALUATION_FIELDS.items():
            raw = row.get(field, "")
            if raw in ("", None):
                continue
            try:
                rows.append((symbol, day, metric_id, float(raw)))
            except (TypeError, ValueError):
                continue
    return rows


def collect_valuation(
    conn: sqlite3.Connection, symbol: str, years: int = VALUATION_YEARS
) -> int:
    """增量拉取估值指标并落库，返回写入的记录数。"""
    from datetime import date, timedelta

    today = date.today()
    last = _last_valuation_date(conn, symbol)
    try:
        start = (
            date.fromisoformat(last) - timedelta(days=10)
            if last
            else today - timedelta(days=365 * years + 30)
        )
    except ValueError:
        start = today - timedelta(days=365 * years + 30)

    rows = _query_valuation_rows(symbol, start.isoformat(), today.isoformat())
    if not rows:
        return 0
    conn.executemany(
        "INSERT OR REPLACE INTO daily_metric (symbol, metric_date, metric_id, value)"
        " VALUES (?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return len(rows)
