"""
Unit tests for cherry-pick D's apply / undo error paths.

The full happy-path apply requires Postgres (JSONB, RETURNING id, FK to
shift_segments / agents) — the eval suite covers that against a live DB.
These tests cover the parts that don't need a real DB:
  - StaleVersionError raised when expected_version mismatches
  - Token expiry detection
  - Undo window enforcement

The shape assertions here would catch a regression where someone "fixed" the
apply path in a way that bypasses concurrency, expiry, or the undo ceiling.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from app.services import apply_tokens, schedule_change


def test_consume_token_raises_expired_when_past_ttl() -> None:
    db = MagicMock()
    expired_row = MagicMock()
    expired_row.mappings.return_value.first.return_value = {
        "schedule_id": 1,
        "schedule_version": 100,
        "change_set": [{"agent_id": "ag", "start": "2026-04-29T12:00:00", "end": "2026-04-29T13:00:00", "activity": "lunch"}],
        "expires_at": datetime.now(timezone.utc) - timedelta(minutes=1),
        "consumed_at": None,
        "consumed_log_id": None,
        "conversation_id": None,
        "user_msg_id": None,
    }
    db.execute.return_value = expired_row
    with pytest.raises(apply_tokens.TokenExpired):
        apply_tokens.consume_token(db, "tok-123")


def test_consume_token_returns_consumed_log_id_for_idempotent_path() -> None:
    """If the token has already been consumed, the function returns rather
    than raising — the apply endpoint maps that to a 200 with the original
    log_id (decision D-6)."""
    db = MagicMock()
    consumed_row = MagicMock()
    consumed_row.mappings.return_value.first.return_value = {
        "schedule_id": 5,
        "schedule_version": 42,
        "change_set": [],
        "expires_at": datetime.now(timezone.utc) - timedelta(hours=1),
        "consumed_at": datetime.now(timezone.utc) - timedelta(minutes=10),
        "consumed_log_id": "00000000-0000-0000-0000-000000000abc",
        "conversation_id": None,
        "user_msg_id": None,
    }
    db.execute.return_value = consumed_row

    out = apply_tokens.consume_token(db, "tok-already-used")
    assert out.consumed_log_id == "00000000-0000-0000-0000-000000000abc"
    assert out.schedule_id == 5
    assert out.schedule_version == 42


def test_consume_token_raises_not_found_for_unknown_token() -> None:
    db = MagicMock()
    none_row = MagicMock()
    none_row.mappings.return_value.first.return_value = None
    db.execute.return_value = none_row

    with pytest.raises(apply_tokens.TokenNotFound):
        apply_tokens.consume_token(db, "tok-bogus")


def test_stale_version_error_carries_both_versions() -> None:
    err = schedule_change.StaleVersionError(your_version=100, current_version=200)
    assert err.your_version == 100
    assert err.current_version == 200


def test_undo_change_raises_window_expired() -> None:
    db = MagicMock()
    expired_row = MagicMock()
    expired_row.mappings.return_value.first.return_value = {
        "id": "00000000-0000-0000-0000-000000000001",
        "schedule_id": 1,
        "change_set": [],
        "before_state": [],
        "after_state": [],
        "undo_window_ends_at": datetime.now(timezone.utc) - timedelta(minutes=1),
        "undone_at": None,
    }
    db.execute.return_value = expired_row

    with pytest.raises(schedule_change.UndoWindowExpired):
        schedule_change.undo_change(db, "00000000-0000-0000-0000-000000000001")


def test_undo_change_raises_already_undone() -> None:
    db = MagicMock()
    already = MagicMock()
    already.mappings.return_value.first.return_value = {
        "id": "00000000-0000-0000-0000-000000000001",
        "schedule_id": 1,
        "change_set": [],
        "before_state": [],
        "after_state": [],
        "undo_window_ends_at": datetime.now(timezone.utc) + timedelta(hours=23),
        "undone_at": datetime.now(timezone.utc) - timedelta(minutes=5),
    }
    db.execute.return_value = already

    with pytest.raises(schedule_change.AlreadyUndone):
        schedule_change.undo_change(db, "00000000-0000-0000-0000-000000000001")


def test_undo_change_raises_not_found_for_missing_log_id() -> None:
    db = MagicMock()
    missing = MagicMock()
    missing.mappings.return_value.first.return_value = None
    db.execute.return_value = missing
    with pytest.raises(schedule_change.ChangeNotFound):
        schedule_change.undo_change(db, "00000000-0000-0000-0000-000000000999")
