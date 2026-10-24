"""The website's API: auth, CSRF, login throttling, signed ingest from the Pi, streamed scans and budgets."""

import asyncio
import json
import threading
from datetime import UTC, datetime
from decimal import Decimal as D

import httpx
import pytest

from helpers import ROOT, event, market, registry
from omen.config import load_settings
from omen.http import HttpClient
from omen.publish import Publisher, fit, sign
from omen.strategies.lock import near_misses
from omen.web import app as webapp
from omen.web import auth
from omen.web.service import WebService
from omen.web.store import MemoryStore
from test_end_to_end import FakeKalshi

PASSWORD = "correct horse battery"
SECRET = "0123456789abcdef-ingest"


def call(app, method, path, body=None, headers=None, cookie=None):
    """Drive the ASGI app in-process; returns (status, headers, raw body)."""
    raw = b"" if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
    path, _, query = path.partition("?")
    hdrs = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    if cookie:
        hdrs.append((b"cookie", cookie.encode()))
    scope = {"type": "http", "method": method, "path": path, "query_string": query.encode(), "headers": hdrs}
    out = {"status": None, "headers": {}, "body": b""}
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": raw, "more_body": False}

    async def send(msg):
        if msg["type"] == "http.response.start":
            out["status"] = msg["status"]
            out["headers"] = {k.decode(): v.decode() for k, v in msg["headers"]}
        else:
            out["body"] += msg.get("body", b"")

    asyncio.run(app(scope, receive, send))
    return out["status"], out["headers"], out["body"]


def j(resp):
    return json.loads(resp[2])


@pytest.fixture
def site(monkeypatch):
    monkeypatch.setenv("OMEN_WEB_PASSWORD", PASSWORD)
    monkeypatch.setenv("OMEN_INGEST_SECRET", SECRET)
    monkeypatch.delenv("OMEN_SESSION_SECRET", raising=False)
    fake = FakeKalshi()
    s = load_settings(ROOT / "config" / "omen.toml", env={"OMEN_PROJECT_ROOT": str(ROOT)})
    s.lock.min_apr_pct = D("0")
    svc = WebService(
        MemoryStore(),
        settings=s,
        http_factory=lambda: HttpClient(
            "https://api.test/trade-api/v2",
            rate_per_second=1000,
            transport=httpx.MockTransport(fake.handler),
            sleep=lambda _: None,
        ),
    )
    webapp.set_service(svc)
    yield svc, fake
    webapp.set_service(None)


def login(app=webapp.app):
    status, headers, _ = call(app, "POST", "/api/auth/login", {"password": PASSWORD}, {"x-omen-csrf": "1"})
    assert status == 200
    return headers["set-cookie"].split(";")[0]


# ------------------------------------------------------------------------------------------------------- auth
def test_health_is_public_and_everything_else_needs_a_session(site):
    assert j(call(webapp.app, "GET", "/api/health"))["setup_required"] is False
    assert call(webapp.app, "GET", "/api/dashboard")[0] == 401
    cookie = login()
    status, headers, body = call(webapp.app, "GET", "/api/dashboard", cookie=cookie)
    assert status == 200 and json.loads(body)["plays"] == []
    assert headers["cache-control"] == "no-store" and headers["x-frame-options"] == "DENY"


def test_session_cookie_is_hardened_and_tamper_evident(site):
    status, headers, _ = call(
        webapp.app,
        "POST",
        "/api/auth/login",
        {"password": PASSWORD},
        {"x-omen-csrf": "1", "x-forwarded-proto": "https"},
    )
    cookie = headers["set-cookie"]
    assert status == 200 and "HttpOnly" in cookie and "SameSite=Strict" in cookie and "Secure" in cookie
    value = cookie.split(";")[0].split("=", 1)[1]
    assert auth.valid_session(value)
    assert not auth.valid_session(value[:-1] + ("0" if value[-1] != "0" else "1"))
    expired = auth.make_session(now=datetime.now(UTC).timestamp() - 31 * 86400)
    assert not auth.valid_session(expired)


