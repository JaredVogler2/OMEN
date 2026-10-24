"""The ``omen lp`` loop: quote where Kalshi pays liquidity rewards, account for every fill and reward, settle."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from omen.alerts.discord import Notifier
from omen.alerts.render import notice_payload
from omen.config import Settings
from omen.http import HttpError
from omen.kalshi.client import KalshiClient
from omen.kalshi.models import Market, OrderBook
from omen.ledger import Ledger
from omen.liquidity.maker import (
    DeadSideFlow,
    Fill,
    Quote,
    QuotePolicy,
    Venue,
    dead_side_quote,
    dead_side_sales,
    decided,
    levels,
)
from omen.liquidity.programs import Program, eligible, fetch_active, series_of
from omen.liquidity.scoring import snapshot

log = logging.getLogger(__name__)
REFRESH = timedelta(minutes=5)
MAX_TAPE_PAGES = 30
MAX_BACKFILLS = 10  # markets whose 1c selling history is fetched per cycle (a few requests each)


def policy_from(settings: Settings) -> QuotePolicy:
    lp = settings.liquidity
    return QuotePolicy(
        sides=lp.sides,
        min_price=lp.min_quote_price,
        max_price=lp.max_quote_price,
        capital_per_market=lp.capital_per_market_usd,
        max_contracts=lp.max_contracts_per_order,
    )


def reward_rate(program: Program, book: OrderBook, ours: dict[str, tuple[float, float]]) -> float:
    """Dollars per second our resting bids earn in this book under the filed scoring."""
    snap = snapshot(
        levels(book, "yes"), levels(book, "no"), program.target, program.discount, ours.get("yes"), ours.get("no")
    )
    return program.per_second * snap.our_fraction


def mid(book: OrderBook | None) -> float | None:
    if book is None or not book.yes_bids or not book.no_bids:
        return None
    return (float(book.yes_bids[0].price) + 1 - float(book.no_bids[0].price)) / 2


class LiquidityService:
    def __init__(
        self,
        settings: Settings,
        client: KalshiClient,
        ledger: Ledger,
        notifier: Notifier,
        venue: Venue,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self.s = settings
        self.cfg = settings.liquidity
        self.client = client
        self.ledger = ledger
        self.notifier = notifier
        self.venue = venue
        self.clock = clock
        self.policy = policy_from(settings)
        self.programs: list[Program] = []
        self.dead_programs: list[Program] = []
        self.markets: dict[str, Market] = {}
        self.books: dict[str, OrderBook] = {}
        self._refreshed: datetime | None = None
        self._last: datetime | None = None
        self._rates: dict[str, float] = {}
        self._tape_since: datetime | None = None
        self.flow = DeadSideFlow()
        self._flow_at: datetime | None = None
        self._killed = False
        self._last_summary: datetime | None = None
        self.last_error: str | None = None

    @property
    def mode(self) -> str:
        return self.venue.mode

    def kill_file(self):
        return self.s.state_dir / self.s.execution.kill_switch_file

    # ---------------------------------------------------------------------------------------------- one cycle
    def step(self) -> dict:
        now = self.clock()
        stats: dict = {"time": now.isoformat(timespec="seconds"), "mode": self.mode}
        if self.kill_file().exists():
            self._accrue(now, {})
            if not self._killed:
                self.venue.cancel_all()
                self._killed = True
                self._say(
                    "Liquidity quoting stopped (kill switch)",
                    f"{self.kill_file()} exists; every resting order was canceled.",
                    "warning",
                )
            stats["killed"] = True
            self._rates = {}
            return stats
        self._killed = False
        if self._refreshed is None or now - self._refreshed >= REFRESH:
            self._refresh(now)
        held = self.ledger.lp_positions(self.mode)
        tickers = sorted({p.ticker for p in self.programs} | {p.ticker for p in self.dead_programs} | set(held))
        self.books = self.client.orderbooks(tickers) if tickers else {}
        # paper fills come from the public tape; dead-side completion also needs it (live) to measure 1c sales
        tape = self._tape(now) if self.mode == "paper" or self.cfg.complete_dead_sides else []
        self._observe_flow(now, tape)
        fills = self.venue.fills(tape, now)
        new = [f for f in fills if self.ledger.lp_record_fill(self.mode, f)]
        if new:
            self._report_fills(new)
        # the interval just ended earned the average of its start rate and its end rate: the end rate prices our
        # orders where they actually rested in the new book (a best bid that moved past us, or a fill, cuts it)
        self._accrue(now, self._current_rates())
        held = self.ledger.lp_positions(self.mode)
        desired = self._desired(now, held)
        self.venue.sync(desired, self.books, now)
        self._rates = self._current_rates()
        settled = self._settle(now, held)
        stats.update(
            programs=len(self.programs),
            quotes=len(self.venue.orders),
            dead_quotes=sum(1 for o in self.venue.orders.values() if o.price <= 0.01 + 1e-9),
            resting_usd=round(sum(o.price * o.size for o in self.venue.orders.values()), 2),
            inventory_usd=round(sum(v[1] for sides in held.values() for v in sides.values()), 2),
            reward_per_hour=round(sum(self._rates.values()) * 3600, 4),
            tape_trades=len(tape),
            fills=len(new),
            settled=settled,
        )
        if self.mode == "live" and getattr(self.venue, "errors", None):
            stats["errors"] = self.venue.errors[-5:]
            self.venue.errors.clear()
        self._maybe_summary(now)
        return stats

    def _refresh(self, now: datetime) -> None:
        try:
            active = fetch_active(self.client.http)
        except HttpError as exc:
            self.last_error = f"incentive programs: {exc}"
            log.warning(self.last_error)
            return
        self.programs = eligible(
            active,
            now,
            min_hours=self.cfg.min_program_hours,
            min_reward_per_day=self.cfg.min_reward_per_day_usd,
            exclude=self.cfg.exclude_series,
            min_time_left=timedelta(minutes=10),
            limit=self.cfg.max_markets,
        )
        self.dead_programs = (
            eligible(
                active,
                now,
                min_hours=self.cfg.min_program_hours,
                min_reward_per_day=self.cfg.min_reward_per_day_usd,
                exclude=self.cfg.dead_side_exclude_series,
                min_time_left=timedelta(minutes=10),
                limit=10 * self.cfg.max_markets,
            )
            if self.cfg.complete_dead_sides
            else []
        )
        held = set(self.ledger.lp_positions(self.mode))
        tickers = sorted({p.ticker for p in self.programs} | {p.ticker for p in self.dead_programs} | held)
        self.markets = {m.ticker: m for m in self.client.markets_by_ticker(tickers)} if tickers else {}
        self._refreshed = now

    def _tape(self, now: datetime) -> list[dict]:
        """Public trades since the last cycle, exchange-wide (a few requests instead of one per market)."""
        since = self._tape_since or (now - timedelta(seconds=self.cfg.cycle_seconds))
        ours = {o.ticker for o in self.venue.orders.values()} | {p.ticker for p in self.dead_programs}
        out: list[dict] = []
        cursor = ""
        for _ in range(MAX_TAPE_PAGES):
            params: dict = {"min_ts": int(since.timestamp()), "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            try:
                data = self.client.http.get_json("/markets/trades", params=params)
            except HttpError as exc:
                self.last_error = f"trades: {exc}"
                break
            out.extend(t for t in data.get("trades") or [] if t.get("ticker") in ours)
            cursor = data.get("cursor") or ""
            if not cursor:
                break
        self._tape_since = now
        return out

    def _observe_flow(self, now: datetime, tape: list[dict]) -> None:
        """Record, for every decided market, how much was sold into 1c dead-side bids and for how long one rested."""
        cycle = timedelta(seconds=self.cfg.cycle_seconds)
        elapsed = min(now - self._flow_at, 3 * cycle) if self._flow_at else cycle
        self._flow_at = now
        by_ticker: dict[str, list[dict]] = {}
        for t in tape:
            by_ticker.setdefault(t.get("ticker", ""), []).append(t)
        for p in self.dead_programs:
            book = self.books.get(p.ticker)
            sides = decided(book) if book is not None else None
            if sides is None:
                continue
            dead = sides[0]
            sold = sum(dead_side_sales(t, dead) for t in by_ticker.get(p.ticker, []))
            dead_levels = levels(book, dead)
            # a sale proves a 1c bid rested during the interval even if it was used up by the end
            supply = sold > 0 or any(price <= 0.01 + 1e-9 for price, _ in dead_levels)
            self.flow.observe(p.ticker, now, elapsed.total_seconds() / 60 if supply else 0.0, sold)

    def _backfill_flow(self, ticker: str, dead: str, now: datetime) -> None:
        """Load a market's last day of 1-minute candles and trades into the flow window (once per market)."""
        start = int((now - self.flow.window).timestamp())
        try:
            candles = (
                self.client.http.get_json(
                    f"/series/{series_of(ticker)}/markets/{ticker}/candlesticks",
                    params={"start_ts": start, "end_ts": int(now.timestamp()), "period_interval": 1},
                ).get("candlesticks")
                or []
            )
            trades: list[dict] = []
            cursor = ""
            for _ in range(MAX_TAPE_PAGES):
                params: dict = {"ticker": ticker, "min_ts": start, "limit": 1000}
                if cursor:
                    params["cursor"] = cursor
                data = self.client.http.get_json("/markets/trades", params=params)
                trades.extend(data.get("trades") or [])
                cursor = data.get("cursor") or ""
                if not cursor:
                    break
        except HttpError as exc:
            self.last_error = f"flow history {ticker}: {exc}"
            log.warning(self.last_error)
            return
        self.flow.backfill(ticker, dead, candles, trades)

    def _desired(self, now: datetime, held: dict[str, dict[str, list[float]]]) -> list[Quote]:
        """Quotes for every eligible market, the best reward per dollar first, until the capital budget is used."""
        stop = timedelta(minutes=self.cfg.stop_before_close_minutes)
        best: dict[tuple[str, str], tuple[float, Quote]] = {}

        def usable(p: Program) -> OrderBook | None:
            m = self.markets.get(p.ticker)
            book = self.books.get(p.ticker)
            if m is None or book is None or not m.tradable:
                return None
            if m.close_time is not None and m.close_time - now < stop:
                return None
            return book

        for p in self.programs:
            book = usable(p)
            if book is None:
                continue
            cost = {side: v[1] for side, v in held.get(p.ticker, {}).items()}
            for q in self.policy.quotes(book, cost):
                rate = reward_rate(p, book, {q.side: (q.price, q.size)})
                if rate * 86400 / q.size < self.cfg.min_reward_per_contract_day:
                    continue  # the reward would not cover what fills cost on average
                best[(q.ticker, q.side)] = (rate / max(q.price * q.size, 1e-9), q)
        backfills = 0
        for p in self.dead_programs:
            book = usable(p)
            if book is None:
                continue
            cost = {side: v[1] for side, v in held.get(p.ticker, {}).items()}
            q = dead_side_quote(book, p.target, p.discount, cost, self.cfg.dead_side_max_loss_usd)
            if q is None:
                continue
            rate = reward_rate(p, book, {q.side: (q.price, q.size)})
            if not self.flow.seeded(p.ticker) and backfills < MAX_BACKFILLS:
                backfills += 1
                self._backfill_flow(p.ticker, q.side, now)
            flow = self.flow.rate(p.ticker, now, self.cfg.dead_side_flow_prior)
            if flow * 1440 * 0.01 > self.cfg.dead_side_max_flow_ratio * rate * 86400:
                continue  # 1c sales here would cost more than the reward: such markets lost money in the replay
            score = rate / max(q.price * q.size, 1e-9)
            if rate > 0 and score > best.get((q.ticker, q.side), (0.0, q))[0]:
                best[(q.ticker, q.side)] = (score, q)
        candidates = sorted(best.values(), key=lambda c: -c[0])
        budget = self.cfg.max_total_capital_usd - sum(v[1] for sides in held.values() for v in sides.values())
        # one series (often one news event or one settlement) may not take more than its share of the capital
        series_cap = self.cfg.max_total_capital_usd * self.cfg.max_series_share
        by_series: dict[str, float] = {}
        for ticker, sides in held.items():
            key = series_of(ticker)
            by_series[key] = by_series.get(key, 0.0) + sum(v[1] for v in sides.values())
        out: list[Quote] = []
        for score, q in candidates:
            if score <= 0:
                break
            cost = q.price * q.size
            key = series_of(q.ticker)
            if cost > budget or by_series.get(key, 0.0) + cost > series_cap:
                continue
            budget -= cost
            by_series[key] = by_series.get(key, 0.0) + cost
            out.append(q)
        return out

    def _current_rates(self) -> dict[str, float]:
        rates: dict[str, float] = {}
        by_ticker: dict[str, dict[str, tuple[float, float]]] = {}
        for o in self.venue.orders.values():
            by_ticker.setdefault(o.ticker, {})[o.side] = (o.price, o.size)
        programs = {p.ticker: p for p in [*self.dead_programs, *self.programs]}
        for ticker, ours in by_ticker.items():
            p, book = programs.get(ticker), self.books.get(ticker)
            if p is not None and book is not None:
                rates[ticker] = reward_rate(p, book, ours)
        return rates

    def _accrue(self, now: datetime, end_rates: dict[str, float]) -> None:
        if self._last is not None and self._rates:
            seconds = min((now - self._last).total_seconds(), 3 * self.cfg.cycle_seconds)
            for ticker, start in self._rates.items():
                rate = (start + end_rates.get(ticker, 0.0)) / 2
                if rate > 0:
                    self.ledger.lp_add_reward(self.mode, now, ticker, rate * seconds, seconds)
        self._last = now

    def _settle(self, now: datetime, held: dict[str, dict[str, list[float]]]) -> int:
        done = 0
        for ticker in held:
            m = self.markets.get(ticker)
            payout = m.payout_yes() if m is not None else None
            if payout is None:
                continue
            pnl = self.ledger.lp_settle(self.mode, ticker, float(payout), now)
            done += 1
            self._say(
                f"Liquidity inventory settled · {ticker}", f"YES paid ${float(payout):.2f}; P&L ${pnl:+.2f}.", "info"
            )
        return done

    # ------------------------------------------------------------------------------------------------ reporting
    def _say(self, title: str, body: str, severity: str) -> None:
        self.notifier.queue(
            "research", notice_payload(f"[{self.mode}] {title}", body, severity), "lp", None, self.clock()
        )

    def _report_fills(self, fills: list[Fill]) -> None:
        lines = [f"• bought {f.qty:g} {f.side.upper()} {f.ticker} @ ${f.price:.2f}" for f in fills[:15]]
        more = f"\n… and {len(fills) - 15} more" if len(fills) > 15 else ""
        self._say(f"{len(fills)} liquidity fill(s)", "\n".join(lines) + more, "info")

    def summary(self, now: datetime, books: dict[str, OrderBook] | None = None) -> dict:
        books = books if books is not None else self.books
        day = self.ledger.lp_summary(self.mode, now - timedelta(days=1))
        week = self.ledger.lp_summary(self.mode, now - timedelta(days=7))
        total = self.ledger.lp_summary(self.mode)
        held = self.ledger.lp_positions(self.mode)
        cost = mtm = 0.0
        for ticker, sides in held.items():
            m = mid(books.get(ticker))
            for side, (q, c) in sides.items():
                cost += c
                if m is not None:
                    mtm += q * (m if side == "yes" else 1 - m)
                else:
                    mtm += c
        return {
            "day": day,
            "week": week,
            "total": total,
            "inventory_cost": cost,
            "inventory_mark": mtm,
            "by_series": self.ledger.lp_reward_by_series(self.mode, now - timedelta(days=7))[:8],
        }

    def _maybe_summary(self, now: datetime) -> None:
        every = timedelta(hours=self.cfg.summary_every_hours)
        if self._last_summary is None:
            self._last_summary = now
            return
        if now - self._last_summary < every:
            return
        self._last_summary = now
        s = self.summary(now)
        d, t = s["day"], s["total"]
        unreal = s["inventory_mark"] - s["inventory_cost"]
        net = t["reward_usd"] + t["settled_pnl"] + unreal - t["fees"]
        body = (
            f"Last 24h: rewards ${d['reward_usd']:.2f} across {d['reward_markets']:.0f} markets, "
            f"{d['fills']:.0f} fills ({d['contracts']:.0f} contracts), settled P&L ${d['settled_pnl']:+.2f}.\n"
            f"All time: rewards ${t['reward_usd']:.2f}, settled P&L ${t['settled_pnl']:+.2f}, fees ${t['fees']:.2f}.\n"
            f"Inventory: cost ${s['inventory_cost']:.2f}, marked ${s['inventory_mark']:.2f} ({unreal:+.2f}).\n"
            f"Net so far (rewards + settled + unrealized - fees): ${net:+.2f}"
        )
        if self.mode == "live":
            body += "\nRewards are estimated from the filed formula; Kalshi pays them after each period ends."
        self._say("Liquidity rewards summary", body, "info")

    # ------------------------------------------------------------------------------------------------ the loop
    def run(self, cycles: int | None = None, sleep: Callable[[float], None] = time.sleep) -> None:
        n = 0
        try:
            while cycles is None or n < cycles:
                started = time.monotonic()
                try:
                    stats = self.step()
                    log.info("lp cycle: %s", stats)
                except HttpError as exc:
                    self.last_error = str(exc)
                    log.warning("lp cycle failed: %s", exc)
                try:
                    self.notifier.flush(self.clock())
                except Exception:
                    log.exception("discord flush failed")
                n += 1
                if cycles is None or n < cycles:
                    sleep(max(0.0, self.cfg.cycle_seconds - (time.monotonic() - started)))
        finally:
            self.venue.cancel_all()
