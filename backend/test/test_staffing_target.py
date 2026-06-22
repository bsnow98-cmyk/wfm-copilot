"""
Unit tests for Surface #5 (staffing_target) token + version + undo paths.
DB-touching apply/recompute/undo live in the Postgres integration suite.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from app.services import apply_tokens, staffing_target as ST


def _token_row(**over):
    base = {
        "change_set": {
            "staffing_id": 5,
            "new_targets": {"sl": 0.85},
            "expected_version": 123,
        },
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=4),
        "consumed_at": None,
        "consumed_staffing_log_id": None,
        "conversation_id": None,
        "user_msg_id": None,
        "target_kind": "staffing_target",
    }
    base.update(over)
    return base


def test_consume_staffing_token_expired() -> None:
    db = MagicMock()
    row = MagicMock()
    row.mappings.return_value.first.return_value = _token_row(
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1)
    )
    db.execute.return_value = row
    with pytest.raises(apply_tokens.TokenExpired):
        apply_tokens.consume_staffing_token(db, "t")


def test_consume_staffing_token_idempotent() -> None:
    db = MagicMock()
    row = MagicMock()
    row.mappings.return_value.first.return_value = _token_row(
        consumed_at=datetime.now(timezone.utc),
        consumed_staffing_log_id="00000000-0000-0000-0000-0000000000aa",
        expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    db.execute.return_value = row
    out = apply_tokens.consume_staffing_token(db, "t")
    assert out.consumed_log_id == "00000000-0000-0000-0000-0000000000aa"
    assert out.staffing_id == 5 and out.new_targets == {"sl": 0.85}


def test_consume_staffing_token_rejects_wrong_kind() -> None:
    db = MagicMock()
    row = MagicMock()
    row.mappings.return_value.first.return_value = _token_row(target_kind="offer")
    db.execute.return_value = row
    with pytest.raises(apply_tokens.TokenNotFound):
        apply_tokens.consume_staffing_token(db, "t")


def test_targets_version_stable_and_sensitive() -> None:
    a = {"sl": 0.8, "target_answer_seconds": 20, "target_asa_seconds": 30, "shrinkage": 0.3}
    b = {"sl": 0.85, "target_answer_seconds": 20, "target_asa_seconds": 30, "shrinkage": 0.3}
    assert ST.compute_targets_version(a) == ST.compute_targets_version(dict(a))
    assert ST.compute_targets_version(a) != ST.compute_targets_version(b)


def test_summarize_targets_reads_clearly() -> None:
    before = {"sl": 0.8, "target_answer_seconds": 20, "target_asa_seconds": 30, "shrinkage": 0.3}
    after = {"sl": 0.85, "target_answer_seconds": 20, "target_asa_seconds": 20, "shrinkage": 0.3}
    s = ST.summarize_targets(before, after)
    assert "80%/20s" in s and "85%/20s" in s and "ASA≤20s" in s


def test_stale_version_error_carries_staffing_id() -> None:
    e = ST.StaleVersionError(1, 2, staffing_id=5)
    assert e.your_version == 1 and e.current_version == 2 and e.staffing_id == 5


def test_undo_window_expired() -> None:
    db = MagicMock()
    row = MagicMock()
    row.mappings.return_value.first.return_value = {
        "id": "00000000-0000-0000-0000-000000000001",
        "staffing_id": 5,
        "before_targets": {"sl": 0.8},
        "undo_window_ends_at": datetime.now(timezone.utc) - timedelta(minutes=1),
        "undone_at": None,
    }
    db.execute.return_value = row
    with pytest.raises(ST.UndoWindowExpired):
        ST.undo_target_change(db, "00000000-0000-0000-0000-000000000001")


def test_undo_already_undone() -> None:
    db = MagicMock()
    row = MagicMock()
    row.mappings.return_value.first.return_value = {
        "id": "00000000-0000-0000-0000-000000000001",
        "staffing_id": 5,
        "before_targets": {"sl": 0.8},
        "undo_window_ends_at": datetime.now(timezone.utc) + timedelta(hours=23),
        "undone_at": datetime.now(timezone.utc) - timedelta(minutes=5),
    }
    db.execute.return_value = row
    with pytest.raises(ST.AlreadyUndone):
        ST.undo_target_change(db, "00000000-0000-0000-0000-000000000001")


def test_undo_not_found() -> None:
    db = MagicMock()
    row = MagicMock()
    row.mappings.return_value.first.return_value = None
    db.execute.return_value = row
    with pytest.raises(ST.ChangeNotFound):
        ST.undo_target_change(db, "00000000-0000-0000-0000-000000000999")
