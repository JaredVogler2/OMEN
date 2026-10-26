"""``omen`` command line."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx

from omen import __version__
from omen.alerts.discord import DiscordConfigError, DiscordTransport, Notifier, build_routes, resolve
from omen.alerts.render import notice_payload, play_text
from omen.config import ConfigError, Settings, load_settings, read_env_file
from omen.engine import Engine
from omen.fees import FeeUnavailable
from omen.http import HttpClient
from omen.kalshi.client import KalshiClient
from omen.ledger import Ledger
from omen.money import usd
from omen.publish import Publisher
from omen.runner import InstanceLock, Runner, build_digest

log = logging.getLogger("omen")


def _setup_logging(verbose: bool) -> None:
    level = os.environ.get("OMEN_LOG_LEVEL", "DEBUG" if verbose else "INFO").upper()
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def _client(s: Settings) -> KalshiClient:
    http = HttpClient(
        s.base_url, rate_per_second=s.scan.max_requests_per_second, timeout=s.scan.request_timeout_seconds
    )
    return KalshiClient(http)


def _notifier(
    s: Settings, ledger: Ledger, dry_run: bool = False, timeout: float = 20.0, max_wait: float = 30.0
) -> Notifier:
    routes = build_routes(s.discord_webhooks, s.discord_bot_token, s.discord_channel_map)
    transport = DiscordTransport(s.discord_bot_token, timeout=timeout, max_wait=max_wait) if routes else None
    return Notifier(ledger, routes, transport, dry_run=dry_run)


def _models(s: Settings, client: KalshiClient, ledger: Ledger, engine: Engine) -> list:
    models: list = []
    if s.crypto.enabled:
        from omen.strategies.crypto import CryptoThresholdModel

        models.append(CryptoThresholdModel(s, client, ledger, engine))
    if s.edge.enabled:
        from omen.strategies.edge import CalibrationEdgeModel

        models.append(CalibrationEdgeModel(s, client, ledger, engine))  # dormant without a validated atlas
    return models


def _executor(s: Settings, ledger: Ledger, notifier: Notifier):
    if s.execution.mode == "off":
        return None
    from omen.execution import build_executor

    return build_executor(s, ledger, notifier)


def build(s: Settings, *, dry_run: bool = False) -> tuple[Runner, Ledger]:
    db = s.database if not dry_run else s.state_dir / "omen-dryrun.sqlite3"
    ledger = Ledger(db)
    client = _client(s)
    engine = Engine(s, client, ledger)
    notifier = _notifier(s, ledger, dry_run=dry_run)
    if dry_run:
        s.paper.enabled = False
    models = _models(s, client, ledger, engine)
    executor = None if dry_run else _executor(s, ledger, notifier)
    publisher = None if dry_run else Publisher.from_settings(s)
    return Runner(s, engine, ledger, notifier, models=models, executor=executor, publisher=publisher), ledger


# ---------------------------------------------------------------------------------------------------- commands
def cmd_run(s: Settings, args: argparse.Namespace) -> int:
    with InstanceLock(s.state_dir / "omen.lock"):
        runner, _ = build(s)
        if not runner.notifier.configured:
            log.warning("no Discord route configured: plays are recorded in the ledger only")
        runner.run_forever()
    return 0


def cmd_once(s: Settings, args: argparse.Namespace) -> int:
    with InstanceLock(s.state_dir / "omen.lock"):
        runner, _ = build(s, dry_run=args.dry_run)
        report = runner.run_once(args.kind)
        print(f"{args.kind} scan: {report.opportunities} opportunities, {report.alerts} alerts")
        if args.dry_run:
            for item in runner.notifier.printed:
                print(json.dumps(item, indent=2)[:4000])
    return 0


def cmd_scan(s: Settings, args: argparse.Namespace) -> int:
    """Scan and print verified plays without alerting or touching the main ledger."""
    ledger = Ledger(s.state_dir / "omen-dryrun.sqlite3")
    engine = Engine(s, _client(s), ledger)
    result = engine.scan(args.kind)
    print(
        f"{result.kind} scan: {result.markets:,} markets, {result.candidates} candidates, "
        f"{len(result.opportunities)} verified plays, {result.requests} requests, {result.seconds:.0f}s"
    )
    for n in result.notices:
        print(f"[{n.severity}] {n.title}: {n.body}")
    for ticker, why in result.rejected[: args.show_rejected]:
        print(f"rejected {ticker}: {why}")
    for opp in result.opportunities:
        print()
        print(play_text(opp, opp.found_at))
        if args.orders:
            for i, lp in enumerate(opp.legs):
                print("   ", json.dumps(lp.order_v2(f"omen-{opp.short_id}-{i}")))
    return 0


def cmd_fees(s: Settings, args: argparse.Namespace) -> int:
    ledger = Ledger(s.database)
    engine = Engine(s, _client(s), ledger)
    now = datetime.now(UTC)
    for n in engine.refresh_fees(now, force=True):
        print(f"[{n.severity}] {n.title}: {n.body}")
    sch = engine.schedule
    print(
        f"Formula: schedule {sch.version} (verified {sch.verified_on}), rounding up to "
        f"{sch.rounding_increment} per fill. Source {sch.source_url}"
    )
    for name, f in sorted(sch.formulas.items()):
        print(f"  {name}: taker {f.taker_rate} maker {f.maker_rate}")
    counts: dict[tuple[str, str], int] = {}
    for ser in engine.fees.series.values():
        k = (ser.fee_type, f"{ser.fee_multiplier.normalize():f}")
        counts[k] = counts.get(k, 0) + 1
    print(f"Live per-series terms ({len(engine.fees.series):,} series):")
    for (ft, m), n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {ft} ×{m}: {n}")
    upcoming = engine.fees.upcoming_changes(now, timedelta(days=args.days))
    print(f"Scheduled changes in the next {args.days} days: {len(upcoming)}")
    for ch in upcoming:
        print("  " + ch.describe())
    if args.series:
        price, count = Decimal(args.price), Decimal(args.count)
        try:
            q = engine.fees.quote(args.series, args.event, now)
        except FeeUnavailable as exc:
            print(f"{args.series}: cannot price ({exc})")
            return 1
        print(f"{args.series}{' / ' + args.event if args.event else ''}: {q.describe()}")
        print(
            f"  taker fee for {count} @ {price}: {usd(q.taker_fee(price, count))}; "
            f"maker fee: {usd(q.maker_fee(price, count))}"
        )
    return 0


def cmd_fee_log(s: Settings, args: argparse.Namespace) -> int:
    ledger = Ledger(s.database)
    for r in ledger.fee_log(args.limit):
        print(f"{r['ts']}  {r['kind']:<22} {r['ticker'] or '':<28} {r['detail']}")
    return 0


def cmd_report(s: Settings, args: argparse.Namespace) -> int:
    runner, ledger = build(s)
    lines, fields = build_digest(ledger, runner.models, runner.engine, datetime.now(UTC))
    print("\n".join(lines))
    for f in fields:
        print(f"\n== {f['name']}\n{f['value']}")
    print("\nRecent runs:")
    for r in ledger.recent_runs(10):
        print(
            f"  {r['started_at']} {r['kind']:<22} markets={r['markets']} opps={r['opportunities']} "
            f"alerts={r['alerts']} requests={r['requests']} {('ERROR ' + r['error']) if r['error'] else ''}"
        )
    return 0


def cmd_show(s: Settings, args: argparse.Namespace) -> int:
    ledger = Ledger(s.database)
    row = ledger.db.execute(
        "SELECT * FROM opportunities WHERE short_id=? ORDER BY last_seen DESC", (args.id,)
    ).fetchone()
    if row is None:
        print(f"no opportunity {args.id}")
        return 1
    print(json.dumps({k: row[k] for k in row.keys() if k != "last_payload"}, indent=2))  # noqa: SIM118 (sqlite Row)
    print(json.dumps(json.loads(row["last_payload"]), indent=2))
    return 0


def cmd_doctor(s: Settings, args: argparse.Namespace) -> int:
    ok = True

    def check(name: str, passed: bool, detail: str = "") -> None:
        nonlocal ok
        ok &= passed
        print(f"[{'ok' if passed else 'FAIL'}] {name}{': ' + detail if detail else ''}")

    try:
        routes = build_routes(s.discord_webhooks, s.discord_bot_token, s.discord_channel_map)
        for ch in ("plays", "research", "status"):
            r = resolve(routes, ch)
            check(
                f"discord route '{ch}'",
                r is not None or ch == "research",
                r.redacted() if r else "not configured (messages stay queued)",
            )
    except DiscordConfigError as exc:
        check("discord configuration", False, str(exc))
    try:
        ledger = Ledger(s.database)
        ledger.set_meta("doctor", datetime.now(UTC).isoformat())
        check("ledger", True, str(s.database))
    except (OSError, sqlite3.Error) as exc:
        check("ledger", False, str(exc))
        return 1
    try:
        engine = Engine(s, _client(s), ledger)
        status = engine.client.exchange_status()
        check(
            "kalshi api",
            True,
            f"exchange_active={status.get('exchange_active')} trading_active={status.get('trading_active')}",
        )
        notices = engine.refresh_fees(datetime.now(UTC), force=True)
        unknown = engine.fees.unknown_fee_types()
        check(
            "fee registry",
            not unknown,
            f"{len(engine.fees.series):,} series; unknown types: {sorted(unknown)}"
            if unknown
            else f"{len(engine.fees.series):,} series priced",
        )
        age = (datetime.now(UTC).date() - engine.schedule.verified_on).days
        check(
            "fee formula freshness",
            age <= s.fees.verify_reminder_days,
            f"schedule {engine.schedule.version} verified {age} days ago",
        )
        for n in notices:
            print(f"   note: {n.title}: {n.body}")
    except Exception as exc:
        check("kalshi api", False, str(exc))
    if s.web_url:
        try:
            r = httpx.get(f"{s.web_url}/api/health", timeout=15)
            info = r.json() if r.status_code == 200 else {}
            check(
                "website",
                bool(info.get("ingest_enabled")),
                f"{s.web_url} store={info.get('store')} ingest_enabled={info.get('ingest_enabled')}"
                if info
                else f"{s.web_url}/api/health -> HTTP {r.status_code}",
            )
        except (httpx.HTTPError, ValueError) as exc:
            check("website", False, f"{s.web_url}: {exc}")
    else:
        check("website", True, "not configured (OMEN_WEB_URL unset): the site still works with on-demand scans")
    check("execution mode", True, s.execution.mode)
    if s.execution.mode != "off":
        check(
            "kalshi api key",
            bool(s.kalshi_api_key_id and s.kalshi_private_key_path),
            "KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH",
        )
    return 0 if ok else 1


def cmd_discord_test(s: Settings, args: argparse.Namespace) -> int:
    ledger = Ledger(s.database)
    notifier = _notifier(s, ledger)
    if not notifier.configured:
        print("No Discord route configured (see docs/DISCORD.md)")
        return 1
    now = datetime.now(UTC)
    for ch in ("plays", "research", "status"):
        if resolve(notifier.routes, ch):
            notifier.queue(
                ch,
                notice_payload(f"OMEN test · #{ch}", f"OMEN {__version__} can post here. Channel key: `{ch}`.", "info"),
                "test",
                None,
                now,
            )
    sent, failed = notifier.flush(now)
    print(f"sent {sent}, failed {failed}")
    return 0 if failed == 0 else 1


def cmd_digest(s: Settings, args: argparse.Namespace) -> int:
    runner, _ = build(s)
    runner.maybe_digest(force=True)
    runner.flush()
    print("digest queued and flushed")
    return 0


def cmd_promote(s: Settings, args: argparse.Namespace) -> int:
    from omen.calibration import evaluate_gates

    ledger = Ledger(s.database)
    now = datetime.now(UTC)
    if args.state == "shadow":
        ledger.set_model_state(args.model, "shadow", args.note or "demoted by owner", now)
        print(f"{args.model}: shadow")
        return 0
    verdict = evaluate_gates(ledger.resolved_predictions(args.model), s.gates)
    print(verdict.summary)
    if not verdict.passed and not args.force:
        print("Gates not passed; refusing to promote (use --force with --note to override).")
        return 1
    if not verdict.passed and not args.note:
        print("--force requires --note explaining why")
        return 1
    ledger.set_model_state(args.model, "live", args.note or "gates passed", now)
    print(f"{args.model}: live (its plays now go to #plays and the paper ledger)")
    return 0


def cmd_backup(s: Settings, args: argparse.Namespace) -> int:
    src = sqlite3.connect(str(s.database))
    dest = sqlite3.connect(args.dest)
    with dest:
        src.backup(dest)
    print(f"backed up {s.database} -> {args.dest}")
    return 0


def cmd_atlas(s: Settings, args: argparse.Namespace) -> int:
    """Rebuild the calibration atlas from Kalshi's settled markets (public data; ~30-60 min under rate limits)."""
    from omen import atlas

    http = HttpClient(s.base_url, rate_per_second=s.scan.max_requests_per_second / 2, timeout=40, max_retries=10)
    series = {x.ticker: x.category for x in KalshiClient(http).all_series()}
    doc = atlas.run(http, series, days=args.days, progress=lambda msg: print(msg, flush=True))
    out = args.out or str(s.state_dir / "atlas.json")
    with open(out, "w") as fh:
        json.dump(doc, fh, indent=1)
    print(
        f"atlas: {doc['markets']} markets, {doc['series']} series, {len(doc['cells'])} cells, "
        f"{len(doc['edges'])} validated edges -> {out}"
    )
    for e in doc["edges"]:
        d, v = e["discovery"], e["validation"]
        print(
            f"  EDGE {e['category']:13s} {e['horizon']:>3s} {e['band'][0] * 100:>4.0f}-{e['band'][1] * 100:<3.0f}c  "
            f"EV {d['ev'] * 100:+.2f}c [{d['ev_lo'] * 100:+.2f}, {d['ev_hi'] * 100:+.2f}] n={d['n']} "
            f"validation EV {v['ev'] * 100:+.2f}c n={v['n']}"
        )
    return 0


