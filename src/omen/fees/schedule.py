"""Fee formulas: turn (fee_type, multiplier, price, contracts) into dollars, rounded up, never understated."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from omen.money import ONE, ZERO, ceil_to, dec


class FeeUnavailable(RuntimeError):
    """The fee for this market cannot be computed with confidence; the market must not be traded."""


@dataclass(frozen=True)
class FeeFormula:
    taker_rate: Decimal = ZERO
    maker_rate: Decimal = ZERO
    taker_per_contract: Decimal | None = None
    maker_per_contract: Decimal | None = None

    @property
    def is_flat(self) -> bool:
        return self.taker_per_contract is not None


@dataclass(frozen=True)
class FeeSchedule:
    version: str
    verified_on: date
    source_url: str
    formulas: dict[str, FeeFormula]
    rounding_increment: Decimal

    @classmethod
    def load(cls, path: str | Path, rounding_increment: Decimal) -> FeeSchedule:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
        formulas: dict[str, FeeFormula] = {}
        for name, body in (data.get("fee_types") or {}).items():
            if "taker_per_contract" in body:
                formulas[name] = FeeFormula(
                    taker_per_contract=dec(body["taker_per_contract"]),
                    maker_per_contract=dec(body.get("maker_per_contract"), ZERO),
                )
            elif "taker_rate" in body:
                formulas[name] = FeeFormula(dec(body["taker_rate"]), dec(body.get("maker_rate"), ZERO))
            # a fee type with neither (e.g. an unconfigured flat table) is deliberately left out -> fail closed
        return cls(
            version=str(data.get("schedule_version", "unknown")),
            verified_on=date.fromisoformat(str(data.get("verified_on", "1970-01-01"))),
            source_url=str(data.get("source_url", "")),
            formulas=formulas,
            rounding_increment=rounding_increment,
        )

    def formula(self, fee_type: str) -> FeeFormula:
        try:
            return self.formulas[fee_type]
        except KeyError:
            raise FeeUnavailable(f"fee_type {fee_type!r} has no configured formula") from None


@dataclass(frozen=True)
class FeeQuote:
    """The fee terms in force for one market at one moment."""

    fee_type: str
    multiplier: Decimal
    formula: FeeFormula
    rounding_increment: Decimal
    source: str  # e.g. "series", "event override", "scheduled change (guard)"

    def _round(self, fee: Decimal) -> Decimal:
        if fee <= 0:
            return ZERO
        return ceil_to(fee, self.rounding_increment)

    def taker_fee(self, price: Decimal, contracts: Decimal) -> Decimal:
        """Fee for one fill of ``contracts`` at ``price`` taking liquidity, rounded up."""
        if not ZERO < price < ONE:
            raise ValueError(f"price out of range: {price}")
        if self.formula.is_flat:
            return self._round(self.multiplier * self.formula.taker_per_contract * contracts)  # type: ignore[operator]
        return self._round(self.multiplier * self.formula.taker_rate * contracts * price * (ONE - price))

    def maker_fee(self, price: Decimal, contracts: Decimal) -> Decimal:
        if not ZERO < price < ONE:
            raise ValueError(f"price out of range: {price}")
        if self.formula.is_flat:
            return self._round(self.multiplier * (self.formula.maker_per_contract or ZERO) * contracts)
        return self._round(self.multiplier * self.formula.maker_rate * contracts * price * (ONE - price))

    def taker_rate_per_contract(self, price: Decimal) -> Decimal:
        """Unrounded marginal taker fee per contract (for screening and ranking only)."""
        if self.formula.is_flat:
            return self.multiplier * self.formula.taker_per_contract  # type: ignore[operator]
        return self.multiplier * self.formula.taker_rate * price * (ONE - price)

    def describe(self) -> str:
        mult = f"×{self.multiplier.normalize():f}" if self.multiplier != 1 else ""
        return f"{self.fee_type}{mult} ({self.source})"
