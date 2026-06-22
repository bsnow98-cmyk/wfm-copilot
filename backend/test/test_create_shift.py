"""
Unit tests for Surface #6 (preview_create_shift).
The apply path rides POST /schedules/apply (gated to manager+, verified in the
RBAC integration suite); these cover the new change-set construction + guards.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from app.tools import preview_create_shift


def _agent_row(value):
    m = MagicMock()
    m.mappings.return_value.one_or_none.return_value = value
    return m


def test_bad_window_rejected() -> None:
    db = MagicMock()
    out = preview_create_shift.handler(
        {"employee_id": "E1", "date": "2099-05-01", "start_time": "13:00", "end_time": "12:00"},
        db,
    )
    assert out["render"] == "error" and out["code"] == "BAD_WINDOW"


def test_unknown_agent_rejected() -> None:
    db = MagicMock()
    db.execute.return_value = _agent_row(None)
    out = preview_create_shift.handler({"employee_id": "NOPE", "date": "2099-05-01"}, db)
    assert out["render"] == "error" and out["code"] == "NO_AGENT"


def test_builds_work_lunch_work(monkeypatch) -> None:
    captured: dict = {}

    def fake_preview(args, db):
        captured["args"] = args
        return {"render": "gantt", "date": args["date"], "agents": [], "apply_token": "tok"}

    monkeypatch.setattr("app.tools.preview_schedule_change.handler", fake_preview)

    db = MagicMock()
    db.execute.return_value = _agent_row({"full_name": "Adams"})
    out = preview_create_shift.handler(
        {"employee_id": "E1", "date": "2099-05-01"}, db
    )
    assert out["apply_token"] == "tok"
    changes = captured["args"]["changes"]
    acts = [c["activity"] for c in changes]
    assert acts == ["available", "lunch", "available"]
    # default 09:00–17:00 with 12:00–12:30 lunch
    assert changes[0]["start"].endswith("09:00:00+00:00")
    assert changes[1]["start"].endswith("12:00:00+00:00")
    assert changes[1]["end"].endswith("12:30:00+00:00")
    assert changes[2]["end"].endswith("17:00:00+00:00")
