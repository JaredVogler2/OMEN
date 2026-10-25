"""Typed views over Kalshi API objects. Only the fields OMEN relies on are kept (a full sweep holds ~170k markets)."""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from omen.money import ONE, ZERO, PriceGrid, dec


def _i(value: str | None) -> str:
    """Intern a repeated string (titles, settlement boilerplate): a full sweep holds ~170k markets."""
    return sys.intern(value) if value else ""


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


@dataclass(frozen=True)
class Series:
    ticker: str
    title: str
    category: str
    fee_type: str
    fee_multiplier: Decimal
    last_updated: datetime | None
    settlement_sources: tuple[str, ...] = ()

    @classmethod
    def from_api(cls, d: dict) -> Series:
        return cls(
            ticker=d["ticker"],
            title=_i(d.get("title")),
            category=d.get("category") or "",
            fee_type=d.get("fee_type") or "",
            fee_multiplier=dec(d.get("fee_multiplier"), Decimal(1)),
            last_updated=parse_ts(d.get("last_updated_ts")),
            settlement_sources=tuple(s.get("name", "") for s in d.get("settlement_sources") or []),
        )


@dataclass(frozen=True)
class Event:
    event_ticker: str
    series_ticker: str
    title: str
    sub_title: str
    category: str
    mutually_exclusive: bool
    collateral_return_type: str

    @classmethod
    def from_api(cls, d: dict) -> Event:
        return cls(
            event_ticker=d["event_ticker"],
            series_ticker=d.get("series_ticker") or "",
            title=d.get("title") or "",
            sub_title=d.get("sub_title") or "",
            category=d.get("category") or "",
            mutually_exclusive=bool(d.get("mutually_exclusive")),
            collateral_return_type=d.get("collateral_return_type") or "",
        )


_NUM = re.compile(r"[-+]?\$?\d[\d,]*(?:\.\d+)?")


def mask_numbers(text: str) -> str:
    """Replace every number with ``#`` so that markets differing only by strike share a template."""
    return _NUM.sub("#", text or "").strip()


@dataclass(frozen=True)
class Market:
    ticker: str
    event_ticker: str
    status: str
    title: str
    yes_sub_title: str
    rules_primary: str
    rules_secondary: str
    strike_type: str | None
    floor_strike: Decimal | None
    cap_strike: Decimal | None
    yes_bid: Decimal
    yes_ask: Decimal
    yes_bid_size: Decimal
    yes_ask_size: Decimal
    last_price: Decimal
    volume_24h: Decimal
    open_interest: Decimal
    close_time: datetime
    expiration_time: datetime | None
    expected_expiration_time: datetime | None
    can_close_early: bool
    is_provisional: bool
    primary_participant_key: str | None
    result: str
    settlement_value: Decimal | None
    grid: PriceGrid
    market_type: str
    exchange_index: int = 0  # the matching engine (shard) the market trades on; API orders need funds there

    @classmethod
    def from_api(cls, d: dict) -> Market:
        return cls(
            ticker=d["ticker"],
            event_ticker=_i(d.get("event_ticker")),
            status=_i(d.get("status")),
            title=d.get("title") or "",
            yes_sub_title=d.get("yes_sub_title") or d.get("subtitle") or "",
            rules_primary=d.get("rules_primary") or "",
            rules_secondary=_i(d.get("rules_secondary")),
            strike_type=d.get("strike_type"),
            floor_strike=dec(d["floor_strike"]) if d.get("floor_strike") is not None else None,
            cap_strike=dec(d["cap_strike"]) if d.get("cap_strike") is not None else None,
            yes_bid=dec(d.get("yes_bid_dollars"), ZERO),
            yes_ask=dec(d.get("yes_ask_dollars"), ZERO),
            yes_bid_size=dec(d.get("yes_bid_size_fp"), ZERO),
            yes_ask_size=dec(d.get("yes_ask_size_fp"), ZERO),
            last_price=dec(d.get("last_price_dollars"), ZERO),
            volume_24h=dec(d.get("volume_24h_fp"), ZERO),
            open_interest=dec(d.get("open_interest_fp"), ZERO),
            close_time=parse_ts(d.get("close_time")) or datetime.max.replace(tzinfo=UTC),
            expiration_time=parse_ts(d.get("expiration_time")),
            expected_expiration_time=parse_ts(d.get("expected_expiration_time")),
            can_close_early=bool(d.get("can_close_early")),
            is_provisional=bool(d.get("is_provisional")),
            primary_participant_key=d.get("primary_participant_key"),
            result=d.get("result") or "",
            settlement_value=dec(d["settlement_value_dollars"]) if d.get("settlement_value_dollars") else None,
            grid=PriceGrid.from_api(d.get("price_ranges")),
            market_type=d.get("market_type") or "binary",
            exchange_index=int(d.get("exchange_index") or 0),
        )

    @property
    def series_ticker(self) -> str:
        return self.event_ticker.split("-", 1)[0]

    @property
    def has_yes_ask(self) -> bool:
        """Someone is offering YES (i.e. there is a NO bid). Kalshi reports an empty side as 0 (or 1)."""
        return ZERO < self.yes_ask < ONE and self.yes_ask_size > 0

    @property
    def has_yes_bid(self) -> bool:
        """Someone bids YES, which is the liquidity a NO buyer takes."""
        return ZERO < self.yes_bid < ONE and self.yes_bid_size > 0

    @property
    def no_ask(self) -> Decimal:
        """Price to buy NO now (= 1 - best YES bid). Only meaningful when ``has_yes_bid``."""
        return ONE - self.yes_bid

    @property
    def tradable(self) -> bool:
        return self.status in {"active", "open"} and self.market_type == "binary" and not self.is_provisional

    @property
    def settles_at(self) -> datetime:
        """Best estimate of when the payout lands: the expected expiration, else the close time."""
        return self.expected_expiration_time or self.close_time

    def payout_yes(self) -> Decimal | None:
        """Per-contract YES payout once settled (handles scalar / fair-price settlements), None if unsettled."""
        if self.settlement_value is not None and self.status in {"settled", "finalized", "determined"}:
            return self.settlement_value
        if self.result == "yes":
            return ONE
        if self.result == "no":
            return ZERO
        return None


@dataclass(frozen=True)
class Level:
    price: Decimal
    size: Decimal


@dataclass(frozen=True)
class OrderBook:
    """Resting bids for YES and for NO, best (highest) first.

    Kalshi only publishes bids: buying YES lifts the NO bids (at ``1 - no_bid``) and buying NO lifts YES bids.
    """

    ticker: str
    yes_bids: tuple[Level, ...]
    no_bids: tuple[Level, ...]

    @classmethod
    def from_api(cls, ticker: str, book: dict) -> OrderBook:
        def levels(rows: list | None) -> tuple[Level, ...]:
            out = [Level(dec(p), dec(s)) for p, s in (rows or []) if dec(s) > 0]
            return tuple(sorted(out, key=lambda lv: lv.price, reverse=True))

        return cls(ticker, levels(book.get("yes_dollars")), levels(book.get("no_dollars")))

    def asks(self, side: str) -> list[Level]:
        """Levels available to a buyer of ``side`` ('yes' or 'no'), cheapest first."""
        opposite = self.no_bids if side == "yes" else self.yes_bids
        return [Level(ONE - lv.price, lv.size) for lv in opposite]

    def best_ask(self, side: str) -> Level | None:
        asks = self.asks(side)
        return asks[0] if asks else None
