"""Quoting policy and the two venues it runs against: a paper venue (fills from the public tape with FIFO queue
position) and a live venue (post-only good-till-canceled orders on Kalshi)."""

from __future__ import annotations

import logging
import math
import uuid
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Protocol

from omen.http import HttpError
from omen.kalshi.client import KalshiClient
from omen.kalshi.models import Level, OrderBook

log = logging.getLogger(__name__)
SIDES = ("yes", "no")
DEAD_SIDE_MARGIN = 1.1  # dead-side bids rest 10% above what the side needs, so a small fill does not void it


def levels(book: OrderBook, side: str) -> list[tuple[float, float]]:
    rows = book.yes_bids if side == "yes" else book.no_bids
    return [(float(lv.price), float(lv.size)) for lv in rows]


@dataclass(frozen=True)
class Quote:
    """A resting bid to BUY ``side`` at ``price`` (a NO bid at p is a YES ask at 1 - p)."""

    ticker: str
    side: str
    price: float
    size: int
    keep_min: float = 0.0  # replace the resting order once fills leave it smaller than this


@dataclass(frozen=True)
class Fill:
    ts: datetime
    ticker: str
    side: str  # the side we bought
    price: float
    qty: float
    fee: float
    order_id: str
    trade_id: str


@dataclass
class QuotePolicy:
    """Rest a bid at the current best price, by default only on the cheaper side of the book.

    Rewards are paid per contract of qualifying size whichever side it rests on, while a bid costs its price in
    collateral and can lose at most that price per contract when it fills, so the cheap side earns the same reward on a
    fraction of the capital and risk."""

    sides: str = "cheap"  # cheap | both
    min_price: float = 0.02
    max_price: float = 0.50
    capital_per_market: float = 25.0
    max_contracts: int = 500

    def quotes(self, book: OrderBook, held_cost: dict[str, float]) -> list[Quote]:
        best = {s: (levels(book, s)[0][0] if levels(book, s) else None) for s in SIDES}
        if best["yes"] is None or best["no"] is None:
            return []  # one-sided books: nobody earns rewards there, and pricing is unknowable
        chosen = list(SIDES) if self.sides == "both" else [min(SIDES, key=lambda s: (best[s], s))]
        out = []
        for side in chosen:
            price = best[side]
            other = best["no" if side == "yes" else "yes"]
            if not (self.min_price <= price <= self.max_price) or price + other >= 1 - 1e-9:
                continue
            budget = self.capital_per_market / len(chosen) - held_cost.get(side, 0.0)
            size = min(self.max_contracts, math.floor(budget / price + 1e-9))
            if size >= 1:
                out.append(Quote(book.ticker, side, round(price, 4), size))
        return out


def decided(book: OrderBook) -> tuple[str, str] | None:
    """(dead side, live side) once one side is bid at 97c or more and the other at 2c or less."""
    for dead, live in (("yes", "no"), ("no", "yes")):
        lv_dead, lv_live = levels(book, dead), levels(book, live)
        best_dead = lv_dead[0][0] if lv_dead else 0.0
        best_live = lv_live[0][0] if lv_live else 0.0
        if best_dead <= 0.02 and best_live >= 0.97:
            return dead, live
    return None


