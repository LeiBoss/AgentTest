"""流程编排入口：配置 → 取数 → 判定 → 解读 → 规则 → 推送。"""

from __future__ import annotations

import logging
import os
import sys
from contextlib import contextmanager
from typing import Iterator

from src.collector import collect_chain, collect_companies, collect_valuation
from src.compliance import ComplianceError, filter_or_raise
from src.config_loader import load_chain_config, validate_chain_config
from src.db import get_last_status, get_metric_history, init_db, set_signal_payload
from src.judge import judge_status, run_judge
from src.llm import interpret_transition
from src.models import ChainConfig
from src.notifier import push_feishu
from src.rules import run_rules

logger = logging.getLogger(__name__)

DISCLAIMER_TAIL = "信息整理，不构成投资建议。"
VALUATION_ONLY_FLAG = "--valuation-only"


class ConfigError(RuntimeError):
    """配置加载或语义校验失败。"""


class CollectError(RuntimeError):
    """采集阶段没有任何可用数据。"""


@contextmanager
def _step(name: str) -> Iterator[None]:
    """为每个编排步骤统一记录 start/done/error 日志。"""
    logger.info("[main] step=%s status=start", name)
    try:
        yield
    except Exception:
        logger.error("[main] step=%s status=error", name)
        raise
    logger.info("[main] step=%s status=done", name)


def run_chain(chain_yaml_path: str, symbol: str) -> dict:
    """跑通单条产业链的完整流程。"""
    with _step("config"):
        config = _load_config(chain_yaml_path)

    conn = init_db()
    try:
        with _step("collect"):
            values = collect_chain(conn, config, symbol)
            if not _has_data(values):
                raise CollectError("采集阶段无任何可用数据，请检查数据源或先补齐渠道调研数据")
            try:
                logger.info("[main] valuation rows=%s", collect_valuation(conn, symbol))
            except Exception as exc:  # noqa: BLE001 - 估值抓取失败不阻断主流程
                logger.warning("[main] valuation skipped: %s", exc)
            try:
                logger.info("[main] company rows=%s", collect_companies(conn, config))
            except Exception as exc:  # noqa: BLE001 - 公司数据抓取失败不阻断主流程
                logger.warning("[main] company collect skipped: %s", exc)

        with _step("judge"):
            transitions = run_judge(conn, config, values)

        with _step("interpret"):
            _annotate_transitions(conn, config, symbol, transitions)

        with _step("rules"):
            statuses = _build_statuses(conn, config, values)
            rules_triggered = run_rules(conn, config, statuses)
            for rule in rules_triggered:
                logger.info("[main] rule=%s res=%s", rule["rule_cond"], rule["res"])

        with _step("push"):
            pushed = _push(config, transitions, rules_triggered)
    finally:
        conn.close()

    return {
        "transitions": transitions,
        "rules_triggered": rules_triggered,
        "pushed": pushed,
    }


def backfill_valuation(symbol: str) -> int:
    """仅回补估值数据，不触发判定、LLM 解读与推送。"""
    conn = init_db()
    try:
        return collect_valuation(conn, symbol)
    finally:
        conn.close()


def main() -> None:
    """读取参数并执行，失败以非 0 退出码结束。"""
    argv = sys.argv[1:]
    if VALUATION_ONLY_FLAG in argv:
        args = [arg for arg in argv if arg != VALUATION_ONLY_FLAG]
        symbol = args[0] if args else os.getenv("SYMBOL", "")
        if not symbol:
            logger.error("[main] 缺少参数：symbol")
            sys.exit(1)
        try:
            logger.info("[main] valuation rows=%s", backfill_valuation(symbol))
        except Exception as exc:  # noqa: BLE001 - 顶层边界，统一转为退出码
            logger.error("[main] valuation failed: %s", exc)
            sys.exit(1)
        return

    chain_yaml_path, symbol = _read_args()
    if not chain_yaml_path or not symbol:
        logger.error("[main] 缺少参数：chain_yaml_path 与 symbol")
        sys.exit(1)

    try:
        run_chain(chain_yaml_path, symbol)
    except Exception as exc:  # noqa: BLE001 - 顶层边界，统一转为退出码
        logger.error("[main] run failed: %s", exc)
        sys.exit(1)


def _load_config(chain_yaml_path: str) -> ChainConfig:
    """加载配置并做语义校验，失败统一抛 ConfigError。"""
    try:
        config = load_chain_config(chain_yaml_path)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc

    issues = validate_chain_config(config)
    if issues:
        raise ConfigError("配置校验失败: " + "；".join(issues))
    return config


def _has_data(values: dict[str, float]) -> bool:
    """至少有一个指标拿到了可用值。"""
    return any(
        not key.startswith("_missing_") and value is not None
        for key, value in values.items()
    )


def _annotate_transitions(
    conn, config: ChainConfig, symbol: str, transitions: list[dict]
) -> None:
    """对每次迁移调用 LLM，成功则把解读挂到对应 signal_log 记录。"""
    triggers = {trigger.id: trigger for trigger in config.triggers}
    for transition in transitions:
        trigger = triggers.get(transition["trigger_id"])
        if trigger is None:
            continue
        history = get_metric_history(conn, symbol, trigger.id)
        result = interpret_transition(trigger, transition, history)
        if result is None:
            continue
        signal_id = transition.get("signal_id")
        if signal_id is not None:
            set_signal_payload(conn, signal_id, result)


def _build_statuses(conn, config: ChainConfig, values: dict[str, float]) -> dict[str, str]:
    """有值按区间判定，缺值沿用最近一次状态。"""
    statuses: dict[str, str] = {}
    for trigger in config.triggers:
        value = values.get(trigger.id)
        if value is None:
            statuses[trigger.id] = (
                get_last_status(conn, config.chain_name, trigger.id) or "normal"
            )
        else:
            statuses[trigger.id] = judge_status(trigger, value)
    return statuses


def _push(config: ChainConfig, transitions: list[dict], rules_triggered: list[dict]) -> bool:
    """拼装周报文本，过合规后推送。"""
    title = f"{config.chain_name}产业链周度跟踪"
    body = _compose_body(transitions, rules_triggered)
    try:
        filter_or_raise(f"{title}\n\n{body}\n\n{DISCLAIMER_TAIL}", "weekly_report")
    except ComplianceError as exc:
        logger.error("[main] 合规拦截，跳过推送: %s", exc)
        return False
    try:
        return push_feishu(title, body, DISCLAIMER_TAIL)
    except EnvironmentError as exc:
        logger.warning("[main] 未配置飞书推送，跳过: %s", exc)
        return False


def _compose_body(transitions: list[dict], rules_triggered: list[dict]) -> str:
    """汇总本周状态迁移与规则触发。"""
    lines: list[str] = []
    if transitions:
        lines.append("状态迁移：")
        lines.extend(
            f"- {item['trigger_id']}：{item['from_status']}→{item['to_status']}"
            f"（{item['value']} {item['unit']}）"
            for item in transitions
        )
    if rules_triggered:
        lines.append("规则触发：")
        lines.extend(f"- {item['res']}：{item['rationale']}" for item in rules_triggered)
    return "\n".join(lines) if lines else "本周无状态迁移与规则触发。"


def _read_args() -> tuple[str, str]:
    """命令行参数优先，其次环境变量。"""
    chain_yaml_path = sys.argv[1] if len(sys.argv) > 1 else os.getenv("CHAIN_YAML", "")
    symbol = sys.argv[2] if len(sys.argv) > 2 else os.getenv("SYMBOL", "")
    return chain_yaml_path, symbol


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    main()
