"""compliance.py 的审查规则测试。"""

from __future__ import annotations

import logging

import pytest

from src.compliance import ComplianceError, check_content, filter_or_raise


def test_banned_word_detected() -> None:
    result = check_content("建议关注该环节的批价变化。不构成投资建议。", "daily_report")

    assert result["pass"] is False
    assert any(item["type"] == "banned_word" for item in result["violations"])


def test_stock_code_detected() -> None:
    result = check_content("600519 的批价回落至观察区间。不构成投资建议。", "daily_report")

    assert result["pass"] is False
    assert any(item["text"] == "600519" for item in result["violations"])


def test_missing_disclaimer_detected() -> None:
    result = check_content("批价进入观察区间，渠道库存上升。", "daily_report")

    assert result["pass"] is False
    assert any(item["type"] == "missing_disclaimer" for item in result["violations"])


def test_weekly_causal_claim_detected() -> None:
    text = "该环节是卡点，所以可投。不构成投资建议。"
    result = check_content(text, "weekly_report")

    assert result["pass"] is False
    assert any(item["type"] == "causal_claim" for item in result["violations"])


def test_compliant_text_passes() -> None:
    text = "批价进入观察区间，渠道库存上升，需持续跟踪。不构成投资建议。"

    result = check_content(text, "daily_report")

    assert result["pass"] is True
    assert result["violations"] == []
    assert result["requires_human_review"] is False
    assert filter_or_raise(text, "daily_report") == text


def test_filter_or_raise_rejects() -> None:
    with pytest.raises(ComplianceError, match="合规审查"):
        filter_or_raise("建议关注该环节。不构成投资建议。", "daily_report")


def test_weekly_report_requires_human_review(caplog: pytest.LogCaptureFixture) -> None:
    text = "批价进入观察区间，需持续跟踪。不构成投资建议。"

    with caplog.at_level(logging.WARNING):
        assert filter_or_raise(text, "weekly_report") == text

    assert any("需人工复核" in record.message for record in caplog.records)
