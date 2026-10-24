"""固定评测集：对研究链路与最近研报做确定性的质量检查。"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from typing import Any

from .agent import research_run
from .compliance import check_content
from .db import get_latest_report
from .models import ChainConfig
from .report import DISCLAIMER_TEXT, NARRATIVE_FIELDS, build_report_data, cites_trigger
from .thesis import CATALYST_STATUSES, THESIS_STATUSES

DIRECTIONS = ("up", "down", "flat", "none")


def _check(key: str, label: str, passed: bool, detail: str) -> dict[str, Any]:
    return {"key": key, "label": label, "status": "pass" if passed else "fail", "detail": detail}


def _skip(key: str, label: str, detail: str) -> dict[str, Any]:
    return {"key": key, "label": label, "status": "skip", "detail": detail}


def _pipeline_check(run: dict[str, Any]) -> dict[str, Any]:
    failed = [step["label"] for step in run["steps"] if step["status"] != "ok"]
    findings = run["findings"]
    detail = f"{len(run['steps'])} 个工具步骤，{len(findings)} 条结论"
    if failed:
        detail += "；失败步骤：" + "、".join(failed)
    return _check("pipeline", "研究链路可完整运行", not failed and bool(findings), detail)


def _evidence_check(run: dict[str, Any]) -> dict[str, Any]:
    findings = run["findings"]
    covered = [item for item in findings if item["evidence"]]
    return _check(
        "evidence",
        "结论均带证据",
        len(covered) == len(findings) and bool(findings),
        f"{len(covered)}/{len(findings)} 条结论有证据",
    )


def _scenario_check(data: dict[str, Any], config: ChainConfig) -> dict[str, Any]:
    scenario = data["scenario"]
    nodes = scenario["nodes"]
    problems: list[str] = []
    if len(nodes) != len(config.transmission.chain):
        problems.append("节点数与传导链不一致")
    if not nodes:
        return _check("scenario", "情景推演自洽", False, "推演结果为空")
    if abs(nodes[0]["impact_pct"] - scenario["shock_pct"]) > 1e-2:
        problems.append("首节点幅度与冲击不一致")
    last_days = -1
    for item in nodes:
        if item["direction"] not in DIRECTIONS:
            problems.append(f"{item['node']} 方向取值非法")
        if item["impact_low"] > item["impact_high"]:
            problems.append(f"{item['node']} 区间上下界颠倒")
        if item["cumulative_days"] < last_days:
            problems.append(f"{item['node']} 累计滞后倒挂")
        last_days = item["cumulative_days"]
    detail = f"{len(nodes)} 个环节，终点 {nodes[-1]['impact_pct']:+.2f}%"
    return _check("scenario", "情景推演自洽", not problems, detail + ("；" + "；".join(problems) if problems else ""))


def _ledger_check(data: dict[str, Any]) -> dict[str, Any]:
    ledger = data["thesis"]
    problems: list[str] = []
    for item in ledger["theses"]:
        if item["status"] not in THESIS_STATUSES:
            problems.append(f"{item['title']} 状态非法")
        if not item["confirm_condition"] or not item["invalidate_condition"]:
            problems.append(f"{item['title']} 缺少确认或证伪条件")
    for item in ledger["catalysts"]:
        if item["status"] not in CATALYST_STATUSES:
            problems.append(f"{item['title']} 状态非法")
        try:
            date.fromisoformat(item["event_date"])
        except ValueError:
            problems.append(f"{item['title']} 日期格式非法")
    detail = f"Thesis {len(ledger['theses'])} 条，催化剂 {len(ledger['catalysts'])} 条"
    return _check("ledger", "账本字段完整可证伪", not problems, detail + ("；" + "；".join(problems) if problems else ""))


def _report_text(report: dict[str, Any]) -> str:
    parts = [str(report.get(field, "")) for field in NARRATIVE_FIELDS]
    parts.extend(str(item) for item in report.get("risks", []))
    parts.append(str(report.get("disclaimer") or DISCLAIMER_TEXT))
    return "\n".join(part for part in parts if part)


def _report_checks(report: dict[str, Any] | None, config: ChainConfig) -> list[dict[str, Any]]:
    if report is None:
        return [
            _skip("report_evidence", "研报证据可追溯", "暂无研报"),
            _skip("report_compliance", "研报合规", "暂无研报"),
        ]
    ids = [trigger.id for trigger in config.triggers]
    names = [trigger.name for trigger in config.triggers]
    evidence = report.get("evidence")
    if not isinstance(evidence, list):
        evidence = []
    valid = [item for item in evidence if isinstance(item, str) and cites_trigger(item, ids, names)]
    evidence_check = _check(
        "report_evidence",
        "研报证据可追溯",
        len(valid) >= 3,
        f"{len(valid)}/{len(evidence)} 条证据引用真实指标",
    )
    result = check_content(_report_text(report), "research_report")
    violations = [item["text"] for item in result["violations"]]
    compliance_check = _check(
        "report_compliance",
        "研报合规",
        result["pass"],
        "通过" if result["pass"] else "违规：" + "、".join(violations),
    )
    return [evidence_check, compliance_check]


def evaluate_chain(
    config: ChainConfig, conn: sqlite3.Connection, symbol: str
) -> dict[str, Any]:
    """对单条产业链运行全部评测项。"""
    data = build_report_data(config, conn, symbol)
    run = research_run(config, conn, symbol, data=data)
    checks = [
        _pipeline_check(run),
        _evidence_check(run),
        _scenario_check(data, config),
        _ledger_check(data),
        *_report_checks(get_latest_report(conn, config.chain_name), config),
    ]
    passed = sum(1 for item in checks if item["status"] == "pass")
    failed = sum(1 for item in checks if item["status"] == "fail")
    return {
        "chain": config.chain_name,
        "checks": checks,
        "passed": passed,
        "failed": failed,
        "total": passed + failed,
    }


def evaluate_chains(
    configs: list[ChainConfig], conn: sqlite3.Connection, symbol: str
) -> dict[str, Any]:
    """跑完全部产业链的评测集并汇总得分。"""
    cases: list[dict[str, Any]] = []
    for config in configs:
        try:
            cases.append(evaluate_chain(config, conn, symbol))
        except Exception as exc:  # noqa: BLE001 - 单条链失败不影响整体评测
            cases.append(
                {
                    "chain": config.chain_name,
                    "checks": [_check("runtime", "评测运行", False, str(exc))],
                    "passed": 0,
                    "failed": 1,
                    "total": 1,
                }
            )
    passed = sum(case["passed"] for case in cases)
    failed = sum(case["failed"] for case in cases)
    total = passed + failed
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "chain_count": len(cases),
        "passed": passed,
        "failed": failed,
        "total": total,
        "score": round(passed / total * 100, 1) if total else 0.0,
        "cases": cases,
    }
