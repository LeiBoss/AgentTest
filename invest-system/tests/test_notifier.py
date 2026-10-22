"""notifier.py 的签名与推送测试。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
from types import SimpleNamespace

import pytest

from src import notifier
from src.notifier import feishu_sign, push_feishu


def test_feishu_sign_matches_hmac_sha256() -> None:
    secret = "test-secret"
    timestamp = 1700000000
    expected = base64.b64encode(
        hmac.new(f"{timestamp}\n{secret}".encode(), digestmod=hashlib.sha256).digest()
    ).decode()

    assert feishu_sign(secret, timestamp) == expected


def test_push_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FEISHU_WEBHOOK", "https://example.com/hook")
    monkeypatch.setenv("FEISHU_SECRET", "test-secret")
    captured: dict = {}

    def fake_post(url: str, **kwargs: object) -> SimpleNamespace:
        captured["url"] = url
        captured["json"] = kwargs["json"]
        captured["timeout"] = kwargs["timeout"]
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(notifier.requests, "post", fake_post)

    assert push_feishu("标题", "正文") is True
    payload = captured["json"]
    assert captured["url"] == "https://example.com/hook"
    assert captured["timeout"] == 10
    assert payload["msg_type"] == "text"
    assert payload["content"]["text"] == "标题\n\n正文\n\n信息整理，不构成投资建议。"
    assert payload["sign"] == feishu_sign("test-secret", int(payload["timestamp"]))


def test_push_failure_logs(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("FEISHU_WEBHOOK", "https://example.com/hook")
    monkeypatch.setenv("FEISHU_SECRET", "test-secret")
    monkeypatch.setattr(
        notifier.requests, "post", lambda url, **kwargs: SimpleNamespace(status_code=400)
    )

    with caplog.at_level(logging.ERROR):
        assert push_feishu("标题", "正文") is False

    assert any("飞书推送失败" in record.message for record in caplog.records)


def test_missing_env_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FEISHU_WEBHOOK", raising=False)
    monkeypatch.delenv("FEISHU_SECRET", raising=False)

    with pytest.raises(EnvironmentError, match="FEISHU_WEBHOOK"):
        push_feishu("标题", "正文")
