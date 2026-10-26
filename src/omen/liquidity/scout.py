"""The weekly GO/NO-GO test of docs/PLAYBOOK.md: is there enough of what OMEN can actually trade to justify one capped
live measurement of the dead-side play?

Only programs OMEN's dead-side filters accept count: 1-7 days (``min_program_hours``/``max_program_hours``), at least
``dead_side_min_period_reward_usd`` a period, a Target Size the per-market loss cap can complete, and ending at most
``dead_side_close_gap_hours`` before the market closes. Each counts its reward per day over the hours it overlaps the
week, and programs of 48 h or more count at ``scout_long_program_weight`` (they earned about 0.3x per reward dollar in
the replays). GO needs the weighted pool, enough markets across enough series, and ``scout_rewards_confirmed``: the
Oct 28, 2026 amendment sets rewards per sub-category, and until Kalshi's Liquidity Rewards page is seen to match the
per-market amounts the API lists, every estimate OMEN makes may be several times too high."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from omen.config import LiquiditySettings
from omen.kalshi.client import KalshiClient
from omen.kalshi.models import OrderBook
from omen.liquidity.maker import decided, levels
from omen.liquidity.programs import Program, fetch_programs
from omen.liquidity.scoring import side_score

LONG_PROGRAM_HOURS = 48.0
POOLED_GROUP = 5  # this many markets of one series sharing reward, start, end and target may be one pooled reward


@dataclass
class ScoutReport:
    start: datetime
    end: datetime
    programs: int  # every program fetched (upcoming and active)
    unreadable: int  # liquidity entries OMEN could not read (a new API shape)
    qualifying: list[Program] = field(default_factory=list)
    pool_per_day: float = 0.0  # weighted, averaged over the week
    raw_pool_per_day: float = 0.0  # the same without the long-program weight
    by_series: list[tuple[str, float]] = field(default_factory=list)
    empty_dead_sides: list[str] = field(default_factory=list)  # qualifying markets OMEN could bid on right now
    daily_fuel: int = 0  # daily gas or diesel programs running in the week (most of the old profit)
    pooled_groups: int = 0  # groups of markets that look like one sub-category reward repeated per market
    rewards_confirmed: bool = False
    reasons: list[str] = field(default_factory=list)  # each unmet GO condition

    @property
    def markets(self) -> int:
        return len({p.ticker for p in self.qualifying})

    @property
    def series(self) -> int:
        return len({p.series for p in self.qualifying})

    @property
    def go(self) -> bool:
        return not self.reasons


def empty_dead_side(book: OrderBook, p: Program) -> bool:
    """Decided (favorite bid 97-98c) with the favorite's side at the target and nobody at all on the other side."""
    sides = decided(book)
    if sides is None:
        return False
    dead, live = sides
    return not levels(book, dead) and side_score(levels(book, live), p.target, p.discount).met


def scout(client: KalshiClient, lp: LiquiditySettings, start: datetime, days: float = 7.0) -> ScoutReport:
    end = start + timedelta(days=days)
    unreadable: list[dict] = []
    progs = fetch_programs(client.http, ("upcoming", "active"), unreadable)
    report = ScoutReport(start, end, len(progs), len(unreadable), rewards_confirmed=lp.scout_rewards_confirmed)
    exclude = re.compile(lp.dead_side_exclude_series) if lp.dead_side_exclude_series else None
    week = [p for p in progs if p.start < end and p.end > start]
    report.daily_fuel = sum(1 for p in week if p.series.startswith(("KXAAAGASD", "KXDIESELD")))
    groups = Counter((p.series, p.start, p.end, p.reward_usd, p.target) for p in week if p.start >= start)
    report.pooled_groups = sum(1 for n in groups.values() if n >= POOLED_GROUP)
    cands = [
        p
        for p in week
        if p.hours >= lp.min_program_hours
        and (lp.max_program_hours <= 0 or p.hours <= lp.max_program_hours)
        and p.reward_usd >= lp.dead_side_min_period_reward_usd
        and (lp.dead_side_max_target <= 0 or p.target <= lp.dead_side_max_target)
        and not (exclude and exclude.search(p.series))
    ]
    tickers = sorted({p.ticker for p in cands})
    markets = {m.ticker: m for m in client.markets_by_ticker(tickers)} if tickers else {}
    gap = timedelta(hours=lp.dead_side_close_gap_hours)
    for p in cands:
        m = markets.get(p.ticker)
        if m is None or m.close_time is None:
            continue
        if lp.dead_side_close_gap_hours > 0 and p.end < m.close_time - gap:
            continue
        report.qualifying.append(p)
    totals: dict[str, float] = {}
    for p in report.qualifying:
        overlap = (min(p.end, end) - max(p.start, start)).total_seconds() / 86400
        per_day = p.per_day * overlap / days
        weight = lp.scout_long_program_weight if p.hours >= LONG_PROGRAM_HOURS else 1.0
        report.raw_pool_per_day += per_day
        report.pool_per_day += per_day * weight
        totals[p.series] = totals.get(p.series, 0.0) + per_day * weight
    report.by_series = sorted(totals.items(), key=lambda kv: -kv[1])
    if report.qualifying:
        books = client.orderbooks({p.ticker for p in report.qualifying})
        report.empty_dead_sides = sorted(
            {p.ticker for p in report.qualifying if p.ticker in books and empty_dead_side(books[p.ticker], p)}
        )
    if report.pool_per_day < lp.scout_go_pool_per_day_usd:
        report.reasons.append(
            f"weighted pool ${report.pool_per_day:,.0f}/day is under ${lp.scout_go_pool_per_day_usd:,.0f}/day"
        )
    if report.markets < lp.scout_go_markets or report.series < lp.scout_go_series:
        report.reasons.append(
            f"{report.markets} markets in {report.series} series (needs {lp.scout_go_markets} in {lp.scout_go_series})"
        )
    if not lp.scout_rewards_confirmed:
        report.reasons.append(
            "per-market rewards not confirmed against Kalshi's Liquidity Rewards page (scout_rewards_confirmed)"
        )
    return report


def render(r: ScoutReport, tz) -> str:
    lines = [
        f"Week {r.start.astimezone(tz):%a %b %d %I:%M %p} to {r.end.astimezone(tz):%a %b %d %I:%M %p} ET: "
        + ("GO (one capped measurement week)" if r.go else "NO-GO ($0 deployed)"),
        f"Qualifying: {r.markets} markets in {r.series} series; pool ${r.pool_per_day:,.0f}/day weighted "
        f"(${r.raw_pool_per_day:,.0f}/day unweighted).",
    ]
    if r.by_series:
        lines.append("Largest: " + ", ".join(f"{s} ${v:,.0f}" for s, v in r.by_series[:6]) + ".")
    lines.append(
        f"Decided with an empty dead side right now: {len(r.empty_dead_sides)}"
        + (f" ({', '.join(r.empty_dead_sides[:5])})" if r.empty_dead_sides else "")
        + "."
    )
    lines.append(f"Daily gas/diesel programs in the week: {r.daily_fuel}.")
    if r.pooled_groups:
        lines.append(
            f"{r.pooled_groups} group(s) of {POOLED_GROUP}+ markets share one reward, start, end and target: check "
            "whether Kalshi's page lists that amount per market or for the whole sub-category."
        )
    if r.unreadable:
        lines.append(f"{r.unreadable} program(s) could not be read (new API shape?).")
    lines.extend(f"Not met: {reason}." for reason in r.reasons)
    return "\n".join(lines)