def dead_side_quote(
    book: OrderBook, target: float, discount: float, held_cost: dict[str, float], max_loss: float
) -> Quote | None:
    """A 1c bid that completes the empty side of a decided market, or None.

    Rewards pay only while BOTH sides reach Target Size. Once one side trades at 97c or more, the other is worth about
    1c and often has too few bids, so nobody is paid. A 1c bid on that side is worth what it costs, can lose at most
    1c per contract when it fills (it fills when someone buys the favorite at 99c), and makes the market pay rewards
    again. Only a side that is short of the target is completed, and only while the favorite's best bid is below 99c:
    prices are whole cents, so a 1c bid against a 99c bid would cross and a post-only order is rejected."""
    from omen.liquidity.scoring import side_score

    sides = decided(book)
    if sides is None:
        return None
    dead, live = sides
    lv_dead, lv_live = levels(book, dead), levels(book, live)
    if side_score(lv_dead, target, discount).met or not side_score(lv_live, target, discount).met:
        return None  # already paid (or can't be completed from this side alone)
    if lv_live[0][0] + 0.01 >= 1 - 1e-9:
        return None
    # the side must reach the target in every snapshot: one contract short and the snapshot pays nobody, so rest a
    # margin above the shortfall and replace the bid as soon as fills eat into what is needed
    shortfall = math.ceil(target - sum(size for _, size in lv_dead) - 1e-9)
    size = min(math.ceil(shortfall * DEAD_SIDE_MARGIN), math.floor((max_loss - held_cost.get(dead, 0.0)) / 0.01 + 1e-9))
    return Quote(book.ticker, dead, 0.01, size, keep_min=shortfall) if size >= shortfall else None


def dead_side_sales(trade: dict, dead: str) -> float:
    """Contracts a public trade sold into a 1c bid on the dead side (a favorite bought at 99c), else 0."""
    _, _, yes_price, taker, count, _ = trade_fields(trade)
    if dead == "no" and taker == "yes" and yes_price >= 0.99 - 1e-9:
        return count
    if dead == "yes" and taker == "no" and yes_price <= 0.01 + 1e-9:
        return count
    return 0.0


@dataclass
class DeadSideFlow:
    """Contracts sold into 1c dead-side bids per minute that such a bid rested, over a trailing window.

    The tape only shows those sales while someone bids 1c on the dead side (with no bid there is nobody to sell to),
    so the rate is per *supply minute*, and a 1c bid of ours would face it every minute it rests. Markets where that
    flow costs more than the reward lost money in the settled replay; the rest made money."""

    window: timedelta = timedelta(hours=24)
    min_supply_minutes: float = 30.0
    _obs: dict[str, deque] = field(default_factory=dict)  # ticker -> (time, supply minutes, contracts sold)
    _seeded: set[str] = field(default_factory=set)  # markets whose history was loaded

    def observe(self, ticker: str, now: datetime, supply_minutes: float, contracts: float) -> None:
        if supply_minutes > 0 or contracts > 0:
            self._obs.setdefault(ticker, deque()).append((now, supply_minutes, contracts))

    def seeded(self, ticker: str) -> bool:
        return ticker in self._seeded

    def backfill(self, ticker: str, dead: str, candles: list[dict], trades: list[dict]) -> None:
        """Seed a market's window from 1-minute candles (did a 1c bid rest on the dead side?) and the trades of the
        same span, so a restart does not wait a day to learn which markets are safe. Minutes already observed live
        are kept as they are."""
        ask_or_bid, mark = ("yes_ask", 0.99) if dead == "no" else ("yes_bid", 0.01)
        sold: dict[int, float] = {}
        for t in trades:
            n = dead_side_sales(t, dead)
            if n:
                minute = math.ceil(trade_fields(t)[0].timestamp() / 60) * 60
                sold[minute] = sold.get(minute, 0.0) + n
        existing = self._obs.get(ticker) or deque()
        cutoff = existing[0][0] if existing else None
        seeded = []
        for c in candles:
            end = int(c["end_period_ts"])
            ts = datetime.fromtimestamp(end, UTC)
            if cutoff is not None and ts >= cutoff:
                continue
            close = (c.get(ask_or_bid) or {}).get("close_dollars")
            n = sold.get(end, 0.0)
            supply = n > 0 or (close is not None and abs(float(close) - mark) < 1e-9)
            if supply:
                seeded.append((ts, 1.0, n))
        self._obs[ticker] = deque([*seeded, *existing])
        self._seeded.add(ticker)

    def rate(self, ticker: str, now: datetime, prior: float) -> float:
        """Contracts per supply minute over the window, or ``prior`` while too little supply was seen."""
        obs = self._obs.get(ticker)
        while obs and now - obs[0][0] > self.window:
            obs.popleft()
        supply = sum(o[1] for o in obs) if obs else 0.0
        if supply < self.min_supply_minutes:
            return prior
        return sum(o[2] for o in obs) / supply


