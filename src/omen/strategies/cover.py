"""Cover locks: guaranteed plays across every market that settles on the same number at the same moment.

Kalshi often lists one quantity several ways in separate series: "SOL above $X at 5 pm" thresholds next to
"SOL between $X and $Y at 5 pm" buckets, both settling on the same 60-second CF Benchmarks average. Each market pays
YES on an interval of that number, so a YES buys its interval and a NO on a one-sided market buys the complementary
half-line. Any set of positions whose intervals cover the whole line pays at least 1, whatever the number turns out
to be. Finding the cheapest such set is a shortest path over interval end points: it finds the 3-leg
"NO below + bucket + YES above" combination, longer chains, and anything else that covers the line.

A second shape covers the line twice: NO on a bucket [L, U] plus YES on "at least a" (a <= L) plus NO on "above b"
(b >= U) pays at least 2 everywhere, and never relies on reporting precision.

Strictness: markets are combined only when their settlement wording (with every number masked and the strike
phrase removed), units, close time and settlement time all match. Single-event combinations are left to the ladder
and YES-basket strategies, so nothing is reported twice.
"""

from __future__ import annotations

import heapq
import itertools
import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal

from omen.fees.schedule import FeeUnavailable
from omen.kalshi.models import Event, Market, mask_numbers
from omen.money import ZERO
from omen.strategies.structure import INTERVAL_TYPES, Interval, interval_of

log = logging.getLogger(__name__)

_COMPARE = re.compile(
    r"\b(?:is|be|are|was)\s+(?:strictly\s+)?(?:above|below|over|under|at least|at most|greater than(?: or equal to)?|"
    r"less than(?: or equal to)?|more than|fewer than|between|exactly|equal to)\b",
    re.I,
)
# what may follow the comparison word as part of the strike: numbers, ranges, units and "or above"-style tails
_STRIKE_TAIL = re.compile(
    r"(?:\s*(?:#|-|–|to|and|or|above|below|more|less|higher|lower|greater|fewer|%|°[CF]?))*", re.I
)
_FAIR_PRICE = re.compile(r"last fair price|fair (?:market )?value", re.I)
_NO_ON_MISSING = re.compile(
    r"(no data|not available|unavailable|incomplete)[^.]{0,120}resolve[sd]?\s+to\s+no"
    r"|resolve[sd]?\s+to\s+no[^.]{0,120}(no data|not available|unavailable|incomplete)",
    re.I,
)


def settlement_key(m: Market) -> tuple | None:
    """Markets with equal keys pay on the same number at the same time, whatever series they belong to."""
    if m.strike_type not in INTERVAL_TYPES or not m.rules_primary:
        return None
    masked = mask_numbers(m.rules_primary)
    hit = _COMPARE.search(masked)
    if hit is None:
        return None
    tail = _STRIKE_TAIL.match(masked, hit.end())
    strike_text = m.rules_primary  # units come from the unmasked rules
    units = tuple(sorted(u for u in ("$", "%", "°") if u in strike_text))
    suffix = masked[tail.end() if tail else hit.end() :]
    return (masked[: hit.start()].strip().lower(), suffix.strip().lower(), units, m.close_time, m.settles_at)


@dataclass(frozen=True)
class Position:
    market: Market
    side: str  # "yes" or "no"
    lo: Decimal | None  # covered outcome values, None = infinite
    lo_closed: bool
    hi: Decimal | None
    hi_closed: bool
    cost: Decimal  # per contract at the top of the book, including the marginal taker fee


def positions(markets: list[tuple[Market, Interval]], fee) -> list[Position]:
    """Every contiguous piece of the line one contract can buy: YES on any interval, NO on a one-sided market."""
    out = []
    for m, iv in markets:
        try:
            if m.has_yes_ask:
                out.append(
                    Position(m, "yes", iv.lo, iv.lo_closed, iv.hi, iv.hi_closed, m.yes_ask + _rate(fee, m, m.yes_ask))
                )
            if m.has_yes_bid and (iv.lo is None) != (iv.hi is None):  # NO on a half-line is the other half-line
                cost = m.no_ask + _rate(fee, m, m.no_ask)
                if iv.hi is None:  # YES on [a, inf) / (a, inf): NO covers (-inf, a) / (-inf, a]
                    out.append(Position(m, "no", None, False, iv.lo, not iv.lo_closed, cost))
                else:  # YES on (-inf, b] / (-inf, b): NO covers (b, inf) / [b, inf)
                    out.append(Position(m, "no", iv.hi, not iv.hi_closed, None, False, cost))
        except FeeUnavailable:
            continue
    return out


def _rate(fee, m: Market, price: Decimal) -> Decimal:
    return fee(m).taker_rate_per_contract(price)


def _decimals(v: Decimal) -> int:
    return max(0, -int(v.normalize().as_tuple().exponent))


def reporting_unit(markets: list[tuple[Market, Interval]]) -> Decimal:
    bounds = [b for _, iv in markets for b in (iv.lo, iv.hi) if b is not None]
    return Decimal(1).scaleb(-max((_decimals(b) for b in bounds), default=0))


