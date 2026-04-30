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
                (:token, :expires, :sched, :ver, :cs::jsonb, :conv::uuid, :umid::uuid)
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
            SET consumed_at = NOW(), consumed_log_id = :log_id::uuid
            WHERE token = :token
            """
        ),
        {"token": token, "log_id": log_id},
    )


def _json_dumps(value: Any) -> str:
    import json
    return json.dumps(value, default=str)
