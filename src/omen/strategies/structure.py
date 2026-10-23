"""Structural facts about markets: what outcome set each one pays on, which markets share an underlying, and
whether an event's markets provably cover every outcome.

Everything here is deliberately strict. A false "guaranteed" signal is far more expensive than a missed one, so any
ambiguity (different wording, different times, unknown strike semantics, an untradable bucket) disqualifies.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal

from omen.kalshi.models import Market, mask_numbers

UPPER_TYPES = {"greater", "greater_or_equal"}
LOWER_TYPES = {"less", "less_or_equal"}
INTERVAL_TYPES = UPPER_TYPES | LOWER_TYPES | {"between"}


@dataclass(frozen=True)
class Interval:
    """Outcome values on which a market pays YES. ``None`` bounds are infinite."""

    lo: Decimal | None
    lo_closed: bool
    hi: Decimal | None
    hi_closed: bool

    def contains_interval(self, other: Interval) -> bool:
        """True when every outcome in ``other`` is also in ``self`` (``other`` implies ``self``)."""
        if self.lo is not None:
            if other.lo is None or other.lo < self.lo:
                return False
            if other.lo == self.lo and other.lo_closed and not self.lo_closed:
                return False
        if self.hi is not None:
            if other.hi is None or other.hi > self.hi:
                return False
            if other.hi == self.hi and other.hi_closed and not self.hi_closed:
                return False
        return True

    def describe(self) -> str:
        lo = "-∞" if self.lo is None else f"{self.lo.normalize():f}"
        hi = "+∞" if self.hi is None else f"{self.hi.normalize():f}"
        return f"{'[' if self.lo_closed else '('}{lo}, {hi}{']' if self.hi_closed else ')'}"


# The settlement rules must say, in words, the same thing the strike fields say. Kalshi's metadata is not always
# self-consistent: on 2026-10-23 "exactly N Starship launches" markets were tagged strike_type="less" with
# floor = cap = N, "#3 seed or better" was tagged "less" (really <= 3) and some totals read "score more 1.5 runs".
# Taking the fields at face value turned "exactly 4" vs "exactly 7" into a fake 29% "guaranteed" ladder. So a market
# only gets interval semantics when its fields and its rules text agree; otherwise it is excluded from every
# structural strategy and from the crypto model.
_UP_WORDS = re.compile(
    r"\b(above|over|more|greater than|higher than|at least|or more|or higher|or above|exceeds?|exceeding)\b"
    r"|\d\+|%?\s+to\s+100%",
    re.I,
)
_DOWN_WORDS = re.compile(r"\b(below|under|less than|fewer than|lower than|at most|or less|or fewer|or lower)\b", re.I)
_RANGE_WORDS = re.compile(r"\bbetween\b|\d\s*(?:-|–|to)\s*\$?\d", re.I)
_EXACTLY = re.compile(r"\bexactly\b", re.I)


def semantic_problem(m: Market) -> str | None:
    """Why this market's strike fields cannot be trusted as an interval, or None when fields and rules agree."""
    st, lo, hi, text = m.strike_type, m.floor_strike, m.cap_strike, m.rules_primary
    if st not in INTERVAL_TYPES:
        return f"strike type {st!r} has no interval semantics"
    if not text:
        return "no settlement rules text to cross-check"
    if st in UPPER_TYPES:
        if lo is None or hi is not None:
            return f"{st} market with floor={lo} cap={hi}"
        if _EXACTLY.search(text) or not _UP_WORDS.search(text) or _DOWN_WORDS.search(text):
            return f"rules do not read as '{st}'"
    elif st in LOWER_TYPES:
        if hi is None or lo is not None:
            return f"{st} market with floor={lo} cap={hi}"
        if _EXACTLY.search(text) or not _DOWN_WORDS.search(text) or _UP_WORDS.search(text):
            return f"rules do not read as '{st}'"
    else:  # between
        if lo is None or hi is None or lo > hi:
            return f"between market with floor={lo} cap={hi}"
        if lo != hi and (_EXACTLY.search(text) or not _RANGE_WORDS.search(text)):
            return "rules do not read as a range"
    return None


def interval_of(m: Market) -> Interval | None:
    if semantic_problem(m) is not None:
        return None
    st, lo, hi = m.strike_type, m.floor_strike, m.cap_strike
    if st == "greater":
        return Interval(lo, False, None, False)
    if st == "greater_or_equal":
        return Interval(lo, True, None, False)
    if st == "less":
        return Interval(None, False, hi, False)
    if st == "less_or_equal":
        return Interval(None, False, hi, True)
    return Interval(lo, True, hi, True)


def underlying_key(m: Market) -> tuple:
    """Markets with equal keys are the same question about the same quantity at the same time, differing only in the
    threshold. Wording (title and settlement rules with every number masked), strike semantics, timing and the
    participant all have to match."""
    return (
        m.event_ticker,
        mask_numbers(m.title),
        mask_numbers(m.rules_primary),
        m.strike_type,
        m.close_time,
        m.settles_at,
        m.primary_participant_key or "",
    )


