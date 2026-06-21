"""
Unit tests for the shared apply envelope (write_actions.apply_via_token).

Locks in the ordering every write surface depends on: token errors map to
404/410, an already-consumed token short-circuits to the idempotent result
(write/mark/notify/commit NOT called), and the success path runs
write → mark_consumed → notify → commit exactly once, in order.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from app.services import write_actions
from app.services.apply_tokens import TokenExpired, TokenNotFound


def _callbacks(**over):
    base = dict(
        consume=lambda db, t: MagicMock(consumed_ref=None),
        consumed_ref=lambda tok: None,
        idempotent_result=lambda db, ref: ("idem", ref),
        write=lambda db, tok: ("result", "ref-1"),
        mark_consumed=MagicMock(),
        notify=MagicMock(),
        response=lambda r: ("ok", r),
    )
    base.update(over)
    return base


def test_token_not_found_maps_to_404() -> None:
    def consume(db, t):
        raise TokenNotFound("nope")

    with pytest.raises(HTTPException) as exc:
        write_actions.apply_via_token(MagicMock(), "t", **_callbacks(consume=consume))
    assert exc.value.status_code == 404


def test_token_expired_maps_to_410() -> None:
    def consume(db, t):
        raise TokenExpired("old")

    with pytest.raises(HTTPException) as exc:
        write_actions.apply_via_token(MagicMock(), "t", **_callbacks(consume=consume))
    assert exc.value.status_code == 410


def test_already_consumed_short_circuits() -> None:
    db = MagicMock()
    mark = MagicMock()
    notify = MagicMock()
    write = MagicMock()
    out = write_actions.apply_via_token(
        db,
        "t",
        **_callbacks(
            consumed_ref=lambda tok: "log-99",
            write=write,
            mark_consumed=mark,
            notify=notify,
        ),
    )
    assert out == ("idem", "log-99")
    write.assert_not_called()
    mark.assert_not_called()
    notify.assert_not_called()
    db.commit.assert_not_called()


def test_success_path_runs_write_mark_notify_commit() -> None:
    db = MagicMock()
    order: list[str] = []
    cbs = _callbacks(
        write=lambda d, tok: (order.append("write") or ("R", "ref-7")),
        mark_consumed=lambda d, t, ref: order.append(f"mark:{ref}"),
        notify=lambda d, tok, r: order.append("notify"),
    )
    db.commit.side_effect = lambda: order.append("commit")
    out = write_actions.apply_via_token(db, "t", **cbs)
    assert out == ("ok", "R")
    assert order == ["write", "mark:ref-7", "notify", "commit"]


def test_domain_error_in_write_propagates() -> None:
    class DomainError(Exception):
        pass

    def write(db, tok):
        raise DomainError("stale")

    db = MagicMock()
    with pytest.raises(DomainError):
        write_actions.apply_via_token(db, "t", **_callbacks(write=write))
    db.commit.assert_not_called()
