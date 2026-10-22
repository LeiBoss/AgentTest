"""DeepSeek 调用封装与 evidence 校验。"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from openai import OpenAI

from .models import Trigger
from .prompts import P4_SYSTEM

logger = logging.getLogger(__name__)

MODEL = "deepseek-chat"
BASE_URL = "https://api.deepseek.com"


class LLMError(RuntimeError):
    """LLM 调用或解析失败。"""


def call_llm(system_prompt: str, user_payload: dict, max_retries: int = 2) -> dict:
    """调用 DeepSeek 并返回 JSON 对象，失败重试后抛 LLMError。"""
    client = OpenAI(api_key=os.getenv("DEEPSEEK_API_KEY"), base_url=BASE_URL)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
    ]
    last_error: Exception | None = None

    for _ in range(max_retries + 1):
        try:
            response = client.chat.completions.create(
                model=MODEL,
                messages=messages,
                response_format={"type": "json_object"},
            )
        except Exception as exc:  # noqa: BLE001 - 网络类异常统一重试
            last_error = exc
            continue

        usage = getattr(response, "usage", None)
        logger.info(
            "[llm] %s tokens_in=%s tokens_out=%s",
            "call_llm",
            getattr(usage, "prompt_tokens", 0),
            getattr(usage, "completion_tokens", 0),
        )

        content = response.choices[0].message.content
        if not content:
            last_error = ValueError("LLM 返回空内容")
            continue
        try:
            return json.loads(content)
        except json.JSONDecodeError as exc:
            last_error = exc

    raise LLMError(f"LLM 调用失败: {last_error}")


def validate_evidence(result: dict) -> bool:
    """evidence 必须是非空字符串列表。"""
    evidence = result.get("evidence")
    return isinstance(evidence, list) and len(evidence) > 0


def interpret_transition(trigger: Trigger, transition: dict, history: list) -> dict | None:
    """解读单次状态迁移，无效输出返回 None。"""
    payload: dict[str, Any] = {
        "trigger": {
            "id": trigger.id,
            "name": trigger.name,
            "layer": trigger.layer,
            "unit": trigger.unit,
            "rule": trigger.rule,
            "meaning": trigger.meaning,
        },
        "transition": transition,
        "recent_history": [
            {"value": value, "date": date} for value, date in history
        ],
    }

    try:
        result = call_llm(P4_SYSTEM, payload)
    except LLMError as exc:
        logger.warning("[llm] interpret_transition failed: %s", exc)
        return None

    if not isinstance(result, dict) or not validate_evidence(result):
        logger.warning("[llm] interpret_transition invalid evidence: %s", trigger.id)
        return None

    if result.get("trigger_id") != trigger.id:
        logger.warning(
            "[llm] interpret_transition trigger_id mismatch: %s != %s",
            result.get("trigger_id"),
            trigger.id,
        )
        return None

    return result
