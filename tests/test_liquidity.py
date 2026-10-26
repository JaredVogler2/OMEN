"""Liquidity-reward market making: filed scoring, quoting policy, paper fills, live order bodies, the service loop."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from omen.alerts.discord import Notifier
from omen.config import ConfigError, Settings, load_settings
from omen.http import HttpClient, HttpError
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
    """The signed order endpoints of one Kalshi account: a created order rests until it is cancelled."""

    def __init__(self):
        self.created: list[dict] = []
        self.cancelled: list[str] = []
        self.fills_resp: dict = {"fills": []}
        self.fail_cancels = 0  # this many cancels answer 429 (after the HTTP client's own retries)
        self.lose_creates = 0  # this many creates are applied but their response is lost (a timeout)
        self.rows: dict[str, dict] = {}  # order id -> the Order row Kalshi lists
        self.leftovers: list[dict] = []  # resting orders of an earlier run, or placed by hand
        self.resting: list[dict] | None = None  # overrides what /portfolio/orders?status=resting lists

    def balance(self) -> dict:
        return {"balance": 10000, "balance_dollars": "100.0000"}

    def create_order(self, body: dict) -> dict:
        self.created.append(body)
        oid = f"o{len(self.created)}"
        yes = float(body["price"])
        self.rows[oid] = {
            "order_id": oid,
            "client_order_id": body["client_order_id"],
            "ticker": body["ticker"],
            "outcome_side": "yes" if body["side"] == "bid" else "no",
            "yes_price_dollars": f"{yes:.4f}",
            "no_price_dollars": f"{1 - yes:.4f}",
            "remaining_count_fp": body["count"],
            "status": "resting",
        }
        if self.lose_creates:
            self.lose_creates -= 1
            raise HttpError("POST /portfolio/events/orders: read timeout")  # no status: outcome unknown
        return {"order_id": oid, "fill_count": "0.00", "remaining_count": body["count"]}

    def _signed(self, method: str, path: str, body=None, params=None, retry=False):
        params = params or {}
        if method == "DELETE":
            if self.fail_cancels:
                self.fail_cancels -= 1
                raise HttpError("DELETE -> HTTP 429", 429, "too many requests")
            oid = path.rsplit("/", 1)[-1]
            self.cancelled.append(oid)
            if oid in self.rows:
                self.rows[oid]["status"] = "canceled"
            self.leftovers = [r for r in self.leftovers if r["order_id"] != oid]
            return {}
        if path == "/portfolio/orders":
            if params.get("status") == "resting":
                rows = self.resting
                if rows is None:
                    rows = self.leftovers + [r for r in self.rows.values() if r["status"] == "resting"]
            else:
                rows = self.leftovers + list(self.rows.values())
            if params.get("ticker"):
                rows = [r for r in rows if r.get("ticker") == params["ticker"]]
            return {"orders": rows, "cursor": ""}
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
    c.leftovers = [
        {
            "order_id": "old1",
            "ticker": "M",
            "client_order_id": "omen-lp-abc",
            "outcome_side": "no",
            "no_price_dollars": "0.0100",
            "yes_price_dollars": "0.9900",
            "remaining_count_fp": "1100.00",
        },
        {"order_id": "manual", "ticker": "M", "client_order_id": "my-own-order", "outcome_side": "yes"},
    ]
    assert v.cancel_stale() == 1 and c.cancelled == ["old1"]  # the hand-placed order is left alone


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
        self.history: list[dict] = []  # trades returned for a single-market query (flow backfill)
        self.candles: list[dict] = []
        self.close = "2026-11-01T00:00:00Z"
        self.shard = 0
        self.trading = True  # False: Kalshi's trading pause (Thursdays 3-5 AM ET)
        self.book = {"yes_dollars": [["0.0500", "500.00"]], "no_dollars": [["0.9300", "2000.00"]]}

    def handler(self, request: httpx.Request) -> httpx.Response:
        u = urlsplit(str(request.url))
        q = parse_qs(u.query)
        if u.path.endswith("/exchange/status"):
            return httpx.Response(200, json={"exchange_active": True, "trading_active": self.trading})
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
                            "orderbook_fp": self.book,
                        }
                    ]
                },
            )
        if u.path.endswith("/markets/trades"):
            trades = self.history if "ticker" in q else self.trades
            return httpx.Response(200, json={"trades": trades, "cursor": ""})
        if u.path.endswith("/candlesticks"):
            return httpx.Response(200, json={"candlesticks": self.candles})
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
                            "close_time": self.close,
                            "market_type": "binary",
                            "exchange_index": self.shard,
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
    s.liquidity.dead_side_flow_prior = 1.0  # the $100/day test program could not afford the default 3 a minute
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


def test_quotes_need_a_reward_that_covers_typical_fill_costs(tmp_path):
    fake = FakeKalshi()
    svc, _ = service(tmp_path, fake)
    # this program pays $100/day; our 500 contracts earn a quarter of it: 5c per contract-day
    svc.cfg.min_reward_per_contract_day = 0.06
    assert svc.step()["quotes"] == 0
    svc.cfg.min_reward_per_contract_day = 0.04
    assert svc.step()["quotes"] == 1


def test_default_exclusions_cover_speech_and_insider_markets():
    import re

    pattern = re.compile(Settings().liquidity.exclude_series)
    for series in ("KXTRUMPMENTION", "KXTRUMPSAY", "KXTRUTHSOCIAL", "KXBIGBROTHERELIMINATION", "KXVMA", "KXEURUSD15M"):
        assert pattern.search(series), series
    for series in ("KXRT", "KXTRUMPAPPROVE", "KXYTVIEWSW", "KXBABELMANDEBWEEKLY"):
        assert not pattern.search(series), series


def test_dead_side_completion_bids_1c_only_where_the_side_is_short():
    from omen.liquidity.maker import dead_side_quote

    decided = book("M", yes=[(0.98, 1500), (0.97, 700)], no=[])
    # 10% above the 1000 the side needs, replaced once fills leave less than 1000
    assert dead_side_quote(decided, 1000, 0.5, {}, 50) == Quote("M", "no", 0.01, 1100, keep_min=1000)
    partly = book("M", yes=[(0.98, 1500)], no=[(0.01, 400)])
    assert dead_side_quote(partly, 1000, 0.5, {}, 50) == Quote("M", "no", 0.01, 660, keep_min=600)
    crossed = book("M", yes=[(0.99, 1500), (0.98, 700)], no=[])
    assert dead_side_quote(crossed, 1000, 0.5, {}, 50) is None  # 1c + 99c = $1: the bid would trade, not rest
    # the loss cap leaves room for 500 contracts, too few to complete the side: no bid at all
    assert dead_side_quote(decided, 1000, 0.5, {"no": 45.0}, 50) is None
    assert dead_side_quote(decided, 1000, 0.5, {"no": 39.5}, 50) == Quote("M", "no", 0.01, 1050, keep_min=1000)
    paid = book("M", yes=[(0.98, 1500)], no=[(0.01, 3000)])
    assert dead_side_quote(paid, 1000, 0.5, {}, 50) is None  # already reaches the target: nothing to complete
    open_market = book("M", yes=[(0.60, 1500)], no=[(0.38, 3000)])
    assert dead_side_quote(open_market, 1000, 0.5, {}, 50) is None


def test_service_completes_a_dead_side_and_earns_half_the_reward(tmp_path):
    fake = FakeKalshi()
    fake.book = {"yes_dollars": [["0.9800", "2000.00"]], "no_dollars": []}
    svc, ledger = service(tmp_path, fake)
    st = svc.step()
    o = svc.venue.orders[("KXRT-FILM-90", "no")]
    assert (o.price, o.size) == (0.01, 1100.0)
    assert st["reward_per_hour"] == pytest.approx(100 / 24 * 0.5, rel=1e-3)  # the whole NO side: half the reward
    # someone buys the favorite at 99c: our 1c NO bid fills and we hold 300 NO at 1c
    fake.now = T0 + timedelta(minutes=1)
    fake.trades = [trade("KXRT-FILM-90", T0 + timedelta(seconds=30), 0.99, "yes", 300, "f1")]
    st = svc.step()
    assert ledger.lp_positions("paper")["KXRT-FILM-90"]["no"] == [300.0, 3.0]
    # 800 left would leave the side short of 1000 and void every snapshot: a fresh full bid replaces it
    assert svc.venue.orders[("KXRT-FILM-90", "no")].size == 1100.0
    assert st["reward_per_hour"] == pytest.approx(100 / 24 * 0.5, rel=1e-3)


def test_dead_side_reward_accrues_in_markets_excluded_from_normal_quoting(tmp_path):
    fake = FakeKalshi()
    fake.book = {"yes_dollars": [["0.9800", "2000.00"]], "no_dollars": []}
    svc, _ = service(tmp_path, fake)
    svc.cfg.exclude_series = "^KXRT"
    st = svc.step()
    assert not svc.programs and ("KXRT-FILM-90", "no") in svc.venue.orders
    assert st["reward_per_hour"] == pytest.approx(100 / 24 * 0.5, rel=1e-3)


def test_paper_venue_rejects_a_quote_that_would_cross_like_post_only():
    venue = PaperVenue()
    b = book("M", yes=[(0.99, 100)], no=[])
    venue.sync([Quote("M", "no", 0.01, 50)], {"M": b}, T0)
    assert venue.orders == {}
    venue.sync([Quote("M", "no", 0.01, 50)], {"M": book("M", yes=[(0.98, 100)], no=[])}, T0)
    assert ("M", "no") in venue.orders


def test_dead_side_flow_is_measured_per_minute_a_1c_bid_rested():
    from omen.liquidity.maker import DeadSideFlow

    f = DeadSideFlow()
    assert f.rate("M", T0, prior=1.0) == 1.0
    f.observe("M", T0, 20, 10)
    assert f.rate("M", T0, prior=1.0) == 1.0  # under 30 minutes of 1c bids seen: still the prior
    f.observe("M", T0 + timedelta(minutes=20), 20, 30)
    assert f.rate("M", T0 + timedelta(minutes=20), prior=1.0) == pytest.approx(1.0)  # 40 contracts in 40 minutes
    f.observe("M", T0 + timedelta(minutes=40), 0, 0)  # no 1c bid and no sales: nothing learned
    f.observe("M", T0 + timedelta(minutes=60), 20, 0)
    assert f.rate("M", T0 + timedelta(minutes=60), prior=1.0) == pytest.approx(40 / 60)
    # a day later the first observation has aged out: 40 minutes left, 30 contracts
    assert f.rate("M", T0 + timedelta(hours=24, minutes=10), prior=7.0) == pytest.approx(30 / 40)


def test_service_stops_completing_a_dead_side_that_sellers_hit_faster_than_the_reward_pays(tmp_path):
    fake = FakeKalshi()
    # someone else rests 50 NO at 1c, short of the 1000 target: the side still needs completing
    fake.book = {"yes_dollars": [["0.9800", "2000.00"]], "no_dollars": [["0.0100", "50.00"]]}
    svc, _ = service(tmp_path, fake)
    svc.flow.min_supply_minutes = 2
    st = svc.step()
    assert ("KXRT-FILM-90", "no") in svc.venue.orders and st["dead_quotes"] == 1  # the prior costs less than the reward
    for i in range(1, 3):
        fake.now = T0 + timedelta(minutes=i)
        fake.trades = [trade("KXRT-FILM-90", fake.now - timedelta(seconds=30), 0.99, "yes", 100, f"s{i}")]
        st = svc.step()
    # 100 contracts a minute sold at 1c cost $1,440 a day, far more than the ~$48 a day the bid earns
    assert ("KXRT-FILM-90", "no") not in svc.venue.orders and st["dead_quotes"] == 0


def candle(ts: datetime, yes_bid: float, yes_ask: float) -> dict:
    return {
        "end_period_ts": int(ts.timestamp()),
        "yes_bid": {"close_dollars": f"{yes_bid:.4f}"},
        "yes_ask": {"close_dollars": f"{yes_ask:.4f}"},
    }


def test_dead_side_flow_backfills_from_candles_and_trades_without_double_counting():
    from omen.liquidity.maker import DeadSideFlow

    f = DeadSideFlow()
    start = T0 - timedelta(hours=2)
    # NO is dead: a 1c NO bid (YES ask 99c) rested in 40 of 60 minutes; 2 contracts were sold into it in 10 of them
    candles = [candle(start + timedelta(minutes=i), 0.98, 0.99 if i < 40 else 0.98) for i in range(1, 61)]
    trades = [trade("M", start + timedelta(minutes=i, seconds=-20), 0.99, "yes", 2, f"h{i}") for i in range(1, 11)]
    f.observe("M", start + timedelta(minutes=50), 10, 0)  # already seen live: minutes from 50 on are not re-counted
    f.backfill("M", "no", candles, trades)
    assert f.seeded("M")
    assert f.rate("M", T0, prior=9.0) == pytest.approx(20 / (39 + 10))


def test_service_backfills_flow_history_before_completing_a_dead_side(tmp_path):
    fake = FakeKalshi()
    fake.book = {"yes_dollars": [["0.9800", "2000.00"]], "no_dollars": []}
    start = T0 - timedelta(hours=1)
    # the last hour: a 1c NO bid rested every minute and someone sold 100 contracts into it each minute
    fake.candles = [candle(start + timedelta(minutes=i), 0.98, 0.99) for i in range(1, 61)]
    fake.history = [
        trade("KXRT-FILM-90", start + timedelta(minutes=i, seconds=-30), 0.99, "yes", 100, f"h{i}")
        for i in range(1, 61)
    ]
    svc, _ = service(tmp_path, fake)
    st = svc.step()
    assert svc.flow.seeded("KXRT-FILM-90")
    assert st["dead_quotes"] == 0  # without history the prior (1 contract a minute) would have allowed the bid


def test_dead_sides_rest_closer_to_the_close_than_cheap_side_quotes(tmp_path):
    fake = FakeKalshi()
    fake.book = {"yes_dollars": [["0.9800", "2000.00"]], "no_dollars": []}
    fake.close = (T0 + timedelta(hours=8)).isoformat().replace("+00:00", "Z")
    svc, _ = service(tmp_path, fake)
    assert svc.cfg.stop_before_close_minutes == 720 and svc.cfg.dead_side_stop_before_close_minutes == 360
    assert svc.step()["dead_quotes"] == 1  # 8 h before close: inside the 12 h stop, outside the 6 h dead-side stop
    svc.cfg.dead_side_stop_before_close_minutes = 720
    fake.now = T0 + timedelta(minutes=1)
    assert svc.step()["dead_quotes"] == 0


def test_paper_charges_expected_fills_to_a_dead_side_bid_that_rests_alone(tmp_path):
    fake = FakeKalshi()
    fake.book = {"yes_dollars": [["0.9800", "2000.00"]], "no_dollars": []}
    start = T0 - timedelta(hours=1)
    # the last hour: a 1c NO bid rested every minute and 30 contracts were sold into it: 0.5 a minute
    fake.candles = [candle(start + timedelta(minutes=i), 0.98, 0.99) for i in range(1, 61)]
    fake.history = [
        trade("KXRT-FILM-90", start + timedelta(minutes=i, seconds=-30), 0.99, "yes", 1, f"h{i}") for i in range(1, 31)
    ]
    svc, ledger = service(tmp_path, fake)
    assert svc.step()["dead_quotes"] == 1
    fake.now = T0 + timedelta(minutes=2)
    svc.step()  # nobody else bids 1c, so the tape shows nothing: two minutes at 0.5 a minute are charged instead
    assert ledger.lp_positions("paper")["KXRT-FILM-90"]["no"] == pytest.approx([1.0, 0.01])


def test_live_decisions_leave_our_own_orders_out_of_the_book(tmp_path):
    from omen.liquidity.maker import Resting, without_orders

    b = book("M", yes=[(0.05, 800)], no=[(0.01, 1100), (0.93, 2000)])
    stripped = without_orders(b, [Resting("M", "no", 0.01, 1100.0), Resting("M", "yes", 0.05, 300.0)])
    assert stripped.no_bids == book("M", yes=[], no=[(0.93, 2000)]).no_bids
    assert stripped.yes_bids == book("M", yes=[(0.05, 500)], no=[]).yes_bids

    class LiveLike(PaperVenue):  # paper fills, but the service treats it as live
        mode = "live"

    fake = FakeKalshi()
    fake.book = {"yes_dollars": [["0.9800", "2000.00"]], "no_dollars": []}
    svc, _ = service(tmp_path, fake)
    svc.venue = LiveLike()
    svc.step()
    assert svc.venue.orders[("KXRT-FILM-90", "no")].size == 1100
    # the exchange now shows our own bid in the book: it must not look like someone else completed the side
    fake.book = {"yes_dollars": [["0.9800", "2000.00"]], "no_dollars": [["0.0100", "1100.00"]]}
    fake.now = T0 + timedelta(minutes=1)
    st = svc.step()
    assert st["dead_quotes"] == 1
    assert st["reward_per_hour"] == pytest.approx(100 / 24 * 0.5, rel=1e-3)


def test_no_dead_side_bid_before_the_market_s_selling_history_is_loaded(tmp_path):
    import omen.liquidity.service as service_module

    fake = FakeKalshi()
    fake.book = {"yes_dollars": [["0.9800", "2000.00"]], "no_dollars": []}
    svc, _ = service(tmp_path, fake)
    limit = service_module.MAX_BACKFILLS
    service_module.MAX_BACKFILLS = 0  # no history requests left this cycle
    try:
        assert svc.step()["dead_quotes"] == 0
    finally:
        service_module.MAX_BACKFILLS = limit
    fake.now = T0 + timedelta(minutes=1)
    assert svc.step()["dead_quotes"] == 1  # loaded now: quiet history, so the bid goes in


def test_the_default_flow_prior_keeps_unknown_low_reward_markets_out(tmp_path):
    fake = FakeKalshi()
    fake.book = {"yes_dollars": [["0.9800", "2000.00"]], "no_dollars": []}
    svc, _ = service(tmp_path, fake)
    svc.cfg.dead_side_flow_prior = Settings().liquidity.dead_side_flow_prior
    assert svc.cfg.dead_side_flow_prior == 3.0
    # no selling history: 3 contracts a minute would cost $43.20 a day, more than half of the $50 a day the bid earns
    assert svc.step()["dead_quotes"] == 0


def test_unreadable_programs_are_skipped_and_reported_once(tmp_path):
    from omen.liquidity.programs import Program

    good = {
        "market_ticker": "M",
        "incentive_type": "liquidity",
        "start_date": "2026-10-28T00:00:00Z",
        "end_date": "2026-10-29T00:00:00Z",
        "period_reward": 100_000,
        "target_size_fp": "500.00",
    }
    pooled = {
        "eligible_market": "Daily temperature",
        "incentive_type": "liquidity",
        "target_size_fp": "500.00",
        "period_reward": 150_000_000,
    }
    assert Program.from_api(good) is not None
    assert Program.from_api(pooled) is None  # no market ticker or dates: skipped, not a crash

    fake = FakeKalshi()
    svc, _ = service(tmp_path, fake)
    sent = []
    svc._say = lambda title, body, severity: sent.append(title)
    svc._warn_unreadable([pooled])
    svc._warn_unreadable([pooled])
    assert sent == ["Reward programs changed shape"]  # once per new set of fields


def test_no_bid_where_the_period_reward_cannot_reach_kalshi_s_1_dollar_minimum(tmp_path):
    fake = FakeKalshi()
    fake.book = {"yes_dollars": [["0.9800", "2000.00"]], "no_dollars": []}
    svc, ledger = service(tmp_path, fake)
    # the program ends at midnight; a dead-side bid earns $50 a day, $1.00 takes 29 minutes
    fake.now = datetime(2026, 10, 24, 23, 40, tzinfo=UTC)
    assert svc.step()["dead_quotes"] == 0  # 20 minutes left: at most $0.69, which Kalshi would not pay
    ledger.lp_add_reward("paper", T0, "KXRT-FILM-90", 0.50, 3600)  # earned earlier in this period
    fake.now += timedelta(minutes=1)
    assert svc.step()["dead_quotes"] == 1  # $0.50 so far plus $0.66 more clears the minimum


def test_live_cycles_record_the_account_cash_for_reconciling_rewards(tmp_path):
    from types import SimpleNamespace

    from test_reconcile import FakeAccount

    fake = FakeKalshi()
    svc, ledger = service(tmp_path, fake)
    svc._record_cash(T0)  # paper: there is no account to read
    acct = FakeAccount(
        100.0,
        orders=[{"outcome_side": "no", "no_price_dollars": "0.0100", "remaining_count_fp": "1100.00"}],
    )
    svc.venue = SimpleNamespace(client=acct, mode="live")
    svc._record_cash(T0)
    assert ledger.lp_cash("paper") == [] and ledger.lp_cash("live") == [(T0, 100.0, 11.0)]


def test_a_failed_cancel_keeps_the_order_tracked_and_never_doubles_it():
    c = FakeTradingClient()
    v = LiveVenue(c)
    v.sync([Quote("M", "no", 0.01, 1100)], {}, T0)
    c.fail_cancels = 1
    v.sync([Quote("M", "no", 0.02, 1100)], {}, T0)  # the cancel is rate-limited: the old bid is still on Kalshi
    assert [o.order_id for o in v.orders.values()] == ["o1"] and len(c.created) == 1 and v.errors
    v.sync([Quote("M", "no", 0.02, 1100)], {}, T0)  # next cycle: canceled, then replaced
    assert c.cancelled == ["o1"] and c.created[-1]["price"] == "0.9800" and len(c.created) == 2
    c.fail_cancels = 2  # the tracked cancel and the sweep's retry both hit 429
    v.cancel_all()
    assert ("M", "no") in v.orders  # still resting: kept so the kill switch or the next start can cancel it
    v.cancel_all()
    assert not v.orders and c.cancelled == ["o1", "o2"]


def test_orders_kalshi_cancelled_on_its_own_are_dropped_and_placed_again():
    c = FakeTradingClient()
    v = LiveVenue(c)
    v.sync([Quote("M", "no", 0.01, 1100), Quote("N", "no", 0.01, 1100)], {}, T0)
    assert len(c.created) == 2
    # Thursday's trading pause cancelled both (cancel_order_on_pause); one partly filled before
    c.resting = [{"order_id": "o2", "remaining_count_fp": "700.00"}]
    later = T0 + timedelta(minutes=1)
    assert v.reconcile(later) == 1
    assert ("M", "no") not in v.orders and v.orders[("N", "no")].size == 700.0
    v.sync([Quote("M", "no", 0.01, 1100), Quote("N", "no", 0.01, 1100)], {}, later)
    assert len(c.created) == 3 and c.created[-1]["ticker"] == "M"  # M is placed again; N keeps its 700


def test_a_just_placed_order_is_not_dropped_before_kalshi_lists_it():
    c = FakeTradingClient()
    v = LiveVenue(c)
    c.resting = []
    v.sync([Quote("M", "no", 0.01, 1100)], {}, T0)
    assert v.reconcile(T0 + timedelta(seconds=5)) == 0 and ("M", "no") in v.orders
    assert v.reconcile(T0 + timedelta(seconds=31)) == 1


def test_no_bids_on_an_exchange_shard_the_account_has_not_funded(tmp_path):
    fake = FakeKalshi()
    fake.book = {"yes_dollars": [["0.9800", "2000.00"]], "no_dollars": []}
    fake.shard = 2  # commodities and crypto trade on shard 2, with a balance of their own
    svc, _ = service(tmp_path, fake)
    assert svc.step()["dead_quotes"] == 0
    svc.cfg.exchange_indexes = [0, 2]  # after funding shard 2
    svc._refreshed = None
    fake.now = T0 + timedelta(minutes=1)
    assert svc.step()["dead_quotes"] == 1


# ----------------------------------------------------------------------------- live venue: review findings
def fill_row(oid: str, qty: float, ts: datetime, fid: str = "f1", side: str = "no", price: float = 0.01) -> dict:
    return {
        "fill_id": fid,
        "order_id": oid,
        "ticker": "M",
        "outcome_side": side,
        "count_fp": f"{qty:.2f}",
        "yes_price_dollars": f"{price if side == 'yes' else 1 - price:.4f}",
        "no_price_dollars": f"{price if side == 'no' else 1 - price:.4f}",
        "fee_cost": "0.0000",
        "created_time": ts.isoformat().replace("+00:00", "Z"),
    }


def test_a_create_whose_response_was_lost_is_adopted_not_doubled():
    c = FakeTradingClient()
    v = LiveVenue(c)
    q = Quote("M", "no", 0.01, 1100)
    c.lose_creates = 1  # Kalshi took the order; the response timed out
    v.sync([q], {}, T0)
    assert ("M", "no") in v.pending and not v.orders
    v.sync([q], {}, T0 + timedelta(seconds=10))
    assert len(c.created) == 1  # never a second order while the first one's fate is unknown
    later = T0 + timedelta(minutes=1)
    v.reconcile(later)  # Kalshi lists it as resting with our client order id: adopted
    assert v.orders[("M", "no")].order_id == "o1" and not v.pending
    v.sync([q], {}, later)
    assert len(c.created) == 1
    c.fills_resp = {"fills": [fill_row("o1", 300, later)]}
    assert [f.qty for f in v.fills([], later)] == [300.0]  # its fills are ours


def test_a_lost_create_that_filled_before_it_was_seen_still_books_its_fills():
    c = FakeTradingClient()
    v = LiveVenue(c)
    c.lose_creates = 1
    v.sync([Quote("M", "no", 0.01, 1100)], {}, T0)
    c.rows["o1"]["status"] = "executed"  # filled at once, so never listed as resting
    later = T0 + timedelta(minutes=1)
    v.reconcile(later)  # found among all of the market's orders by its client order id
    assert not v.pending and not v.orders
    c.fills_resp = {"fills": [fill_row("o1", 1100, T0 + timedelta(seconds=5))]}
    assert [f.qty for f in v.fills([], later)] == [1100.0]


def test_a_create_that_never_reached_kalshi_is_placed_again_after_the_timeout():
    c = FakeTradingClient()
    v = LiveVenue(c)
    q = Quote("M", "no", 0.01, 1100)
    c.lose_creates = 1
    v.sync([q], {}, T0)
    c.rows.clear()  # it was never applied
    v.reconcile(T0 + timedelta(minutes=1))
    v.sync([q], {}, T0 + timedelta(minutes=1))
    assert len(c.created) == 1  # still within the timeout: wait
    v.reconcile(T0 + timedelta(minutes=6))
    v.sync([q], {}, T0 + timedelta(minutes=6))
    assert len(c.created) == 2 and ("M", "no") in v.orders


def test_fills_never_change_sizes_reconcile_reads_them_from_kalshi():
    c = FakeTradingClient()
    v = LiveVenue(c)
    v.sync([Quote("M", "no", 0.01, 1100)], {}, T0)
    later = T0 + timedelta(minutes=1)
    c.fills_resp = {"fills": [fill_row("o1", 300, later)]}
    v.fills([], later)
    assert v.orders[("M", "no")].size == 1100  # not subtracted here...
    c.rows["o1"]["remaining_count_fp"] = "800.00"
    v.reconcile(later)
    assert v.orders[("M", "no")].size == 800  # ...but taken from Kalshi, once


def test_fills_of_an_earlier_run_are_booked_after_a_restart():
    c = FakeTradingClient()
    ledger = Ledger(":memory:")
    LiveVenue(c, ledger=ledger).sync([Quote("M", "no", 0.01, 1100)], {}, T0)
    restarted = LiveVenue(c, ledger=ledger)  # a new process: the order id comes from the ledger
    assert "o1" in restarted._ours and restarted._fills_since == T0
    c.fills_resp = {"fills": [fill_row("o1", 200, T0 + timedelta(seconds=40))]}
    assert [f.qty for f in restarted.fills([], T0 + timedelta(minutes=3))] == [200.0]


def test_cancel_all_also_cancels_our_untracked_orders_on_kalshi():
    c = FakeTradingClient()
    v = LiveVenue(c)
    v.sync([Quote("M", "no", 0.01, 1100)], {}, T0)
    c.leftovers = [
        {
            "order_id": "lost",
            "ticker": "N",
            "client_order_id": "omen-lp-x",
            "outcome_side": "no",
            "no_price_dollars": "0.0100",
            "remaining_count_fp": "1100.00",
        }
    ]
    v.cancel_all()
    assert sorted(c.cancelled) == ["lost", "o1"] and not v.orders


def test_start_up_survives_an_api_outage_and_adopts_what_it_could_not_cancel():
    c = FakeTradingClient()
    v = LiveVenue(c)
    real = c._signed

    def down(method, path, body=None, params=None, retry=False):
        raise HttpError("GET /portfolio/orders -> HTTP 503", 503, "")

    c._signed = down
    assert v.cancel_stale() == 0 and v.errors  # no exception: the loop starts and retries
    c._signed = real
    c.leftovers = [
        {
            "order_id": "old",
            "ticker": "M",
            "client_order_id": "omen-lp-y",
            "outcome_side": "no",
            "no_price_dollars": "0.0100",
            "remaining_count_fp": "900.00",
        }
    ]
    c.fail_cancels = 1
    assert v.cancel_stale() == 0
    assert v.orders[("M", "no")].order_id == "old"  # tracked: retried next cycle, its fills are ours
    assert "old" in v._ours


def test_an_order_larger_than_the_wanted_size_is_replaced_to_respect_the_caps():
    c = FakeTradingClient()
    v = LiveVenue(c)
    v.sync([Quote("M", "no", 0.01, 1100)], {}, T0)
    v.sync([Quote("M", "no", 0.01, 500)], {}, T0)  # the loss cap now allows only 500
    assert c.cancelled == ["o1"] and c.created[-1]["count"] == "500.00"


def live_service(tmp_path, fake: FakeKalshi):
    svc, ledger = service(tmp_path, fake)
    svc.venue = LiveVenue(FakeTradingClient(), ledger=ledger)
    return svc, ledger


def test_no_new_orders_while_kalshi_trading_is_paused(tmp_path):
    fake = FakeKalshi()
    fake.book = {"yes_dollars": [["0.9800", "2000.00"]], "no_dollars": []}
    svc, _ = live_service(tmp_path, fake)
    fake.trading = False
    svc.step()
    assert svc.venue.client.created == []
    fake.trading = True
    fake.now = T0 + timedelta(minutes=1)
    svc.step()
    assert [b["side"] for b in svc.venue.client.created] == ["ask"]  # the 1c NO bid (a YES ask at 99c)


def test_no_quote_on_the_side_opposite_a_held_position(tmp_path):
    fake = FakeKalshi()
    fake.book = {"yes_dollars": [["0.9800", "2000.00"]], "no_dollars": []}
    svc, ledger = service(tmp_path, fake)
    ledger.lp_record_fill("paper", Fill(T0, "KXRT-FILM-90", "yes", 0.30, 10, 0.0, "x", "t1"))
    assert svc.step()["dead_quotes"] == 0  # a NO bid would net our YES: a trade, not liquidity


def test_inventory_settles_only_once_the_result_is_final(tmp_path):
    fake = FakeKalshi()
    svc, ledger = service(tmp_path, fake)
    ledger.lp_record_fill("paper", Fill(T0, "KXRT-FILM-90", "yes", 0.05, 100, 0.0, "x", "t1"))
    fake.status, fake.result = "determined", "no"  # can still be disputed
    svc._refreshed = None
    svc.step()
    assert ledger.lp_positions("paper")
    fake.status = "finalized"
    fake.now = T0 + timedelta(minutes=1)
    svc._refreshed = None
    svc.step()
    assert not ledger.lp_positions("paper")


def test_fills_since_the_last_poll_are_booked_on_exit(tmp_path):
    fake = FakeKalshi()
    svc, ledger = live_service(tmp_path, fake)
    venue = svc.venue
    venue._ours.add("o9")
    calls = {"n": 0}
    real = venue.fills

    def fills(trades, now):
        calls["n"] += 1
        if calls["n"] == 2:  # the poll in run()'s finally
            venue.client.fills_resp = {"fills": [fill_row("o9", 50, T0, fid="last")]}
        return real(trades, now)

    venue.fills = fills
    svc.run(cycles=1, sleep=lambda _: None)
    assert ledger.lp_positions("live")["M"]["no"][0] == 50.0