@dataclass
class Cover:
    cost: Decimal
    legs: list[Position]
    step_gaps: int  # joins that rely on the outcome being reported at the bounds' precision


def cheapest_cover(pos: list[Position], unit: Decimal, allow_step_gaps: bool) -> Cover | None:
    """Minimum-cost set of positions whose pieces join from -inf to +inf (Dijkstra over the covered frontier)."""
    start = ("start",)
    best: dict[tuple, Decimal] = {start: ZERO}
    back: dict[tuple, tuple[tuple, Position, bool]] = {}
    tie = itertools.count()
    heap: list = [(ZERO, next(tie), start)]
    done: set[tuple] = set()
    while heap:
        cost, _, node = heapq.heappop(heap)
        if node in done:
            continue
        done.add(node)
        if node[0] == "end":
            legs, gaps, cur = [], 0, node
            while cur != start:
                prev, p, gap = back[cur]
                legs.append(p)
                gaps += gap
                cur = prev
            return Cover(cost, list(reversed(legs)), gaps)
        for p in pos:
            gap = False
            if node == start:
                if p.lo is not None:
                    continue
            else:
                hi, hi_closed = node[1], node[2]
                if p.lo is None or p.lo < hi or (p.lo == hi and (hi_closed or p.lo_closed)):
                    pass  # overlaps or touches with the shared boundary covered
                elif allow_step_gaps and hi_closed and p.lo_closed and p.lo - hi == unit:
                    gap = True
                else:
                    continue
                if p.hi is not None and (p.hi < hi or (p.hi == hi and (hi_closed or not p.hi_closed))):
                    continue  # does not extend the frontier
            nxt = ("end",) if p.hi is None else ("at", p.hi, p.hi_closed)
            c = cost + p.cost
            if c < best.get(nxt, Decimal("Infinity")):
                best[nxt] = c
                back[nxt] = (node, p, gap)
                heapq.heappush(heap, (c, next(tie), nxt))
    return None


def double_covers(markets: list[tuple[Market, Interval]], fee) -> list[tuple[Decimal, list[tuple[Market, str]]]]:
    """NO on a bucket [L, U] + YES on 'at least a' (a <= L) + NO on 'above b' (b >= U): pays at least 2 everywhere.
    Returns (cost, legs) for the cheapest such triple per bucket."""
    ups = [(m, iv) for m, iv in markets if iv.hi is None and iv.lo is not None]
    out = []
    for b, ib in markets:
        if ib.lo is None or ib.hi is None or not b.has_yes_bid:
            continue
        try:
            no_bucket = b.no_ask + _rate(fee, b, b.no_ask)
            lower = [
                (m.yes_ask + _rate(fee, m, m.yes_ask), m)
                for m, iv in ups
                if m.has_yes_ask and (iv.lo < ib.lo or (iv.lo == ib.lo and iv.lo_closed))  # YES covers [L, U]
            ]
            upper = [
                (m.no_ask + _rate(fee, m, m.no_ask), m)
                for m, iv in ups
                if m.has_yes_bid and (iv.lo > ib.hi or (iv.lo == ib.hi and not iv.lo_closed))
            ]
        except FeeUnavailable:
            continue
        if not lower or not upper:
            continue
        lc, lm = min(lower, key=lambda x: x[0])
        uc, um = min(upper, key=lambda x: x[0])
        out.append((no_bucket + lc + uc, [(b, "no"), (lm, "yes"), (um, "no")]))
    return out


def universes(markets: list[Market]) -> list[list[tuple[Market, Interval]]]:
    """Groups of tradable markets that settle on the same number at the same time and span two or more events."""
    groups: dict[tuple, list[tuple[Market, Interval]]] = defaultdict(list)
    for m in markets:
        key = settlement_key(m)
        if key is None:
            continue
        iv = interval_of(m)
        if iv is not None:
            groups[key].append((m, iv))
    return [g for g in groups.values() if len({m.event_ticker for m, _ in g}) >= 2]


def settlement_clause_problem(legs: list[tuple[Market, str]]) -> str | None:
    """A cover pays 1 because exactly the right legs resolve YES. A 'fair price' settlement breaks that; a
    'resolves NO if data is missing' clause is survivable only if a NO leg would then pay."""
    has_no_leg = any(side == "no" for _, side in legs)
    for m, _ in legs:
        text = f"{m.rules_primary}\n{m.rules_secondary}"
        if _FAIR_PRICE.search(text):
            return f"{m.ticker}: settles to a fair price in some cases"
        if not has_no_leg and _NO_ON_MISSING.search(text):
            return f"{m.ticker}: every leg could resolve NO if data is missing"
    return None


def _event(m: Market, events: dict[str, Event]) -> Event:
    return events.get(m.event_ticker) or Event(m.event_ticker, m.series_ticker, m.title, "", "", False, "")


def describe_leg(m: Market, side: str) -> str:
    iv = interval_of(m)
    span = iv.describe() if iv else "?"
    if side == "no" and iv is not None:
        span = f"not {span}"
    return f"{side.upper()} {m.yes_sub_title or m.ticker} ({m.series_ticker}, pays on {span})"
