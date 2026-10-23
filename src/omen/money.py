"""Exact money, contract-count and price-grid arithmetic.

Kalshi sends prices as fixed-point dollar strings (``*_dollars``, up to 4 decimals) and contract counts as fixed-point
strings (``*_fp``, 2 decimals). Everything that touches money in OMEN is a ``Decimal``; floats are only used for model
probabilities.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation

ZERO = Decimal(0)
ONE = Decimal(1)
CENT = Decimal("0.01")
CONTRACT_STEP = Decimal("0.01")  # minimum fractional-contract granularity


def dec(value: object, default: Decimal | None = None) -> Decimal:
    """Parse an API value (string, int, float or None) into a Decimal without binary-float noise."""
    if value is None or value == "":
        if default is None:
            raise ValueError("missing numeric value")
        return default
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        if default is not None:
            return default
        raise ValueError(f"not a number: {value!r}") from exc


def ceil_to(value: Decimal, step: Decimal) -> Decimal:
    """Round ``value`` up to the next multiple of ``step`` (exact)."""
    if step <= 0:
        raise ValueError("step must be positive")
    return (value / step).to_integral_value(rounding=ROUND_CEILING) * step


def floor_to(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        raise ValueError("step must be positive")
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def usd(value: Decimal, places: int = 2) -> str:
    q = Decimal(1).scaleb(-places)
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value).quantize(q):,}"


def cents(price: Decimal) -> str:
    """Render a price as cents, keeping sub-cent ticks (``0.0350`` -> ``3.5¢``)."""
    c = (price * 100).normalize()
    text = f"{c:f}"
    return f"{text}¢"


def fmt_count(count: Decimal) -> str:
    c = count.normalize()
    return f"{c:f}" if c != c.to_integral_value() else f"{int(c):,}"


@dataclass(frozen=True)
class PriceBand:
    start: Decimal
    end: Decimal
    step: Decimal


@dataclass(frozen=True)
class PriceGrid:
    """A market's valid price grid, from its ``price_ranges`` (the API's source of truth for ticks)."""

    bands: tuple[PriceBand, ...]

    @classmethod
    def from_api(cls, ranges: list[dict] | None) -> PriceGrid:
        if not ranges:
            return _DEFAULT_GRID
        key = tuple((str(r["start"]), str(r["end"]), str(r["step"])) for r in ranges)
        grid = _GRIDS.get(key)
        if grid is None:  # a handful of distinct structures exist; parse each once
            bands = tuple(sorted((PriceBand(dec(a), dec(b), dec(c)) for a, b, c in key), key=lambda b: b.start))
            grid = _GRIDS[key] = cls(bands)
        return grid

    def _band_for(self, price: Decimal) -> PriceBand:
        for band in self.bands:
            if band.start <= price <= band.end:
                return band
        raise ValueError(f"price {price} outside grid")

    def is_valid(self, price: Decimal) -> bool:
        if price <= ZERO or price >= ONE:
            return False
        return any(band.start <= price <= band.end and (price - band.start) % band.step == 0 for band in self.bands)

    def snap_down(self, price: Decimal) -> Decimal:
        band = self._band_for(price)
        return band.start + floor_to(price - band.start, band.step)

    def snap_up(self, price: Decimal) -> Decimal:
        band = self._band_for(price)
        snapped = band.start + ceil_to(price - band.start, band.step)
        return min(snapped, band.end)


_DEFAULT_GRID = PriceGrid((PriceBand(ZERO, ONE, CENT),))
_GRIDS: dict[tuple, PriceGrid] = {}
