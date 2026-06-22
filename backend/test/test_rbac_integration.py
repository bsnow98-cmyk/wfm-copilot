"""
Postgres-backed integration tests for RBAC role gating across the write surfaces.

Uses a bogus apply_token so nothing mutates: the role dependency runs BEFORE the
token is consumed, so a blocked role returns 403 and an allowed role falls
through to 404 (token-not-found). That cleanly asserts the gate without writes.
SKIPPED when no DB is reachable.
"""
from __future__ import annotations

import base64
import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text


def _database_url() -> str | None:
    user = os.environ.get("POSTGRES_USER")
    pwd = os.environ.get("POSTGRES_PASSWORD")
    dbname = os.environ.get("POSTGRES_DB")
    if not (user and pwd and dbname):
        return None
    host = os.environ.get("TEST_POSTGRES_HOST", "localhost")
    port = os.environ.get("TEST_POSTGRES_PORT", "5432")
    return f"postgresql+psycopg://{user}:{pwd}@{host}:{port}/{dbname}"


@pytest.fixture(scope="module")
def client():
    url = _database_url()
    if url is None:
        pytest.skip("POSTGRES_* env vars not set — skipping RBAC integration tests")
    try:
        eng = create_engine(url, future=True)
        with eng.connect() as c:
            c.execute(text("SELECT 1"))
        eng.dispose()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Postgres unreachable ({type(exc).__name__})")
    from app.main import app

    return TestClient(app)


def _h(user: str) -> dict[str, str]:
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:x".encode()).decode()}


def test_me_resolves_roles(client) -> None:
    assert client.get("/me", headers=_h("jchen")).json()["role"] == "wfm_manager"
    assert client.get("/me", headers=_h("apatel")).json()["role"] == "analyst"
    assert client.get("/me", headers=_h("nobody")).json()["role"] == "viewer"  # fallback


def test_users_list(client) -> None:
    roles = {u["role"] for u in client.get("/users", headers=_h("jchen")).json()}
    assert {"admin", "wfm_manager", "analyst", "viewer"} <= roles


def _code(client, path: str, user: str) -> int:
    return client.post(path, json={"apply_token": "bogus"}, headers=_h(user)).status_code


# 403 = blocked by role; 404 = passed role gate, failed token lookup (no mutation).
@pytest.mark.parametrize(
    "path,user,expected",
    [
        # viewer is blocked everywhere
        ("/leave/decisions/apply", "guest", 403),
        ("/offers/apply", "guest", 403),
        ("/staffing/targets/apply", "guest", 403),
        ("/schedules/apply", "guest", 403),
        # analyst: leave + staffing yes; offers + schedule no
        ("/leave/decisions/apply", "apatel", 404),
        ("/staffing/targets/apply", "apatel", 404),
        ("/offers/apply", "apatel", 403),
        ("/schedules/apply", "apatel", 403),
        # manager: everything
        ("/offers/apply", "jchen", 404),
        ("/schedules/apply", "jchen", 404),
        ("/leave/decisions/apply", "jchen", 404),
        ("/staffing/targets/apply", "jchen", 404),
        # admin: everything
        ("/schedules/apply", "admin", 404),
        ("/offers/apply", "admin", 404),
    ],
)
def test_role_gating_matrix(client, path: str, user: str, expected: int) -> None:
    assert _code(client, path, user) == expected
