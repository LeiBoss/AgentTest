"""登录令牌与账号校验。"""

from __future__ import annotations

import pytest

from src.auth import auth_enabled, check_credentials, make_token, verify_token


def test_token_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_PASSWORD", "secret")
    token = make_token("admin")

    assert verify_token(token) is True
    assert verify_token(token + "x") is False
    assert verify_token("") is False
    assert verify_token(None) is False


def test_expired_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_PASSWORD", "secret")

    assert verify_token(make_token("admin", ttl=-1)) is False


def test_token_signed_with_other_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_PASSWORD", "secret")
    monkeypatch.setenv("SESSION_SECRET", "one")
    token = make_token("admin")
    monkeypatch.setenv("SESSION_SECRET", "two")

    assert verify_token(token) is False


def test_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_PASSWORD", "secret")
    monkeypatch.setenv("AUTH_USER", "admin")

    assert check_credentials("admin", "secret") is True
    assert check_credentials("admin", "wrong") is False
    assert check_credentials("other", "secret") is False


def test_auth_disabled_without_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AUTH_PASSWORD", raising=False)

    assert auth_enabled() is False
    assert check_credentials("anyone", "anything") is True