@dataclass
class Resting:
    ticker: str
    side: str
    price: float
    size: float  # remaining
    ahead: float = 0.0  # contracts queued in front of us at our price (paper only)
    order_id: str = ""
    placed: datetime = field(default_factory=lambda: datetime.now(UTC))


class Venue(Protocol):
    mode: str
    orders: dict[tuple[str, str], Resting]

    def sync(self, desired: list[Quote], books: dict[str, OrderBook], now: datetime) -> None: ...

    def fills(self, trades: list[dict], now: datetime) -> list[Fill]: ...

    def cancel_all(self) -> None: ...


def without_orders(book: OrderBook, orders: Iterable[Resting]) -> OrderBook:
    """The book as everyone else's bids: a live book includes our own resting orders, which must not be mistaken for
    competition (or for someone else completing a dead side). Paper books never contain them."""
    mine: dict[tuple[str, Decimal], Decimal] = {}
    for o in orders:
        if o.ticker == book.ticker and o.size > 0:
            key = (o.side, Decimal(str(round(o.price, 4))))
            mine[key] = mine.get(key, Decimal(0)) + Decimal(str(o.size))
    if not mine:
        return book

    def strip(side: str, rows: tuple[Level, ...]) -> tuple[Level, ...]:
        out = []
        for lv in rows:
            left = lv.size - mine.get((side, lv.price), Decimal(0))
            if left > 0:
                out.append(Level(lv.price, left))
        return tuple(out)

    return OrderBook(book.ticker, strip("yes", book.yes_bids), strip("no", book.no_bids))


def crosses(book: OrderBook, q: Quote) -> bool:
    """A bid crosses when it and the other side's best bid add up to a dollar or more (prices are whole cents)."""
    other = levels(book, "no" if q.side == "yes" else "yes")
    return bool(other) and q.price + other[0][0] >= 1 - 1e-9


def _keep(order: Resting | None, q: Quote) -> bool:
    """Keep a resting order (and its queue place) while it is at the wanted price and at least half its wanted size
    (and at least ``keep_min``)."""
    return order is not None and abs(order.price - q.price) < 1e-9 and order.size >= max(1.0, q.size / 2, q.keep_min)


def trade_fields(t: dict) -> tuple[datetime, str, float, str, float, str]:
    """(time, ticker, yes price, taker outcome side, contracts, trade id) from a public trade."""
    ts = datetime.fromisoformat(t["created_time"].replace("Z", "+00:00"))
    taker = t.get("taker_outcome_side") or t.get("taker_side") or ""
    return ts, t["ticker"], float(t["yes_price_dollars"]), taker, float(t["count_fp"]), t.get("trade_id", "")


class PaperVenue:
    """Virtual orders filled from the public tape.

    A bid to buy YES at p is hit by takers buying NO (selling YES) at a YES price <= p; a bid to buy NO at q by takers
    buying YES at a YES price >= 1 - q. A trade at our price first consumes the queue that was ahead of us when we
    joined the level; a trade through our price fills us completely (the matching engine reached us first)."""

    mode = "paper"

    def __init__(self) -> None:
        self.orders: dict[tuple[str, str], Resting] = {}

    def sync(self, desired: list[Quote], books: dict[str, OrderBook], now: datetime) -> None:
        want = {(q.ticker, q.side): q for q in desired}
        for key in list(self.orders):
            if key not in want:
                del self.orders[key]
        for key, q in want.items():
            if _keep(self.orders.get(key), q):
                continue
            book = books.get(q.ticker)
            if book is not None and crosses(book, q):
                self.orders.pop(key, None)  # the exchange rejects a post-only order that would trade
                continue
            ahead = sum(s for p, s in levels(book, q.side) if abs(p - q.price) < 1e-9) if book else 0.0
            self.orders[key] = Resting(
                q.ticker, q.side, q.price, float(q.size), ahead, f"paper-{uuid.uuid4().hex[:10]}", now
            )

    def fills(self, trades: list[dict], now: datetime) -> list[Fill]:
        out: list[Fill] = []
        for t in sorted(trades, key=lambda x: x["created_time"]):
            ts, ticker, yes_price, taker, count, trade_id = trade_fields(t)
            for side in SIDES:
                o = self.orders.get((ticker, side))
                if o is None or o.size <= 0 or ts < o.placed:
                    continue
                # a taker buying NO sells YES into YES bids; a taker buying YES sells NO into NO bids
                if (side == "yes" and taker != "no") or (side == "no" and taker != "yes"):
                    continue
                paid = yes_price if side == "yes" else round(1 - yes_price, 4)
                if paid > o.price + 1e-9:
                    continue
                if paid < o.price - 1e-9:
                    qty = o.size
                else:
                    if o.ahead >= count:
                        o.ahead -= count
                        continue
                    qty = min(o.size, count - o.ahead)
                    o.ahead = 0.0
                o.size -= qty
                out.append(Fill(ts, ticker, side, o.price, qty, 0.0, o.order_id, trade_id))
        for key in [k for k, o in self.orders.items() if o.size <= 1e-9]:
            del self.orders[key]
        return out

    def cancel_all(self) -> None:
        self.orders.clear()


