"""What the website does, built on the same engine, fee registry and strategies as the Pi.

One engine per warm function instance (fee terms and event metadata are cached between requests); scans are
serialized, rate-limited and budgeted because Vercel Hobby includes 4 hours of active CPU a month.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import hmac
import json
import os
import queue
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from omen import __version__
from omen.config import Settings, load_settings
from omen.engine import Engine, Notice, ScanResult
from omen.fees import FeeUnavailable
from omen.http import HttpClient
from omen.kalshi.client import KalshiClient
from omen.ledger import Ledger
from omen.snapshot import play_dict
from omen.strategies.lock import basket_event_prescreen, near_misses
from omen.web.asgi import HTTPError
from omen.web.store import MemoryStore, StoreError, UpstashStore

FEED_KEY = "omen:feed"
FEED_META_KEY = "omen:feed:meta"
SCAN_KEY = "omen:scan:last"
HISTORY_KEY = "omen:history"
SCAN_LOCK_KEY = "omen:lock:scan"
LAST_SCAN_KEY = "omen:scan:last_ts"
INGEST_TS_KEY = "omen:ingest:last_ts"
HISTORY_MAX = 2016  # 7 days at one point per 5 minutes
MAX_FEED_BYTES = 950_000
ASSETS = ("BTC", "ETH", "SOL", "XRP", "DOGE")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclasses.dataclass
class ScanParams:
    horizon_hours: float | None  # None = every open market
    strategies: set[str]
    min_profit_usd: Decimal
    min_return_pct: Decimal
    min_apr_pct: Decimal
    max_capital_usd: Decimal
    near_misses: bool

    @classmethod
    def parse(cls, body: dict, defaults: Settings) -> ScanParams:
        def num(name: str, default: Decimal, lo: Decimal, hi: Decimal) -> Decimal:
            raw = body.get(name)
            if raw is None or raw == "":
                return default
            try:
                v = Decimal(str(raw))
            except InvalidOperation:
                raise HTTPError(400, f"{name} must be a number") from None
            if not lo <= v <= hi:
                raise HTTPError(400, f"{name} must be between {lo} and {hi}")
            return v

        horizon = body.get("horizon_hours", defaults.scan.hot_horizon_hours)
        if horizon in ("all", None, 0):
            horizon_hours = None
        else:
            try:
                horizon_hours = float(horizon)
            except (TypeError, ValueError):
                raise HTTPError(400, "horizon_hours must be a number or 'all'") from None
            if not 1 <= horizon_hours <= 24 * 60:
                raise HTTPError(400, "horizon_hours must be between 1 and 1440")
        allowed = {"no_basket", "ladder", "yes_partition", "cover"}
        strategies = set(body.get("strategies") or allowed)
        if not strategies <= allowed:
            raise HTTPError(400, f"unknown strategy: {sorted(strategies - allowed)}")
        lock = defaults.lock
        return cls(
            horizon_hours=horizon_hours,
            strategies=strategies,
            min_profit_usd=num("min_profit_usd", lock.min_profit_usd, Decimal(0), Decimal(10000)),
            min_return_pct=num("min_return_pct", lock.min_return_pct, Decimal(0), Decimal(100)),
            min_apr_pct=num("min_apr_pct", lock.min_apr_pct, Decimal(0), Decimal(100000)),
            max_capital_usd=num("max_capital_usd", lock.max_capital_usd, Decimal(1), Decimal(100000)),
            near_misses=bool(body.get("near_misses", True)),
        )

    def as_dict(self) -> dict:
        return {
            "horizon_hours": self.horizon_hours,
            "strategies": sorted(self.strategies),
            "min_profit_usd": float(self.min_profit_usd),
            "min_return_pct": float(self.min_return_pct),
            "min_apr_pct": float(self.min_apr_pct),
            "max_capital_usd": float(self.max_capital_usd),
            "near_misses": self.near_misses,
        }


class WebService:
    def __init__(
        self,
        store: MemoryStore | UpstashStore,
        settings: Settings | None = None,
        http_factory: Callable[[], HttpClient] | None = None,
        clock: Callable[[], datetime] = utcnow,
        feeds: Any = None,
    ):
        self.store = store
        self.settings = settings or load_settings()
        self.clock = clock
        self.http_factory = http_factory or (
            lambda: HttpClient(
                self.settings.base_url,
                rate_per_second=self.settings.scan.max_requests_per_second,
                timeout=25,
                max_retries=8,
            )
        )
        self.feeds = feeds
        self.lock = threading.Lock()  # scans and re-verify (they replace the engine's market universe)
        self.read_lock = threading.Lock()  # fee pages and the crypto view, so they never wait for a running scan
        self._engine: Engine | None = None
        self._reader: Engine | None = None
        self._crypto: Any = None
        self._crypto_cache: dict[str, tuple[float, dict]] = {}
        self.daily_scan_cap = _env_int("OMEN_WEB_DAILY_SCANS", 30)
        self.monthly_cpu_budget = _env_int("OMEN_WEB_MONTHLY_CPU_SECONDS", 3 * 3600)
        self.min_scan_gap = _env_int("OMEN_WEB_MIN_SCAN_GAP_SECONDS", 45)
        # Vercel stops a function at 300 s: page markets for at most this long, leaving time to screen and verify
        self.sweep_seconds = _env_int("OMEN_WEB_SWEEP_SECONDS", 200)

    # ------------------------------------------------------------------------------------------- shared engine
    @property
    def engine(self) -> Engine:
        if self._engine is None:
            self._engine = Engine(
                self.settings, KalshiClient(self.http_factory()), Ledger(":memory:"), clock=self.clock
            )
        return self._engine

    @property
    def reader(self) -> Engine:
        """A second engine for read-only pages (fee terms, crypto ladders): cheap, and free while a scan runs."""
        if self._reader is None:
            self._reader = Engine(
                self.settings, KalshiClient(self.http_factory()), Ledger(":memory:"), clock=self.clock
            )
        return self._reader

    @property
    def crypto(self) -> Any:
        if self._crypto is None:
            from omen.data.crypto_feeds import CryptoFeeds
            from omen.strategies.crypto import CryptoThresholdModel

            feeds = self.feeds or CryptoFeeds(HttpClient(rate_per_second=6, timeout=15))
            self._crypto = CryptoThresholdModel(
                self.settings, self.reader.client, self.reader.ledger, self.reader, feeds
            )
        return self._crypto

    # ----------------------------------------------------------------------------------------------- budget
    def _keys(self, now: datetime) -> tuple[str, str]:
        return f"omen:scans:{now:%Y-%m-%d}", f"omen:cpu:{now:%Y-%m}"

    def budget(self) -> dict:
        now = self.clock()
        day_key, cpu_key = self._keys(now)
        try:
            scans, cpu = self.store.mget([day_key, cpu_key])
        except StoreError:
            scans, cpu = None, None
        return {
            "scans_today": int(float(scans or 0)),
            "daily_scan_cap": self.daily_scan_cap,
            "cpu_seconds_month": round(float(cpu or 0), 1),
            "monthly_cpu_budget_seconds": self.monthly_cpu_budget,
            "min_scan_gap_seconds": self.min_scan_gap,
            "store": self.store.kind,
        }

    # ------------------------------------------------------------------------------------------------- scans
    def scan(self, body: dict) -> Iterator[dict]:
        params = ScanParams.parse(body, self.settings)
        now = self.clock()
        b = self.budget()
        if b["scans_today"] >= self.daily_scan_cap:
            raise HTTPError(429, f"daily scan limit reached ({self.daily_scan_cap}); resets at midnight UTC")
        if b["cpu_seconds_month"] >= self.monthly_cpu_budget:
            raise HTTPError(429, "this month's CPU budget for on-demand scans is used up (protects the free plan)")
        last = self.store.get(LAST_SCAN_KEY)
        if last and time.time() - float(last) < self.min_scan_gap:
            wait = int(self.min_scan_gap - (time.time() - float(last))) + 1
            raise HTTPError(429, f"a scan just ran; try again in {wait}s", retry_after=wait)
        if not self.store.set(SCAN_LOCK_KEY, str(time.time()), ex=290, nx=True):
            raise HTTPError(409, "another scan is running")
        self.store.set(LAST_SCAN_KEY, str(time.time()), ex=3600)
        events: queue.Queue[dict | None] = queue.Queue()
        threading.Thread(target=self._scan_worker, args=(params, now, events), daemon=True).start()

        def stream() -> Iterator[dict]:
            yield {"type": "start", "params": params.as_dict(), "at": now.isoformat()}
            while True:
                ev = events.get()
                if ev is None:
                    return
                yield ev

        return stream()

    def _scan_worker(self, params: ScanParams, now: datetime, events: queue.Queue) -> None:
        cpu0, t0 = time.process_time(), time.monotonic()
        try:
            with self.lock:
                result = self._run_scan(params, now, events.put)
            cpu = time.process_time() - cpu0
            result["stats"]["cpu_seconds"] = round(cpu, 2)
            result["stats"]["seconds"] = round(time.monotonic() - t0, 1)
            day_key, cpu_key = self._keys(now)
            try:
                self.store.incr(day_key, 1, ex=2 * 86400)
                self.store.incr(cpu_key, cpu, ex=40 * 86400)
                self.store.set(SCAN_KEY, json.dumps(result, default=str))
                self.store.lpush_trim(
                    HISTORY_KEY,
                    json.dumps(
                        {
                            "t": now.isoformat(timespec="seconds"),
                            "source": "scan",
                            "plays": len(result["plays"]),
                            "best_profit": max((p["profit"] for p in result["plays"]), default=0),
                            "markets": result["stats"]["markets"],
                        }
                    ),
                    HISTORY_MAX,
                )
            except StoreError as exc:
                result["notices"].append({"severity": "warning", "title": "Result not saved", "body": str(exc)})
            events.put({"type": "result", "result": result})
        except Exception as exc:
            events.put({"type": "error", "message": f"{type(exc).__name__}: {exc}"[:500]})
        finally:
            with contextlib.suppress(StoreError):
                self.store.delete(SCAN_LOCK_KEY)
            events.put(None)

    def _run_scan(self, params: ScanParams, now: datetime, emit: Callable[[dict], None]) -> dict:
        eng = self.engine
        original = eng.s.lock
        eng.s.lock = dataclasses.replace(
            original,
            min_profit_usd=params.min_profit_usd,
            min_return_pct=params.min_return_pct,
            min_apr_pct=params.min_apr_pct,
            max_capital_usd=params.max_capital_usd,
            enable_no_basket="no_basket" in params.strategies,
            enable_ladder="ladder" in params.strategies,
            enable_yes_partition="yes_partition" in params.strategies,
            enable_cover="cover" in params.strategies,
        )
        try:
            deadline = time.monotonic() + self.sweep_seconds
            req0 = eng.client.http.requests_made
            result = ScanResult(kind="web", started=now)
            emit({"type": "stage", "stage": "fees", "label": "Loading live fee terms"})
            result.notices += eng.refresh_fees(now)
            emit({"type": "stage", "stage": "fees_done", "series": len(eng.fees.series)})
            trading = eng._trading_active(now, result)
            emit({"type": "stage", "stage": "markets", "label": "Fetching markets"})
            eng.markets = {}  # a web scan covers exactly the requested universe
            kind = "full" if params.horizon_hours is None else "hot"
            markets = eng.sweep(
                kind,
                now,
                params.horizon_hours,
                progress=lambda n: emit({"type": "progress", "stage": "markets", "count": n}),
                deadline=deadline,
            )
            if eng.sweep_partial:
                result.notices.append(
                    Notice(
                        "scan",
                        "Partial scan",
                        f"Kalshi was slow to page through markets, so this scan stopped after {markets:,} markets to "
                        "stay inside the website's time limit. Narrow the horizon for a complete pass (the Pi always "
                        "scans everything).",
                        "warning",
                    )
                )
            emit({"type": "stage", "stage": "screen", "label": "Screening structures", "markets": markets})
            if params.near_misses:
                eng.ensure_events(basket_event_prescreen(eng.by_event(), eng.fee_for, Decimal("-0.03")), now)
            candidates, rejected = eng.screen(now) if trading else ([], [])
            emit(
                {
                    "type": "stage",
                    "stage": "verify",
                    "label": "Verifying on full order books",
                    "candidates": len(candidates),
                }
            )
            opps, failed = eng.verify_report(candidates, self.clock())
            nm = []
            if params.near_misses:
                nm = near_misses(
                    eng.events,
                    eng.by_event(),
                    list(eng.markets.values()),
                    eng.fee_for,
                    allow_step_gaps=eng.s.lock.allow_step_gap_partitions,
                )
            limit = eng.s.lock.max_plausible_return_pct
            return {
                "id": hashlib.sha256(now.isoformat().encode()).hexdigest()[:10],
                "at": now.isoformat(timespec="seconds"),
                "params": params.as_dict(),
                "trading_active": trading,
                "plays": [play_dict(o, "scan", limit) for o in opps],
                "failed": [
                    {
                        "strategy": c.strategy,
                        "title": c.title,
                        "event_ticker": c.event.event_ticker,
                        "top_edge": float(c.top_edge),
                        "reason": why,
                    }
                    for c, why in failed[:20]
                ],
                "near_misses": nm,
                "rejected": [{"event_ticker": t, "reason": r} for t, r in rejected[:20]],
                "notices": [{"severity": n.severity, "title": n.title, "body": n.body} for n in result.notices],
                "stats": {
                    "markets": markets,
                    "partial": eng.sweep_partial,
                    "candidates": len(candidates),
                    "verified": len(opps),
                    "requests": eng.client.http.requests_made - req0,
                },
            }
        finally:
            eng.s.lock = original

    # ---------------------------------------------------------------------------------------------- re-verify
    def reverify(self, body: dict) -> dict:
        key = str(body.get("key") or "")
        strategy, _, legs = key.partition("|")
        tickers = [leg.rsplit(":", 1)[0] for leg in legs.split(",") if leg]
        if strategy not in {"no_basket", "ladder", "yes_partition", "cover"} or not tickers or len(tickers) > 60:
            raise HTTPError(400, "re-verify works for LOCK plays (no_basket, ladder, yes_partition, cover)")
        now = self.clock()
        with self.lock:
            eng = self.engine
            eng.refresh_fees(now)
            saved = eng.markets
            try:
                fresh = eng.client.markets_by_ticker(tickers)
                events = {m.event_ticker for m in fresh}
                if strategy == "yes_partition":
                    fresh = [m for ev in events for m in eng.client.iter_markets(event_ticker=ev)]
                eng.markets = {m.ticker: m for m in fresh if m.tradable}
                eng.ensure_events(events, now)
                candidates, _ = eng.screen(now)
                candidates = [c for c in candidates if c.strategy == strategy]
                opps, failed = eng.verify_report(candidates, self.clock())
            finally:
                eng.markets = saved
        same = [o for o in opps if o.key == key] or [
            o for o in opps if {lp.market.ticker for lp in o.legs} == set(tickers)
        ]
        if same:
            return {
                "status": "live",
                "play": play_dict(same[0], "reverify", eng.s.lock.max_plausible_return_pct),
                "at": now.isoformat(timespec="seconds"),
            }
        reason = failed[0][1] if failed else "the prices no longer form this play"
        return {"status": "gone", "reason": reason, "at": now.isoformat(timespec="seconds")}

    # ---------------------------------------------------------------------------------------------- dashboard
    def dashboard(self) -> dict:
        try:
            feed_raw, meta_raw, scan_raw = self.store.mget([FEED_KEY, FEED_META_KEY, SCAN_KEY])
            store_ok = True
        except StoreError:
            feed_raw = meta_raw = scan_raw = None
            store_ok = False
        feed = json.loads(feed_raw) if feed_raw else None
        meta = json.loads(meta_raw) if meta_raw else None
        last_scan = json.loads(scan_raw) if scan_raw else None
        plays: dict[str, dict] = {}
        for p in (last_scan or {}).get("plays", []):
            plays[p["key"]] = p
        for p in (feed or {}).get("plays", []):
            prior = plays.get(p["key"])
            if prior is None or (p.get("found_at") or "") >= (prior.get("found_at") or ""):
                plays[p["key"]] = p
        ordered = sorted(plays.values(), key=lambda p: (p.get("suspicious", False), -(p.get("profit") or 0)))
        now = self.clock()
        feed_age = None
        if meta and meta.get("received_at"):
            feed_age = (now - datetime.fromisoformat(meta["received_at"])).total_seconds()
        return {
            "server_time": now.isoformat(timespec="seconds"),
            "version": __version__,
            "plays": ordered,
            "feed": feed,
            "feed_meta": meta,
            "feed_age_seconds": feed_age,
            "last_scan": last_scan,
            "budget": self.budget(),
            "store_ok": store_ok,
        }

    def history(self) -> list[dict]:
        try:
            rows = self.store.lrange(HISTORY_KEY, 0, HISTORY_MAX - 1)
        except StoreError:
            return []
        out = []
        for r in rows:
            try:
                out.append(json.loads(r))
            except (TypeError, json.JSONDecodeError):
                continue
        return list(reversed(out))

    # --------------------------------------------------------------------------------------------------- fees
    def fees(self) -> dict:
        now = self.clock()
        with self.read_lock:
            notices = self.reader.refresh_fees(now)
            eng = self.reader
            counts: dict[tuple[str, str], int] = {}
            nonstandard = []
            for s in eng.fees.series.values():
                mult = f"{s.fee_multiplier.normalize():f}"
                counts[(s.fee_type, mult)] = counts.get((s.fee_type, mult), 0) + 1
                if s.fee_type != "quadratic" or s.fee_multiplier != 1:
                    nonstandard.append(
                        {
                            "ticker": s.ticker,
                            "title": s.title,
                            "category": s.category,
                            "fee_type": s.fee_type,
                            "multiplier": float(s.fee_multiplier),
                        }
                    )
            upcoming = [
                {
                    "scope": c.scope,
                    "ticker": c.ticker,
                    "series_ticker": c.series_ticker,
                    "fee_type": c.fee_type,
                    "multiplier": None if c.multiplier is None else float(c.multiplier),
                    "scheduled": c.scheduled.isoformat(),
                    "describe": c.describe(),
                }
                for c in eng.fees.upcoming_changes(now, timedelta(days=30))
            ]
            sch = eng.schedule
        return {
            "schedule": {
                "version": sch.version,
                "verified_on": sch.verified_on.isoformat(),
                "source_url": sch.source_url,
                "rounding_increment": float(sch.rounding_increment),
                "formulas": {
                    name: {"taker_rate": float(f.taker_rate), "maker_rate": float(f.maker_rate), "flat": f.is_flat}
                    for name, f in sch.formulas.items()
                },
            },
            "terms": [
                {"fee_type": ft, "multiplier": float(m), "series": n}
                for (ft, m), n in sorted(counts.items(), key=lambda kv: -kv[1])
            ],
            "series_count": len(eng.fees.series),
            "nonstandard": sorted(nonstandard, key=lambda r: (r["fee_type"], r["multiplier"], r["ticker"])),
            "upcoming": upcoming,
            "unknown_fee_types": sorted(eng.fees.unknown_fee_types()),
            "notices": [{"severity": n.severity, "title": n.title, "body": n.body} for n in notices],
            "as_of": now.isoformat(timespec="seconds"),
        }

    def quote(self, body: dict) -> dict:
        series = str(body.get("series") or "").strip().upper()
        event = str(body.get("event") or "").strip().upper() or None
        try:
            price = Decimal(str(body.get("price", "0.5")))
            count = Decimal(str(body.get("count", "100")))
        except InvalidOperation:
            raise HTTPError(400, "price and count must be numbers") from None
        if not Decimal("0") < price < Decimal("1"):
            raise HTTPError(400, "price must be between 0 and 1 dollars (e.g. 0.37)")
        if not Decimal("0.01") <= count <= Decimal("1000000"):
            raise HTTPError(400, "count must be between 0.01 and 1,000,000")
        now = self.clock()
        with self.read_lock:
            self.reader.refresh_fees(now)
            try:
                q = self.reader.fees.quote(series, event, now)
            except FeeUnavailable as exc:
                raise HTTPError(404, str(exc)) from None
        raw = q.multiplier * q.formula.taker_rate * count * price * (1 - price)
        return {
            "series": series,
            "event": event,
            "price": float(price),
            "count": float(count),
            "fee_type": q.fee_type,
            "multiplier": float(q.multiplier),
            "source": q.source,
            "taker_rate": float(q.formula.taker_rate),
            "maker_rate": float(q.formula.maker_rate),
            "taker_fee": float(q.taker_fee(price, count)),
            "maker_fee": float(q.maker_fee(price, count)),
            "taker_fee_unrounded": float(raw),
            "rounding_increment": float(q.rounding_increment),
            "cost": float(price * count),
            "describe": q.describe(),
        }

    def series_search(self, q: str) -> list[dict]:
        q = q.strip().lower()
        if len(q) < 2:
            return []
        with self.read_lock:
            self.reader.refresh_fees(self.clock())
            rows = self.reader.fees.series.values()
            hits = [s for s in rows if q in s.ticker.lower() or q in s.title.lower()]
        hits.sort(key=lambda s: (not s.ticker.lower().startswith(q), len(s.ticker)))
        return [
            {
                "ticker": s.ticker,
                "title": s.title,
                "category": s.category,
                "fee_type": s.fee_type,
                "multiplier": float(s.fee_multiplier),
            }
            for s in hits[:20]
        ]

    # ------------------------------------------------------------------------------------------------- crypto
    def crypto_view(self, asset: str) -> dict:
        asset = asset.upper()
        if asset not in set(self.settings.crypto.series.values()):
            raise HTTPError(400, f"asset must be one of {sorted(set(self.settings.crypto.series.values()))}")
        hit = self._crypto_cache.get(asset)
        if hit and time.monotonic() - hit[0] < 20:
            return hit[1]
        with self.read_lock:
            view = self.crypto.ladder_view(asset, self.clock())
        self._crypto_cache[asset] = (time.monotonic(), view)
        return view

    # ------------------------------------------------------------------------------------------------- ingest
    def ingest(self, body: bytes, headers: dict[str, str]) -> dict:
        secret = os.environ.get("OMEN_INGEST_SECRET", "")
        if len(secret) < 16:
            raise HTTPError(503, "ingest disabled: set OMEN_INGEST_SECRET (16+ characters) here and on the Pi")
        try:
            ts = int(headers.get("x-omen-ts", ""))
        except ValueError:
            raise HTTPError(401, "missing timestamp") from None
        if abs(time.time() - ts) > 300:
            raise HTTPError(401, "stale or future timestamp")
        expected = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, headers.get("x-omen-sig", "")):
            raise HTTPError(401, "bad signature")
        last = self.store.get(INGEST_TS_KEY)
        if last and ts <= int(last):
            raise HTTPError(409, "replayed or out-of-order snapshot")
        if len(body) > MAX_FEED_BYTES:
            raise HTTPError(413, "snapshot too large")
        try:
            feed = json.loads(body)
        except json.JSONDecodeError:
            raise HTTPError(400, "snapshot is not JSON") from None
        if feed.get("version") != 1 or not isinstance(feed.get("plays"), list):
            raise HTTPError(400, "unsupported snapshot version")
        now = self.clock()
        meta = {
            "received_at": now.isoformat(timespec="seconds"),
            "bytes": len(body),
            "host": feed.get("host"),
            "omen_version": feed.get("omen_version"),
        }
        self.store.set(FEED_KEY, body.decode())
        self.store.set(FEED_META_KEY, json.dumps(meta))
        self.store.set(INGEST_TS_KEY, str(ts))
        paper = feed.get("paper", {}).get("totals", [])
        settled = sum((r.get("pnl") or 0) for r in paper if r.get("status") == "settled")
        self.store.lpush_trim(
            HISTORY_KEY,
            json.dumps(
                {
                    "t": now.isoformat(timespec="seconds"),
                    "source": "pi",
                    "plays": len(feed["plays"]),
                    "best_profit": max((p.get("profit") or 0 for p in feed["plays"]), default=0),
                    "paper_pnl": round(float(settled), 2),
                }
            ),
            HISTORY_MAX,
        )
        return {"ok": True, "stored_bytes": len(body)}
