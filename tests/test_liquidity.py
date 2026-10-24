"""Liquidity-reward market making: filed scoring, quoting policy, paper fills, live order bodies, the service loop."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from omen.alerts.discord import Notifier
from omen.config import ConfigError, Settings, load_settings
from omen.http import HttpClient
from omen.kalshi.client import KalshiClient
from omen.kalshi.models import OrderBook
from omen.ledger import Ledger
from omen.liquidity.maker import Fill, LiveVenue, PaperVenue, Quote, QuotePolicy
from omen.liquidity.programs import Program, eligible
from omen.liquidity.scoring import payout, side_score, snapshot
from omen.liquidity.service import LiquidityService

T0 = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)


def book(ticker: str, yes: list[tuple[float, float]], no: list[tuple[float, float]]) -> OrderBook:
    return OrderBook.from_api(
        ticker,
        {
            "yes_dollars": [[f"{p:.4f}", f"{s:.2f}"] for p, s in yes],
            "no_dollars": [[f"{p:.4f}", f"{s:.2f}"] for p, s in no],
        },
    )


# ------------------------------------------------------------------------------------------------------ scoring
def test_reference_price_discount_and_whole_levels_qualify():
    # 300 at 0.40 reaches a fifth of 1000 -> reference 0.40; 0.39 completes the target, so ALL of it qualifies
    s = side_score([(0.40, 300), (0.39, 500), (0.30, 1000)], 1000, 0.5, ours=200, our_price=0.39)
    assert s.met and s.reference == pytest.approx(0.40)
    assert s.total == pytest.approx(300 + 700 * 0.5)  # the 0.30 level is never visited
    assert s.share == pytest.approx(100 / 650)
    big_level = side_score([(0.40, 5000)], 1000, 0.5, ours=1000, our_price=0.40)
    assert big_level.share == pytest.approx(1000 / 6000)  # no time priority inside a level


def test_a_side_short_of_target_excludes_the_snapshot_for_everyone():
    snap = snapshot([(0.05, 400)], [(0.93, 2000)], 1000, 0.5, our_yes=(0.05, 500))
    assert not snap.valid and snap.our_fraction == 0.0
    filled = snapshot([(0.05, 400)], [(0.93, 2000)], 1000, 0.5, our_yes=(0.05, 600))
    assert filled.valid and filled.our_fraction == pytest.approx((600 / 1000) / 2)


def test_payout_matches_kalshis_worked_example():
    # 20% of the period score, $100 reward, 8,000 of 10,000 snapshots valid -> $16.00; under $1 is not paid
    assert payout(0.2, 1.0, 100.0, 8000, 10000) == 16.00
    assert payout(0.001, 1.0, 100.0, 8000, 10000) == 0.0


# ------------------------------------------------------------------------------------------------------- policy
def test_policy_quotes_the_cheap_side_sized_to_the_capital_left():
    b = book("M", yes=[(0.05, 800)], no=[(0.93, 1500)])
    pol = QuotePolicy(capital_per_market=25, max_contracts=1000)
    assert pol.quotes(b, {}) == [Quote("M", "yes", 0.05, 500)]
    assert pol.quotes(b, {"yes": 20.0}) == [Quote("M", "yes", 0.05, 100)]  # inventory uses the budget
    assert pol.quotes(b, {"yes": 25.0}) == []
    assert QuotePolicy(sides="both", capital_per_market=20).quotes(b, {}) == [
        Quote("M", "yes", 0.05, 200)
    ]  # NO 0.93 > max


def test_policy_skips_one_sided_and_crossed_books():
    pol = QuotePolicy()
    assert pol.quotes(book("M", yes=[(0.05, 800)], no=[]), {}) == []
    assert pol.quotes(book("M", yes=[(0.50, 10)], no=[(0.50, 10)]), {}) == []


# ------------------------------------------------------------------------------------------------- paper venue
def trade(ticker: str, ts: datetime, yes_price: float, taker: str, count: float, tid: str = "t") -> dict:
    return {
        "ticker": ticker,
        "created_time": ts.isoformat().replace("+00:00", "Z"),
        "yes_price_dollars": f"{yes_price:.4f}",
        "taker_outcome_side": taker,
        "count_fp": f"{count:.2f}",
        "trade_id": tid,
    }


def test_paper_fills_wait_for_the_queue_then_fill_and_through_trades_fill_everything():
    b = book("M", yes=[(0.05, 300)], no=[(0.93, 2000)])
    v = PaperVenue()
    v.sync([Quote("M", "yes", 0.05, 100)], {"M": b}, T0)
    later = T0 + timedelta(seconds=30)
    assert v.fills([trade("M", later, 0.05, "no", 250, "a")], T0) == []  # 300 queued ahead of us
    got = v.fills([trade("M", later, 0.05, "no", 80, "b")], T0)
    assert [(f.qty, f.price) for f in got] == [(30.0, 0.05)]
    assert v.fills([trade("M", later, 0.05, "yes", 500, "c")], T0) == []  # takers buying YES do not hit YES bids
    got = v.fills([trade("M", later, 0.04, "no", 1, "d")], T0)  # traded through our price
    assert [(f.qty, f.side) for f in got] == [(70.0, "yes")] and not v.orders


def test_paper_no_bids_fill_from_takers_buying_yes_and_trades_before_placement_do_not_count():
    b = book("M", yes=[(0.90, 2000)], no=[(0.08, 0)])
    v = PaperVenue()
    v.sync([Quote("M", "no", 0.08, 50)], {"M": b}, T0)
    assert v.fills([trade("M", T0 - timedelta(seconds=5), 0.90, "yes", 500, "x")], T0) == []
    got = v.fills([trade("M", T0 + timedelta(seconds=5), 0.92, "yes", 10, "y")], T0)  # NO traded at 0.08
    assert [(f.side, f.qty, f.price) for f in got] == [("no", 10.0, 0.08)]


def test_paper_resync_keeps_queue_place_until_the_price_moves():
    b = book("M", yes=[(0.05, 300)], no=[(0.93, 2000)])
    v = PaperVenue()
    v.sync([Quote("M", "yes", 0.05, 100)], {"M": b}, T0)
    v.orders[("M", "yes")].ahead = 10
    v.sync([Quote("M", "yes", 0.05, 100)], {"M": b}, T0 + timedelta(minutes=1))
    assert v.orders[("M", "yes")].ahead == 10
    v.sync([Quote("M", "yes", 0.06, 100)], {"M": book("M", yes=[(0.06, 40)], no=[(0.93, 2000)])}, T0)
    assert v.orders[("M", "yes")].ahead == 40 and v.orders[("M", "yes")].price == 0.06


# -------------------------------------------------------------------------------------------------- live venue
class FakeTradingClient:
    def __init__(self):
        self.created: list[dict] = []
        self.cancelled: list[str] = []
        self.fills_resp: dict = {"fills": []}

    def create_order(self, body: dict) -> dict:
        self.created.append(body)
        return {"order_id": f"o{len(self.created)}", "fill_count": "0.00", "remaining_count": body["count"]}

    def _signed(self, method: str, path: str, body=None, params=None):
        if method == "DELETE":
            self.cancelled.append(path.rsplit("/", 1)[-1])
            return {}
        if path == "/portfolio/orders":
            return {
                "orders": [
                    {"order_id": "old1", "ticker": "M", "client_order_id": "omen-lp-abc"},
                    {"order_id": "manual", "ticker": "M", "client_order_id": "my-own-order"},
                ],
                "cursor": "",
            }
        return self.fills_resp


def test_live_orders_are_post_only_gtc_and_no_bids_become_yes_asks():
    yes = LiveVenue.order_body(Quote("M", "yes", 0.05, 500), "c1")
    no = LiveVenue.order_body(Quote("M", "no", 0.08, 300), "c2")
    assert (yes["side"], yes["price"], yes["count"]) == ("bid", "0.0500", "500.00")
    assert (no["side"], no["price"]) == ("ask", "0.9200")
    for b in (yes, no):
        assert (
            b["post_only"] is True and b["time_in_force"] == "good_till_canceled" and b["cancel_order_on_pause"] is True
        )


def test_live_sync_places_keeps_and_cancels_and_reads_our_fills():
    c = FakeTradingClient()
    v = LiveVenue(c)
    v.sync([Quote("M", "yes", 0.05, 500)], {}, T0)
    v.sync([Quote("M", "yes", 0.05, 500)], {}, T0)  # unchanged: no new order
    assert len(c.created) == 1
    c.fills_resp = {
        "fills": [
            {
                "fill_id": "f1",
                "order_id": "o1",
                "ticker": "M",
                "outcome_side": "yes",
                "book_side": "bid",
                "count_fp": "300.00",
                "yes_price_dollars": "0.0500",
                "no_price_dollars": "0.9500",
                "is_taker": False,
                "fee_cost": "0.0000",
                "created_time": "2026-10-24T12:00:30Z",
            }
        ]
    }
    got = v.fills([], T0 + timedelta(minutes=1))
    assert [(f.side, f.qty, f.price) for f in got] == [("yes", 300.0, 0.05)]
    assert v.fills([], T0 + timedelta(minutes=2)) == []  # the same fill is not counted twice
    v.sync([Quote("M", "yes", 0.06, 500)], {}, T0)  # price moved: cancel and replace
    assert c.cancelled == ["o1"] and c.created[-1]["price"] == "0.0600"
    v.cancel_all()
    assert c.cancelled == ["o1", "o2"] and not v.orders


def test_live_ignores_fills_of_orders_it_did_not_place_and_cleans_up_a_previous_run():
    c = FakeTradingClient()
    v = LiveVenue(c)
    c.fills_resp = {
        "fills": [
            {
                "fill_id": "f9",
                "order_id": "someone-else",
                "ticker": "M",
                "outcome_side": "yes",
                "count_fp": "5.00",
                "yes_price_dollars": "0.5000",
                "no_price_dollars": "0.5000",
                "is_taker": True,
                "fee_cost": "0.0200",
                "created_time": "2026-10-24T12:00:30Z",
            }
        ]
    }
    assert v.fills([], T0) == []
    assert v.cancel_stale() == 1 and c.cancelled == ["old1"]


# ---------------------------------------------------------------------------------------------------- programs
def test_eligible_drops_short_excluded_and_ending_programs():
    def prog(ticker, hours, per_day, ends_in=timedelta(days=1)):
        return Program(ticker, T0 + ends_in - timedelta(hours=hours), T0 + ends_in, per_day * hours / 24, 1000, 0.5)

    progs = [
        prog("KXRT-A-90", 24, 100),
        prog("KXEURUSD15M-X", 0.25, 3000),
        prog("KXRT-B-80", 24, 2),
        prog("KXAAAGASD-Y", 24, 150),
        prog("KXRT-C-70", 24, 100, ends_in=timedelta(minutes=5)),
    ]
    got = eligible(
        progs,
        T0,
        min_hours=12,
        min_reward_per_day=5,
        exclude=r"15M$|^KXAAAGAS",
        min_time_left=timedelta(minutes=10),
        limit=10,
    )
    assert [p.ticker for p in got] == ["KXRT-A-90"]


# ----------------------------------------------------------------------------------------------- service loop
class FakeKalshi:
    """Just enough of the public API for one rewarded market."""

    def __init__(self):
        self.now = T0
        self.status = "active"
        self.result = ""
        self.trades: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        u = urlsplit(str(request.url))
        q = parse_qs(u.query)
        if u.path.endswith("/incentive_programs"):
            return httpx.Response(
                200,
                json={
                    "incentive_programs": [
                        {
                            "market_ticker": "KXRT-FILM-90",
                            "incentive_type": "liquidity",
                            "start_date": "2026-10-24T00:00:00Z",
                            "end_date": "2026-10-25T00:00:00Z",
                            "period_reward": 1_000_000,
                            "target_size_fp": "1000.00",
                            "discount_factor_bps": 5000,
                        }
                    ],
                    "next_cursor": "",
                },
            )
        if u.path.endswith("/markets/orderbooks"):
            return httpx.Response(
                200,
                json={
                    "orderbooks": [
                        {
                            "ticker": "KXRT-FILM-90",
                            "orderbook_fp": {
                                "yes_dollars": [["0.0500", "500.00"]],
                                "no_dollars": [["0.9300", "2000.00"]],
                            },
                        }
                    ]
                },
            )
        if u.path.endswith("/markets/trades"):
            return httpx.Response(200, json={"trades": self.trades, "cursor": ""})
        if u.path.endswith("/markets"):
            return httpx.Response(
                200,
                json={
                    "markets": [
                        {
                            "ticker": t,
                            "event_ticker": "KXRT-FILM",
                            "status": self.status,
                            "result": self.result,
                            "close_time": "2026-11-01T00:00:00Z",
                            "market_type": "binary",
                        }
                        for t in q.get("tickers", [""])[0].split(",")
                    ],
                    "cursor": "",
                },
            )
        return httpx.Response(404)


def service(tmp_path, fake: FakeKalshi) -> tuple[LiquidityService, Ledger]:
    s = Settings()
    s.state_dir = tmp_path
    s.liquidity.capital_per_market_usd = 25
    client = KalshiClient(
        HttpClient(
            "https://api.test/trade-api/v2",
            rate_per_second=1000,
            transport=httpx.MockTransport(fake.handler),
            sleep=lambda _: None,
        )
    )
    ledger = Ledger(":memory:")
    svc = LiquidityService(
        s, client, ledger, Notifier(ledger, {}, None, dry_run=True), PaperVenue(), clock=lambda: fake.now
    )
    return svc, ledger


def test_service_quotes_accrues_rewards_records_fills_and_settles(tmp_path):
    fake = FakeKalshi()
    svc, ledger = service(tmp_path, fake)
    st = svc.step()
    assert st["quotes"] == 1 and svc.venue.orders[("KXRT-FILM-90", "yes")].size == 500
    # 500 others + our 500 at 0.05 -> half the YES side; NO side all others -> our fraction 0.25 of $100/day
    assert st["reward_per_hour"] == pytest.approx(100 / 24 * 0.25, rel=1e-3)
    fake.now = T0 + timedelta(minutes=1)
    fake.trades = [trade("KXRT-FILM-90", T0 + timedelta(seconds=20), 0.04, "no", 1, "z")]  # through our bid
    svc.step()
    # we were filled during that minute, so it earned the average of the start rate and zero
    assert ledger.lp_summary("paper")["reward_usd"] == pytest.approx(100 / 86400 * 0.25 * 60 / 2, rel=1e-3)
    assert ledger.lp_positions("paper") == {"KXRT-FILM-90": {"yes": [500.0, 25.0], "no": [0.0, 0.0]}}
    assert ("KXRT-FILM-90", "yes") not in svc.venue.orders  # the inventory cap is used: no new bid
    fake.now = T0 + timedelta(minutes=10)
    fake.status, fake.result = "finalized", "no"
    svc._refreshed = None
    svc.step()
    assert ledger.lp_summary("paper")["settled_pnl"] == pytest.approx(-25.0)
    assert ledger.lp_positions("paper") == {}


def test_kill_switch_cancels_everything(tmp_path):
    fake = FakeKalshi()
    svc, _ = service(tmp_path, fake)
    svc.step()
    assert svc.venue.orders
    (tmp_path / "KILL").write_text("stop")
    fake.now = T0 + timedelta(minutes=1)
    assert svc.step()["killed"] and not svc.venue.orders


def test_ledger_settles_fractional_payouts():
    ledger = Ledger(":memory:")
    ledger.lp_record_fill("paper", Fill(T0, "M", "yes", 0.40, 10, 0.0, "o", "t1"))
    ledger.lp_record_fill("paper", Fill(T0, "M", "no", 0.50, 4, 0.02, "o2", "t2"))
    assert not ledger.lp_record_fill("paper", Fill(T0, "M", "yes", 0.40, 10, 0.0, "o", "t1"))  # duplicate
    assert ledger.lp_settle("paper", "M", 0.25, T0) == pytest.approx(10 * 0.25 + 4 * 0.75 - (4.0 + 2.02))


def test_liquidity_settings_validation(tmp_path):
    assert load_settings(env={"OMEN_LP_MODE": "live"}).liquidity.mode == "live"
    with pytest.raises(ConfigError):
        load_settings(env={"OMEN_LP_MODE": "yolo"})
    cfg = tmp_path / "omen.toml"
    cfg.write_text('[liquidity]\nsides = "everything"\n')
    with pytest.raises(ConfigError):
        load_settings(cfg, env={})


def test_published_feed_carries_the_liquidity_block():
    from datetime import date
    from types import SimpleNamespace

    from omen.snapshot import build_feed

    ledger = Ledger(":memory:")
    ledger.lp_record_fill("paper", Fill(T0, "KXRT-FILM-90", "yes", 0.05, 500, 0.0, "o", "t1"))
    ledger.lp_add_reward("paper", T0, "KXRT-FILM-90", 1.25, 60)
    engine = SimpleNamespace(
        fees=SimpleNamespace(upcoming_changes=lambda now, horizon: [], series={}),
        schedule=SimpleNamespace(version="test", verified_on=date(2026, 10, 1)),
        markets={},
    )
    feed = build_feed(ledger, engine, [], Settings(), T0 + timedelta(minutes=5))
    lp = feed["liquidity"]
    assert lp["mode"] == "paper" and lp["day"]["reward_usd"] == pytest.approx(1.25)
    assert lp["inventory"] == [{"ticker": "KXRT-FILM-90", "side": "yes", "qty": 500.0, "cost": 25.0}]
    assert lp["by_series"] == [{"series": "KXRT", "usd": 1.25}] and len(lp["fills"]) == 1


def test_capital_is_capped_per_series(tmp_path):
    fake = FakeKalshi()
    svc, _ = service(tmp_path, fake)
    svc.cfg.max_total_capital_usd = 100
    svc.cfg.max_series_share = 0.2  # $20 for KXRT, less than the $25 quote
    assert svc.step()["quotes"] == 0
    svc.cfg.max_series_share = 0.3
    assert svc.step()["quotes"] == 1