def test_writes_require_the_csrf_header(site):
    assert call(webapp.app, "POST", "/api/auth/login", {"password": PASSWORD})[0] == 403
    cookie = login()
    assert call(webapp.app, "POST", "/api/reverify", {"key": "x"}, cookie=cookie)[0] == 403


def test_wrong_passwords_are_throttled(site, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _: None)
    for _ in range(auth.LOGIN_MAX_ATTEMPTS):
        assert call(webapp.app, "POST", "/api/auth/login", {"password": "nope"}, {"x-omen-csrf": "1"})[0] == 401
    assert call(webapp.app, "POST", "/api/auth/login", {"password": PASSWORD}, {"x-omen-csrf": "1"})[0] == 429


def test_setup_required_without_a_password(site, monkeypatch):
    monkeypatch.delenv("OMEN_WEB_PASSWORD")
    assert j(call(webapp.app, "GET", "/api/health"))["setup_required"] is True
    status, _, body = call(webapp.app, "POST", "/api/auth/login", {"password": "x"}, {"x-omen-csrf": "1"})
    assert status == 503 and json.loads(body)["setup_required"] is True


def test_vercel_rewrite_path_is_resolved(site):
    cookie = login()
    assert call(webapp.app, "GET", "/api/index?__path=dashboard", cookie=cookie)[0] == 200
    assert call(webapp.app, "GET", "/api/index?__path=nope", cookie=cookie)[0] == 404


# ----------------------------------------------------------------------------------------------------- ingest
def feed(plays=None):
    return {"version": 1, "omen_version": "test", "host": "pi", "plays": plays or [], "paper": {"totals": []}}


def signed(body: bytes, ts=None, secret=SECRET):
    ts = int(ts or datetime.now(UTC).timestamp())
    return {"x-omen-ts": str(ts), "x-omen-sig": sign(secret, ts, body), "content-type": "application/json"}


def test_ingest_accepts_only_fresh_signed_snapshots(site):
    play = {"key": "no_basket|A:no", "profit": 3.5, "found_at": "2026-10-23T15:00:00+00:00", "title": "t"}
    body = json.dumps(feed([play])).encode()
    assert call(webapp.app, "POST", "/api/ingest", body, signed(body, secret="wrong-secret-0000000"))[0] == 401
    stale = datetime.now(UTC).timestamp() - 600
    assert call(webapp.app, "POST", "/api/ingest", body, signed(body, ts=stale))[0] == 401
    headers = signed(body)
    assert call(webapp.app, "POST", "/api/ingest", body, headers)[0] == 200
    assert call(webapp.app, "POST", "/api/ingest", body, headers)[0] == 409  # replay
    dash = j(call(webapp.app, "GET", "/api/dashboard", cookie=login()))
    assert [p["key"] for p in dash["plays"]] == ["no_basket|A:no"]
    assert dash["feed_meta"]["host"] == "pi" and dash["feed_age_seconds"] is not None


def test_ingest_is_disabled_without_a_secret(site, monkeypatch):
    monkeypatch.delenv("OMEN_INGEST_SECRET")
    body = json.dumps(feed()).encode()
    assert call(webapp.app, "POST", "/api/ingest", body, signed(body))[0] == 503


