"""
Unit tests for the RBAC identity layer (app/identity.py).
"""
from __future__ import annotations

import base64
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from app.identity import (
    User,
    decode_username,
    require_role,
    resolve_user,
)


def _basic(user: str, pw: str = "x") -> str:
    return "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()


def test_decode_username_extracts_user() -> None:
    assert decode_username(_basic("jchen")) == "jchen"


def test_decode_username_handles_empty_and_garbage() -> None:
    assert decode_username(None) is None
    assert decode_username("Bearer xyz") is None
    assert decode_username("Basic !!!notbase64!!!") is None
    assert decode_username(_basic("", "pw")) is None  # empty username


def test_user_rank_ordering() -> None:
    mgr = User("jchen", "J. Chen", "wfm_manager")
    assert mgr.has_at_least("analyst")
    assert mgr.has_at_least("wfm_manager")
    assert not mgr.has_at_least("admin")


def test_resolve_user_known(monkeypatch) -> None:
    db = MagicMock()
    row = MagicMock()
    row.mappings.return_value.first.return_value = {
        "username": "apatel", "display_name": "A. Patel", "role": "analyst"
    }
    db.execute.return_value = row
    u = resolve_user(db, "apatel")
    assert u.username == "apatel" and u.role == "analyst"


def test_resolve_user_unknown_falls_back_to_guest() -> None:
    db = MagicMock()
    row = MagicMock()
    row.mappings.return_value.first.return_value = None
    db.execute.return_value = row
    u = resolve_user(db, "nobody")
    assert u.username == "guest" and u.role == "viewer"


def test_resolve_user_empty_is_guest_without_query() -> None:
    db = MagicMock()
    u = resolve_user(db, None)
    assert u.role == "viewer"
    db.execute.assert_not_called()


def test_require_role_allows_listed_and_admin() -> None:
    dep = require_role("analyst", "wfm_manager")
    assert dep(user=User("apatel", "A", "analyst")).username == "apatel"
    assert dep(user=User("jchen", "J", "wfm_manager")).username == "jchen"
    # admin always passes even if not listed
    assert dep(user=User("admin", "Admin", "admin")).username == "admin"


def test_require_role_blocks_insufficient() -> None:
    dep = require_role("wfm_manager")
    with pytest.raises(HTTPException) as exc:
        dep(user=User("guest", "Guest", "viewer"))
    assert exc.value.status_code == 403
    with pytest.raises(HTTPException) as exc2:
        dep(user=User("apatel", "A", "analyst"))
    assert exc2.value.status_code == 403
