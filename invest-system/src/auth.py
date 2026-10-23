"""单账号登录：签名会话 Cookie 的签发与校验。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time

SESSION_COOKIE = "invest_session"
SESSION_TTL = 7 * 24 * 3600


def auth_enabled() -> bool:
    """配置了 AUTH_PASSWORD 才启用登录，否则放行（本地开发用）。"""
    return bool(os.getenv("AUTH_PASSWORD"))


def auth_user() -> str:
    return os.getenv("AUTH_USER", "admin")


def _secret() -> bytes:
    secret = os.getenv("SESSION_SECRET")
    if secret:
        return secret.encode("utf-8")
    # 未显式配置时用密码派生，密码变更即让旧会话失效
    return hashlib.sha256(f"invest:{os.getenv('AUTH_PASSWORD', '')}".encode("utf-8")).digest()


def _sign(payload: bytes) -> str:
    return hmac.new(_secret(), payload, hashlib.sha256).hexdigest()


def make_token(user: str | None = None, ttl: int = SESSION_TTL) -> str:
    """签发 `base64url(payload).hmac` 形式的会话令牌。"""
    payload = json.dumps(
        {"u": user or auth_user(), "exp": int(time.time()) + ttl},
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    return f"{encoded}.{_sign(payload)}"


def verify_token(token: str | None) -> bool:
    """校验签名与有效期。"""
    if not token or "." not in token:
        return False
    encoded, signature = token.rsplit(".", 1)
    try:
        padded = encoded + "=" * (-len(encoded) % 4)
        payload = base64.urlsafe_b64decode(padded.encode("ascii"))
    except (ValueError, UnicodeEncodeError):
        return False
    if not hmac.compare_digest(_sign(payload), signature):
        return False
    try:
        data = json.loads(payload)
        return int(data["exp"]) > int(time.time())
    except (ValueError, KeyError, TypeError):
        return False


def check_credentials(user: str, password: str) -> bool:
    """常量时间比对账号与密码。"""
    if not auth_enabled():
        return True
    ok_user = hmac.compare_digest(user, auth_user())
    ok_password = hmac.compare_digest(password, os.getenv("AUTH_PASSWORD", ""))
    return ok_user and ok_password