def cmd_publish(s: Settings, args: argparse.Namespace) -> int:
    """Push the live feed to the website once, now (setup check; the service publishes on its own)."""
    publisher = Publisher.from_settings(s)
    if publisher is None:
        print("set OMEN_WEB_URL and OMEN_INGEST_SECRET (the same secret as in Vercel) first", file=sys.stderr)
        return 2
    runner, ledger = build(s)
    runner.engine.refresh_fees(datetime.now(UTC))
    from omen.snapshot import build_feed

    feed = build_feed(ledger, runner.engine, runner.models, s, datetime.now(UTC))
    if publisher.send(feed):
        print(f"published {len(feed['plays'])} live plays to {s.web_url}")
        return 0
    print(f"publish failed: {publisher.last_error}", file=sys.stderr)
    return 1


def build_lp(s: Settings):
    """The liquidity-reward service with a paper venue, or (``[liquidity].mode = "live"``) real post-only orders."""
    from omen.config import PROD_BASE_URL
    from omen.execution import LIVE_ACK
    from omen.kalshi.auth import KalshiSigner
    from omen.liquidity.maker import LiveVenue, PaperVenue
    from omen.liquidity.service import LiquidityService

    ledger = Ledger(s.database)
    if s.liquidity.mode == "live":
        if s.live_ack != LIVE_ACK:
            raise ConfigError(f'live liquidity quoting needs OMEN_LIVE_ACK="{LIVE_ACK}"')
        if not (s.kalshi_api_key_id and s.kalshi_private_key_path):
            raise ConfigError("live liquidity quoting needs KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH")
        signer = KalshiSigner.from_file(s.kalshi_api_key_id, s.kalshi_private_key_path)
        client = KalshiClient(HttpClient(PROD_BASE_URL, rate_per_second=8, max_retries=2), signer)
        venue = LiveVenue(client, ledger=ledger)  # the ledger keeps our order ids, so fills survive restarts
    else:
        venue = PaperVenue()
    # short Discord waits: delivery runs between quoting cycles, and a slow Discord must not hold up cancels
    return LiquidityService(s, _client(s), ledger, _notifier(s, ledger, timeout=5.0, max_wait=2.0), venue), ledger


