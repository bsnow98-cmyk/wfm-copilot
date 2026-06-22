"""
apply_token issue + consume — cherry-pick D, decisions D-5 / D-6.

A token is minted by the preview tool. The apply endpoint consumes it inside
the same transaction as the schedule write, which gives us idempotency and
cuts off LLM-driven writes (the model has no way to mint a token).

TTL is enforced at consume time, not by a sweeper — old rows are tiny and
safe to keep around for forensic purposes.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

TOKEN_TTL = timedelta(minutes=5)


@dataclass(frozen=True)
class IssuedToken:
    token: str
    schedule_version: int
    expires_at: datetime


@dataclass(frozen=True)
class ConsumedToken:
    schedule_id: int
    schedule_version: int
    change_set: list[dict[str, Any]]
    conversation_id: str | None
    user_msg_id: str | None
    consumed_log_id: str | None  # set if this token was already consumed (idempotent path)


class TokenError(Exception):
    """Base class for token consume failures the router should map to HTTP."""


class TokenNotFound(TokenError):
    pass


class TokenExpired(TokenError):
    pass


def issue_token(
    db: Session,
    *,
    schedule_id: int,
    schedule_version: int,
    change_set: list[dict[str, Any]],
    conversation_id: str | None = None,
    user_msg_id: str | None = None,
) -> IssuedToken:
    """Mint a single-use opaque token for an apply preview.

    Idempotency note: each preview call mints a new token (cheap), so two
    consecutive previews of the same change get distinct tokens. That's
    intentional — the user can change their mind between previews, and we
    want each apply attempt to be tied to exactly one rendered preview.
    """
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + TOKEN_TTL
    db.execute(
        text(
            """
            INSERT INTO chat_apply_tokens
                (token, expires_at, schedule_id, schedule_version, change_set,
                 conversation_id, user_msg_id)
            VALUES
                (:token, :expires, :sched, :ver, CAST(:cs AS jsonb), CAST(:conv AS uuid), CAST(:umid AS uuid))
            """
        ),
        {
            "token": token,
            "expires": expires_at,
            "sched": schedule_id,
            "ver": schedule_version,
            "cs": _json_dumps(change_set),
            "conv": conversation_id,
            "umid": user_msg_id,
        },
    )
    return IssuedToken(token=token, schedule_version=schedule_version, expires_at=expires_at)


def consume_token(db: Session, token: str) -> ConsumedToken:
    """Look up and lock-for-update the token row.

    Returns the row even if already consumed — the caller (the apply endpoint)
    needs to know consumed_log_id so duplicate requests can return the
    original 200 response. Raises TokenExpired or TokenNotFound for the
    sad paths.
    """
    row = (
        db.execute(
            text(
                """
                SELECT schedule_id, schedule_version, change_set, expires_at,
                       consumed_at, consumed_log_id, conversation_id, user_msg_id
                FROM chat_apply_tokens
                WHERE token = :token
                FOR UPDATE
                """
            ),
            {"token": token},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise TokenNotFound(f"Unknown apply_token")

    if row["consumed_at"] is None and row["expires_at"] < datetime.now(timezone.utc):
        raise TokenExpired("apply_token expired (5-minute TTL)")

    return ConsumedToken(
        schedule_id=int(row["schedule_id"]),
        schedule_version=int(row["schedule_version"]),
        change_set=row["change_set"] or [],
        conversation_id=str(row["conversation_id"]) if row["conversation_id"] else None,
        user_msg_id=str(row["user_msg_id"]) if row["user_msg_id"] else None,
        consumed_log_id=str(row["consumed_log_id"]) if row["consumed_log_id"] else None,
    )


def mark_consumed(db: Session, token: str, log_id: str) -> None:
    db.execute(
        text(
            """
            UPDATE chat_apply_tokens
            SET consumed_at = NOW(), consumed_log_id = CAST(:log_id AS uuid)
            WHERE token = :token
            """
        ),
        {"token": token, "log_id": log_id},
    )


# --------------------------------------------------------------------------
# Leave-decision tokens (Surface #1).
#
# Reuse the chat_apply_tokens table via target_kind='leave'. The schedule
# columns stay NULL; the leave context (request_id, decision, note, version)
# rides in change_set, and consumption is tracked by consumed_leave_log_id
# (consumed_log_id's FK points at the wrong table — schedule_change_log).
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class ConsumedLeaveToken:
    request_id: int
    request_version: int
    decision: str
    note: str | None
    conversation_id: str | None
    user_msg_id: str | None
    consumed_log_id: str | None  # set if already consumed (idempotent path)


def issue_leave_token(
    db: Session,
    *,
    request_id: int,
    request_version: int,
    decision: str,
    note: str | None = None,
    conversation_id: str | None = None,
    user_msg_id: str | None = None,
) -> IssuedToken:
    """Mint a single-use token authorizing one leave approve/deny decision.

    The decision + request_version are pinned here at preview time so the apply
    endpoint writes exactly what the user previewed — the LLM can preview but
    never mint a token, and never decides.
    """
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + TOKEN_TTL
    change_set = {
        "request_id": request_id,
        "decision": decision,
        "note": note,
        "request_version": request_version,
    }
    db.execute(
        text(
            """
            INSERT INTO chat_apply_tokens
                (token, expires_at, target_kind, change_set,
                 conversation_id, user_msg_id)
            VALUES
                (:token, :expires, 'leave', CAST(:cs AS jsonb),
                 CAST(:conv AS uuid), CAST(:umid AS uuid))
            """
        ),
        {
            "token": token,
            "expires": expires_at,
            "cs": _json_dumps(change_set),
            "conv": conversation_id,
            "umid": user_msg_id,
        },
    )
    return IssuedToken(token=token, schedule_version=request_version, expires_at=expires_at)


def consume_leave_token(db: Session, token: str) -> ConsumedLeaveToken:
    """Lock-for-update a leave token. Returns even if already consumed so the
    apply endpoint can fold a duplicate request into the original 200."""
    row = (
        db.execute(
            text(
                """
                SELECT change_set, expires_at, consumed_at, consumed_leave_log_id,
                       conversation_id, user_msg_id, target_kind
                FROM chat_apply_tokens
                WHERE token = :token
                FOR UPDATE
                """
            ),
            {"token": token},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise TokenNotFound("Unknown apply_token")
    if row["target_kind"] != "leave":
        raise TokenNotFound("apply_token is not a leave-decision token")
    if row["consumed_at"] is None and row["expires_at"] < datetime.now(timezone.utc):
        raise TokenExpired("apply_token expired (5-minute TTL)")

    cs = row["change_set"] or {}
    return ConsumedLeaveToken(
        request_id=int(cs["request_id"]),
        request_version=int(cs["request_version"]),
        decision=str(cs["decision"]),
        note=cs.get("note"),
        conversation_id=str(row["conversation_id"]) if row["conversation_id"] else None,
        user_msg_id=str(row["user_msg_id"]) if row["user_msg_id"] else None,
        consumed_log_id=(
            str(row["consumed_leave_log_id"]) if row["consumed_leave_log_id"] else None
        ),
    )


def mark_leave_consumed(db: Session, token: str, log_id: str) -> None:
    db.execute(
        text(
            """
            UPDATE chat_apply_tokens
            SET consumed_at = NOW(), consumed_leave_log_id = CAST(:log_id AS uuid)
            WHERE token = :token
            """
        ),
        {"token": token, "log_id": log_id},
    )


# --------------------------------------------------------------------------
# Offer tokens (Surface #2 — OT/VTO publish).
#
# target_kind='offer'; the full offer spec rides in change_set. An offer is a
# create, so there's no version to pin — single-use is the only guard needed.
# Consumption tracked by consumed_offer_id.
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class ConsumedOfferToken:
    spec: dict[str, Any]
    conversation_id: str | None
    user_msg_id: str | None
    consumed_offer_id: int | None  # set if already consumed (idempotent path)


def issue_offer_token(
    db: Session,
    *,
    spec: dict[str, Any],
    conversation_id: str | None = None,
    user_msg_id: str | None = None,
) -> IssuedToken:
    """Mint a single-use token authorizing publication of one OT/VTO offer.
    `spec` is the full offer the user previewed (kind, window, targets, slots)."""
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + TOKEN_TTL
    db.execute(
        text(
            """
            INSERT INTO chat_apply_tokens
                (token, expires_at, target_kind, change_set,
                 conversation_id, user_msg_id)
            VALUES
                (:token, :expires, 'offer', CAST(:cs AS jsonb),
                 CAST(:conv AS uuid), CAST(:umid AS uuid))
            """
        ),
        {
            "token": token,
            "expires": expires_at,
            "cs": _json_dumps(spec),
            "conv": conversation_id,
            "umid": user_msg_id,
        },
    )
    return IssuedToken(token=token, schedule_version=0, expires_at=expires_at)


def consume_offer_token(db: Session, token: str) -> ConsumedOfferToken:
    row = (
        db.execute(
            text(
                """
                SELECT change_set, expires_at, consumed_at, consumed_offer_id,
                       conversation_id, user_msg_id, target_kind
                FROM chat_apply_tokens
                WHERE token = :token
                FOR UPDATE
                """
            ),
            {"token": token},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise TokenNotFound("Unknown apply_token")
    if row["target_kind"] != "offer":
        raise TokenNotFound("apply_token is not an offer token")
    if row["consumed_at"] is None and row["expires_at"] < datetime.now(timezone.utc):
        raise TokenExpired("apply_token expired (5-minute TTL)")
    return ConsumedOfferToken(
        spec=row["change_set"] or {},
        conversation_id=str(row["conversation_id"]) if row["conversation_id"] else None,
        user_msg_id=str(row["user_msg_id"]) if row["user_msg_id"] else None,
        consumed_offer_id=int(row["consumed_offer_id"]) if row["consumed_offer_id"] else None,
    )


def mark_offer_consumed(db: Session, token: str, offer_id: int) -> None:
    db.execute(
        text(
            """
            UPDATE chat_apply_tokens
            SET consumed_at = NOW(), consumed_offer_id = :oid
            WHERE token = :token
            """
        ),
        {"token": token, "oid": offer_id},
    )


# --------------------------------------------------------------------------
# Forecast-override tokens (Surface #4).
#
# target_kind='forecast_override'; change_set pins {forecast_run_id,
# interval_start, new_value, expected_version}. Consumption tracked by
# consumed_forecast_log_id.
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class ConsumedForecastToken:
    forecast_run_id: int
    interval_start: str
    new_value: float
    expected_version: int
    conversation_id: str | None
    user_msg_id: str | None
    consumed_log_id: str | None  # set if already consumed (idempotent path)


def issue_forecast_token(
    db: Session,
    *,
    forecast_run_id: int,
    interval_start: str,
    new_value: float,
    expected_version: int,
    conversation_id: str | None = None,
    user_msg_id: str | None = None,
) -> IssuedToken:
    """Mint a single-use token authorizing one forecast-interval override."""
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + TOKEN_TTL
    change_set = {
        "forecast_run_id": forecast_run_id,
        "interval_start": interval_start,
        "new_value": new_value,
        "expected_version": expected_version,
    }
    db.execute(
        text(
            """
            INSERT INTO chat_apply_tokens
                (token, expires_at, target_kind, change_set,
                 conversation_id, user_msg_id)
            VALUES
                (:token, :expires, 'forecast_override', CAST(:cs AS jsonb),
                 CAST(:conv AS uuid), CAST(:umid AS uuid))
            """
        ),
        {
            "token": token,
            "expires": expires_at,
            "cs": _json_dumps(change_set),
            "conv": conversation_id,
            "umid": user_msg_id,
        },
    )
    return IssuedToken(token=token, schedule_version=expected_version, expires_at=expires_at)


def consume_forecast_token(db: Session, token: str) -> ConsumedForecastToken:
    row = (
        db.execute(
            text(
                """
                SELECT change_set, expires_at, consumed_at, consumed_forecast_log_id,
                       conversation_id, user_msg_id, target_kind
                FROM chat_apply_tokens
                WHERE token = :token
                FOR UPDATE
                """
            ),
            {"token": token},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise TokenNotFound("Unknown apply_token")
    if row["target_kind"] != "forecast_override":
        raise TokenNotFound("apply_token is not a forecast-override token")
    if row["consumed_at"] is None and row["expires_at"] < datetime.now(timezone.utc):
        raise TokenExpired("apply_token expired (5-minute TTL)")
    cs = row["change_set"] or {}
    return ConsumedForecastToken(
        forecast_run_id=int(cs["forecast_run_id"]),
        interval_start=str(cs["interval_start"]),
        new_value=float(cs["new_value"]),
        expected_version=int(cs["expected_version"]),
        conversation_id=str(row["conversation_id"]) if row["conversation_id"] else None,
        user_msg_id=str(row["user_msg_id"]) if row["user_msg_id"] else None,
        consumed_log_id=(
            str(row["consumed_forecast_log_id"]) if row["consumed_forecast_log_id"] else None
        ),
    )


def mark_forecast_consumed(db: Session, token: str, log_id: str) -> None:
    db.execute(
        text(
            """
            UPDATE chat_apply_tokens
            SET consumed_at = NOW(), consumed_forecast_log_id = CAST(:log_id AS uuid)
            WHERE token = :token
            """
        ),
        {"token": token, "log_id": log_id},
    )


# --------------------------------------------------------------------------
# Staffing-target tokens (Surface #5).
#
# target_kind='staffing_target'; change_set pins {staffing_id, new_targets,
# expected_version}. Consumption tracked by consumed_staffing_log_id.
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class ConsumedStaffingToken:
    staffing_id: int
    new_targets: dict[str, Any]
    expected_version: int
    conversation_id: str | None
    user_msg_id: str | None
    consumed_log_id: str | None  # set if already consumed (idempotent path)


def issue_staffing_token(
    db: Session,
    *,
    staffing_id: int,
    new_targets: dict[str, Any],
    expected_version: int,
    conversation_id: str | None = None,
    user_msg_id: str | None = None,
) -> IssuedToken:
    """Mint a single-use token authorizing one staffing-target change."""
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + TOKEN_TTL
    change_set = {
        "staffing_id": staffing_id,
        "new_targets": new_targets,
        "expected_version": expected_version,
    }
    db.execute(
        text(
            """
            INSERT INTO chat_apply_tokens
                (token, expires_at, target_kind, change_set,
                 conversation_id, user_msg_id)
            VALUES
                (:token, :expires, 'staffing_target', CAST(:cs AS jsonb),
                 CAST(:conv AS uuid), CAST(:umid AS uuid))
            """
        ),
        {
            "token": token,
            "expires": expires_at,
            "cs": _json_dumps(change_set),
            "conv": conversation_id,
            "umid": user_msg_id,
        },
    )
    return IssuedToken(token=token, schedule_version=expected_version, expires_at=expires_at)


def consume_staffing_token(db: Session, token: str) -> ConsumedStaffingToken:
    row = (
        db.execute(
            text(
                """
                SELECT change_set, expires_at, consumed_at, consumed_staffing_log_id,
                       conversation_id, user_msg_id, target_kind
                FROM chat_apply_tokens
                WHERE token = :token
                FOR UPDATE
                """
            ),
            {"token": token},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise TokenNotFound("Unknown apply_token")
    if row["target_kind"] != "staffing_target":
        raise TokenNotFound("apply_token is not a staffing-target token")
    if row["consumed_at"] is None and row["expires_at"] < datetime.now(timezone.utc):
        raise TokenExpired("apply_token expired (5-minute TTL)")
    cs = row["change_set"] or {}
    return ConsumedStaffingToken(
        staffing_id=int(cs["staffing_id"]),
        new_targets=cs["new_targets"],
        expected_version=int(cs["expected_version"]),
        conversation_id=str(row["conversation_id"]) if row["conversation_id"] else None,
        user_msg_id=str(row["user_msg_id"]) if row["user_msg_id"] else None,
        consumed_log_id=(
            str(row["consumed_staffing_log_id"]) if row["consumed_staffing_log_id"] else None
        ),
    )


def mark_staffing_consumed(db: Session, token: str, log_id: str) -> None:
    db.execute(
        text(
            """
            UPDATE chat_apply_tokens
            SET consumed_at = NOW(), consumed_staffing_log_id = CAST(:log_id AS uuid)
            WHERE token = :token
            """
        ),
        {"token": token, "log_id": log_id},
    )


def _json_dumps(value: Any) -> str:
    import json
    return json.dumps(value, default=str)