RECONCILE_GRACE = timedelta(seconds=30)


class LiveVenue:
    """Real resting orders: post-only (never takes), good till canceled, canceled if trading pauses."""

    mode = "live"

    def __init__(self, client: KalshiClient, tag: str = "omen-lp"):
        self.client = client
        self.tag = tag
        self.orders: dict[tuple[str, str], Resting] = {}
        self.errors: list[str] = []
        self._fills_since: datetime | None = None
        self._seen_fills: set[str] = set()
        self._ours: set[str] = set()  # every order id this venue placed: other fills on the account are not ours

    @staticmethod
    def order_body(q: Quote, client_order_id: str) -> dict:
        """V2 quotes the YES book: buying YES at p is a ``bid`` at p; buying NO at q is an ``ask`` at 1 - q."""
        price = q.price if q.side == "yes" else 1 - q.price
        return {
            "ticker": q.ticker,
            "client_order_id": client_order_id,
            "side": "bid" if q.side == "yes" else "ask",
            "count": f"{q.size:.2f}",
            "price": f"{price:.4f}",
            "time_in_force": "good_till_canceled",
            "post_only": True,
            "self_trade_prevention_type": "taker_at_cross",
            "cancel_order_on_pause": True,
        }

    def _cancel(self, o: Resting) -> bool:
        """True once the order is gone. A cancel is safe to repeat, so it is retried on 429 (Basic tier: 100 write
        tokens a second, 10 per cancel); one that still fails stays tracked and is tried again next cycle."""
        try:
            self.client._signed(
                "DELETE", f"/portfolio/events/orders/{o.order_id}", params={"market_ticker": o.ticker}, retry=True
            )
        except HttpError as exc:
            if exc.status != 404:  # 404: already filled or gone
                self.errors.append(f"cancel {o.ticker}: {exc}")
                return False
        return True

    def reconcile(self, now: datetime) -> int:
        """Drop orders Kalshi no longer has resting and refresh remaining sizes; returns how many were dropped.

        Kalshi cancels orders on its own: at every trading pause (Thursdays 3-5 AM ET) for orders placed with
        cancel_order_on_pause, when a paused market reactivates, after close, and below a raised settlement floor.
        Orders younger than RECONCILE_GRACE are left alone: the API can lag the exchange by a moment."""
        rows: list[dict] = []
        cursor = ""
        for _ in range(20):
            params: dict = {"status": "resting", "limit": 200}
            if cursor:
                params["cursor"] = cursor
            try:
                data = self.client._signed("GET", "/portfolio/orders", params=params)
            except HttpError as exc:
                self.errors.append(f"resting orders: {exc}")
                return 0  # keep the local book as it is; try again next cycle
            rows.extend(data.get("orders") or [])
            cursor = data.get("cursor") or ""
            if not cursor:
                break
        resting = {o.get("order_id"): o for o in rows}
        dropped = 0
        for key, o in list(self.orders.items()):
            live = resting.get(o.order_id)
            if live is not None:
                if live.get("remaining_count_fp") is not None:
                    o.size = float(live["remaining_count_fp"])
            elif now - o.placed >= RECONCILE_GRACE:
                del self.orders[key]
                dropped += 1
        return dropped

    def sync(self, desired: list[Quote], books: dict[str, OrderBook], now: datetime) -> None:
        self.reconcile(now)
        want = {(q.ticker, q.side): q for q in desired}
        for key in list(self.orders):
            if (key not in want or not _keep(self.orders[key], want[key])) and self._cancel(self.orders[key]):
                del self.orders[key]
        for key, q in want.items():
            if key in self.orders:
                continue  # resting as wanted, or a cancel that failed: never a second order beside it
            body = self.order_body(q, f"{self.tag}-{uuid.uuid4().hex[:12]}")
            try:
                resp = self.client.create_order(body)
            except HttpError as exc:  # e.g. post-only rejected because the book moved: try again next cycle
                self.errors.append(f"place {q.ticker} {q.side}@{q.price}: {exc}")
                continue
            self._ours.add(resp.get("order_id", ""))
            remaining = float(resp.get("remaining_count") or 0)
            if remaining > 0:
                self.orders[key] = Resting(q.ticker, q.side, q.price, remaining, 0.0, resp.get("order_id", ""), now)

    def fills(self, trades: list[dict], now: datetime) -> list[Fill]:
        """Our own fills from ``/portfolio/fills`` (the public tape is not needed live)."""
        since = self._fills_since or now
        out: list[Fill] = []
        rows: list[dict] = []
        cursor = ""
        for _ in range(10):
            params: dict = {"min_ts": int(since.timestamp()) - 5, "limit": 200}
            if cursor:
                params["cursor"] = cursor
            try:
                data = self.client._signed("GET", "/portfolio/fills", params=params)
            except HttpError as exc:
                self.errors.append(f"fills: {exc}")
                return out
            rows.extend(data.get("fills") or [])
            cursor = data.get("cursor") or ""
            if not cursor:
                break
        ids = {o.order_id for o in self.orders.values()}
        for f in rows:
            fid = f.get("fill_id") or f.get("trade_id")
            if not fid or fid in self._seen_fills or f.get("order_id") not in self._ours:
                continue
            self._seen_fills.add(fid)
            side = f.get("outcome_side") or ("yes" if f.get("book_side") == "bid" else "no")
            price = float(f["yes_price_dollars"] if side == "yes" else f["no_price_dollars"])
            qty = float(f["count_fp"])
            ts = datetime.fromisoformat(f["created_time"].replace("Z", "+00:00")) if f.get("created_time") else now
            out.append(
                Fill(ts, f["ticker"], side, price, qty, float(f.get("fee_cost") or 0), f.get("order_id", ""), fid)
            )
            o = self.orders.get((f["ticker"], side))
            if o is not None and f.get("order_id") in ids:
                o.size -= qty
                if o.size <= 1e-9:
                    del self.orders[(f["ticker"], side)]
        self._fills_since = now
        return out

    def cancel_all(self) -> None:
        for key in list(self.orders):
            if self._cancel(self.orders[key]):
                del self.orders[key]

    def cancel_stale(self) -> int:
        """Cancel resting orders a previous run left behind (client order ids start with this venue's tag)."""
        n = 0
        cursor = ""
        for _ in range(20):
            params: dict = {"status": "resting", "limit": 200}
            if cursor:
                params["cursor"] = cursor
            data = self.client._signed("GET", "/portfolio/orders", params=params)
            for o in data.get("orders") or []:
                if str(o.get("client_order_id") or "").startswith(self.tag):
                    self._cancel(Resting(o["ticker"], "", 0.0, 0.0, order_id=o["order_id"]))
                    n += 1
            cursor = data.get("cursor") or ""
            if not cursor:
                return n
        return n