def _lp_reconcile(s: Settings, args: argparse.Namespace) -> int:
    """Kalshi's actual reward credits (the cash nothing else explains) against OMEN's formula estimate."""
    from zoneinfo import ZoneInfo

    from omen.config import PROD_BASE_URL
    from omen.kalshi.auth import KalshiSigner
    from omen.liquidity.reconcile import Snapshot, reconcile

    if not (s.kalshi_api_key_id and s.kalshi_private_key_path):
        print("reconciling reads the account: set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH", file=sys.stderr)
        return 2
    ledger = Ledger(s.database)
    since = datetime.fromisoformat(args.since) if args.since else None
    if since is not None and since.tzinfo is None:
        since = since.replace(tzinfo=UTC)
    snaps = [Snapshot(ts, b, h) for ts, b, h in ledger.lp_cash("live", since)]
    if len(snaps) < 2:
        print("no live cash snapshots yet: `omen lp run` records one every cycle in live mode")
        return 1
    signer = KalshiSigner.from_file(s.kalshi_api_key_id, s.kalshi_private_key_path)
    try:
        r = reconcile(KalshiClient(HttpClient(PROD_BASE_URL, rate_per_second=8, max_retries=2), signer), snaps)
    except ValueError as exc:
        print(f"cannot reconcile yet: {exc}")
        return 1
    et = ZoneInfo("America/New_York")
    f = r.flows
    print("Kalshi reward credits, measured from the account's cash")
    span = f"{r.start.astimezone(et):%b %d %I:%M %p} to {r.end.astimezone(et):%b %d %I:%M %p} ET"
    print(f"  {span}, {len(snaps)} snapshots")
    if r.cutoff is None:
        print("  (Kalshi's archive cutoff could not be read: fills or settlements older than it would be missing)")
    print(f"  balance change {r.balance_change:+.2f}")
    print(
        f"  explained: fills -{f.fills:.2f}, fees -{f.fees:.2f}, settlements +{f.settlements:.2f}, "
        f"deposits +{f.deposits:.2f}, withdrawals -{f.withdrawals:.2f}, netted pairs +{f.netted:.2f}"
    )
    if r.paid is None:
        print("  reward credits: not yet known whether the balance counts cash held by resting orders")
        print(f"    if it does: ${r.credits[True]:.2f}; if it does not: ${r.credits[False]:.2f}")
    else:
        if r.holds_in_balance is None:
            how = "resting-order cash was the same at both ends, so either balance convention gives this"
        else:
            how = f"the balance {'counts' if r.holds_in_balance else 'excludes'} resting-order cash; {r.votes} agree"
        print(f"  reward credits: ${r.paid:.2f} ({how})")
    # Kalshi pays up to a day after a period ends: compare with what OMEN estimated it earned until a day earlier
    est = ledger.lp_reward_between("live", r.start, r.end - timedelta(days=1))
    print(f"  OMEN's formula estimate, earned until a day before the end: ${est:.2f}")
    if r.paid is not None and est > 0:
        print(f"  paid / estimated: {r.paid / est:.0%}")
    return 0


