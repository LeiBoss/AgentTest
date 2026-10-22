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
_LOCAL_SOURCES = frozenset({"渠道调研", "研报"})

_BS_LOGGED_IN = False


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


def _fetch_contract_liab(trigger: Trigger, symbol: str) -> pd.DataFrame:
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


def _fetch_gross_margin(trigger: Trigger, symbol: str) -> pd.DataFrame:
    """Baostock 利润表：毛利率（百分数）。"""
    rows = _baostock_profit_history(symbol, HISTORY_LIMIT)
    if not rows:
        raise ValueError("Baostock 毛利率无数据")
    return pd.DataFrame(rows, columns=["date", "value"])


def _fetch_financial(trigger: Trigger, symbol: str) -> pd.DataFrame:
    if trigger.id == "contract_liab":
        return _fetch_contract_liab(trigger, symbol)
    if trigger.id == "gross_margin":
        return _fetch_gross_margin(trigger, symbol)
    raise NotImplementedError(f"未支持的财报指标: {trigger.id}")


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
