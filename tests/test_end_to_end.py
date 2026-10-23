"""The whole pipeline against a fake Kalshi API and a fake Discord: scan -> verify -> alert -> paper -> gone ->
settle, plus fee-change notices."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from urllib.parse import parse_qs, urlsplit

import httpx

from helpers import ROOT
from omen.alerts.discord import DiscordTransport, Notifier, build_routes
from omen.config import load_settings
from omen.engine import Engine
from omen.http import HttpClient
from omen.kalshi.client import KalshiClient
from omen.ledger import Ledger
from omen.runner import Runner

HOOK = "https://discord.com/api/webhooks/123456789012345678/abcdefghijklmnopqrstuvwxyz_ABCDEFG-123"
T0 = datetime(2026, 10, 23, 15, 0, tzinfo=UTC)
CLOSE = T0 + timedelta(days=2)


def iso(ts):
    return ts.isoformat().replace("+00:00", "Z")


class FakeKalshi:
    def __init__(self):
        self.yes_bids = {"A": "0.40", "B": "0.35", "C": "0.33"}
        self.series_mult = 1
        self.event_changes = []
        self.settled: dict[str, str] = {}
        self.trading = True
        self.listed = True  # False: the event's markets have closed and no longer appear in sweeps

    def market(self, c):
        t = f"KXELECT-26-{c}"
        d = {
            "ticker": t,
            "event_ticker": "KXELECT-26",
            "status": "active",
            "title": f"Will {c} win?",
            "yes_sub_title": f"{c}",
            "rules_primary": f"If {c} wins, Yes.",
            "rules_secondary": "",
            "yes_bid_dollars": self.yes_bids[c],
            "yes_ask_dollars": str(D(self.yes_bids[c]) + D("0.02")),
            "yes_bid_size_fp": "100.00",
            "yes_ask_size_fp": "100.00",
            "close_time": iso(CLOSE),
            "market_type": "binary",
            "price_ranges": [{"start": "0", "end": "1", "step": "0.01"}],
        }
        if t in self.settled:
            d.update(status="finalized", result=self.settled[t])
        return d

    def handler(self, request: httpx.Request) -> httpx.Response:
        u = urlsplit(str(request.url))
        q = parse_qs(u.query)
        path = u.path.replace("/trade-api/v2", "")
        if path == "/exchange/status":
            return httpx.Response(200, json={"exchange_active": True, "trading_active": self.trading})
        if path == "/series":
            return httpx.Response(
                200,
                json={
                    "series": [
                        {
                            "ticker": "KXELECT",
                            "title": "Election",
                            "category": "Elections",
                            "fee_type": "quadratic",
                            "fee_multiplier": self.series_mult,
                        }
                    ]
                },
            )
        if path == "/series/fee_changes":
            return httpx.Response(200, json={"series_fee_change_arr": []})
        if path == "/events/fee_changes":
            return httpx.Response(200, json={"event_fee_changes": self.event_changes, "cursor": ""})
        if path == "/events":
            return httpx.Response(
                200,
                json={
                    "events": [
                        {
                            "event_ticker": "KXELECT-26",
                            "series_ticker": "KXELECT",
                            "title": "Who wins?",
                            "mutually_exclusive": True,
                            "collateral_return_type": "MECNET",
                        }
                    ],
                    "cursor": "",
                },
            )
        if path == "/markets":
            if "tickers" in q:
                want = q["tickers"][0].split(",")
                return httpx.Response(
                    200, json={"markets": [self.market(t.rsplit("-", 1)[1]) for t in want], "cursor": ""}
                )
            listed = [self.market(c) for c in "ABC"] if self.listed else []
            return httpx.Response(200, json={"markets": listed, "cursor": ""})
        if path == "/markets/orderbooks":
            books = []
            for t in q["tickers"]:
                c = t.rsplit("-", 1)[1]
                books.append(
                    {
                        "ticker": t,
                        "orderbook_fp": {
                            "yes_dollars": [[self.yes_bids[c], "100.00"]],
                            "no_dollars": [["0.50", "100.00"]],
                        },
                    }
                )
            return httpx.Response(200, json={"orderbooks": books})
        return httpx.Response(404, json={"error": path})


def build(tmp_path, fake, posts, clock):
    def discord(request):
        posts.append((request.method, json.loads(request.content)))
        return httpx.Response(200, json={"id": str(len(posts))})

    s = load_settings(ROOT / "config" / "omen.toml", env={"OMEN_PROJECT_ROOT": str(ROOT)})
    s.crypto.enabled = False
    s.lock.min_apr_pct = D("0")
    ledger = Ledger(tmp_path / "omen.sqlite3")
    http = HttpClient(
        "https://api.test/trade-api/v2",
        rate_per_second=1000,
        transport=httpx.MockTransport(fake.handler),
        sleep=lambda s: None,
    )
    engine = Engine(s, KalshiClient(http), ledger, clock=clock)
    notifier = Notifier(
        ledger,
        build_routes({"default": HOOK}, None, {}),
        DiscordTransport(transport=httpx.MockTransport(discord), sleep=lambda s: None),
    )
    no_network = httpx.MockTransport(lambda r: httpx.Response(404))
    runner = Runner(s, engine, ledger, notifier, clock=clock, fee_doc_transport=no_network)
    return runner, ledger


def test_pipeline(tmp_path):
    fake, posts = FakeKalshi(), []
    now = {"t": T0}
    clock = lambda: now["t"]  # noqa: E731
    runner, ledger = build(tmp_path, fake, posts, clock)

    report = runner.run_once("full")
    assert report.opportunities == 1 and report.alerts == 1
    plays = [p for m, p in posts if m == "POST" and "LOCK" in p["embeds"][0]["title"]]
    assert len(plays) == 1
    assert "Guaranteed profit **$3.17**" in plays[0]["embeds"][0]["description"]
    paper = ledger.open_paper_positions()
    assert len(paper) == 1 and D(paper[0]["expected_profit"]) == D("3.17")

    # same play next cycle: no duplicate alert
    now["t"] = T0 + timedelta(minutes=5)
    assert runner.scan_cycle("hot").alerts == 0

    # a fee increase is announced and the edge disappears -> alert edited to GONE
    fake.series_mult = 2
    ledger.set_meta("series_refreshed_at", "2000-01-01T00:00:00+00:00")
    now["t"] = T0 + timedelta(minutes=10)
    runner.scan_cycle("hot")
    runner.flush()
    titles = [p["embeds"][0]["title"] for _, p in posts]
    assert any("Fee terms changed: KXELECT" in t for t in titles)
    edits = [p for m, p in posts if m == "PATCH"]
    assert edits and edits[-1]["embeds"][0]["title"].startswith("⌛ GONE")

    # settlement: A wins -> NO on B and C pay, NO on A loses: payout 200 on 196.83 capital
    fake.settled = {"KXELECT-26-A": "yes", "KXELECT-26-B": "no", "KXELECT-26-C": "no"}
    now["t"] = CLOSE + timedelta(hours=1)
    runner.settle_cycle()
    row = ledger.db.execute("SELECT * FROM paper_positions").fetchone()
    assert row["status"] == "settled" and D(row["payout"]) == D("200") and D(row["pnl"]) == D("3.17")


def test_event_fee_override_is_used(tmp_path):
    fake, posts = FakeKalshi(), []
    fake.event_changes = [
        {
            "id": "x",
            "event_ticker": "KXELECT-26",
            "series_ticker": "KXELECT",
            "fee_type_override": "quadratic",
            "fee_multiplier_override": 3,
            "scheduled_ts": iso(T0 - timedelta(hours=1)),
        }
    ]
    runner, _ = build(tmp_path, fake, posts, lambda: T0)
    report = runner.run_once("full")
    assert report.opportunities == 0  # triple fees on this event erase the edge


def test_no_plays_while_trading_is_paused(tmp_path):
    fake, posts = FakeKalshi(), []
    fake.trading = False
    runner, _ = build(tmp_path, fake, posts, lambda: T0)
    report = runner.run_once("full")
    assert report.opportunities == 0
    assert any("trading is paused" in p["embeds"][0]["title"] for _, p in posts)


def test_improved_play_realerts_and_supersedes_old_message(tmp_path):
    fake, posts = FakeKalshi(), []
    now = {"t": T0}
    runner, _ = build(tmp_path, fake, posts, lambda: now["t"])
    runner.run_once("full")
    first = [p for m, p in posts if m == "POST" and "LOCK" in p["embeds"][0]["title"]]
    assert len(first) == 1
    # an hour later the YES bids rose: the basket is worth much more -> one new alert, old one edited
    fake.yes_bids = {"A": "0.45", "B": "0.38", "C": "0.35"}
    now["t"] = T0 + timedelta(minutes=61)
    runner.scan_cycle("hot")
    runner.flush()
    plays = [p for m, p in posts if m == "POST" and "LOCK" in p["embeds"][0]["title"]]
    edits = [p for m, p in posts if m == "PATCH"]
    assert len(plays) == 2 and edits[-1]["embeds"][0]["title"].startswith("↻ UPDATED")


def test_play_is_gone_once_its_markets_close(tmp_path):
    fake, posts = FakeKalshi(), []
    now = {"t": T0}
    runner, ledger = build(tmp_path, fake, posts, lambda: now["t"])
    runner.run_once("full")
    assert len(ledger.live_opportunities()) == 1
    # trading ends: the markets vanish from open-market sweeps, so they can never be "re-checked"
    fake.listed = False
    now["t"] = T0 + timedelta(hours=1)
    runner.scan_cycle("hot")
    assert len(ledger.live_opportunities()) == 1  # still before the close: absence alone proves nothing
    now["t"] = CLOSE + timedelta(minutes=1)
    runner.scan_cycle("hot")
    assert ledger.live_opportunities() == []
