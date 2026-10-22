"""Localhost Host allowlist, per-install session token, and CSRF protection (security-model.md §3.5)."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from pathlib import Path
from urllib.parse import urlparse

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from atlas.utils import ensure_secure_dir, ensure_secure_file

SESSION_COOKIE_NAME = "atlas_session"
CSRF_COOKIE_NAME = "atlas_csrf"
CSRF_HEADER_NAME = "x-atlas-csrf"

MUTATING_METHODS: frozenset[str] = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def load_or_create_session_token(data_dir: Path) -> str:
    """Load or create the per-install random session token at ~/.atlas/session_token (mode 0600)."""
    ensure_secure_dir(data_dir)
    token_path = data_dir / "session_token"
    if token_path.exists():
        ensure_secure_file(token_path)
        existing = token_path.read_text(encoding="utf-8").strip()
        if len(existing) >= 32:
            return existing
    token = secrets.token_urlsafe(32)
    token_path.write_text(f"{token}\n", encoding="utf-8")
    ensure_secure_file(token_path)
    return token


def derive_csrf_token(session_token: str) -> str:
    """Derive a deterministic CSRF token from the session token using HMAC-SHA256."""
    return hmac.new(
        session_token.encode("utf-8"),
        b"atlas-csrf-v1",
        hashlib.sha256,
    ).hexdigest()


def is_allowed_host(host_header: str, *, allow_testserver: bool = False) -> bool:
    """Validate Host header against loopback allowlist to prevent DNS rebinding."""
    if not host_header:
        return False
    host_only = host_header.split(":")[0].strip().lower()
    allowed = {"127.0.0.1", "localhost", "[::1]"}
    if allow_testserver:
        allowed.add("testserver")
    return host_only in allowed


def is_allowed_origin(origin_header: str, *, allow_testserver: bool = False) -> bool:
    """Validate Origin header on mutating requests."""
    parsed = urlparse(origin_header)
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower()
    allowed = {"127.0.0.1", "localhost", "::1"}
    if allow_testserver:
        allowed.add("testserver")
    return host in allowed


class LocalhostSecurityMiddleware(BaseHTTPMiddleware):
    """Enforces Host allowlist, CSP headers, session authentication, and CSRF/Origin checks."""

    def __init__(
        self,
        app: object,
        *,
        session_token: str,
        allow_testserver: bool = False,
    ) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self.session_token = session_token
        self.csrf_token = derive_csrf_token(session_token)
        self.allow_testserver = allow_testserver

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        # 1. Host header allowlist check (DNS rebinding defence)
        host = request.headers.get("host", "")
        if not is_allowed_host(host, allow_testserver=self.allow_testserver):
            return JSONResponse(
                status_code=400,
                content={"detail": f"Disallowed Host header: {host!r}"},
            )

        path = request.url.path

        # 2. Mutating request authentication + CSRF + Origin enforcement
        if request.method.upper() in MUTATING_METHODS:
            origin = request.headers.get("origin")
            if origin and not is_allowed_origin(origin, allow_testserver=self.allow_testserver):
                return JSONResponse(
                    status_code=403,
                    content={"detail": f"Cross-origin request rejected: {origin!r}"},
                )

            auth_header = request.headers.get("authorization", "")
            cookie_session = request.cookies.get(SESSION_COOKIE_NAME, "")
            csrf_header = request.headers.get(CSRF_HEADER_NAME, "")

            # Path A: Cookie-based browser request -> requires BOTH valid session cookie AND matching X-Atlas-CSRF header
            cookie_valid = bool(
                cookie_session
                and hmac.compare_digest(cookie_session, self.session_token)
            )
            csrf_valid = bool(
                csrf_header and hmac.compare_digest(csrf_header, self.csrf_token)
            )

            # Path B: Explicit Bearer token + matching X-Atlas-CSRF header
            bearer_token = (
                auth_header[7:].strip()
                if auth_header.lower().startswith("bearer ")
                else ""
            )
            bearer_valid = bool(
                bearer_token
                and hmac.compare_digest(bearer_token, self.session_token)
            )

            if not ((cookie_valid or bearer_valid) and csrf_valid):
                return JSONResponse(
                    status_code=403,
                    content={
                        "detail": "Missing or invalid session token or X-Atlas-CSRF header"
                    },
                )

        # 3. Read API endpoints (/api/v1/*) also require valid session cookie or Bearer token
        elif path.startswith("/api/v1/"):
            auth_header = request.headers.get("authorization", "")
            cookie_session = request.cookies.get(SESSION_COOKIE_NAME, "")
            bearer_token = (
                auth_header[7:].strip()
                if auth_header.lower().startswith("bearer ")
                else ""
            )
            is_authed = (
                bool(cookie_session)
                and hmac.compare_digest(cookie_session, self.session_token)
            ) or (
                bool(bearer_token)
                and hmac.compare_digest(bearer_token, self.session_token)
            )
            if not is_authed:
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Authentication required"},
                )

        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; img-src 'self' data:; connect-src 'self'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        return response
