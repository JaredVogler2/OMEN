"""Active liquidity-reward programs (``GET /incentive_programs``) and which of them are worth quoting."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from omen.http import HttpClient


def _ts(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)


def series_of(ticker: str) -> str:
    return ticker.split("-", 1)[0]


@dataclass(frozen=True)
class Program:
    ticker: str
    start: datetime
    end: datetime
    reward_usd: float  # for the whole period (the API's period_reward is in centi-cents)
    target: float  # Target Size, contracts per side
    discount: float  # Discount Factor per tick below the Reference Price

    @property
    def series(self) -> str:
        return series_of(self.ticker)

    @property
    def hours(self) -> float:
        return (self.end - self.start).total_seconds() / 3600

    @property
    def per_day(self) -> float:
        return self.reward_usd / max(self.hours / 24, 1e-9)

    @property
    def per_second(self) -> float:
        return self.reward_usd / max((self.end - self.start).total_seconds(), 1e-9)

    def live(self, now: datetime) -> bool:
        return self.start <= now < self.end

    @classmethod
    def from_api(cls, d: dict) -> Program | None:
        """A program, or None for another incentive type or an entry this code cannot read (a new API shape must
        not stop the loop: callers count the misses)."""
        if d.get("incentive_type") != "liquidity" or not d.get("target_size_fp"):
            return None
        try:
            return cls(
                ticker=d["market_ticker"],
                start=_ts(d["start_date"]),
                end=_ts(d["end_date"]),
                reward_usd=float(d["period_reward"]) / 10_000,
                target=float(d["target_size_fp"]),
                discount=(d.get("discount_factor_bps") or 10_000) / 10_000,
            )
        except (KeyError, TypeError, ValueError, AttributeError):
            return None


def fetch_active(http: HttpClient, unreadable: list[dict] | None = None) -> list[Program]:
    """Every active liquidity program (paginated with ``next_cursor``). Liquidity entries that cannot be read are
    appended to ``unreadable`` when given."""
    return fetch_programs(http, ("active",), unreadable)


def fetch_programs(
    http: HttpClient, statuses: Iterable[str] = ("active",), unreadable: list[dict] | None = None
) -> list[Program]:
    """Liquidity programs in each of ``statuses`` (``active``, ``upcoming``, ...), paginated with ``next_cursor``."""
    out: list[Program] = []
    for status in statuses:
        cursor = ""
        while True:
            params: dict = {"status": status, "type": "liquidity", "limit": 10_000}
            if cursor:
                params["cursor"] = cursor
            data = http.get_json("/incentive_programs", params=params)
            for raw in data.get("incentive_programs") or []:
                p = Program.from_api(raw)
                if p is not None:
                    out.append(p)
                elif unreadable is not None and raw.get("incentive_type") == "liquidity":
                    unreadable.append(raw)
            cursor = data.get("next_cursor") or ""
            if not cursor:
                break
    return out


def eligible(
    programs: Iterable[Program],
    now: datetime,
    *,
    min_hours: float,
    min_reward_per_day: float,
    exclude: str,
    min_time_left: timedelta,
    limit: int | None,
    max_hours: float = 0.0,
) -> list[Program]:
    """Live programs worth quoting, richest first.

    Short programs (15-minute and hourly markets) and the excluded series are where resting orders get picked off:
    the backtest of rewarded markets lost far more to fills there than the rewards paid."""
    pattern = re.compile(exclude) if exclude else None
    keep = [
        p
        for p in programs
        if p.live(now)
        and p.hours >= min_hours
        and (max_hours <= 0 or p.hours <= max_hours)
        and p.per_day >= min_reward_per_day
        and p.end - now >= min_time_left
        and not (pattern and pattern.search(p.series))
    ]
    keep.sort(key=lambda p: -p.per_day)
    return keep if limit is None else keep[:limit]
