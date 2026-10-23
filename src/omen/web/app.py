"""HTTP routes for the OMEN website (served at /api/* by the single Python function in api/index.py)."""

from __future__ import annotations

import contextlib
import logging
import os
import time

from omen import __version__
from omen.web import auth
from omen.web.asgi import App, HTTPError, Request, Response, Stream, json_response, ndjson
from omen.web.service import WebService
from omen.web.store import StoreError, get_store

log = logging.getLogger(__name__)

app = App(
    security_headers=[
        ("x-content-type-options", "nosniff"),
        ("referrer-policy", "no-referrer"),
        ("x-frame-options", "DENY"),
    ]
)
_SERVICE: WebService | None = None


def service() -> WebService:
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = WebService(get_store())
    return _SERVICE


def set_service(svc: WebService | None) -> None:
    global _SERVICE
    _SERVICE = svc


def _secure(req: Request) -> bool:
    return req.headers.get("x-forwarded-proto", "https") == "https" and os.environ.get("OMEN_WEB_INSECURE") != "1"


def _guard(req: Request) -> WebService:
    auth.require(req)
    return service()


# ------------------------------------------------------------------------------------------------------ public
@app.route("GET", "/api/health")
def health(req: Request) -> Response:
    svc = service()
    return json_response(
        {
            "ok": True,
            "version": __version__,
            "setup_required": auth.password() is None,
            "store": svc.store.kind,
            "ingest_enabled": len(os.environ.get("OMEN_INGEST_SECRET", "")) >= 16,
        }
    )


@app.route("GET", "/api/auth/session")
def session(req: Request) -> Response:
    return json_response(
        {
            "authenticated": auth.valid_session(req.cookies.get(auth.COOKIE)),
            "setup_required": auth.password() is None,
        }
    )


@app.route("POST", "/api/auth/login")
def login(req: Request) -> Response:
    if auth.password() is None:
        raise HTTPError(
            503, "setup required: set OMEN_WEB_PASSWORD (8+ characters) in Vercel and redeploy", setup_required=True
        )
    if req.headers.get(auth.CSRF_HEADER) != "1":
        raise HTTPError(403, "missing CSRF header")
    svc = service()
    key = f"omen:login:{req.client}"
    try:
        attempts = svc.store.incr(key, 1, ex=auth.LOGIN_WINDOW_SECONDS)
    except StoreError:
        attempts = 0
    if attempts > auth.LOGIN_MAX_ATTEMPTS:
        raise HTTPError(429, "too many attempts; wait 15 minutes")
    body = req.json()
    if not auth.check_password(str(body.get("password", ""))):
        time.sleep(0.4)
        raise HTTPError(401, "wrong password")
    with contextlib.suppress(StoreError):
        svc.store.delete(key)
    return json_response({"authenticated": True}, headers=[auth.session_cookie(auth.make_session(), _secure(req))])


@app.route("POST", "/api/auth/logout")
def logout(req: Request) -> Response:
    return json_response({"authenticated": False}, headers=[auth.clear_cookie(_secure(req))])


@app.route("POST", "/api/ingest")
def ingest(req: Request) -> Response:
    try:
        return json_response(service().ingest(req.body, req.headers))
    except StoreError as exc:
        raise HTTPError(503, f"store unavailable: {exc}") from None


# ------------------------------------------------------------------------------------------------ signed-in
def _store_errors(fn):
    def wrapped(req: Request):
        try:
            return fn(req)
        except StoreError as exc:
            raise HTTPError(503, f"store unavailable: {exc}") from None

    wrapped.__name__ = fn.__name__
    return wrapped


@app.route("GET", "/api/dashboard")
@_store_errors
def dashboard(req: Request) -> Response:
    return json_response(_guard(req).dashboard())


@app.route("GET", "/api/history")
@_store_errors
def history(req: Request) -> Response:
    return json_response({"points": _guard(req).history()})


@app.route("POST", "/api/scan")
@_store_errors
def scan(req: Request) -> Stream:
    events = _guard(req).scan(req.json())
    return Stream(ndjson(events))


@app.route("POST", "/api/reverify")
@_store_errors
def reverify(req: Request) -> Response:
    return json_response(_guard(req).reverify(req.json()))


@app.route("GET", "/api/fees")
def fees(req: Request) -> Response:
    return json_response(_guard(req).fees())


@app.route("POST", "/api/fees/quote")
def fee_quote(req: Request) -> Response:
    return json_response(_guard(req).quote(req.json()))


@app.route("GET", "/api/series")
def series(req: Request) -> Response:
    return json_response({"series": _guard(req).series_search(req.arg("q", "") or "")})


@app.route("GET", "/api/crypto")
def crypto(req: Request) -> Response:
    return json_response(_guard(req).crypto_view(req.arg("asset", "BTC") or "BTC"))
