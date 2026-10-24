"""Thesis 账本与催化剂日历。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from src.db import init_db
from src.thesis import (
    ThesisError,
    create_catalyst,
    create_thesis,
    set_catalyst_status,
    set_thesis_status,
    thesis_board,
    validate_catalyst,
    validate_thesis,
)

TODAY = date(2026, 10, 24)


@pytest.fixture()
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = init_db(str(tmp_path / "invest.db"))
    yield connection
    connection.close()


def thesis_payload(**overrides: Any) -> dict[str, Any]:
    payload = {
        "title": "批价下行将传导至报表",
        "direction": "down",
        "horizon": "未来两个季度",
        "core_logic": "渠道利润压缩后，打款意愿下降，合同负债先走弱。",
        "confirm_condition": "合同负债连续两期同比为负",
        "invalidate_condition": "批价回升且打款意愿恢复",
        "confidence": 60,
    }
    payload.update(overrides)
    return payload


def catalyst_payload(**overrides: Any) -> dict[str, Any]:
    payload = {
        "event_date": "2026-10-28",
        "title": "三季报披露",
        "kind": "earnings",
        "related": "酒企报表",
        "expectation": "看合同负债与预收款环比",
    }
    payload.update(overrides)
    return payload


def test_empty_board(conn: sqlite3.Connection) -> None:
    board = thesis_board(conn, "白酒", TODAY)

    assert board["theses"] == []
    assert board["catalysts"] == []
    assert board["stats"] == {"active": 0, "confirmed": 0, "invalidated": 0}
    assert board["catalyst_stats"] == {"pending": 0, "done": 0, "missed": 0, "soon": 0, "overdue": 0}


def test_create_thesis_returns_active_with_labels(conn: sqlite3.Connection) -> None:
    board = create_thesis(conn, "白酒", thesis_payload(), TODAY)

    item = board["theses"][0]
    assert item["status"] == "active"
    assert item["status_label"] == "待验证"
    assert item["direction_label"] == "下行"
    assert item["confidence"] == 60
    assert item["age_days"] == 0
    assert board["stats"]["active"] == 1


def test_board_keeps_active_first_and_counts_status(conn: sqlite3.Connection) -> None:
    create_thesis(conn, "白酒", thesis_payload(title="第一条"), TODAY)
    second = create_thesis(conn, "白酒", thesis_payload(title="第二条"), TODAY)
    closed = set_thesis_status(conn, "白酒", second["theses"][0]["id"], {"status": "confirmed"}, TODAY)

    assert [item["title"] for item in closed["theses"]] == ["第一条", "第二条"]
    assert closed["stats"] == {"active": 1, "confirmed": 1, "invalidated": 0}


@pytest.mark.parametrize(
    "field",
    ["title", "core_logic", "confirm_condition", "invalidate_condition"],
)
def test_thesis_requires_falsifiable_fields(field: str) -> None:
    with pytest.raises(ThesisError):
        validate_thesis(thesis_payload(**{field: "  "}))


@pytest.mark.parametrize(
    ("field", "value"),
    [("direction", "sideways"), ("confidence", 120), ("confidence", "高")],
)
def test_thesis_rejects_bad_enums(field: str, value: Any) -> None:
    with pytest.raises(ThesisError):
        validate_thesis(thesis_payload(**{field: value}))


def test_thesis_rejects_overlong_title() -> None:
    with pytest.raises(ThesisError):
        validate_thesis(thesis_payload(title="判" * 81))


def test_set_thesis_status_rejects_unknown_id(conn: sqlite3.Connection) -> None:
    with pytest.raises(ThesisError):
        set_thesis_status(conn, "白酒", 999, {"status": "confirmed"}, TODAY)

    create_thesis(conn, "白酒", thesis_payload(), TODAY)
    with pytest.raises(ThesisError):
        set_thesis_status(conn, "啤酒", 1, {"status": "confirmed"}, TODAY)


def test_set_thesis_status_rejects_unknown_status(conn: sqlite3.Connection) -> None:
    created = create_thesis(conn, "白酒", thesis_payload(), TODAY)
    thesis_id = created["theses"][0]["id"]

    with pytest.raises(ThesisError):
        set_thesis_status(conn, "白酒", thesis_id, {"status": "done"}, TODAY)


def test_catalyst_timing_and_ordering(conn: sqlite3.Connection) -> None:
    create_catalyst(conn, "白酒", catalyst_payload(event_date="2026-11-30", title="年度配额"), TODAY)
    create_catalyst(conn, "白酒", catalyst_payload(event_date="2026-10-20", title="批价窗口"), TODAY)
    create_catalyst(conn, "白酒", catalyst_payload(event_date="2026-10-24", title="经销商大会"), TODAY)
    create_catalyst(conn, "白酒", catalyst_payload(event_date="2026-10-28", title="三季报披露"), TODAY)
    done = create_catalyst(conn, "白酒", catalyst_payload(event_date="2026-09-01", title="半年报"), TODAY)
    done_id = next(item["id"] for item in done["catalysts"] if item["title"] == "半年报")
    set_catalyst_status(conn, "白酒", done_id, {"status": "done"}, TODAY)

    board = thesis_board(conn, "白酒", TODAY)
    phases = {item["title"]: item["phase"] for item in board["catalysts"]}
    labels = {item["title"]: item["timing_label"] for item in board["catalysts"]}

    assert phases["批价窗口"] == "overdue"
    assert labels["批价窗口"] == "逾期 4 天"
    assert phases["经销商大会"] == "today"
    assert phases["三季报披露"] == "soon"
    assert labels["三季报披露"] == "4 天后"
    assert phases["年度配额"] == "later"
    assert phases["半年报"] == "done"

    pending_titles = [item["title"] for item in board["catalysts"] if item["status"] == "pending"]
    assert pending_titles == ["批价窗口", "经销商大会", "三季报披露", "年度配额"]
    assert board["catalysts"][-1]["title"] == "半年报"
    assert board["catalyst_stats"] == {"pending": 4, "done": 1, "missed": 0, "soon": 1, "overdue": 1}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("event_date", "2026/10/28"),
        ("event_date", ""),
        ("title", " "),
        ("kind", "unknown"),
    ],
)
def test_catalyst_rejects_bad_input(field: str, value: str) -> None:
    with pytest.raises(ThesisError):
        validate_catalyst(catalyst_payload(**{field: value}))


def test_catalyst_kind_defaults_to_other() -> None:
    validated = validate_catalyst({**catalyst_payload(), "kind": ""})

    assert validated["kind"] == "other"


def test_set_catalyst_status_rejects_unknown_id(conn: sqlite3.Connection) -> None:
    with pytest.raises(ThesisError):
        set_catalyst_status(conn, "白酒", 42, {"status": "done"}, TODAY)


def test_board_is_scoped_by_chain(conn: sqlite3.Connection) -> None:
    create_thesis(conn, "白酒", thesis_payload(), TODAY)
    create_catalyst(conn, "白酒", catalyst_payload(), TODAY)

    board = thesis_board(conn, "啤酒", TODAY)

    assert board["theses"] == []
    assert board["catalysts"] == []
