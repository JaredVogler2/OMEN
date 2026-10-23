"""The scanning service: tiered schedule, alert decisions, paper ledger, settlement, digest, health pings."""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import signal
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import httpx

from omen.alerts.discord import Notifier
from omen.alerts.render import digest_payload, gone_payload, notice_payload, play_payload, superseded_payload
from omen.config import Settings
from omen.engine import Engine, Notice, ScanResult
from omen.ledger import Ledger, from_iso, iso
from omen.money import usd
from omen.paper import open_position, resolve_predictions, settle_positions
from omen.publish import Publisher
from omen.snapshot import play_dict
from omen.strategies.base import LOCK, Opportunity
from omen.watchdog import check_fee_document

log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")
LOCK_STRATEGIES = {"no_basket", "ladder", "yes_partition"}


class ModelStrategy(Protocol):
    name: str

    def run(self, now: datetime) -> tuple[list[Opportunity], set[str]]: ...

    def status(self) -> dict[str, Any]: ...


class Executor(Protocol):
    def consider(self, opp: Opportunity, now: datetime) -> None: ...


@dataclass
class CycleReport:
    kind: str
    opportunities: int = 0
    alerts: int = 0
    gone: int = 0
    notices: list[Notice] = field(default_factory=list)


def tickers_of(key: str) -> set[str]:
    _, _, legs = key.partition("|")
    return {leg.rsplit(":", 1)[0] for leg in legs.split(",") if leg}


