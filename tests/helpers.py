from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from omen.fees import FeeRegistry, FeeSchedule
from omen.kalshi.models import Event, Market, OrderBook, Series

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 23, 21, 0, tzinfo=UTC)


def schedule(increment: str = "0.01") -> FeeSchedule:
    return FeeSchedule.load(ROOT / "config" / "fee_schedule.toml", Decimal(increment))


def registry(series: dict[str, tuple[str, str]] | None = None) -> FeeRegistry:
    reg = FeeRegistry(schedule(), timedelta(minutes=60))
    rows = [
        Series(t, t, "", ft, Decimal(m), None)
        for t, (ft, m) in (series or {"KXTEST": ("quadratic", "1"), "KXSPREAD": ("quadratic", "1")}).items()
    ]
    reg.load_series(rows, NOW - timedelta(hours=1))
    return reg


def default_rules(strike_type: str | None, floor: str | None, cap: str | None) -> str:
    phrase = {
        "greater": f"above {floor}",
        "greater_or_equal": f"at least {floor}",
        "less": f"below {cap}",
        "less_or_equal": f"at most {cap}",
        "between": f"between {floor} and {cap}",
    }.get(strike_type or "", "the named outcome")
    return f"If the value is {phrase}, then the market resolves to Yes."


def market(
    ticker: str,
    event: str = "KXTEST-26OCT30",
    *,
    yes_bid: str = "0",
    yes_ask: str = "0",
    bid_size: str = "100",
    ask_size: str = "100",
    strike_type: str | None = None,
    floor: str | None = None,
    cap: str | None = None,
    title: str = "Test market?",
    sub: str = "",
    rules: str | None = None,
    rules2: str = "",
    close: datetime | None = None,
    status: str = "active",
    ranges: list[dict] | None = None,
) -> Market:
    d = {
        "ticker": ticker,
        "event_ticker": event,
        "status": status,
        "title": title,
        "yes_sub_title": sub,
        "rules_primary": rules if rules is not None else default_rules(strike_type, floor, cap),
        "rules_secondary": rules2,
        "strike_type": strike_type,
        "floor_strike": floor,
        "cap_strike": cap,
        "yes_bid_dollars": yes_bid,
        "yes_ask_dollars": yes_ask,
        "yes_bid_size_fp": bid_size,
        "yes_ask_size_fp": ask_size,
        "close_time": (close or NOW + timedelta(days=2)).isoformat().replace("+00:00", "Z"),
        "market_type": "binary",
        "price_ranges": ranges or [{"start": "0.0000", "end": "1.0000", "step": "0.0100"}],
    }
    return Market.from_api(d)


def event(ticker: str = "KXTEST-26OCT30", me: bool = True, title: str = "Test event") -> Event:
    return Event(ticker, ticker.split("-")[0], title, "", "Test", me, "MECNET" if me else "")


def book(ticker: str, yes: list[tuple[str, str]] = (), no: list[tuple[str, str]] = ()) -> OrderBook:
    return OrderBook.from_api(ticker, {"yes_dollars": [list(x) for x in yes], "no_dollars": [list(x) for x in no]})
