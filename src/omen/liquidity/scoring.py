"""Kalshi Liquidity Incentive Program scoring, exactly as filed with the CFTC (October 2026 amendment, Appendix A §5).

Once a second Kalshi snapshots the book. Per side (YES bids, NO bids; a YES ask counts as a NO bid):

1. Walk bid levels from the best price down. Every bid at a visited level qualifies (no time priority inside a level).
2. The Reference Price is the first level at which the cumulative size reaches one fifth of the Target Size.
3. Stop after the level at which the cumulative size reaches the Target Size. If it never does, that side has no
   qualifying bids and the snapshot is excluded for everyone (rewards need two-sided liquidity).
4. Score(bid) = DiscountFactor ^ (ticks below the Reference Price) x size; bids at or above it count in full.
5. Your snapshot score = your share of the YES-side score + your share of the NO-side score (at most 2 in total).

Payout = (your snapshot scores / all snapshot scores) x period reward x (valid snapshots / all snapshots), which is the
same as summing, over every snapshot, reward / snapshots x (your YES share + your NO share) / 2, with excluded
snapshots paying nothing. That per-snapshot form is what the paper and live accounting accrue.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

TICK = 0.01


@dataclass(frozen=True)
class SideScore:
    met: bool  # the side reached Target Size
    total: float  # sum of all qualifying scores on the side (ours included)
    ours: float  # our qualifying score
    reference: float | None  # Reference Price

    @property
    def share(self) -> float:
        return self.ours / self.total if self.met and self.total > 0 else 0.0


def side_score(
    levels: Iterable[tuple[float, float]],
    target: float,
    discount: float,
    ours: float = 0.0,
    our_price: float | None = None,
    tick: float = TICK,
) -> SideScore:
    """Score one side of the book with our resting bid of ``ours`` contracts at ``our_price`` added to it."""
    book: dict[float, list[float]] = {}
    for price, size in levels:
        if size > 0:
            book.setdefault(round(price, 4), [0.0, 0.0])[0] += size
    if ours > 0 and our_price is not None:
        book.setdefault(round(our_price, 4), [0.0, 0.0])[1] += ours
    cum = 0.0
    ref: float | None = None
    visited: list[tuple[float, float, float]] = []
    for price in sorted(book, reverse=True):
        others, mine = book[price]
        cum += others + mine
        visited.append((price, others, mine))
        if ref is None and cum >= target / 5 - 1e-9:
            ref = price
        if cum >= target - 1e-9:
            break
    if cum < target - 1e-9 or ref is None:
        return SideScore(False, 0.0, 0.0, ref)
    total = mine_score = 0.0
    for price, others, mine in visited:
        ticks = max(0, round((ref - price) / tick))
        mult = discount**ticks
        total += (others + mine) * mult
        mine_score += mine * mult
    return SideScore(True, total, mine_score, ref)


@dataclass(frozen=True)
class Snapshot:
    yes: SideScore
    no: SideScore

    @property
    def valid(self) -> bool:
        """Both sides reached Target Size, so the snapshot counts for everyone."""
        return self.yes.met and self.no.met

    @property
    def our_fraction(self) -> float:
        """Our share of this snapshot's reward: (YES share + NO share) / 2, or 0 if the snapshot is excluded."""
        return (self.yes.share + self.no.share) / 2 if self.valid else 0.0


def snapshot(
    yes_levels: Iterable[tuple[float, float]],
    no_levels: Iterable[tuple[float, float]],
    target: float,
    discount: float,
    our_yes: tuple[float, float] | None = None,
    our_no: tuple[float, float] | None = None,
    tick: float = TICK,
) -> Snapshot:
    """Score both sides with our bids (``(price, size)``) inserted."""

    def score(levels: Iterable[tuple[float, float]], ours: tuple[float, float] | None) -> SideScore:
        if ours is None:
            return side_score(levels, target, discount, tick=tick)
        price, size = ours
        return side_score(levels, target, discount, ours=size, our_price=price, tick=tick)

    return Snapshot(score(yes_levels, our_yes), score(no_levels, our_no))


def payout(our_snapshot_scores: float, all_snapshot_scores: float, reward: float, valid: int, total: int) -> float:
    """Kalshi's payout formula, rounded down to the cent; nothing under $1.00 is paid."""
    if all_snapshot_scores <= 0 or total <= 0:
        return 0.0
    amount = our_snapshot_scores / all_snapshot_scores * reward * valid / total
    cents = int(amount * 100 + 1e-9)
    return cents / 100 if cents >= 100 else 0.0
