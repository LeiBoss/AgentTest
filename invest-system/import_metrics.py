"""命令行导入渠道调研指标。"""

from __future__ import annotations

import logging
import sys

from src.config_loader import load_chain_config, validate_chain_config
from src.db import init_db
from src.metrics_import import import_metrics

logger = logging.getLogger(__name__)

USAGE = "用法: python import_metrics.py <chain.yaml> <symbol> <csv>"


def main() -> None:
    """解析参数、导入并打印结果，失败以非 0 退出。"""
    if len(sys.argv) < 4:
        print(USAGE, file=sys.stderr)
        sys.exit(2)

    config_path, symbol, csv_path = sys.argv[1:4]
    conn = None
    try:
        config = load_chain_config(config_path)
        issues = validate_chain_config(config)
        if issues:
            raise ValueError("配置校验失败: " + "；".join(issues))

        conn = init_db()
        report = import_metrics(conn, config, symbol, csv_path)
    except (ValueError, RuntimeError) as exc:
        logger.error("[import] 失败: %s", exc)
        sys.exit(1)
    finally:
        if conn is not None:
            conn.close()

    logger.info(
        "[import] 导入 %s 条，覆盖指标: %s",
        report.imported,
        ", ".join(report.metric_ids),
    )
    if report.missing:
        logger.info("[import] 仍无数据的触发器: %s", ", ".join(report.missing))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    main()