def cmd_lp(s: Settings, args: argparse.Namespace) -> int:
    """Liquidity-reward market making: run the quoting loop, show results, or rank today's reward programs."""
    from omen.liquidity.programs import eligible, fetch_active
    from omen.liquidity.service import mid, policy_from, reward_rate

    now = datetime.now(UTC)
    if args.lp_cmd == "run":
        import signal

        if not s.liquidity.enabled:
            print("liquidity quoting is disabled ([liquidity].enabled = false)")
            return 0

        def stop(signum, frame):  # systemd stops with SIGTERM: exit through run()'s cancel-everything cleanup
            # a second SIGTERM (uv forwards one, systemd may send another) must not abort that cleanup
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            raise SystemExit(0)

        signal.signal(signal.SIGTERM, stop)
        with InstanceLock(s.state_dir / "omen-lp.lock"):
            service, _ = build_lp(s)
            if service.mode == "live":
                n = service.venue.cancel_stale()  # never raises; anything left is adopted and retried each cycle
                if n is None:
                    print("could not list resting orders (API down?); no order is placed until they can be read")
                else:
                    print(f"canceled {n} order(s) left by a previous run; {len(service.venue.orders)} still open")
            print(f"liquidity quoting ({service.mode}); kill switch: {service.kill_file()}")
            service.run(cycles=args.cycles)
        return 0
    if args.lp_cmd == "status":
        service, ledger = build_lp(s)
        held = ledger.lp_positions(service.mode)
        books = service.client.orderbooks(sorted(held)) if held else {}
        sm = service.summary(now, books)
        print(f"liquidity rewards ({service.mode})")
        for label in ("day", "week", "total"):
            d = sm[label]
            print(
                f"  {label:5s}: rewards ${d['reward_usd']:9.2f} in {d['reward_markets']:4.0f} markets"
                f" | fills {d['fills']:5.0f} ({d['contracts']:7.0f} contracts, ${d['notional']:8.2f})"
                f" | settled P&L ${d['settled_pnl']:+8.2f} | fees ${d['fees']:.2f}"
            )
        unreal = sm["inventory_mark"] - sm["inventory_cost"]
        print(f"  inventory: cost ${sm['inventory_cost']:.2f}, marked ${sm['inventory_mark']:.2f} ({unreal:+.2f})")
        for ticker, sides in sorted(held.items()):
            m = mid(books.get(ticker))
            for side, (q, c) in sides.items():
                if q:
                    mark = "" if m is None else f" mark {(m if side == 'yes' else 1 - m):.2f}"
                    print(f"    {ticker:44s} {side.upper():3s} {q:7.0f} @ {c / q:.3f}{mark}")
        if sm["by_series"]:
            print("  rewards by series (7 days): " + ", ".join(f"{k} ${v:.2f}" for k, v in sm["by_series"]))
        return 0
    if args.lp_cmd == "reconcile":
        return _lp_reconcile(s, args)
    # programs: what the policy would earn per dollar in each live program right now
    client = _client(s)
    lp = s.liquidity
    progs = eligible(
        fetch_active(client.http),
        now,
        min_hours=lp.min_program_hours,
        min_reward_per_day=lp.min_reward_per_day_usd,
        exclude=lp.exclude_series,
        min_time_left=timedelta(minutes=10),
        limit=max(args.top * 3, lp.max_markets),
    )
    books = client.orderbooks([p.ticker for p in progs])
    policy = policy_from(s)
    rows = []
    for p in progs:
        book = books.get(p.ticker)
        if book is None:
            continue
        for q in policy.quotes(book, {}):
            per_day = reward_rate(p, book, {q.side: (q.price, q.size)}) * 86400
            rows.append((per_day / (q.price * q.size), per_day, p, q))
    rows.sort(key=lambda r: -r[0])
    print(
        f"{len(progs)} eligible reward programs; quoting the {lp.sides} side(s) at the best bid, "
        f"${lp.capital_per_market_usd:.0f} per market"
    )
    for ratio, per_day, p, q in rows[: args.top]:
        print(
            f"  {p.ticker:44s} pays ${p.per_day:7.0f}/day | bid {q.side.upper():3s} {q.size:4d} @ {q.price:.2f} "
            f"(${q.price * q.size:6.2f}) -> ~${per_day:6.2f}/day ({ratio * 100:5.1f}%/day of the capital)"
        )
    return 0


