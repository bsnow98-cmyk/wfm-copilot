"""
Phase 6 — single-password Basic-Auth gate.

Why this and not real auth: bounds Anthropic spend during a portfolio-stage
demo without forcing a user model. README documents the password.

Behavior:
- If WFM_DEMO_PASSWORD is unset → middleware is a pass-through (dev mode).
- If set → every request must carry `Authorization: Basic base64(<anything>:<pwd>)`.
  The username is ignored; the password must match exactly.
- /health stays open so probes don't 401.
"""
from __future__ import annotations

import base64
import hmac
import logging

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

log = logging.getLogger("wfm.auth")

OPEN_PATHS = {"/health", "/health/", "/docs", "/openapi.json", "/redoc"}


class BasicAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, password: str | None) -> None:
        super().__init__(app)
        self._password = password
        if password:
            log.info("Basic-Auth gate enabled.")
        else:
            log.warning(
                "WFM_DEMO_PASSWORD unset — running open. Set it before any "
                "deployment that exposes /chat."
            )

    async def dispatch(self, request: Request, call_next) -> Response:
        if self._password is None or request.url.path in OPEN_PATHS:
            return await call_next(request)

        if request.method == "OPTIONS":
            # Let CORS preflight through; the actual request will be authenticated.
            return await call_next(request)

        header = request.headers.get("authorization", "")
        if not _password_matches(header, self._password):
            return _unauthorized()

        return await call_next(request)


def _password_matches(header: str, expected: str) -> bool:
    if not header.lower().startswith("basic "):
        return False
    try:
        decoded = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return False
    _, _, supplied = decoded.partition(":")
    # Constant-time compare to avoid timing leaks on the password.
    return hmac.compare_digest(supplied, expected)


def _unauthorized() -> JSONResponse:
    # Starlette quirk: when BaseHTTPMiddleware (this class) short-circuits
    # with a Response, the outer CORSMiddleware doesn't always get a chance
    # to add CORS headers. Without them, the browser blocks the 401 on a
    # cross-origin fetch — which masks the auth failure as a CORS error.
    # Set the CORS headers ourselves so the 401 actually reaches the client.
    return JSONResponse(
        status_code=401,
        content={"detail": "Authentication required."},
        headers={
            "WWW-Authenticate": 'Basic realm="WFM Copilot"',
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Credentials": "false",
        },
    )