def ladder_groups(markets: list[Market]) -> list[list[Market]]:
    """Groups of 2+ tradable markets on one underlying with one-sided strikes of a single type, sorted by strike."""
    groups: dict[tuple, list[Market]] = defaultdict(list)
    for m in markets:
        if m.strike_type in UPPER_TYPES | LOWER_TYPES and interval_of(m) is not None and m.rules_primary:
            groups[underlying_key(m)].append(m)
    out = []
    for g in groups.values():
        strikes = [_strike(m) for m in g]
        if len(g) >= 2 and len(set(strikes)) == len(strikes):  # duplicate strikes = ambiguous: skip the group
            out.append(sorted(g, key=_strike))
    return out


def _strike(m: Market) -> Decimal:
    v = m.floor_strike if m.strike_type in UPPER_TYPES else m.cap_strike
    assert v is not None
    return v


@dataclass(frozen=True)
class PartitionCheck:
    ok: bool
    reason: str
    step: Decimal | None = None  # reporting step the buckets rely on (gaps of exactly one step), None if gapless
    intervals: tuple[tuple[str, str], ...] = ()


def _decimals(v: Decimal) -> int:
    exponent = v.normalize().as_tuple().exponent
    return max(0, -int(exponent))


def check_partition(markets: list[Market], allow_step_gaps: bool) -> PartitionCheck:
    """Do these markets (the whole event) pay YES on every possible outcome?

    Touching intervals need at least one closed end at the shared boundary; overlaps are fine. Closed buckets may also
    be separated by exactly one reporting step, the finest precision used by any of the event's strikes: ``59-60`` /
    ``61-62`` (whole degrees), ``8.1`` / ``8.2`` (tenths), ``84,999.99`` / ``85,000`` (cents). Those gaps are only
    accepted when ``allow_step_gaps`` is set, because they rely on the outcome being reported at that precision.
    Anything else is a hole and the event is not a partition.
    """
    pairs = []
    for m in markets:
        iv = interval_of(m)
        if iv is None:
            return PartitionCheck(False, f"{m.ticker}: {semantic_problem(m)}")
        pairs.append((iv, m))
    if len(pairs) < 2:
        return PartitionCheck(False, "fewer than two markets")
    if len({(m.close_time, m.settles_at) for _, m in pairs}) != 1:
        return PartitionCheck(False, "markets close or settle at different times")
    bounds = [b for iv, _ in pairs for b in (iv.lo, iv.hi) if b is not None]
    unit = Decimal(1).scaleb(-max((_decimals(b) for b in bounds), default=0))
    pairs.sort(key=lambda p: (Decimal("-Infinity") if p[0].lo is None else p[0].lo, not p[0].lo_closed))
    if pairs[0][0].lo is not None:
        return PartitionCheck(False, "no bucket covers the low tail")

    step: Decimal | None = None
    reach_hi: Decimal | None = pairs[0][0].hi  # None = +inf
    reach_closed = pairs[0][0].hi_closed
    for iv, m in pairs[1:]:
        if reach_hi is None:
            break
        lo = iv.lo
        if lo is None or lo < reach_hi or (lo == reach_hi and (reach_closed or iv.lo_closed)):
            pass  # overlapping, or touching with the shared boundary covered
        elif lo == reach_hi:
            return PartitionCheck(False, f"outcome exactly {lo.normalize():f} is not covered (before {m.ticker})")
        elif allow_step_gaps and reach_closed and iv.lo_closed and lo - reach_hi == unit:
            step = unit
        else:
            return PartitionCheck(False, f"gap {reach_hi.normalize():f}..{lo.normalize():f} before {m.ticker}")
        if iv.hi is None:
            reach_hi = None
        elif iv.hi > reach_hi or (iv.hi == reach_hi and iv.hi_closed):
            reach_hi, reach_closed = iv.hi, iv.hi_closed
    if reach_hi is not None:
        return PartitionCheck(False, "no bucket covers the high tail")
    return PartitionCheck(True, "covers every outcome", step, tuple((m.ticker, iv.describe()) for iv, m in pairs))


# Settlement clauses under which every bucket of an event can resolve NO (or to an arbitrary "fair" price), which
# breaks the "exactly one bucket pays" premise of a YES basket. NO baskets and ladders are immune to these.
_ALL_NO_CLAUSES = [
    re.compile(r"last fair price", re.I),
    re.compile(r"(no data|not available|unavailable|incomplete)[^.]{0,120}resolve[sd]?\s+to\s+no", re.I),
    re.compile(r"resolve[sd]?\s+to\s+no[^.]{0,120}(no data|not available|unavailable|incomplete)", re.I),
    re.compile(r"fair (market )?value", re.I),
]


def all_no_settlement_risk(markets: list[Market]) -> str | None:
    for m in markets:
        text = f"{m.rules_primary}\n{m.rules_secondary}"
        for pat in _ALL_NO_CLAUSES:
            hit = pat.search(text)
            if hit:
                return f"{m.ticker}: rules say '{hit.group(0)[:80]}'"
    return None