def cmd_web(s: Settings, args: argparse.Namespace) -> int:
    from omen.web.local import serve

    if not os.environ.get("OMEN_WEB_PASSWORD"):
        print("set OMEN_WEB_PASSWORD (8+ characters) first; the site asks for it", file=sys.stderr)
        return 2
    print(f"OMEN website on http://{args.host}:{args.port} (Ctrl+C to stop)")
    serve(args.host, args.port)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="omen", description="High-confidence Kalshi scanner")
    p.add_argument("--config", help="TOML settings file (default config/omen.toml)")
    p.add_argument("--env-file", help="KEY=VALUE file to load (e.g. ~/.config/omen/omen.env); real env wins")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("run", help="run the scanner service (loop)").set_defaults(fn=cmd_run)
    o = sub.add_parser("once", help="one full cycle with alerts (cron / GitHub Actions)")
    o.add_argument("--kind", choices=["hot", "full"], default="full")
    o.add_argument("--dry-run", action="store_true", help="print Discord payloads instead of sending")
    o.set_defaults(fn=cmd_once)
    sc = sub.add_parser("scan", help="scan and print verified plays (no alerts, separate dry-run ledger)")
    sc.add_argument("--kind", choices=["hot", "full"], default="hot")
    sc.add_argument("--orders", action="store_true", help="print the exact V2 order payloads")
    sc.add_argument("--show-rejected", type=int, default=10)
    sc.set_defaults(fn=cmd_scan)
    f = sub.add_parser("fees", help="show the fee terms in force and scheduled changes")
    f.add_argument("--series")
    f.add_argument("--event")
    f.add_argument("--price", default="0.50")
    f.add_argument("--count", default="100")
    f.add_argument("--days", type=int, default=14)
    f.set_defaults(fn=cmd_fees)
    fl = sub.add_parser("fee-log", help="fee changes OMEN has observed")
    fl.add_argument("--limit", type=int, default=50)
    fl.set_defaults(fn=cmd_fee_log)
    sub.add_parser("report", help="paper ledger, models, recent runs").set_defaults(fn=cmd_report)
    sh = sub.add_parser("show", help="details and order payloads for an alerted play")
    sh.add_argument("id")
    sh.set_defaults(fn=cmd_show)
    sub.add_parser("doctor", help="check configuration, API, fees and Discord routes").set_defaults(fn=cmd_doctor)
    sub.add_parser("discord-test", help="post a test message to each configured channel").set_defaults(
        fn=cmd_discord_test
    )
    sub.add_parser("digest", help="send the daily digest now").set_defaults(fn=cmd_digest)
    pr = sub.add_parser("promote", help="move a model between shadow and live (live requires passed gates)")
    pr.add_argument("model")
    pr.add_argument("--state", choices=["live", "shadow"], default="live")
    pr.add_argument("--force", action="store_true")
    pr.add_argument("--note")
    pr.set_defaults(fn=cmd_promote)
    b = sub.add_parser("backup", help="consistent copy of the ledger")
    b.add_argument("dest")
    b.set_defaults(fn=cmd_backup)
    at = sub.add_parser("atlas", help="rebuild the calibration atlas (where Kalshi prices were wrong after fees)")
    at.add_argument("--days", type=int, default=45)
    at.add_argument("--out", help="output file (default ~/.omen/atlas.json)")
    at.set_defaults(fn=cmd_atlas)
    sub.add_parser("publish", help="push the live feed to the website once (setup check)").set_defaults(fn=cmd_publish)
    lpp = sub.add_parser("lp", help="liquidity-reward market making (paper by default)")
    lps = lpp.add_subparsers(dest="lp_cmd", required=True)
    lr = lps.add_parser("run", help="run the quoting loop (paper unless [liquidity].mode = live)")
    lr.add_argument("--cycles", type=int, default=None, help="stop after this many cycles")
    lps.add_parser("status", help="rewards, fills, inventory and settled P&L")
    lrc = lps.add_parser("reconcile", help="what Kalshi actually paid in rewards (live), against OMEN's estimate")
    lrc.add_argument("--since", help="ISO time to start from (default: the first live cash snapshot)")
    lpg = lps.add_parser("programs", help="rank live reward programs by what the policy would earn per dollar")
    lpg.add_argument("--top", type=int, default=25)
    lpp.set_defaults(fn=cmd_lp)
    w = sub.add_parser("web", help="run the website locally (API + built frontend) for previews")
    w.add_argument("--host", default="127.0.0.1")
    w.add_argument("--port", type=int, default=8787)
    w.set_defaults(fn=cmd_web)

    args = p.parse_args(argv)
    _setup_logging(args.verbose)
    try:
        if args.env_file:
            for key, value in read_env_file(args.env_file).items():
                os.environ.setdefault(key, value)
        settings = load_settings(args.config)
    except (ConfigError, DiscordConfigError, FileNotFoundError) as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    try:
        return int(args.fn(settings, args) or 0)
    except DiscordConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
