"""One scan cycle: keep the market universe and fee registry fresh, screen, verify against full depth."""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from omen.config import Settings
from omen.fees import FeeRegistry, FeeSchedule, FeeUnavailable, ScheduledChange
from omen.fees.schedule import FeeQuote
from omen.kalshi.client import KalshiClient
from omen.kalshi.models import Event, Market
from omen.ledger import Ledger, from_iso, iso
from omen.strategies.base import Opportunity
from omen.strategies.lock import (
    Candidate,
    VerifyContext,
    basket_event_prescreen,
    explain_failure,
    screen_ladders,
    screen_no_baskets,
    screen_yes_partitions,
    verify,
)

log = logging.getLogger(__name__)


@dataclass
class Notice:
    """Something the owner should know (fee change, data problem). Routed to #kalshi-status."""

    kind: str
    title: str
    body: str
    severity: str = "info"  # info | warning | error


@dataclass
class ScanResult:
    kind: str
    started: datetime
    markets: int = 0
    candidates: int = 0
    opportunities: list[Opportunity] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)
    notices: list[Notice] = field(default_factory=list)
    requests: int = 0
    seconds: float = 0.0


class Engine:
    def __init__(self, settings: Settings, client: KalshiClient, ledger: Ledger, clock=None):
        self.s = settings
        self.client = client
        self.ledger = ledger
        self.clock = clock or (lambda: datetime.now().astimezone())
        schedule_path = settings.project_root / settings.fees.schedule_file
        self.schedule = FeeSchedule.load(schedule_path, settings.fees.rounding_increment)
        self.fees = FeeRegistry(self.schedule, timedelta(minutes=settings.fees.change_guard_minutes))
        self.markets: dict[str, Market] = {}
        self.scanned: set[str] = set()  # tickers whose quotes the last sweep refreshed
        self.sweep_partial = False  # the last sweep stopped at its deadline before reading every page
        self.events: dict[str, Event] = ledger.load_events()
        self._fee_cache: dict[tuple[str, str], FeeQuote | FeeUnavailable] = {}
        self._fee_cache_at: datetime | None = None

    # ------------------------------------------------------------------------------------------------- timing
    def _due(self, key: str, every_seconds: int, now: datetime) -> bool:
        last = from_iso(self.ledger.get_meta(key))
        return last is None or (now - last).total_seconds() >= every_seconds

    def _mark(self, key: str, now: datetime) -> None:
        self.ledger.set_meta(key, iso(now))

    # --------------------------------------------------------------------------------------------------- fees
    def refresh_fees(self, now: datetime, force: bool = False) -> list[Notice]:
        notices: list[Notice] = []
        if force or not self.fees.series or self._due("series_refreshed_at", self.s.scan.series_refresh_seconds, now):
            rows = self.client.all_series()
            if not rows:
                raise RuntimeError("GET /series returned no series; refusing to price with stale fees")
            self.fees.load_series(rows, now)
            self._mark("series_refreshed_at", now)
            for series, before, after in self.ledger.diff_fee_terms(
                {s.ticker: (s.fee_type, s.fee_multiplier) for s in rows}, now
            ):
                notices.append(Notice("fee_change", f"Fee terms changed: {series}", f"{before} → {after}", "warning"))
        if (
            force
            or not self._fee_changes_loaded()
            or self._due("fee_changes_refreshed_at", self.s.scan.fee_changes_refresh_seconds, now)
        ):
            changes = self.fees.load_changes(
                self.client.series_fee_changes(show_historical=True), self.client.event_fee_changes()
            )
            self._mark("fee_changes_refreshed_at", now)
            self._loaded_changes = True
            fresh: list[ScheduledChange] = self.ledger.new_fee_changes(changes, now)
            for ch in fresh:
                notices.append(Notice("fee_schedule", f"Fee change scheduled: {ch.ticker}", ch.describe(), "warning"))
        unknown = self.fees.unknown_fee_types()
        known_unknown = self.ledger.get_meta("unknown_fee_types", "")
        if ",".join(sorted(unknown)) != known_unknown:
            self.ledger.set_meta("unknown_fee_types", ",".join(sorted(unknown)))
        if unknown and ",".join(sorted(unknown)) != known_unknown:
            notices.append(
                Notice(
                    "fee_unknown",
                    "Unknown Kalshi fee type",
                    f"Series use fee types with no formula in fee_schedule.toml: {', '.join(sorted(unknown))}. "
                    "Those markets are excluded until the formula is added.",
                    "error",
                )
            )
        self._fee_cache.clear()
        self._fee_cache_at = now
        return notices

    def _fee_changes_loaded(self) -> bool:
        return getattr(self, "_loaded_changes", False)

    def fee_for(self, m: Market) -> FeeQuote:
        key = (m.series_ticker, m.event_ticker)
        hit = self._fee_cache.get(key)
        if hit is None:
            try:
                hit = self.fees.quote(m.series_ticker, m.event_ticker, self._fee_cache_at or self.clock())
            except FeeUnavailable as exc:
                hit = exc
            self._fee_cache[key] = hit
        if isinstance(hit, FeeUnavailable):
            raise hit
        return hit

    # ------------------------------------------------------------------------------------------------ universe
    def sweep(
        self,
        kind: str,
        now: datetime,
        horizon_hours: float | None = None,
        progress: Callable[[int], None] | None = None,
        deadline: float | None = None,
    ) -> int:
        """Refresh quotes. 'hot' = markets closing within the hot horizon; 'full' = every open market.

        ``progress`` is called with the running market count as pages arrive (the website streams it).
        ``deadline`` (a ``time.monotonic()`` value) stops paging early; ``sweep_partial`` then reports it."""
        count = 0
        self.sweep_partial = False

        def out_of_time() -> bool:
            if deadline is not None and time.monotonic() > deadline:
                self.sweep_partial = True
            return self.sweep_partial

        if kind == "full":
            fresh = {}
            for m in self.client.iter_markets(status="open", mve_filter="exclude"):
                fresh[m.ticker] = m
                count += 1
                if progress and count % 1000 == 0:
                    progress(count)
                if out_of_time():
                    break
            self.markets = fresh
            self.scanned = set(fresh)
        else:
            horizon = now + timedelta(hours=horizon_hours or self.s.scan.hot_horizon_hours)
            self.scanned = set()
            # status=open matters: without it Kalshi pages through every non-open market closing in the window too
            # (64 pages instead of 5 for a 24 h horizon)
            for m in self.client.iter_markets(
                status="open",
                min_close_ts=int(now.timestamp()),
                max_close_ts=int(horizon.timestamp()),
                mve_filter="exclude",
            ):
                self.markets[m.ticker] = m
                self.scanned.add(m.ticker)
                count += 1
                if progress and count % 1000 == 0:
                    progress(count)
                if out_of_time():
                    break
        if progress:
            progress(count)
        cutoff = now + timedelta(minutes=self.s.scan.min_minutes_to_close)
        self.markets = {t: m for t, m in self.markets.items() if m.tradable and m.close_time > cutoff}
        self._refresh_events(now)
        return len(self.markets)

    def _refresh_events(self, now: datetime) -> None:
        if self._due("events_refreshed_at", self.s.scan.events_refresh_seconds, now) and self.events:
            since = from_iso(self.ledger.get_meta("events_refreshed_at"))
            updated = list(self.client.iter_events(min_updated_ts=int(since.timestamp()))) if since else []
            if updated:
                self.ledger.upsert_events(updated, now)
                self.events.update({e.event_ticker: e for e in updated})
            self._mark("events_refreshed_at", now)

    def ensure_events(self, tickers: set[str], now: datetime) -> None:
        """Fetch event metadata (the mutually-exclusive flag) only for events that could hold a basket play: a
        handful per scan instead of every event in the universe."""
        missing = tickers - set(self.events)
        if not missing:
            return
        fetched = self.client.events_by_ticker(missing)
        self.ledger.upsert_events(fetched, now)
        self.events.update({e.event_ticker: e for e in fetched})
        if not self.ledger.get_meta("events_refreshed_at"):
            self._mark("events_refreshed_at", now)

    def by_event(self) -> dict[str, list[Market]]:
        out: dict[str, list[Market]] = defaultdict(list)
        for m in self.markets.values():
            out[m.event_ticker].append(m)
        return out

    # ---------------------------------------------------------------------------------------------------- scan
    def screen(self, now: datetime | None = None) -> tuple[list[Candidate], list[tuple[str, str]]]:
        lock = self.s.lock
        by_event = self.by_event()
        if lock.enable_no_basket or lock.enable_yes_partition:
            self.ensure_events(basket_event_prescreen(by_event, self.fee_for), now or self.clock())
        candidates: list[Candidate] = []
        rejected: list[tuple[str, str]] = []
        if lock.enable_no_basket:
            candidates += screen_no_baskets(self.events, by_event, self.fee_for)
        if lock.enable_ladder:
            candidates += screen_ladders(self.events, list(self.markets.values()), self.fee_for)
        if lock.enable_yes_partition:
            c, r = screen_yes_partitions(self.events, by_event, self.fee_for, lock.allow_step_gap_partitions)
            candidates += c
            rejected += r
        candidates.sort(key=lambda c: c.top_edge, reverse=True)
        return candidates, rejected

    def verify_all(self, candidates: list[Candidate], now: datetime) -> list[Opportunity]:
        return self.verify_report(candidates, now)[0]

    def verify_report(
        self, candidates: list[Candidate], now: datetime
    ) -> tuple[list[Opportunity], list[tuple[Candidate, str]]]:
        """Verified plays, plus each screened candidate that did not survive full depth and why."""
        if not candidates:
            return [], []
        candidates = self._refresh_partitions(candidates, now)
        tickers = {m.ticker for c in candidates for m, _ in c.legs}
        books = self.client.orderbooks(tickers)
        self.scanned |= set(books)  # freshly re-checked: plays on them can be declared gone this cycle
        ctx = VerifyContext(books=books, fee=self.fee_for, settings=self.s.lock, now=now)
        out = []
        failed = []
        for c in candidates:
            opp = verify(c, ctx)
            if opp is not None:
                out.append(opp)
            else:
                failed.append((c, explain_failure(c, ctx)))
        out.sort(key=lambda o: o.profit, reverse=True)
        return out, failed

    def _refresh_partitions(self, candidates: list[Candidate], now: datetime) -> list[Candidate]:
        """A YES basket is only valid over the event's complete market list, so re-read it fresh from the API."""
        keep = [c for c in candidates if c.strategy != "yes_partition"]
        for c in (c for c in candidates if c.strategy == "yes_partition"):
            fresh = list(self.client.iter_markets(event_ticker=c.event.event_ticker))
            tradable = [m for m in fresh if m.tradable]
            if len(tradable) != len(fresh):
                continue  # some bucket is closed/paused/provisional: the basket cannot be completed
            redo, _ = screen_yes_partitions(
                {c.event.event_ticker: c.event},
                {c.event.event_ticker: tradable},
                self.fee_for,
                self.s.lock.allow_step_gap_partitions,
            )
            keep += redo
        return keep

    def _trading_active(self, now: datetime, result: ScanResult) -> bool:
        """No plays while the exchange is paused (maintenance windows, incidents); announce each change once."""
        status = self.client.exchange_status()
        active = bool(status.get("exchange_active", True)) and bool(status.get("trading_active", True))
        previous = self.ledger.get_meta("trading_active", "1")
        self.ledger.set_meta("trading_active", "1" if active else "0")
        if not active and previous == "1":
            result.notices.append(
                Notice("exchange", "Kalshi trading is paused", "No plays are alerted until trading resumes.", "warning")
            )
        elif active and previous == "0":
            result.notices.append(Notice("exchange", "Kalshi trading resumed", "Scanning normally again."))
        return active

    def scan(self, kind: str) -> ScanResult:
        t0 = time.monotonic()
        req0 = self.client.http.requests_made
        now = self.clock()
        result = ScanResult(kind=kind, started=now)
        result.notices += self.refresh_fees(now)
        trading = self._trading_active(now, result)
        result.markets = self.sweep(kind, now)
        candidates, rejected = self.screen(now) if trading else ([], [])
        result.candidates = len(candidates)
        result.rejected = rejected
        result.opportunities = self.verify_all(candidates, self.clock())
        result.requests = self.client.http.requests_made - req0
        result.seconds = time.monotonic() - t0
        log.info(
            "%s scan: %d markets, %d candidates, %d verified opportunities, %d requests, %.1fs",
            kind,
            result.markets,
            result.candidates,
            len(result.opportunities),
            result.requests,
            result.seconds,
        )
        return result
