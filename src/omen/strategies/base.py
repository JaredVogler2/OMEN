"""Opportunity model shared by every strategy, plus multi-leg sizing against real order-book depth."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from omen.book import LegFill, available, breakpoints, sweep
from omen.fees.schedule import FeeQuote
from omen.kalshi.models import Level, Market
from omen.money import ONE, ZERO, floor_to

LOCK = "LOCK"  # profit guaranteed by contract logic after fees, at prices executable when verified
MODEL = "MODEL"  # profit expected from a probability model (shadow until validated and promoted)

SECONDS_PER_YEAR = Decimal(365 * 24 * 3600)


@dataclass(frozen=True)
class LegSpec:
    market: Market
    side: str  # "yes" | "no"
    levels: list[Level]
    fee: FeeQuote


@dataclass(frozen=True)
class LegPlan:
    market: Market
    side: str
    fill: LegFill
    fee: FeeQuote
    depth: Decimal = ZERO  # contracts offered on this side when verified (execution order: thinnest first)

    @property
    def limit_price(self) -> Decimal:
        """Worst price this leg may pay and still keep the play's edge (the deepest level swept)."""
        return self.fill.worst_price

    def order_v2(self, client_order_id: str) -> dict:
        """Exact POST /portfolio/events/orders body. V2 quotes the YES book: buying NO = selling YES at 1 - price."""
        if self.side == "yes":
            side, price = "bid", self.limit_price
        else:
            side, price = "ask", ONE - self.limit_price
        if not self.market.grid.is_valid(price):
            raise ValueError(f"{self.market.ticker}: {price} is off the market's price grid")
        return {
            "ticker": self.market.ticker,
            "client_order_id": client_order_id,
            "side": side,
            "count": f"{self.fill.count:.2f}",
            "price": f"{price:.4f}",
            "time_in_force": "fill_or_kill",
            "self_trade_prevention_type": "taker_at_cross",
            "cancel_order_on_pause": True,
        }


@dataclass
class Opportunity:
    strategy: str
    tier: str
    title: str
    event_ticker: str
    legs: list[LegPlan]
    sets: Decimal
    payout_min: Decimal
    payout_max: Decimal
    settles_at: datetime
    found_at: datetime
    confidence: float
    rationale: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)
    probability: float | None = None  # MODEL: conservative probability that the bought side wins
    model_probability: float | None = None  # MODEL: central estimate

    @property
    def key(self) -> str:
        legs = ",".join(sorted(f"{lp.market.ticker}:{lp.side}" for lp in self.legs))
        return f"{self.strategy}|{legs}"

    @property
    def short_id(self) -> str:
        return hashlib.sha256(self.key.encode()).hexdigest()[:8]

    @property
    def cost(self) -> Decimal:
        return sum((lp.fill.notional for lp in self.legs), ZERO)

    @property
    def fees(self) -> Decimal:
        return sum((lp.fill.fees for lp in self.legs), ZERO)

    @property
    def capital(self) -> Decimal:
        return self.cost + self.fees

    @property
    def expected_payout(self) -> Decimal:
        if self.tier == LOCK or self.probability is None:
            return self.payout_min
        return Decimal(str(round(self.probability, 6))) * self.payout_max

    @property
    def profit(self) -> Decimal:
        """Guaranteed profit for LOCK plays; conservative expected profit for MODEL plays. After all fees."""
        return self.expected_payout - self.capital

    @property
    def return_pct(self) -> Decimal:
        return self.profit / self.capital * 100 if self.capital > 0 else ZERO

    @property
    def seconds_to_settle(self) -> Decimal:
        return Decimal(max(3600, int((self.settles_at - self.found_at).total_seconds())))

    @property
    def apr_pct(self) -> Decimal:
        return self.return_pct * SECONDS_PER_YEAR / self.seconds_to_settle


@dataclass(frozen=True)
class Sizing:
    sets: Decimal
    fills: list[LegFill]
    payout: Decimal
    capital: Decimal

    @property
    def profit(self) -> Decimal:
        return self.payout - self.capital


@dataclass(frozen=True)
class SizingLimits:
    max_capital: Decimal
    max_sets: Decimal
    whole_contracts: bool
    min_profit: Decimal
    min_return_pct: Decimal
    min_apr_pct: Decimal
    seconds_to_settle: Decimal


def _evaluate(specs: list[LegSpec], n: Decimal, payout_per_set: Decimal) -> Sizing | None:
    fills = []
    for spec in specs:
        f = sweep(spec.levels, n, spec.fee)
        if f is None:
            return None
        fills.append(f)
    capital = sum((f.total for f in fills), ZERO)
    return Sizing(n, fills, payout_per_set * n, capital)


def size_sets(specs: list[LegSpec], payout_per_set: Decimal, limits: SizingLimits) -> Sizing | None:
    """Choose how many sets (one contract of every leg) to buy so guaranteed profit is maximal.

    Candidates are the depth breakpoints of every leg (where some leg's marginal price changes), each capped by the
    thinnest leg, the contract cap and the capital cap. A candidate must clear the profit, return and APR floors.
    """
    step = ONE if limits.whole_contracts else Decimal("0.01")
    depth = min(available(s.levels) for s in specs)
    cap = min(depth, limits.max_sets)
    cap = floor_to(cap, step)
    if cap < step:
        return None

    raw = {cap}
    for spec in specs:
        raw.update(bp for bp in breakpoints(spec.levels) if bp < cap)
    candidates = sorted({floor_to(c, step) for c in raw if floor_to(c, step) >= step})

    best: Sizing | None = None
    for n in candidates:
        sz = _evaluate(specs, n, payout_per_set)
        if sz is None:
            continue
        if sz.capital > limits.max_capital:
            sz = _largest_within_capital(specs, payout_per_set, step, n, limits.max_capital)
            if sz is None:
                break
        if not _passes(sz, limits):
            if sz.capital >= limits.max_capital:
                break
            continue
        if best is None or sz.profit > best.profit:
            best = sz
        if sz.capital >= limits.max_capital:
            break
    return best


def _largest_within_capital(
    specs: list[LegSpec], payout_per_set: Decimal, step: Decimal, hi: Decimal, max_capital: Decimal
) -> Sizing | None:
    lo_units, hi_units = 1, int(hi / step)
    best: Sizing | None = None
    while lo_units <= hi_units:
        mid = (lo_units + hi_units) // 2
        sz = _evaluate(specs, step * mid, payout_per_set)
        if sz is not None and sz.capital <= max_capital:
            best = sz
            lo_units = mid + 1
        else:
            hi_units = mid - 1
    return best


def _passes(sz: Sizing, limits: SizingLimits) -> bool:
    if sz.capital <= 0 or sz.profit < limits.min_profit:
        return False
    ret = sz.profit / sz.capital * 100
    apr = ret * SECONDS_PER_YEAR / max(limits.seconds_to_settle, Decimal(3600))
    return ret >= limits.min_return_pct and apr >= limits.min_apr_pct
