"""
Unit tests for Surface #2 (offer) token + retract error paths.

Mirrors test_leave_decision.py: DB-touching publish/retract happy paths live in
the Postgres integration suite; these mock the DB to lock in token typing,
expiry, idempotency, and the retract-ceiling guarantees.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from app.services import apply_tokens, offer


def _offer_token_row(**over):
    base = {
        "change_set": {"kind": "ot", "targets": [{"employee_id": "EMP1"}], "slots": 1},
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=4),
        "consumed_at": None,
        "consumed_offer_id": None,
        "conversation_id": None,
        "user_msg_id": None,
        "target_kind": "offer",
    }
    base.update(over)
    return base


def test_consume_offer_token_expired() -> None:
    db = MagicMock()
    row = MagicMock()
    row.mappings.return_value.first.return_value = _offer_token_row(
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1)
    )
    db.execute.return_value = row
    with pytest.raises(apply_tokens.TokenExpired):
        apply_tokens.consume_offer_token(db, "tok")


def test_consume_offer_token_idempotent() -> None:
    db = MagicMock()
    row = MagicMock()
    row.mappings.return_value.first.return_value = _offer_token_row(
        consumed_at=datetime.now(timezone.utc),
        consumed_offer_id=77,
        expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    db.execute.return_value = row
    out = apply_tokens.consume_offer_token(db, "tok")
    assert out.consumed_offer_id == 77
    assert out.spec["kind"] == "ot"


def test_consume_offer_token_rejects_wrong_kind() -> None:
    db = MagicMock()
    row = MagicMock()
    row.mappings.return_value.first.return_value = _offer_token_row(target_kind="leave")
    db.execute.return_value = row
    with pytest.raises(apply_tokens.TokenNotFound):
        apply_tokens.consume_offer_token(db, "tok")


def test_summarize_offer_counts_targets() -> None:
    s = offer.summarize_offer(
        {
            "kind": "ot",
            "targets": [{"employee_id": "A"}, {"employee_id": "B"}],
            "slots": 2,
            "target_date": "2026-06-09",
            "window_start": "2026-06-09T09:00:00+00:00",
            "window_end": "2026-06-09T12:00:00+00:00",
        }
    )
    assert "OT" in s and "2 agent" in s and "09:00–12:00" in s


def test_retract_raises_window_expired() -> None:
    db = MagicMock()
    row = MagicMock()
    row.mappings.return_value.first.return_value = {
        "id": 1,
        "kind": "ot",
        "status": "open",
        "undo_window_ends_at": datetime.now(timezone.utc) - timedelta(minutes=1),
        "retracted_at": None,
        "targets": [],
    }
    db.execute.return_value = row
    with pytest.raises(offer.RetractWindowExpired):
        offer.retract_offer(db, 1)


def test_retract_raises_already_retracted() -> None:
    db = MagicMock()
    row = MagicMock()
    row.mappings.return_value.first.return_value = {
        "id": 1,
        "kind": "ot",
        "status": "retracted",
        "undo_window_ends_at": datetime.now(timezone.utc) + timedelta(hours=23),
        "retracted_at": datetime.now(timezone.utc) - timedelta(minutes=5),
        "targets": [],
    }
    db.execute.return_value = row
    with pytest.raises(offer.AlreadyRetracted):
        offer.retract_offer(db, 1)


def test_retract_raises_not_found() -> None:
    db = MagicMock()
    row = MagicMock()
    row.mappings.return_value.first.return_value = None
    db.execute.return_value = row
    with pytest.raises(offer.OfferNotFound):
        offer.retract_offer(db, 999)
