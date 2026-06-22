"""
Identity + RBAC — real identities over the shared-password gate.

The Basic-Auth middleware (app/auth.py) still gates every request on the shared
password; this module turns the *username* in that header into an identity with
a role. Resolution happens in a FastAPI dependency (so it has a DB session),
never in the middleware.

Roles (lowest → highest privilege):
    viewer       — read only
    analyst      — + forecast overrides, staffing targets, leave decisions
    wfm_manager  — + offers, break moves, schedule edits, shift creation (#6)
    admin        — everything

`require_role(*roles)` is the write-gate dependency; admin is always allowed.
An unknown/empty username resolves to the read-only `guest` identity, so reads
keep working for anyone with just the password while writes demand a known,
sufficiently-privileged user.
"""
from __future__ import annotations

import base64
import logging
from dataclasses import dataclass

from fastapi import Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.db import get_db

log = logging.getLogger("wfm.identity")

ROLES = ("viewer", "analyst", "wfm_manager", "admin")
_RANK = {r: i for i, r in enumerate(ROLES)}

GUEST = "guest"


@dataclass(frozen=True)
class User:
    username: str
    display_name: str
    role: str

    def has_at_least(self, role: str) -> bool:
        return _RANK.get(self.role, -1) >= _RANK.get(role, 99)


def decode_username(authorization_header: str | None) -> str | None:
    """Pull the username out of a `Basic base64(user:pass)` header. The password
    is the middleware's concern; here we only want who is acting."""
    if not authorization_header or not authorization_header.lower().startswith("basic "):
        return None
    try:
        decoded = base64.b64decode(authorization_header.split(" ", 1)[1]).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    username, _, _ = decoded.partition(":")
    return username or None


def resolve_user(db: Session, username: str | None) -> User:
    """Map a username to a User. Unknown/empty/inactive → the guest viewer."""
    if username:
        row = (
            db.execute(
                text(
                    "SELECT username, display_name, role FROM users "
                    "WHERE username = :u AND active = TRUE"
                ),
                {"u": username},
            )
            .mappings()
            .first()
        )
        if row is not None:
            return User(username=row["username"], display_name=row["display_name"], role=row["role"])
    return User(username=GUEST, display_name="Guest", role="viewer")


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    """FastAPI dependency — the acting identity for this request."""
    return resolve_user(db, decode_username(request.headers.get("authorization")))


def require_role(*allowed: str):
    """Dependency factory: 403 unless the caller's role is in `allowed`
    (admin always passes). Returns the User so handlers can record applied_by."""
    allowed_set = set(allowed)

    def _dep(user: User = Depends(current_user)) -> User:
        if user.role == "admin" or user.role in allowed_set:
            return user
        raise HTTPException(
            status_code=403,
            detail=(
                f"'{user.display_name}' ({user.role}) is not permitted to perform "
                f"this action. Requires one of: {', '.join(sorted(allowed_set))}."
            ),
        )

    return _dep


# Convenience gates used across the write surfaces.
def writer_analyst():
    """Forecast/staffing/leave surfaces — analyst and up."""
    return require_role("analyst", "wfm_manager")


def writer_manager():
    """Offers, schedule edits, break moves, shift creation — manager and up."""
    return require_role("wfm_manager")
