"""LOCK strategies: plays whose payout is guaranteed by the contracts' own logic, after every fee.

* NO basket   - in a mutually exclusive event at most one market resolves YES, so NO on k of its markets pays at least
                k-1. Profitable when the YES bids sum to more than 1 plus fees.
* Ladder      - if outcome A implies outcome B (BTC > 90k implies BTC > 85k), YES on B plus NO on A pays at least 1.
                Profitable when A's YES bid exceeds B's YES ask by more than fees (the market prices A above B).
* YES basket  - if an event's buckets provably cover every outcome, YES on all of them pays at least 1. Only used
                when the rules carry no "everything resolves NO / to a fair price" clause.

Screening uses top-of-book prices; every candidate is then re-priced against the full order books (batch request),
with each fill's fee rounded up, and sized so the guaranteed profit is maximal within the configured caps.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from omen.book import available
from omen.config import LockSettings
from omen.fees.schedule import FeeQuote, FeeUnavailable
from omen.kalshi.models import Event, Market, OrderBook
from omen.money import ONE, ZERO
from omen.strategies.base import LOCK, LegPlan, LegSpec, Opportunity, SizingLimits, size_sets
from omen.strategies.structure import (
    UPPER_TYPES,
    all_no_settlement_risk,
    check_partition,
    interval_of,
    ladder_groups,
)

log = logging.getLogger(__name__)

FeeLookup = Callable[[Market], FeeQuote]
SCREEN_MARGIN = Decimal("0.0001")


@dataclass
class Candidate:
    strategy: str
    event: Event
    legs: list[tuple[Market, str]]
    payout_per_set: Decimal
    payout_max_per_set: Decimal
    title: str
    rationale: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)
    confidence: float = 0.99
    top_edge: Decimal = ZERO  # per-set edge at top of book, for ranking
    alternates: list[list[tuple[Market, str]]] = field(default_factory=list)


def _rate(fee: FeeLookup, m: Market, price: Decimal) -> Decimal:
    return fee(m).taker_rate_per_contract(price)


# --------------------------------------------------------------------------------------------------- NO basket
def screen_no_baskets(events: dict[str, Event], by_event: dict[str, list[Market]], fee: FeeLookup) -> list[Candidate]:
    out = []
    for ticker, markets in by_event.items():
        ev = events.get(ticker)
        if ev is None or not ev.mutually_exclusive:
            continue
        scored = []
        try:
            for m in markets:
                if not m.has_yes_bid:
                    continue
                contribution = m.yes_bid - _rate(fee, m, m.no_ask)
                if contribution > 0:
                    scored.append((contribution, m))
        except FeeUnavailable as exc:
            log.debug("skip %s: %s", ticker, exc)
            continue
        if len(scored) < 2:
            continue
        edge = sum((c for c, _ in scored), ZERO) - ONE
        if edge <= SCREEN_MARGIN:
            continue
        scored.sort(key=lambda x: x[0], reverse=True)
        legs = [(m, "no") for _, m in scored]
        alternates = [legs[:k] for k in range(len(legs) - 1, 1, -1)]
        k = len(legs)
        out.append(
            Candidate(
                strategy="no_basket",
                event=ev,
                legs=legs,
                payout_per_set=Decimal(k - 1),
                payout_max_per_set=Decimal(k),
                title=f"NO basket · {ev.title}",
                rationale=[
                    "Event is flagged mutually exclusive: at most one of its markets can resolve YES.",
                    f"Buying NO on {k} of its markets therefore pays at least {k - 1} per set "
                    f"({k} if none of them resolves YES).",
                    "Their YES bids sum to more than 1 + fees, so the NO side is underpriced as a group.",
                ],
                risks=[
                    "Leg risk: fill every leg (fill-or-kill) or unwind; a partial basket is a directional bet.",
                    "Relies on Kalshi's mutually-exclusive flag for this event (checked live, not assumed).",
                ],
                evidence={"mutually_exclusive": True, "collateral_return_type": ev.collateral_return_type},
                confidence=0.99,
                top_edge=edge,
                alternates=alternates,
            )
        )
    return out


# ------------------------------------------------------------------------------------------------------ ladder
def screen_ladders(events: dict[str, Event], markets: list[Market], fee: FeeLookup) -> list[Candidate]:
    out = []
    for group in ladder_groups(markets):
        pairs = []
        try:
            for a in group:  # a = the narrower outcome (implies b); we sell it (buy NO)
                if not a.has_yes_bid:
                    continue
                ia = interval_of(a)
                for b in group:
                    if b is a or not b.has_yes_ask:
                        continue
                    ib = interval_of(b)
                    if ia is None or ib is None or not ib.contains_interval(ia):
                        continue
                    edge = a.yes_bid - b.yes_ask - _rate(fee, a, a.no_ask) - _rate(fee, b, b.yes_ask)
                    if edge > SCREEN_MARGIN:
                        pairs.append((edge, a, b))
        except FeeUnavailable as exc:
            log.debug("skip ladder %s: %s", group[0].event_ticker, exc)
            continue
        used: set[str] = set()
        for edge, a, b in sorted(pairs, key=lambda p: p[0], reverse=True):
            if a.ticker in used or b.ticker in used:
                continue
            used.update((a.ticker, b.ticker))
            ev = events.get(a.event_ticker) or Event(a.event_ticker, a.series_ticker, a.title, "", "", False, "")
            direction = "above" if a.strike_type in UPPER_TYPES else "below"
            out.append(
                Candidate(
                    strategy="ladder",
                    event=ev,
                    legs=[(b, "yes"), (a, "no")],
                    payout_per_set=ONE,
                    payout_max_per_set=Decimal(2),
                    title=f"Ladder · {a.title}",
                    rationale=[
                        f"'{a.yes_sub_title}' implies '{b.yes_sub_title}' (same underlying, same time, "
                        f"strikes {direction}), so P(A) can never exceed P(B).",
                        f"The market bids A at {a.yes_bid} but offers B at {b.yes_ask}: YES on B + NO on A pays "
                        "at least 1 in every outcome (2 if the outcome lands between the strikes).",
                    ],
                    risks=["Leg risk: fill both legs (fill-or-kill) or unwind."],
                    evidence={
                        "implied": a.ticker,
                        "implying_interval": interval_of(a).describe(),  # type: ignore[union-attr]
                        "covering": b.ticker,
                        "covering_interval": interval_of(b).describe(),  # type: ignore[union-attr]
                    },
                    confidence=0.99,
                    top_edge=edge,
                )
            )
    return out


# ----------------------------------------------------------------------------------------------- YES partition
def screen_yes_partitions(
    events: dict[str, Event], by_event: dict[str, list[Market]], fee: FeeLookup, allow_step_gaps: bool
) -> tuple[list[Candidate], list[tuple[str, str]]]:
    out: list[Candidate] = []
    rejected: list[tuple[str, str]] = []
    for ticker, markets in by_event.items():
        ev = events.get(ticker)
        if ev is None or not ev.mutually_exclusive or len(markets) < 2:
            continue
        if not all(m.has_yes_ask for m in markets):
            continue
        try:
            edge = ONE - sum((m.yes_ask + _rate(fee, m, m.yes_ask) for m in markets), ZERO)
        except FeeUnavailable:
            continue
        if edge <= SCREEN_MARGIN:
            continue
        check = check_partition(markets, allow_step_gaps)
        if not check.ok:
            rejected.append((ticker, f"not a provable partition: {check.reason}"))
            continue
        clause = all_no_settlement_risk(markets)
        if clause:
            rejected.append((ticker, f"all-NO settlement clause: {clause}"))
            continue
        confidence = 0.97 if check.step is None else 0.95
        risks = [
            "If the exchange voids or resolves every bucket NO (e.g. missing source data), a YES basket pays 0.",
            "Leg risk: fill every leg (fill-or-kill) or unwind.",
        ]
        if check.step is not None:
            risks.append(
                f"Buckets leave gaps of {check.step.normalize():f}: relies on the outcome being reported in steps "
                f"of {check.step.normalize():f}."
            )
        out.append(
            Candidate(
                strategy="yes_partition",
                event=ev,
                legs=[(m, "yes") for m in markets],
                payout_per_set=ONE,
                payout_max_per_set=ONE,
                title=f"YES basket · {ev.title}",
                rationale=[
                    f"The event's {len(markets)} buckets were checked to cover every possible outcome "
                    f"({check.reason}), so exactly one pays $1.",
                    "Their YES asks plus fees sum to less than $1.",
                ],
                risks=risks,
                evidence={
                    "partition": [list(p) for p in check.intervals],
                    "step": None if check.step is None else str(check.step),
                },
                confidence=confidence,
                top_edge=edge,
            )
        )
    return out, rejected


# ----------------------------------------------------------------------------------------------- verification
@dataclass
class VerifyContext:
    books: dict[str, OrderBook]
    fee: FeeLookup
    settings: LockSettings
    now: datetime


def verify(c: Candidate, ctx: VerifyContext) -> Opportunity | None:
    """Re-price a candidate against full depth and size it. None when it no longer clears every threshold."""
    best: Opportunity | None = None
    for legs in [c.legs, *c.alternates]:
        opp = _verify_legs(c, legs, ctx)
        if opp is not None and (best is None or opp.profit > best.profit):
            best = opp
    return best


def _verify_legs(c: Candidate, legs: list[tuple[Market, str]], ctx: VerifyContext) -> Opportunity | None:
    specs = []
    for m, side in legs:
        book = ctx.books.get(m.ticker)
        if book is None:
            return None
        try:
            specs.append(LegSpec(m, side, book.asks(side), ctx.fee(m)))
        except FeeUnavailable:
            return None
    k = len(legs)
    payout = c.payout_per_set if c.strategy != "no_basket" else Decimal(k - 1)
    payout_max = c.payout_max_per_set if c.strategy != "no_basket" else Decimal(k)
    settles = max(m.settles_at for m, _ in legs)
    s = ctx.settings
    limits = SizingLimits(
        max_capital=s.max_capital_usd,
        max_sets=s.max_contracts_per_leg,
        whole_contracts=s.whole_contracts,
        min_profit=s.min_profit_usd,
        min_return_pct=s.min_return_pct,
        min_apr_pct=s.min_apr_pct,
        seconds_to_settle=Decimal(max(3600, int((settles - ctx.now).total_seconds()))),
    )
    sizing = size_sets(specs, payout, limits)
    if sizing is None:
        return None
    plans = [
        LegPlan(spec.market, spec.side, f, spec.fee, available(spec.levels))
        for spec, f in zip(specs, sizing.fills, strict=True)
    ]
    for p in plans:
        price = p.limit_price if p.side == "yes" else ONE - p.limit_price
        if not p.market.grid.is_valid(price):
            log.warning("%s: limit %s off grid, dropping candidate", p.market.ticker, price)
            return None
    rationale = list(c.rationale)
    if c.strategy == "no_basket" and k != len(c.legs):
        rationale[1] = (
            f"Buying NO on the {k} best-priced of its markets pays at least {k - 1} per set ({k} if none resolves YES)."
        )
    return Opportunity(
        strategy=c.strategy,
        tier=LOCK,
        title=c.title,
        event_ticker=c.event.event_ticker,
        legs=plans,
        sets=sizing.sets,
        payout_min=payout * sizing.sets,
        payout_max=payout_max * sizing.sets,
        settles_at=settles,
        found_at=ctx.now,
        confidence=c.confidence,
        rationale=rationale,
        risks=list(c.risks),
        evidence=dict(c.evidence),
    )
