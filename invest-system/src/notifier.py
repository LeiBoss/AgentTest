"""飞书 Webhook 签名与推送。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import time

import requests

logger = logging.getLogger(__name__)


def feishu_sign(secret: str, timestamp: int) -> str:
    """按 timestamp + secret 生成 HMAC-SHA256 签名。"""
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def push_feishu(title: str, body: str, tail: str = "信息整理，不构成投资建议。") -> bool:
    """推送文本消息到飞书，成功返回 True。"""
    webhook = os.getenv("FEISHU_WEBHOOK")
    secret = os.getenv("FEISHU_SECRET")
    if not webhook or not secret:
        raise EnvironmentError("缺少环境变量 FEISHU_WEBHOOK 或 FEISHU_SECRET")

    timestamp = int(time.time())
    payload = {
        "timestamp": str(timestamp),
        "sign": feishu_sign(secret, timestamp),
        "msg_type": "text",
        "content": {"text": f"{title}\n\n{body}\n\n{tail}"},
    }

    try:
        response = requests.post(webhook, json=payload, timeout=10)
    except requests.RequestException as exc:
        logger.error("[notifier] 飞书推送异常: %s", exc)
        return False

    if response.status_code != 200:
        logger.error("[notifier] 飞书推送失败: status=%s", response.status_code)
        return False

    return True
