"""
/users, /me — identity read endpoints for the RBAC layer.

/users powers the frontend identity picker; /me echoes the resolved caller so
the UI can show "acting as …" and disable write affordances the role can't use.
Both are reads (behind the shared-password gate, no role requirement).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import get_db
from app.identity import User, current_user

router = APIRouter(tags=["identity"])


class UserOut(BaseModel):
    username: str
    display_name: str
    role: str


@router.get("/users", response_model=list[UserOut])
def list_users(db: Session = Depends(get_db)) -> list[UserOut]:
    rows = (
        db.execute(
            text(
                "SELECT username, display_name, role FROM users "
                "WHERE active = TRUE ORDER BY "
                "CASE role WHEN 'admin' THEN 0 WHEN 'wfm_manager' THEN 1 "
                "WHEN 'analyst' THEN 2 ELSE 3 END, display_name"
            )
        )
        .mappings()
        .all()
    )
    return [UserOut(**dict(r)) for r in rows]


@router.get("/me", response_model=UserOut)
def whoami(user: User = Depends(current_user)) -> UserOut:
    return UserOut(username=user.username, display_name=user.display_name, role=user.role)
