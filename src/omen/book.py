"""Order-book walking: exact cost, per-fill fees and worst price for buying N contracts right now."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from omen.fees.schedule import FeeQuote
from omen.kalshi.models import Level
from omen.money import ZERO


@dataclass(frozen=True)
class Fill:
    price: Decimal
    count: Decimal
    fee: Decimal


@dataclass(frozen=True)
class LegFill:
    """Result of sweeping one leg's asks for ``count`` contracts (taker, immediate)."""

    fills: tuple[Fill, ...]
    count: Decimal
    notional: Decimal  # sum(price x count), what the contracts cost before fees
    fees: Decimal

    @property
    def total(self) -> Decimal:
        return self.notional + self.fees

    @property
    def worst_price(self) -> Decimal:
        return max(f.price for f in self.fills)

    @property
    def avg_price(self) -> Decimal:
        return self.notional / self.count


def available(levels: list[Level]) -> Decimal:
    return sum((lv.size for lv in levels), ZERO)


def sweep(levels: list[Level], count: Decimal, fee: FeeQuote) -> LegFill | None:
    """Buy ``count`` contracts walking ``levels`` (cheapest first). None when the book is too thin.

    Each price level is treated as its own fill with its fee rounded up separately: the most conservative reading of
    the per-fill rounding rule.
    """
    if count <= 0:
        return None
    remaining = count
    fills: list[Fill] = []
    for lv in levels:
        if remaining <= 0:
            break
        take = min(lv.size, remaining)
        fills.append(Fill(lv.price, take, fee.taker_fee(lv.price, take)))
        remaining -= take
    if remaining > 0:
        return None
    notional = sum((f.price * f.count for f in fills), ZERO)
    fees = sum((f.fee for f in fills), ZERO)
    return LegFill(tuple(fills), count, notional, fees)


def breakpoints(levels: list[Level]) -> list[Decimal]:
    """Cumulative sizes at which the marginal price changes."""
    out: list[Decimal] = []
    total = ZERO
    for lv in levels:
        total += lv.size
        out.append(total)
    return out
