"""
Unit tests for Surface #1 (leave-decision) token + version + undo error paths.

Mirrors test_schedule_apply.py: the DB-touching happy path is covered by the
Postgres integration suite; these mock the DB to lock in the concurrency,
expiry, idempotency, and undo-ceiling guarantees.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from app.services import apply_tokens, leave_decision


def _leave_token_row(**over):
    base = {
        "change_set": {
            "request_id": 3,
            "decision": "approve",
            "note": "x",
            "request_version": 42,
        },
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=4),
        "consumed_at": None,
        "consumed_leave_log_id": None,
        "conversation_id": None,
        "user_msg_id": None,
        "target_kind": "leave",
    }
    base.update(over)
    return base


def test_consume_leave_token_raises_expired_past_ttl() -> None:
    db = MagicMock()
    row = MagicMock()
    row.mappings.return_value.first.return_value = _leave_token_row(
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1)
    )
    db.execute.return_value = row
    with pytest.raises(apply_tokens.TokenExpired):
        apply_tokens.consume_leave_token(db, "tok-123")


def test_consume_leave_token_idempotent_returns_log_id() -> None:
    db = MagicMock()
    row = MagicMock()
    row.mappings.return_value.first.return_value = _leave_token_row(
        consumed_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        consumed_leave_log_id="00000000-0000-0000-0000-0000000000ab",
        expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    db.execute.return_value = row
    out = apply_tokens.consume_leave_token(db, "tok-used")
    assert out.consumed_log_id == "00000000-0000-0000-0000-0000000000ab"
    assert out.request_id == 3 and out.decision == "approve" and out.request_version == 42


def test_consume_leave_token_not_found() -> None:
    db = MagicMock()
    row = MagicMock()
    row.mappings.return_value.first.return_value = None
    db.execute.return_value = row
    with pytest.raises(apply_tokens.TokenNotFound):
        apply_tokens.consume_leave_token(db, "tok-bogus")


def test_consume_leave_token_rejects_schedule_token() -> None:
    db = MagicMock()
    row = MagicMock()
    row.mappings.return_value.first.return_value = _leave_token_row(target_kind="schedule")
    db.execute.return_value = row
    with pytest.raises(apply_tokens.TokenNotFound):
        apply_tokens.consume_leave_token(db, "tok-sched")


def test_stale_version_error_carries_both_versions() -> None:
    err = leave_decision.StaleVersionError(your_version=10, current_version=20)
    assert err.your_version == 10 and err.current_version == 20


def test_compute_request_version_is_stable_and_changes_on_decide() -> None:
    pending = leave_decision.compute_request_version("pending", None)
    assert pending == leave_decision.compute_request_version("pending", None)
    decided = leave_decision.compute_request_version(
        "approved", datetime(2026, 6, 9, tzinfo=timezone.utc)
    )
    assert pending != decided


def test_leave_pto_hours_inclusive_days() -> None:
    start = datetime(2026, 6, 14, 9, tzinfo=timezone.utc)
    end = datetime(2026, 6, 18, 17, tzinfo=timezone.utc)  # 5 calendar days
    assert leave_decision.leave_pto_hours(start, end) == 5 * 8.0


def test_undo_raises_window_expired() -> None:
    db = MagicMock()
    row = MagicMock()
    row.mappings.return_value.first.return_value = {
        "id": "00000000-0000-0000-0000-000000000001",
        "request_id": 3,
        "decision": "approve",
        "before_state": {},
        "after_state": {},
        "ledger_event_id": None,
        "undo_window_ends_at": datetime.now(timezone.utc) - timedelta(minutes=1),
        "undone_at": None,
    }
    db.execute.return_value = row
    with pytest.raises(leave_decision.UndoWindowExpired):
        leave_decision.undo_decision(db, "00000000-0000-0000-0000-000000000001")


def test_undo_raises_already_undone() -> None:
    db = MagicMock()
    row = MagicMock()
    row.mappings.return_value.first.return_value = {
        "id": "00000000-0000-0000-0000-000000000001",
        "request_id": 3,
        "decision": "approve",
        "before_state": {},
        "after_state": {},
        "ledger_event_id": None,
        "undo_window_ends_at": datetime.now(timezone.utc) + timedelta(hours=23),
        "undone_at": datetime.now(timezone.utc) - timedelta(minutes=5),
    }
    db.execute.return_value = row
    with pytest.raises(leave_decision.AlreadyUndone):
        leave_decision.undo_decision(db, "00000000-0000-0000-0000-000000000001")


def test_undo_raises_not_found() -> None:
    db = MagicMock()
    row = MagicMock()
    row.mappings.return_value.first.return_value = None
    db.execute.return_value = row
    with pytest.raises(leave_decision.ChangeNotFound):
        leave_decision.undo_decision(db, "00000000-0000-0000-0000-000000000999")
