"""研究 Agent：按固定工具序列汇总证据，并支持基于事实的追问。"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime
from typing import Any

from .compliance import ComplianceError, DISCLAIMER, filter_or_raise
from .llm import LLMError, call_llm
from .models import ChainConfig
from .prompts import AGENT_SYSTEM
from .report import DISCLAIMER_TEXT, build_report_data, cites_trigger
from .rules import parse_cond

AGENT_DISCLAIMER = "以上内容由系统数据与规则生成，仅用于产业链研究跟踪，不构成投资建议。"
MAX_QUESTION = 200
MIN_EVIDENCE = 2


class AgentError(RuntimeError):
    """研究 Agent 运行或输出校验失败。"""


def _finding(text: str, evidence: list[str]) -> dict[str, Any]:
    return {"text": text, "evidence": evidence}


def _num(value: float | None) -> str:
    return "—" if value is None else f"{value:+.2f}"


def _data_step(data: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    triggers = data["triggers"]
    covered = [item for item in triggers if item["value"] is not None]
    missing = [item for item in triggers if item["value"] is None]
    detail = f"{len(covered)}/{len(triggers)} 项指标有数据，最新数据日 {data['data_date'] or '—'}"
    if missing:
        evidence = [
            f"{item['id']}：{item['name']} 暂无数据" for item in missing[:3]
        ]
        return detail, [_finding(f"仍有 {len(missing)} 项指标没有数据，结论只覆盖有数据的部分", evidence)]
    evidence = [
        f"{item['id']}：{item['name']} 最新 {item['value_text']}{item['unit']}（{item['metric_date']}）"
        for item in covered[:3]
    ]
    return detail, [_finding(f"{len(covered)} 项指标均有数据，数据日期 {data['data_date']}", evidence)]


def _trigger_step(data: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    triggers = data["triggers"]
    stats = data["stats"]
    abnormal = [item for item in triggers if item["status"] in ("watch", "alert")]
    detail = f"关注 {stats['watch']} · 触发 {stats['alert']} · 无数据 {stats['no_data']}"
    if not abnormal:
        evidence = [
            f"{item['id']}：{item['name']} {item['value_text']}{item['unit']}（{item['status_label']}）"
            for item in triggers
            if item["status"] == "normal"
        ][:3]
        if not evidence:
            evidence = [f"{item['id']}：{item['name']} 暂无数据" for item in triggers[:3]]
        return detail, [_finding("当前没有触发器偏离正常区间", evidence)]
    names = "、".join(f"{item['name']}（{item['status_label']}）" for item in abnormal[:4])
    evidence = [
        f"{item['id']}：{item['name']} {item['value_text']}{item['unit']}（{item['status_label']}）"
        for item in abnormal
    ]
    return detail, [_finding(f"{len(abnormal)} 项触发器偏离正常区间：{names}", evidence)]


def _rule_step(data: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    matched = [item for item in data["rules"] if item["current_match"]]
    detail = f"{len(matched)}/{len(data['rules'])} 条组合规则当前成立"
    if not matched:
        return detail, [
            _finding(
                "组合规则当前均未同时满足，尚未形成结构性判断",
                [f"{item['id']}：{item['name']}" for item in data["triggers"][:3]],
            )
        ]
    findings: list[dict[str, Any]] = []
    for rule in matched:
        ids = [trigger_id for trigger_id, _ in parse_cond(rule["cond"])]
        findings.append(_finding(f"组合规则成立：{rule['cond']} → {rule['res']}", ids))
    return detail, findings


def _index_step(data: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    index = data["chain_index"]
    detail = (
        f"{len(data['companies'])} 家公司，价格覆盖 {index['price_coverage']}，"
        f"20 日 {_num(index['ret_20'])}%"
    )
    if index["ret_20"] is None:
        return detail, [
            _finding(
                "链内价格序列不足，暂无法计算等权链指数",
                [f"{item['id']}：{item['name']}" for item in data["triggers"][:3]],
            )
        ]
    leaders = sorted(
        [item for item in data["companies"] if item["ret_20"] is not None],
        key=lambda item: item["ret_20"],
        reverse=True,
    )[:2]
    evidence = [f"{item['name']}：近 20 日 {_num(item['ret_20'])}%" for item in leaders]
    breadth = index["breadth_20"]
    text = f"等权链指数近 20 日 {_num(index['ret_20'])}%"
    if breadth is not None:
        text += f"，上涨占比 {breadth:.1f}%"
    if index["dispersion_60"] is not None:
        text += f"，60 日离散度 {index['dispersion_60']:.2f}"
    return detail, [_finding(text, evidence)]


def _study_step(data: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    study = data["signal_study"]
    detail = f"事件 {study['events']} 次，20 日样本 {study['samples']} 个"
    enough = [
        item
        for item in study["triggers"]
        if item["sample_label"] == "样本充足" and item["excess_20"] is not None
    ]
    if not enough:
        evidence = [
            f"{item['id']}：{item['name']} 事件 {item['events']} 次，样本 {item['samples']}"
            for item in study["triggers"][:3]
        ]
        if not evidence:
            evidence = [f"index_days：{study['index_days']}"]
        return detail, [_finding("信号样本不足，暂不评价触发后的有效性", evidence)]
    best = max(enough, key=lambda item: item["excess_20"])
    evidence = [
        f"{item['id']}：{item['name']} 样本 {item['samples']} 个，20 日平均 {_num(item['avg_20'])}%，"
        f"相对基准超额 {_num(item['excess_20'])}%"
        for item in sorted(enough, key=lambda item: item["excess_20"], reverse=True)[:2]
    ]
    return detail, [
        _finding(
            f"信号「{best['name']}」在 {best['samples']} 个样本上 20 日平均 {_num(best['avg_20'])}%，"
            f"相对全样本基准超额 {_num(best['excess_20'])}%",
            evidence,
        )
    ]


def _scenario_step(data: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    scenario = data["scenario"]
    nodes = scenario["nodes"]
    last = nodes[-1]
    detail = f"{scenario['node']} {scenario['shock_pct']:+.1f}% → {last['node']} {_num(last['impact_pct'])}%"
    evidence = [
        f"{item['node']}：{_num(item['impact_pct'])}%，累计滞后 {item['cumulative_days']} 天"
        for item in nodes[1:4]
    ]
    if not evidence:
        evidence = [f"shock_pct：{scenario['shock_pct']:+.1f}%"]
    return detail, [
        _finding(
            f"默认冲击 {scenario['shock_pct']:+.1f}% 下，{last['node']} 呈{last['direction_label']}，"
            f"幅度 {_num(last['impact_low'])}%~{_num(last['impact_high'])}%，累计滞后 {last['cumulative_days']} 天",
            evidence,
        )
    ]


def _ledger_step(data: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    thesis = data["thesis"]
    stats = thesis["stats"]
    pending = [item for item in thesis["catalysts"] if item["status"] == "pending"]
    detail = (
        f"待验证 {stats['active']} · 催化剂待发生 {len(pending)}"
        f"（7 天内 {thesis['catalyst_stats']['soon']}）"
    )
    active = [item for item in thesis["theses"] if item["status"] == "active"]
    findings: list[dict[str, Any]] = []
    if active:
        findings.append(
            _finding(
                f"有 {len(active)} 条 Thesis 等待验证：{active[0]['title']}",
                [f"{item['title']}：{item['status_label']}，{item['direction_label']}" for item in active[:3]],
            )
        )
    if pending:
        near = pending[0]
        findings.append(
            _finding(
                f"最近催化剂 {near['event_date']} {near['title']}（{near['timing_label']}）",
                [f"{item['event_date']}：{item['title']}（{item['timing_label']}）" for item in pending[:3]],
            )
        )
    if not findings:
        closed = thesis["theses"] + thesis["catalysts"]
        if closed:
            findings.append(
                _finding(
                    "Thesis 与催化剂均无待办项",
                    [f"{item['title']}：{item['status_label']}" for item in closed[:3]],
                )
            )
        else:
            findings.append(
                _finding(
                    "尚未记录 Thesis 与催化剂，研究判断还没有落到纸面",
                    [f"data_date：{data['data_date'] or '—'}"],
                )
            )
    return detail, findings


STEPS = (
    ("data", "数据覆盖", _data_step),
    ("triggers", "触发器", _trigger_step),
    ("rules", "组合规则", _rule_step),
    ("cross_section", "链内横截面", _index_step),
    ("event_study", "信号有效性", _study_step),
    ("scenario", "情景推演", _scenario_step),
    ("ledger", "Thesis 与催化剂", _ledger_step),
)


def _watch_list(data: dict[str, Any]) -> list[str]:
    watch: list[str] = []
    for item in data["triggers"]:
        if item["status"] in ("watch", "alert"):
            watch.append(f"跟踪 {item['name']}（{item['status_label']}）")
    for item in data["thesis"]["catalysts"]:
        if item["status"] == "pending" and item["phase"] in ("overdue", "today", "soon"):
            watch.append(f"{item['event_date']} {item['title']}（{item['timing_label']}）")
    for item in data["thesis"]["theses"]:
        if item["status"] == "active":
            watch.append(f"验证 Thesis：{item['title']}")
    return watch[:6]


def research_run(
    config: ChainConfig,
    conn: sqlite3.Connection,
    symbol: str,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """按固定顺序执行研究工具，返回可追溯的运行记录与结论。"""
    started = time.perf_counter()
    snapshot = data or build_report_data(config, conn, symbol)
    steps: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    for key, label, func in STEPS:
        detail, items = func(snapshot)
        steps.append({"tool": key, "label": label, "status": "ok", "detail": detail})
        findings.extend(items)
    return {
        "chain": config.chain_name,
        "data_date": snapshot["data_date"],
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
        "steps": steps,
        "findings": findings,
        "watch": _watch_list(snapshot),
        "disclaimer": DISCLAIMER_TEXT,
    }


def _ask_payload(config: ChainConfig, data: dict[str, Any], question: str) -> dict[str, Any]:
    return {
        "chain_name": config.chain_name,
        "data_date": data["data_date"],
        "stats": data["stats"],
        "triggers": [
            {
                "id": item["id"],
                "name": item["name"],
                "value": item["value_text"],
                "unit": item["unit"],
                "status": item["status_label"],
                "metric_date": item["metric_date"],
                "pct_label": item["pct_label"],
                "streak": item["streak"],
            }
            for item in data["triggers"]
        ],
        "rules": [
            {"cond": item["cond"], "res": item["res"], "current_match": item["current_match"]}
            for item in data["rules"]
        ],
        "chokes": [
            {"node": item["node"], "state": item["state_label"], "score": item["score"]}
            for item in data["chokes"]
        ],
        "chain_index": data["chain_index"],
        "scenario": {
            "node": data["scenario"]["node"],
            "shock_pct": data["scenario"]["shock_pct"],
            "nodes": [
                {
                    "node": item["node"],
                    "direction": item["direction_label"],
                    "impact_pct": item["impact_pct"],
                    "cumulative_days": item["cumulative_days"],
                }
                for item in data["scenario"]["nodes"]
            ],
        },
        "theses": [
            {"title": item["title"], "status": item["status_label"], "direction": item["direction_label"]}
            for item in data["thesis"]["theses"]
        ],
        "catalysts": [
            {"event_date": item["event_date"], "title": item["title"], "timing": item["timing_label"]}
            for item in data["thesis"]["catalysts"]
            if item["status"] == "pending"
        ],
        "question": question,
    }


def ask(
    config: ChainConfig,
    conn: sqlite3.Connection,
    symbol: str,
    question: str,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """基于已算好的事实回答追问，答案必须引用真实指标。"""
    text = str(question or "").strip()
    if not text:
        raise AgentError("问题不能为空")
    if len(text) > MAX_QUESTION:
        raise AgentError(f"问题不能超过 {MAX_QUESTION} 字")

    snapshot = data or build_report_data(config, conn, symbol)
    try:
        result = call_llm(AGENT_SYSTEM, _ask_payload(config, snapshot, text), max_retries=1)
    except LLMError as exc:
        raise AgentError(str(exc)) from exc
    if not isinstance(result, dict):
        raise AgentError("LLM 返回的不是 JSON 对象")

    answer = result.get("answer")
    if not isinstance(answer, str) or not answer.strip():
        raise AgentError("LLM 输出缺少 answer")
    evidence = result.get("evidence")
    if not isinstance(evidence, list) or len(evidence) < MIN_EVIDENCE:
        raise AgentError(f"LLM 输出 evidence 至少需要 {MIN_EVIDENCE} 条")
    ids = [trigger.id for trigger in config.triggers]
    names = [trigger.name for trigger in config.triggers]
    valid = [item for item in evidence if isinstance(item, str) and cites_trigger(item, ids, names)]
    if len(valid) < MIN_EVIDENCE:
        raise AgentError("LLM 输出 evidence 未引用真实指标，可用 id：" + "、".join(ids))
    unknowns = result.get("unknown", [])
    if not isinstance(unknowns, list) or not all(isinstance(item, str) for item in unknowns):
        raise AgentError("LLM 输出 unknown 必须是字符串数组")

    try:
        filter_or_raise(answer + "\n" + DISCLAIMER, "agent_answer")
    except ComplianceError as exc:
        raise AgentError(f"答案未通过合规审查：{exc}") from exc

    return {
        "chain": config.chain_name,
        "question": text,
        "answer": answer,
        "evidence": valid,
        "unknown": [item for item in unknowns if item.strip()],
        "watch_next": str(result.get("watch_next", "")).strip(),
        "data_date": snapshot["data_date"],
        "disclaimer": DISCLAIMER_TEXT,
    }
