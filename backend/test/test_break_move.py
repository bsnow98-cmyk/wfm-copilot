"""
Unit tests for Surface #3 (preview_break_move).

preview_break_move delegates the write-preview to preview_schedule_change (which
commits its token), so the happy path is verified end-to-end in the live app.
These mock the DB to lock in break resolution + the two-segment change-set shape
(free the old slot, place the break at the new time).
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

from app.tools import preview_break_move


def _result(value):
    m = MagicMock()
    m.mappings.return_value.one_or_none.return_value = value
    return m


def test_unknown_agent_returns_error() -> None:
    db = MagicMock()
    db.execute.return_value = _result(None)  # agent lookup → none
    out = preview_break_move.handler(
        {"employee_id": "NOPE", "direction": "earlier"}, db
    )
    assert out["render"] == "error" and out["code"] == "NO_AGENT"


def test_bad_direction_returns_error() -> None:
    db = MagicMock()
    out = preview_break_move.handler(
        {"employee_id": "EMP1", "direction": "sideways"}, db
    )
    assert out["render"] == "error" and out["code"] == "BAD_ARGS"


def test_no_break_returns_error() -> None:
    db = MagicMock()
    db.execute.side_effect = [
        _result({"id": 1, "full_name": "Adams"}),  # agent
        _result(None),  # break lookup (date branch) → none
    ]
    out = preview_break_move.handler(
        {"employee_id": "EMP1", "direction": "later", "date": "2026-06-10"}, db
    )
    assert out["render"] == "error" and out["code"] == "NO_BREAK"


def test_builds_two_segment_change_set(monkeypatch) -> None:
    """later/30m moves a 12:00–12:30 break to 12:30–13:00 and frees the old
    window back to 'available'."""
    captured: dict = {}

    def fake_preview(args, db):
        captured["args"] = args
        return {"render": "gantt", "date": args["date"], "agents": [], "apply_token": "tok"}

    monkeypatch.setattr(
        "app.tools.preview_schedule_change.handler", fake_preview
    )

    db = MagicMock()
    db.execute.side_effect = [
        _result({"id": 1, "full_name": "Adams"}),  # agent
        _result(
            {
                "start_time": datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc),
                "end_time": datetime(2026, 6, 10, 12, 30, tzinfo=timezone.utc),
            }
        ),  # break (date branch)
    ]
    out = preview_break_move.handler(
        {"employee_id": "EMP1", "direction": "later", "minutes": 30, "date": "2026-06-10"},
        db,
    )
    assert out["apply_token"] == "tok"
    changes = captured["args"]["changes"]
    assert captured["args"]["date"] == "2026-06-10"
    # First change frees the old break window to work.
    assert changes[0]["activity"] == "available"
    assert changes[0]["start"].endswith("12:00:00+00:00")
    assert changes[0]["end"].endswith("12:30:00+00:00")
    # Second change places the break 30 min later.
    assert changes[1]["activity"] == "break"
    assert changes[1]["start"].endswith("12:30:00+00:00")
    assert changes[1]["end"].endswith("13:00:00+00:00")
