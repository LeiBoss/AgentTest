"""对外输出的合规审查。"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

BANNED_WORDS: tuple[str, ...] = (
    "买入",
    "卖出",
    "增持",
    "减持",
    "目标价",
    "建议关注",
    "满仓",
    "抄底",
    "上车",
    "布局",
    "看多",
    "看空",
    "牛股",
    "翻倍",
    "稳赚",
    "保本",
    "必涨",
    "确定性收益",
)

DISCLAIMER = "不构成投资建议"
_STOCK_CODE_RE = re.compile(r"(?<!\d)(?:60|00|30|68)\d{4}(?!\d)")
_CAUSAL_PHRASES = ("可投", "值得投", "可以投", "能投")


class ComplianceError(RuntimeError):
    """内容未通过合规审查。"""


def check_content(text: str, content_type: str) -> dict:
    """返回合规判定结果与违规明细。"""
    violations: list[dict] = []

    for word in BANNED_WORDS:
        position = text.find(word)
        if position >= 0:
            violations.append({"type": "banned_word", "text": word, "position": position})

    for match in _STOCK_CODE_RE.finditer(text):
        violations.append(
            {"type": "stock_code", "text": match.group(), "position": match.start()}
        )

    if DISCLAIMER not in text:
        violations.append({"type": "missing_disclaimer", "text": DISCLAIMER, "position": -1})

    if content_type == "weekly_report" and _has_causal_claim(text):
        violations.append(
            {"type": "causal_claim", "text": "卡点所以可投", "position": text.find("卡点")}
        )

    return {
        "pass": len(violations) == 0,
        "violations": violations,
        "requires_human_review": bool(violations) or content_type == "weekly_report",
    }


def filter_or_raise(text: str, content_type: str) -> str:
    """不合规即抛异常，需人工复核时记 warning 并原样返回。"""
    result = check_content(text, content_type)
    if not result["pass"]:
        raise ComplianceError(f"内容未通过合规审查: {result['violations']}")
    if result["requires_human_review"]:
        logger.warning("[compliance] %s 需人工复核: %s", content_type, text)
    return text


def _has_causal_claim(text: str) -> bool:
    """模糊识别“因为是卡点所以可投”类因果表述。"""
    return "卡点" in text and any(phrase in text for phrase in _CAUSAL_PHRASES)
