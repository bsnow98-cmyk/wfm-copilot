"""
write_actions — the shared apply envelope for token-gated write surfaces.

Every chat write surface (leave decisions, offers, future forecast/staffing
overrides) repeats the same ordering, and getting it wrong is how you double-
write or lose an audit row:

    consume token (→404/410)
      → if already consumed, return the original result (idempotency)
      → domain write (audit + mutation, in the caller's txn)
      → mark token consumed
      → notify
      → commit

`apply_via_token` owns that ordering once. Each surface supplies small callbacks
for the parts that genuinely differ (which token, which table, which
notification). Domain concurrency errors raised inside `write` (e.g.
StaleVersionError) propagate to the caller, which renders the surface-specific
409 — that body differs per surface (fresh preview shape), so it stays out here.

See EXECUTION_ROADMAP §"Cross-cutting work worth doing once".
"""
from __future__ import annotations

from typing import Any, Callable, TypeVar

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.services.apply_tokens import TokenExpired, TokenNotFound

R = TypeVar("R")


def apply_via_token(
    db: Session,
    token_str: str,
    *,
    consume: Callable[[Session, str], Any],
    consumed_ref: Callable[[Any], Any | None],
    idempotent_result: Callable[[Session, Any], R],
    write: Callable[[Session, Any], tuple[Any, Any]],
    mark_consumed: Callable[[Session, str, Any], None],
    notify: Callable[[Session, Any, Any], None],
    response: Callable[[Any], R],
) -> R:
    """Run the shared apply envelope for one token-gated write.

    Args (all callbacks receive the live Session):
      consume(db, token_str) -> token object. Raises TokenNotFound / TokenExpired.
      consumed_ref(token) -> the stored result ref if already consumed, else None.
      idempotent_result(db, ref) -> the response to return for a duplicate apply.
      write(db, token) -> (result, ref_to_store). May raise domain errors
          (e.g. StaleVersionError) which propagate to the caller for 409 mapping.
          Must not commit.
      mark_consumed(db, token_str, ref_to_store) -> None.
      notify(db, token, result) -> None.
      response(result) -> the success response.

    Commits exactly once, on success.
    """
    try:
        token = consume(db, token_str)
    except TokenNotFound:
        raise HTTPException(404, "apply_token not found")
    except TokenExpired:
        raise HTTPException(410, "apply_token expired (5-minute TTL)")

    ref = consumed_ref(token)
    if ref is not None:
        return idempotent_result(db, ref)

    result, ref_to_store = write(db, token)
    mark_consumed(db, token_str, ref_to_store)
    notify(db, token, result)
    db.commit()
    return response(result)
