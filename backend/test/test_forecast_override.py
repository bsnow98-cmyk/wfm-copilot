"""
Unit tests for Surface #4 (forecast_override) token + version + undo paths.
DB-touching apply/undo happy paths live in the Postgres integration suite.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from app.services import apply_tokens, forecast_override


def _token_row(**over):
    base = {
        "change_set": {
            "forecast_run_id": 21,
            "interval_start": "2026-06-04T07:00:00+00:00",
            "new_value": 51.0,
            "expected_version": 123,
        },
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=4),
        "consumed_at": None,
        "consumed_forecast_log_id": None,
        "conversation_id": None,
        "user_msg_id": None,
        "target_kind": "forecast_override",
    }
    base.update(over)
    return base


def test_consume_forecast_token_expired() -> None:
    db = MagicMock()
    row = MagicMock()
    row.mappings.return_value.first.return_value = _token_row(
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1)
    )
    db.execute.return_value = row
    with pytest.raises(apply_tokens.TokenExpired):
        apply_tokens.consume_forecast_token(db, "t")


def test_consume_forecast_token_idempotent() -> None:
    db = MagicMock()
    row = MagicMock()
    row.mappings.return_value.first.return_value = _token_row(
        consumed_at=datetime.now(timezone.utc),
        consumed_forecast_log_id="00000000-0000-0000-0000-0000000000ff",
        expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    db.execute.return_value = row
    out = apply_tokens.consume_forecast_token(db, "t")
    assert out.consumed_log_id == "00000000-0000-0000-0000-0000000000ff"
    assert out.forecast_run_id == 21 and out.new_value == 51.0


def test_consume_forecast_token_rejects_wrong_kind() -> None:
    db = MagicMock()
    row = MagicMock()
    row.mappings.return_value.first.return_value = _token_row(target_kind="offer")
    db.execute.return_value = row
    with pytest.raises(apply_tokens.TokenNotFound):
        apply_tokens.consume_forecast_token(db, "t")


def test_value_version_stable_and_sensitive() -> None:
    assert forecast_override.compute_value_version(100.0) == forecast_override.compute_value_version(100.0)
    assert forecast_override.compute_value_version(100.0) != forecast_override.compute_value_version(101.0)
    # rounds to 2dp — 100.001 hashes same as 100.00
    assert forecast_override.compute_value_version(100.001) == forecast_override.compute_value_version(100.0)


def test_stale_version_error_carries_context() -> None:
    e = forecast_override.StaleVersionError(1, 2, forecast_run_id=21, interval_start="x")
    assert e.your_version == 1 and e.current_version == 2
    assert e.forecast_run_id == 21 and e.interval_start == "x"


def test_undo_window_expired() -> None:
    db = MagicMock()
    row = MagicMock()
    row.mappings.return_value.first.return_value = {
        "id": "00000000-0000-0000-0000-000000000001",
        "forecast_run_id": 21,
        "interval_start": datetime(2026, 6, 4, 7, tzinfo=timezone.utc),
        "before_value": 1.2,
        "after_value": 51.2,
        "undo_window_ends_at": datetime.now(timezone.utc) - timedelta(minutes=1),
        "undone_at": None,
    }
    db.execute.return_value = row
    with pytest.raises(forecast_override.UndoWindowExpired):
        forecast_override.undo_override(db, "00000000-0000-0000-0000-000000000001")


def test_undo_already_undone() -> None:
    db = MagicMock()
    row = MagicMock()
    row.mappings.return_value.first.return_value = {
        "id": "00000000-0000-0000-0000-000000000001",
        "forecast_run_id": 21,
        "interval_start": datetime(2026, 6, 4, 7, tzinfo=timezone.utc),
        "before_value": 1.2,
        "after_value": 51.2,
        "undo_window_ends_at": datetime.now(timezone.utc) + timedelta(hours=23),
        "undone_at": datetime.now(timezone.utc) - timedelta(minutes=5),
    }
    db.execute.return_value = row
    with pytest.raises(forecast_override.AlreadyUndone):
        forecast_override.undo_override(db, "00000000-0000-0000-0000-000000000001")


def test_undo_not_found() -> None:
    db = MagicMock()
    row = MagicMock()
    row.mappings.return_value.first.return_value = None
    db.execute.return_value = row
    with pytest.raises(forecast_override.ChangeNotFound):
        forecast_override.undo_override(db, "00000000-0000-0000-0000-000000000999")
