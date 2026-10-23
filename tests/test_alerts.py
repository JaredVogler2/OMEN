import json
from decimal import Decimal as D

import httpx
import pytest

from helpers import NOW, book, event, market, registry
from omen.alerts.discord import DiscordConfigError, DiscordTransport, Notifier, Route, build_routes, resolve
from omen.alerts.render import fit_embed, gone_payload, play_payload
from omen.config import LockSettings
from omen.ledger import Ledger
from omen.strategies.lock import VerifyContext, screen_no_baskets, verify

HOOK = "https://discord.com/api/webhooks/123456789012345678/abcdefghijklmnopqrstuvwxyz_ABCDEFG-123"
REG = registry()


def no_basket_opp():
    ev = event()
    ms = [
        market(f"KXTEST-26OCT30-{c}", yes_bid=p, sub=f"{c} wins")
        for c, p in zip("ABC", ["0.40", "0.35", "0.33"], strict=True)
    ]
    cands = screen_no_baskets({ev.event_ticker: ev}, {ev.event_ticker: ms}, lambda m: REG.quote("KXTEST", None, NOW))
    books = {m.ticker: book(m.ticker, yes=[(str(m.yes_bid), "100")]) for m in ms}
    ctx = VerifyContext(
        books,
        lambda m: REG.quote("KXTEST", None, NOW),
        LockSettings(min_profit_usd=D("0.5"), min_return_pct=D("0.5"), min_apr_pct=D("0")),
        NOW,
    )
    return verify(cands[0], ctx)


def test_routes_validate_urls_and_keys():
    routes = build_routes({"plays": HOOK}, None, {})
    assert resolve(routes, "plays").kind == "webhook"
    assert resolve(routes, "status") is None
    routes = build_routes({"default": HOOK}, None, {})
    assert resolve(routes, "status").target == HOOK
    with pytest.raises(DiscordConfigError):
        build_routes({"plays": "https://evil.example.com/api/webhooks/1/abc"}, None, {})
    with pytest.raises(DiscordConfigError):
        build_routes({"plays": "http://discord.com/api/webhooks/123456789012345678/" + "a" * 30}, None, {})
    with pytest.raises(DiscordConfigError):
        build_routes({}, None, {"plays": "123"})  # channel map without a bot token
    with pytest.raises(DiscordConfigError):
        build_routes({"alerts": HOOK}, None, {})
    bot = build_routes({}, "token", {"plays": "111222333444555666"})
    assert bot["plays"] == Route("bot", "111222333444555666")
    assert "abcdef" not in routes["default"].redacted()


def test_play_embed_contents_and_limits():
    opp = no_basket_opp()
    payload = play_payload(opp, NOW)
    emb = payload["embeds"][0]
    assert emb["title"].startswith("🔒 LOCK · NO basket")
    assert "Guaranteed profit" in emb["description"]
    orders = next(f for f in emb["fields"] if f["name"].startswith("Orders"))["value"]
    assert "BUY NO" in orders and "KXTEST-26OCT30-A" in orders
    assert opp.short_id in emb["footer"]["text"]
    big = {
        "title": "x" * 500,
        "description": "y" * 5000,
        "fields": [{"name": "n", "value": "v" * 2000}] * 30,
        "footer": {"text": "f"},
    }
    fitted = fit_embed(big)
    total = (
        len(fitted["title"])
        + len(fitted["description"])
        + sum(len(f["name"]) + len(f["value"]) for f in fitted["fields"])
    )
    assert len(fitted["title"]) <= 256 and len(fitted["fields"]) <= 25 and total <= 6000


def test_outbox_delivery_retries_and_edits():
    calls = []
    state = {"fail": True}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, str(request.url), json.loads(request.content)))
        if state["fail"]:
            return httpx.Response(500, text="boom")
        if request.method == "POST":
            return httpx.Response(200, json={"id": "999"})
        return httpx.Response(200, json={"id": "999"})

    ledger = Ledger(":memory:")
    routes = build_routes({"plays": HOOK}, None, {})
    notifier = Notifier(ledger, routes, DiscordTransport(transport=httpx.MockTransport(handler), sleep=lambda s: None))
    opp = no_basket_opp()
    ledger.observe(opp.key, opp.short_id, opp.strategy, opp.tier, opp.event_ticker, opp.title, opp.profit, {}, NOW)
    notifier.queue("plays", play_payload(opp, NOW), "play", opp.key, NOW)
    assert notifier.flush(NOW) == (0, 1)  # Discord down: stays queued
    state["fail"] = False
    assert notifier.flush(NOW) == (1, 0)
    assert calls[-1][1].endswith("?wait=true")
    assert calls[-1][2]["allowed_mentions"] == {"parse": []}  # never pings anyone
    row = ledger.db.execute("SELECT alert_message_id FROM opportunities WHERE key=?", (opp.key,)).fetchone()
    assert row["alert_message_id"] == "999"
    notifier.queue("plays", gone_payload("t", "id", NOW, NOW, D("1")), "gone", opp.key, NOW, edit_of="999")
    notifier.flush(NOW)
    assert calls[-1][0] == "PATCH" and calls[-1][1].endswith("/messages/999")


def test_discord_rate_limit_is_honoured():
    sleeps = []
    responses = iter([httpx.Response(429, json={"retry_after": 1.5}), httpx.Response(200, json={"id": "1"})])
    t = DiscordTransport(transport=httpx.MockTransport(lambda r: next(responses)), sleep=sleeps.append)
    assert t.send(Route("webhook", HOOK), {"content": "x"}) == "1"
    assert sleeps == [1.5]
