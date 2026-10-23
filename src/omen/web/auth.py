"""Owner login for the website: one password from ``OMEN_WEB_PASSWORD``, an HMAC-signed session cookie, a CSRF
header on every state-changing request, and a login throttle. Fails closed: with no password set, nothing but the
public health endpoint answers."""

from __future__ import annotations

import hashlib
import hmac
import os
import time

from omen.web.asgi import HTTPError, Request

COOKIE = "omen_s"
SESSION_DAYS = 30
CSRF_HEADER = "x-omen-csrf"
LOGIN_WINDOW_SECONDS = 900
LOGIN_MAX_ATTEMPTS = 8


def password() -> str | None:
    value = os.environ.get("OMEN_WEB_PASSWORD", "")
    return value if len(value) >= 8 else None


def _key() -> bytes:
    secret = os.environ.get("OMEN_SESSION_SECRET") or password() or ""
    # changing the password (or the optional session secret) signs everyone out
    return hmac.new(b"omen-session-v1", secret.encode(), hashlib.sha256).digest()


def check_password(candidate: str) -> bool:
    expected = password()
    if expected is None:
        return False
    a = hashlib.sha256(candidate.encode()).digest()
    b = hashlib.sha256(expected.encode()).digest()
    return hmac.compare_digest(a, b)


def make_session(now: float | None = None) -> str:
    exp = int((now or time.time()) + SESSION_DAYS * 86400)
    payload = f"v1.{exp}"
    sig = hmac.new(_key(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def valid_session(token: str | None, now: float | None = None) -> bool:
    if not token or password() is None:
        return False
    try:
        version, exp, sig = token.split(".")
        if version != "v1" or int(exp) < (now or time.time()):
            return False
    except ValueError:
        return False
    expected = hmac.new(_key(), f"{version}.{exp}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected)


def session_cookie(token: str, secure: bool = True) -> tuple[str, str]:
    flags = f"Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_DAYS * 86400}"
    return ("set-cookie", f"{COOKIE}={token}; {flags}{'; Secure' if secure else ''}")


def clear_cookie(secure: bool = True) -> tuple[str, str]:
    return ("set-cookie", f"{COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0{'; Secure' if secure else ''}")


def require(req: Request) -> None:
    """Gate for every data endpoint."""
    if password() is None:
        raise HTTPError(
            503, "setup required: set OMEN_WEB_PASSWORD (8+ characters) in Vercel and redeploy", setup_required=True
        )
    if not valid_session(req.cookies.get(COOKIE)):
        raise HTTPError(401, "sign in required")
    if req.method != "GET" and req.headers.get(CSRF_HEADER) != "1":
        raise HTTPError(403, "missing CSRF header")