class Runner:
    def __init__(
        self,
        settings: Settings,
        engine: Engine,
        ledger: Ledger,
        notifier: Notifier,
        models: list[ModelStrategy] | None = None,
        executor: Executor | None = None,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        fee_doc_transport: httpx.BaseTransport | None = None,
        publisher: Publisher | None = None,
    ):
        self.s = settings
        self.engine = engine
        self.ledger = ledger
        self.notifier = notifier
        self.models = models or []
        self.executor = executor
        self.clock = clock or (lambda: datetime.now(UTC))
        self.sleep = sleep
        self.stopping = False
        self.fee_doc_transport = fee_doc_transport
        self.publisher = publisher
        self._last_error_notice: dict[str, datetime] = {}

    # ------------------------------------------------------------------------------------------------ routing
    def _route(self, opp: Opportunity) -> tuple[str | None, str | None]:
        if opp.tier == LOCK:
            limit = self.s.lock.max_plausible_return_pct
            if opp.return_pct > limit:
                # Real structural mispricings of this size are rare; a data or rules inconsistency is likelier.
                return "status", (
                    f"⚠️ SUSPICIOUS · {opp.return_pct:.1f}% return is above the {limit}% plausibility limit. Not posted "
                    "to #kalshi-plays, not paper-traded, never executed. Read every leg's rules before trusting it."
                )
            return "plays", None
        state = self.ledger.model_state(opp.strategy)
        if state == "live":
            return "plays", "VALIDATED · promoted by the owner"
        return "research", "SHADOW · unvalidated, not a recommendation; logged to build its track record"

    def _should_alert(self, row: Any, opp: Opportunity, now: datetime) -> bool:
        if row["alerted_at"] is None:
            return True
        alerted = from_iso(row["alerted_at"])
        if alerted is None or now - alerted < timedelta(minutes=self.s.alerts.cooldown_minutes):
            return False
        before = Decimal(row["alert_profit"] or "0")
        return opp.profit >= before * (1 + self.s.alerts.improvement_pct / 100)

    # ---------------------------------------------------------------------------------------------- handling
    def handle(
        self, opportunities: list[Opportunity], scope: set[str], strategies: set[str], now: datetime, kind: str
    ) -> CycleReport:
        """Record, route and alert this cycle's plays; mark as gone the earlier plays of ``strategies`` whose markets
        were all re-checked (``scope``) and no longer qualify."""
        report = CycleReport(kind, opportunities=len(opportunities))
        live: set[str] = set()
        for opp in sorted(opportunities, key=lambda o: o.profit, reverse=True):
            live.add(opp.key)
            prior = self.ledger.db.execute("SELECT gone_at FROM opportunities WHERE key=?", (opp.key,)).fetchone()
            if prior is not None and prior["gone_at"]:
                gone_for = now - (from_iso(prior["gone_at"]) or now)
                if gone_for >= timedelta(minutes=self.s.alerts.cooldown_minutes):
                    self.ledger.db.execute(
                        "UPDATE opportunities SET alerted_at=NULL, alert_profit=NULL, alert_message_id=NULL "
                        "WHERE key=?",
                        (opp.key,),
                    )
            row = self.ledger.observe(
                opp.key,
                opp.short_id,
                opp.strategy,
                opp.tier,
                opp.event_ticker,
                opp.title,
                opp.profit,
                _payload(opp, self.s.lock.max_plausible_return_pct),
                now,
            )
            channel, state = self._route(opp)
            if channel is None or not self._should_alert(row, opp, now):
                continue
            if report.alerts >= self.s.alerts.max_alerts_per_cycle:
                continue
            if (
                channel == "research"
                and self.notifier.recent_count("research", "play", now - timedelta(hours=1))
                >= self.s.alerts.max_research_alerts_per_hour
            ):
                continue
            if row["alert_message_id"] and row["alert_channel"]:
                self.notifier.queue(
                    row["alert_channel"],
                    superseded_payload(opp.title, opp.short_id),
                    "superseded",
                    opp.key,
                    now,
                    edit_of=row["alert_message_id"],
                )
            self.notifier.queue(channel, play_payload(opp, now, state), "play", opp.key, now)
            self.ledger.mark_alerted(opp.key, opp.profit, channel, now)
            report.alerts += 1
            if channel == "plays":
                if self.s.paper.enabled:
                    open_position(self.ledger, opp, now)
                if self.executor is not None:
                    try:
                        self.executor.consider(opp, now)
                    except Exception as exc:  # execution problems must never stop scanning
                        log.exception("executor failed")
                        report.notices.append(Notice("execution", "Execution error", str(exc)[:1500], "error"))
        for row in self.ledger.live_opportunities():
            if row["key"] in live or row["strategy"] not in strategies:
                continue
            # gone if every market was re-checked and it no longer qualifies, or if a leg has stopped trading
            # (closed markets drop out of the sweeps, so they would never be re-checked)
            if not (tickers_of(row["key"]) <= scope or _closed(row, now)):
                continue
            self.ledger.mark_gone(row["key"], now)
            report.gone += 1
            if row["alerted_at"] and row["alert_message_id"] and self.s.alerts.post_gone_updates:
                payload = gone_payload(
                    row["title"] or "",
                    row["short_id"],
                    from_iso(row["first_seen"]) or now,
                    now,
                    Decimal(row["best_profit"] or "0"),
                )
                self.notifier.queue(
                    row["alert_channel"] or "plays", payload, "gone", row["key"], now, edit_of=row["alert_message_id"]
                )
        return report

    def notify(self, notices: list[Notice], now: datetime) -> None:
        for n in notices:
            self.notifier.queue("status", notice_payload(n.title, n.body, n.severity), f"notice:{n.kind}", None, now)

    # -------------------------------------------------------------------------------------------------- cycles
    def scan_cycle(self, kind: str) -> CycleReport:
        now = self.clock()
        run_id = self.ledger.start_run(kind, now)
        try:
            result: ScanResult = self.engine.scan(kind)
            report = self.handle(result.opportunities, self.engine.scanned, LOCK_STRATEGIES, self.clock(), kind)
            report.notices = result.notices + report.notices
            self.notify(report.notices, self.clock())
            self.ledger.finish_run(
                run_id,
                self.clock(),
                markets=result.markets,
                candidates=result.candidates,
                opportunities=len(result.opportunities),
                alerts=report.alerts,
                requests=result.requests,
            )
            return report
        except Exception as exc:
            self.ledger.finish_run(run_id, self.clock(), error=str(exc)[:500])
            raise

    def model_cycle(self) -> CycleReport:
        report = CycleReport("model")
        for model in self.models:
            now = self.clock()
            run_id = self.ledger.start_run(f"model:{model.name}", now)
            try:
                opps, scope = model.run(now)
            except Exception as exc:
                self.ledger.finish_run(run_id, self.clock(), error=str(exc)[:500])
                raise
            r = self.handle(opps, scope, {model.name}, self.clock(), report.kind)
            self.ledger.finish_run(run_id, self.clock(), opportunities=len(opps), alerts=r.alerts)
            report.opportunities += r.opportunities
            report.alerts += r.alerts
            report.notices += r.notices
        self.notify(report.notices, self.clock())
        return report

    def settle_cycle(self) -> None:
        now = self.clock()
        settled = settle_positions(self.ledger, self.engine.client, now)
        for row in settled:
            if row["tier"] == LOCK and row["pnl"] < row["expected"] - Decimal("0.01"):
                self.notify(
                    [
                        Notice(
                            "lock_shortfall",
                            f"LOCK play {row['short_id']} settled below its guarantee",
                            f"Expected at least {usd(row['expected'])}, realised {usd(row['pnl'])}. Investigate before "
                            "trusting this strategy again (rules, settlement value, fees).",
                            "error",
                        )
                    ],
                    now,
                )
        resolve_predictions(self.ledger, self.engine.client, now)

    def flush(self) -> None:
        if self.notifier.configured:
            self.notifier.flush(self.clock())

    def publish(self) -> None:
        """Push the live feed to the website (if configured). Best-effort: never interrupts scanning."""
        if self.publisher is None:
            return
        try:
            self.publisher.maybe_publish(self.ledger, self.engine, self.models, self.s, self.clock())
        except Exception:
            log.exception("website publish failed")

    # --------------------------------------------------------------------------------------------------- digest
    def maybe_digest(self, force: bool = False) -> bool:
        now = self.clock()
        local = now.astimezone(ET)
        if not force and (
            local.hour < self.s.alerts.digest_hour_et
            or self.ledger.get_meta("digest_sent_on") == local.date().isoformat()
        ):
            return False
        self.ledger.set_meta("digest_sent_on", local.date().isoformat())
        lines, fields = build_digest(self.ledger, self.models, self.engine, now)
        self.notifier.queue("status", digest_payload(lines, fields), "digest", None, now)
        return True

    # ----------------------------------------------------------------------------------------------- service
    def ping(self, ok: bool) -> None:
        url = self.s.healthcheck_url
        if not url:
            return
        with contextlib.suppress(httpx.HTTPError):
            httpx.get(url if ok else url.rstrip("/") + "/fail", timeout=10)

    def _error(self, where: str, exc: Exception) -> None:
        log.exception("%s failed", where)
        now = self.clock()
        last = self._last_error_notice.get(where)
        if last is None or now - last > timedelta(hours=1):
            self._last_error_notice[where] = now
            self.notify([Notice("error", f"OMEN {where} failed", f"{type(exc).__name__}: {exc}"[:1500], "error")], now)
        self.ping(False)

    def run_forever(self) -> None:
        s = self.s.scan
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: setattr(self, "stopping", True))
        due = {"full": 0.0, "hot": 0.0, "model": 0.0, "settle": 0.0, "fee_doc": 0.0}
        self.notify(
            [
                Notice(
                    "startup",
                    "OMEN scanner started",
                    f"hot sweep every {s.hot_interval_seconds}s (markets closing within "
                    f"{s.hot_horizon_hours}h), full sweep every {s.full_interval_seconds}s, models every "
                    f"{s.model_interval_seconds}s. Execution mode: {self.s.execution.mode}.",
                )
            ],
            self.clock(),
        )
        self.flush()
        while not self.stopping:
            mono = time.monotonic()
            try:
                if mono >= due["full"]:
                    self.scan_cycle("full")
                    due["full"] = mono + s.full_interval_seconds
                    due["hot"] = mono + s.hot_interval_seconds
                elif mono >= due["hot"]:
                    self.scan_cycle("hot")
                    due["hot"] = mono + s.hot_interval_seconds
                self.ping(True)
            except Exception as exc:
                self._error("scan", exc)
                due["hot"] = mono + min(s.hot_interval_seconds, 120)
                due["full"] = max(due["full"], mono + 120)
            if self.models and time.monotonic() >= due["model"]:
                try:
                    self.model_cycle()
                except Exception as exc:
                    self._error("model", exc)
                due["model"] = time.monotonic() + s.model_interval_seconds
            if time.monotonic() >= due["settle"]:
                try:
                    self.settle_cycle()
                except Exception as exc:
                    self._error("settlement", exc)
                due["settle"] = time.monotonic() + s.settle_interval_seconds
            if time.monotonic() >= due["fee_doc"]:
                try:
                    self.notify(
                        check_fee_document(
                            self.ledger,
                            self.engine.schedule,
                            self.s.fees.verify_reminder_days,
                            self.clock(),
                            self.fee_doc_transport,
                        ),
                        self.clock(),
                    )
                except Exception as exc:
                    self._error("fee document check", exc)
                due["fee_doc"] = time.monotonic() + 3600
            try:
                self.maybe_digest()
            except Exception as exc:
                self._error("digest", exc)
            self.flush()
            self.publish()
            next_due = min(due.values()) if self.models else min(due["hot"], due["full"], due["settle"])
            while not self.stopping and time.monotonic() < next_due:
                self.sleep(min(1.0, max(0.05, next_due - time.monotonic())))
        log.info("stopped")

    def run_once(self, kind: str = "full") -> CycleReport:
        report = self.scan_cycle(kind)
        if self.models:
            m = self.model_cycle()
            report.alerts += m.alerts
            report.opportunities += m.opportunities
        self.settle_cycle()
        self.notify(
            check_fee_document(
                self.ledger,
                self.engine.schedule,
                self.s.fees.verify_reminder_days,
                self.clock(),
                self.fee_doc_transport,
            ),
            self.clock(),
        )
        self.maybe_digest()
        self.flush()
        self.publish()
        self.ping(True)
        return report