def test_publisher_signs_what_the_site_verifies(site):
    svc, _ = site
    seen = []

    def forward(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        status, _, body = call(webapp.app, "POST", "/api/ingest", request.content, dict(request.headers))
        return httpx.Response(status, content=body)

    pub = Publisher("https://omen.example", SECRET, transport=httpx.MockTransport(forward))
    assert pub.send(feed([{"key": "k", "profit": 1.0}]))
    assert seen[0].url.path == "/api/ingest"
    assert svc.store.get("omen:feed") is not None


def test_publisher_backs_off_and_publishes_on_change_or_heartbeat():
    t = [1000.0]
    pub = Publisher(
        "https://omen.example",
        SECRET,
        interval_seconds=120,
        transport=httpx.MockTransport(lambda r: httpx.Response(500, text="boom")),
        clock=lambda: t[0],
    )
    assert not pub.send(feed())
    assert pub.failures == 1 and pub.last_error.startswith("HTTP 500") and not pub.due("x")
    t[0] += 61
    assert pub.due("x")
    pub.client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": True})))
    assert pub.send(feed(), "sig-a") and pub.failures == 0
    t[0] += 30
    assert not pub.due("sig-a")  # unchanged and before the heartbeat
    assert pub.due("sig-b")  # plays changed
    t[0] += 100
    assert pub.due("sig-a")  # heartbeat


def test_fit_sheds_history_before_plays():
    big = feed([{"key": f"p{i}", "profit": 1} for i in range(5)])
    big["activity"] = [{"title": "x" * 500} for _ in range(400)]
    big["runs"] = [{"kind": "hot"} for _ in range(50)]
    big["paper"] = {"open": [], "settled": [{"id": "y" * 400} for _ in range(400)], "totals": []}
    big["fees"] = {"upcoming": []}
    out = json.loads(fit(big, limit=60_000))
    assert len(out["plays"]) == 5 and len(out["activity"]) < 400 and len(out["paper"]["settled"]) < 400
    assert len(json.dumps(out)) <= 60_000


# ------------------------------------------------------------------------------------------------------- scans
def scan_events(cookie, body):
    status, headers, raw = call(webapp.app, "POST", "/api/scan", body, {"x-omen-csrf": "1"}, cookie=cookie)
    assert status == 200 and headers["content-type"].startswith("application/x-ndjson")
    return [json.loads(line) for line in raw.decode().splitlines() if line]


def test_scan_streams_stages_and_a_verified_play(site):
    svc, _ = site
    svc.min_scan_gap = 0
    cookie = login()
    events = scan_events(cookie, {"horizon_hours": "all"})
    kinds = [e["type"] for e in events]
    assert kinds[0] == "start" and kinds[-1] == "result"
    assert {e["stage"] for e in events if e["type"] == "stage"} >= {"fees", "markets", "screen", "verify"}
    result = events[-1]["result"]
    assert result["stats"]["markets"] == 3 and not result["stats"]["partial"]
    assert [p["strategy"] for p in result["plays"]] == ["no_basket"]
    play = result["plays"][0]
    assert play["profit"] > 0 and all(leg["order"] for leg in play["legs"])
    dash = j(call(webapp.app, "GET", "/api/dashboard", cookie=cookie))
    assert dash["last_scan"]["id"] == result["id"] and dash["budget"]["scans_today"] == 1
    assert [p["key"] for p in dash["plays"]] == [play["key"]]


def test_scans_are_rate_limited_and_budgeted(site):
    svc, _ = site
    cookie = login()
    scan_events(cookie, {"horizon_hours": 24})
    status, _, body = call(webapp.app, "POST", "/api/scan", {}, {"x-omen-csrf": "1"}, cookie=cookie)
    assert status == 429 and json.loads(body)["retry_after"] > 0
    svc.min_scan_gap = 0
    svc.daily_scan_cap = 1
    assert call(webapp.app, "POST", "/api/scan", {}, {"x-omen-csrf": "1"}, cookie=cookie)[0] == 429


def test_scan_parameters_are_validated(site):
    cookie = login()
    for bad in ({"horizon_hours": 0.5}, {"strategies": ["moon"]}, {"min_profit_usd": "abc"}):
        assert call(webapp.app, "POST", "/api/scan", bad, {"x-omen-csrf": "1"}, cookie=cookie)[0] == 400


def test_reverify_reports_live_or_gone(site):
    svc, fake = site
    svc.min_scan_gap = 0
    cookie = login()
    play = scan_events(cookie, {})[-1]["result"]["plays"][0]
    status, _, body = call(webapp.app, "POST", "/api/reverify", {"key": play["key"]}, {"x-omen-csrf": "1"}, cookie)
    assert status == 200 and json.loads(body)["status"] == "live"
    fake.yes_bids = {"A": "0.30", "B": "0.30", "C": "0.30"}
    status, _, body = call(webapp.app, "POST", "/api/reverify", {"key": play["key"]}, {"x-omen-csrf": "1"}, cookie)
    assert status == 200 and json.loads(body)["status"] == "gone"


def test_fee_quote_uses_live_series_terms(site):
    _, fake = site
    fake.series_mult = 2
    cookie = login()
    status, _, body = call(
        webapp.app,
        "POST",
        "/api/fees/quote",
        {"series": "kxelect", "price": 0.5, "count": 100},
        {"x-omen-csrf": "1"},
        cookie,
    )
    q = json.loads(body)
    assert status == 200 and q["multiplier"] == 2 and q["taker_fee"] == 3.5  # 2 x 0.07 x 100 x 0.25
    assert j(call(webapp.app, "GET", "/api/series?q=elect", cookie=cookie))["series"][0]["ticker"] == "KXELECT"


# ------------------------------------------------------------------------------------------------- near misses
def test_near_misses_skip_candidate_lists_that_are_not_partitions():
    reg = registry({"KXTEST": ("quadratic", "1")})
    now = datetime(2026, 10, 23, 21, 0, tzinfo=UTC)

    def fee(m):
        return reg.quote(m.event_ticker.split("-")[0], m.event_ticker, now)

    # two named candidates of an election with others possible: cheap YES asks are not a near miss
    ms = [market(f"KXTEST-26-{c}", yes_bid="0.04", yes_ask="0.06", sub=c) for c in "AB"]
    rows = near_misses({"KXTEST-26OCT30": event()}, {"KXTEST-26OCT30": ms}, ms, fee)
    assert all(r["strategy"] != "yes_partition" for r in rows)
    # a real partition (below 10 / 10 or above) is listed
    lo = market("KXTEST-26-L", strike_type="less", cap="10", yes_ask="0.45", yes_bid="0.40")
    hi = market("KXTEST-26-H", strike_type="greater_or_equal", floor="10", yes_ask="0.50", yes_bid="0.45")
    rows = near_misses({"KXTEST-26OCT30": event()}, {"KXTEST-26OCT30": [lo, hi]}, [lo, hi], fee)
    assert any(r["strategy"] == "yes_partition" for r in rows)


def test_sweep_deadline_marks_partial(site):
    svc, _ = site
    eng = svc.engine
    now = datetime.now(UTC)
    eng.refresh_fees(now)
    assert eng.sweep("full", now, deadline=0.0) == 1 and eng.sweep_partial  # out of time after the first market
    assert eng.sweep("full", now) == 3 and not eng.sweep_partial


def test_fee_pages_do_not_wait_for_a_running_scan(site):
    svc, _ = site
    cookie = login()
    done = []
    with svc.lock:  # a scan holds the scan engine
        t = threading.Thread(target=lambda: done.append(call(webapp.app, "GET", "/api/fees", cookie=cookie)[0]))
        t.start()
        t.join(timeout=10)
    assert done == [200]


def test_atlas_round_trip_from_the_pi_and_only_when_it_changes(site, tmp_path):
    cookie = login()
    sent = []

    def forward(request: httpx.Request) -> httpx.Response:
        sent.append(request.url.path)
        status, _, body = call(webapp.app, "POST", request.url.path, request.content, dict(request.headers))
        return httpx.Response(status, content=body)

    path = tmp_path / "atlas.json"
    doc = {
        "version": 1,
        "generated_at": "2026-10-24T00:00:00+00:00",
        "markets": 9000,
        "cells": [],
        "overall": [],
        "edges": [{"category": "Weather", "horizon": "24h", "band": [0.05, 0.1]}],
    }
    path.write_text(json.dumps(doc))
    pub = Publisher("https://omen.example", SECRET, transport=httpx.MockTransport(forward))
    assert pub.maybe_publish_atlas(path)
    assert not pub.maybe_publish_atlas(path)  # unchanged: not re-sent
    assert sent == ["/api/ingest/atlas"]
    got = j(call(webapp.app, "GET", "/api/atlas", cookie=cookie))
    assert got["source"] == "pi" and got["markets"] == 9000 and len(got["edges"]) == 1
    forged = json.dumps(doc).encode()
    assert (
        call(webapp.app, "POST", "/api/ingest/atlas", forged, signed(forged, secret="wrong-secret-0000000"))[0] == 401
    )
