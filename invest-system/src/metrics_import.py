"""渠道调研类指标的手工导入。"""

from __future__ import annotations

import csv
import io
import math
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from src.db import get_latest_metric, upsert_metric
from src.models import ChainConfig

REQUIRED_COLUMNS = ("metric_date", "metric_id", "value")


@dataclass(frozen=True)
class ImportReport:
    """一次导入的结果。"""

    imported: int
    metric_ids: list[str]
    missing: list[str]


def import_metrics(
    conn,
    config: ChainConfig,
    symbol: str,
    csv_path: str | Path,
) -> ImportReport:
    """校验 CSV 文件后写入 daily_metric，返回导入统计。"""
    path = Path(csv_path)
    if not path.is_file():
        raise ValueError(f"文件不存在: {path}")
    return import_metrics_text(conn, config, symbol, path.read_text(encoding="utf-8-sig"))


def import_metrics_text(
    conn,
    config: ChainConfig,
    symbol: str,
    text: str,
) -> ImportReport:
    """校验 CSV 文本后写入 daily_metric，返回导入统计。"""
    rows = _parse_csv(text, config)
    for metric_date, metric_id, value in rows:
        upsert_metric(conn, symbol, metric_date, metric_id, value)

    metric_ids = sorted({row[1] for row in rows})
    missing = [
        trigger.id
        for trigger in config.triggers
        if get_latest_metric(conn, symbol, trigger.id) is None
    ]
    return ImportReport(len(rows), metric_ids, missing)


def _parse_csv(text: str, config: ChainConfig) -> list[tuple[str, str, float]]:
    """解析并校验全部行，有错误时一次性抛出。"""
    known_ids = {trigger.id for trigger in config.triggers}
    today = date.today()
    rows: list[tuple[str, str, float]] = []
    errors: list[str] = []
    seen: set[tuple[str, str]] = set()

    reader = csv.DictReader(io.StringIO(text))
    header = reader.fieldnames or []
    missing_columns = [name for name in REQUIRED_COLUMNS if name not in header]
    if missing_columns:
        raise ValueError(f"CSV 缺少字段: {', '.join(missing_columns)}")

    for line_no, raw in enumerate(reader, start=2):
        metric_id = (raw.get("metric_id") or "").strip()
        raw_date = (raw.get("metric_date") or "").strip()
        raw_value = (raw.get("value") or "").strip()
        if not metric_id and not raw_date and not raw_value:
            continue

        if metric_id not in known_ids:
            errors.append(
                f"第 {line_no} 行 metric_id: 链路配置中不存在 '{metric_id}'"
            )
            continue

        try:
            metric_date = date.fromisoformat(raw_date)
        except ValueError:
            errors.append(
                f"第 {line_no} 行 metric_date: 需要 YYYY-MM-DD，实际 '{raw_date}'"
            )
            continue
        if metric_date > today:
            errors.append(
                f"第 {line_no} 行 metric_date: 不能晚于今天 '{raw_date}'"
            )
            continue

        try:
            value = float(raw_value)
        except ValueError:
            errors.append(f"第 {line_no} 行 value: 需要数字，实际 '{raw_value}'")
            continue
        if not math.isfinite(value):
            errors.append(f"第 {line_no} 行 value: 不能是 NaN 或 Inf")
            continue

        key = (metric_date.isoformat(), metric_id)
        if key in seen:
            errors.append(
                f"第 {line_no} 行: {key[0]} 的 {metric_id} 重复出现"
            )
            continue
        seen.add(key)
        rows.append((key[0], metric_id, value))

    if errors:
        raise ValueError("导入失败：\n" + "\n".join(errors))
    if not rows:
        raise ValueError("CSV 没有有效数据行")
    return rows