def _closed(row: Any, now: datetime) -> bool:
    """Has any leg of this recorded play reached its close time?"""
    try:
        legs = json.loads(row["last_payload"] or "{}").get("legs") or []
        closes = [from_iso(leg["close_time"]) for leg in legs if leg.get("close_time")]
    except (TypeError, ValueError, AttributeError):
        return False
    return any(c is not None and c <= now for c in closes)


def _payload(opp: Opportunity, max_plausible_return_pct: Decimal | None = None) -> dict:
    return play_dict(opp, "pi", max_plausible_return_pct)


def build_digest(
    ledger: Ledger, models: list[ModelStrategy], engine: Engine, now: datetime
) -> tuple[list[str], list[dict]]:
    since = iso(now - timedelta(hours=24))
    runs = ledger.db.execute(
        "SELECT COUNT(*) n, SUM(error IS NOT NULL) errors, AVG(markets) markets, SUM(requests) requests, "
        "SUM(alerts) alerts FROM runs WHERE started_at >= ?",
        (since,),
    ).fetchone()
    opps = ledger.db.execute(
        "SELECT strategy, tier, COUNT(*) n, SUM(alerted_at IS NOT NULL) alerted FROM opportunities "
        "WHERE last_seen >= ? GROUP BY strategy, tier",
        (since,),
    ).fetchall()
    lines = [
        f"**Last 24h:** {runs['n'] or 0} runs ({runs['errors'] or 0} failed), ~{int(runs['markets'] or 0):,} markets "
        f"per scan, {int(runs['requests'] or 0):,} API requests, {int(runs['alerts'] or 0)} alerts.",
    ]
    if opps:
        lines.append(
            "**Opportunities seen:** "
            + ", ".join(f"{r['strategy']} ({r['tier']}): {r['n']} seen / {r['alerted'] or 0} alerted" for r in opps)
        )
    else:
        lines.append("**Opportunities seen:** none cleared every threshold (normal on efficient days).")
    fields = []
    paper = ledger.db.execute(
        "SELECT status, COUNT(*) n, SUM(CAST(capital AS REAL)) cap, SUM(CAST(pnl AS REAL)) pnl, "
        "SUM(CAST(expected_profit AS REAL)) exp FROM paper_positions GROUP BY status"
    ).fetchall()
    if paper:
        txt = []
        for r in paper:
            if r["status"] == "open":
                txt.append(
                    f"Open: {r['n']} plays, {usd(Decimal(str(r['cap'] or 0)))} capital, "
                    f"{usd(Decimal(str(r['exp'] or 0)))} expected"
                )
            else:
                txt.append(
                    f"Settled: {r['n']} plays, P&L {usd(Decimal(str(r['pnl'] or 0)))} vs expected "
                    f"{usd(Decimal(str(r['exp'] or 0)))}"
                )
        fields.append({"name": "Paper ledger (all time)", "value": "\n".join(txt)})
    for model in models:
        st = model.status()
        fields.append(
            {
                "name": f"Model · {model.name} [{st.get('state', 'shadow').upper()}]",
                "value": st.get("summary", "no data yet"),
            }
        )
    fee_rows = ledger.db.execute(
        "SELECT kind, ticker, detail FROM fee_log WHERE ts >= ? AND kind != 'baseline' ORDER BY id DESC LIMIT 10",
        (since,),
    ).fetchall()
    upcoming = engine.fees.upcoming_changes(now, timedelta(days=7))
    fee_lines = [f"{r['detail']}" for r in fee_rows] + [f"Upcoming: {c.describe()}" for c in upcoming[:10]]
    fields.append(
        {
            "name": "Fees",
            "value": "\n".join(fee_lines)
            or f"No changes. Schedule {engine.schedule.version}, formula verified {engine.schedule.verified_on}.",
        }
    )
    ob = ledger.outbox_stats()
    fields.append({"name": "Delivery", "value": f"Discord outbox: {ob['pending']} pending, {ob['dead']} dead"})
    return lines, fields


class InstanceLock:
    """Exclusive lock so two scanners never share one ledger."""

    def __init__(self, path: Path):
        self.path = path
        self.fh = None

    def __enter__(self) -> InstanceLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(self.path, "w")
        try:
            fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(f"another OMEN process holds {self.path}", file=sys.stderr)
            raise SystemExit(3) from None
        return self

    def __exit__(self, *exc: object) -> None:
        if self.fh:
            fcntl.flock(self.fh, fcntl.LOCK_UN)
            self.fh.close()
