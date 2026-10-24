"""JSON views of OMEN's state, shared by the Pi's live feed and the website's own scans so a play always looks the
same wherever it is shown. Money is rounded for display only; order payloads keep exact strings."""

from __future__ import annotations

import json
import socket
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from omen import __version__
from omen.strategies.base import LOCK, Opportunity

STRATEGY_NAMES = {
    "no_basket": "NO basket",
    "ladder": "Ladder",
    "yes_partition": "YES basket",
    "cover": "Cross-series lock",
    "crypto_threshold": "Crypto model",
    "calibration_edge": "Calibration edge",
}
MAX_PLAYS = 60
MAX_ACTIVITY = 80


def _f(value: Decimal | float | None, places: int = 4) -> float | None:
    return None if value is None else round(float(value), places)


def _ts(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat(timespec="seconds") if value else None


def play_dict(opp: Opportunity, source: str, max_plausible_return_pct: Decimal | None = None) -> dict[str, Any]:
    legs = []
    for i, lp in enumerate(opp.legs):
        try:
            order = lp.order_v2(f"omen-{opp.short_id}-{i}")
        except ValueError:
            order = None
        legs.append(
            {
                "ticker": lp.market.ticker,
                "event_ticker": lp.market.event_ticker,
                "side": lp.side,
                "title": lp.market.title,
                "sub_title": lp.market.yes_sub_title,
                "count": _f(lp.fill.count, 2),
                "limit": _f(lp.limit_price),
                "avg": _f(lp.fill.avg_price),
                "notional": _f(lp.fill.notional),
                "fees": _f(lp.fill.fees),
                "fee_terms": lp.fee.describe(),
                "depth": _f(lp.depth, 2),
                "close_time": _ts(lp.market.close_time),
                "order": order,
            }
        )
    suspicious = opp.tier == LOCK and max_plausible_return_pct is not None and opp.return_pct > max_plausible_return_pct
    return {
        "key": opp.key,
        "id": opp.short_id,
        "source": source,
        "strategy": opp.strategy,
        "strategy_name": STRATEGY_NAMES.get(opp.strategy, opp.strategy),
        "tier": opp.tier,
        "title": opp.title,
        "event_ticker": opp.event_ticker,
        "profit": _f(opp.profit),
        "capital": _f(opp.capital),
        "cost": _f(opp.cost),
        "fees": _f(opp.fees),
        "return_pct": _f(opp.return_pct),
        "apr_pct": _f(opp.apr_pct, 1),
        "payout_min": _f(opp.payout_min),
        "payout_max": _f(opp.payout_max),
        "sets": _f(opp.sets, 2),
        "settles_at": _ts(opp.settles_at),
        "found_at": _ts(opp.found_at),
        "confidence": round(opp.confidence, 4),
        "probability": opp.probability,
        "model_probability": opp.model_probability,
        "rationale": list(opp.rationale),
        "risks": list(opp.risks),
        "evidence": json.loads(json.dumps(opp.evidence, default=str)),
        "legs": legs,
        "suspicious": suspicious,
    }


def build_feed(ledger: Any, engine: Any, models: list[Any], settings: Any, now: datetime) -> dict[str, Any]:
    """The Pi's live state for the website: live plays, recent activity, paper ledger, models, runs, fees, health."""
    db = ledger.db
    live = []
    for row in db.execute(
        "SELECT * FROM opportunities WHERE gone_at IS NULL ORDER BY CAST(last_profit AS REAL) DESC LIMIT ?",
        (MAX_PLAYS,),
    ):
        try:
            payload = json.loads(row["last_payload"] or "{}")
        except json.JSONDecodeError:
            continue
        if "key" not in payload:  # rows written before the shared format: skip rather than guess fields
            continue
        payload.update(
            first_seen=row["first_seen"],
            last_seen=row["last_seen"],
            alerted_at=row["alerted_at"],
            alert_channel=row["alert_channel"],
            source="pi",
        )
        live.append(payload)

    activity: list[dict[str, Any]] = []
    for r in db.execute(
        "SELECT short_id, strategy, tier, title, alerted_at, alert_channel, alert_profit FROM opportunities "
        "WHERE alerted_at IS NOT NULL ORDER BY alerted_at DESC LIMIT 40"
    ):
        activity.append(
            {
                "ts": r["alerted_at"],
                "kind": "alert",
                "id": r["short_id"],
                "tier": r["tier"],
                "title": r["title"],
                "channel": r["alert_channel"],
                "profit": _f(Decimal(r["alert_profit"] or 0)),
            }
        )
    for r in db.execute(
        "SELECT short_id, title, gone_at, best_profit FROM opportunities WHERE gone_at IS NOT NULL "
        "AND alerted_at IS NOT NULL ORDER BY gone_at DESC LIMIT 20"
    ):
        activity.append(
            {
                "ts": r["gone_at"],
                "kind": "gone",
                "id": r["short_id"],
                "title": r["title"],
                "profit": _f(Decimal(r["best_profit"] or 0)),
            }
        )
    for r in db.execute(
        "SELECT ts, kind, ticker, detail FROM fee_log WHERE kind != 'baseline' ORDER BY id DESC LIMIT 20"
    ):
        activity.append(
            {"ts": r["ts"], "kind": "fee", "title": r["detail"], "ticker": r["ticker"], "fee_kind": r["kind"]}
        )
    for r in db.execute("SELECT started_at, kind, error FROM runs WHERE error IS NOT NULL ORDER BY id DESC LIMIT 10"):
        activity.append({"ts": r["started_at"], "kind": "error", "title": f"{r['kind']} failed", "detail": r["error"]})
    activity.sort(key=lambda a: a["ts"] or "", reverse=True)

    paper_open = [
        _paper_row(r)
        for r in db.execute("SELECT * FROM paper_positions WHERE status='open' ORDER BY opened_at DESC LIMIT 100")
    ]
    paper_settled = [
        _paper_row(r)
        for r in db.execute("SELECT * FROM paper_positions WHERE status='settled' ORDER BY settled_at DESC LIMIT 200")
    ]
    totals = db.execute(
        "SELECT status, COUNT(*) n, SUM(CAST(capital AS REAL)) cap, SUM(CAST(pnl AS REAL)) pnl, "
        "SUM(CAST(expected_profit AS REAL)) exp FROM paper_positions GROUP BY status"
    ).fetchall()

    runs = [
        dict(r)
        for r in db.execute(
            "SELECT kind, started_at, finished_at, markets, candidates, opportunities, alerts, requests, error "
            "FROM runs ORDER BY id DESC LIMIT 40"
        )
    ]
    upcoming = [
        {
            "scope": c.scope,
            "ticker": c.ticker,
            "fee_type": c.fee_type,
            "multiplier": None if c.multiplier is None else float(c.multiplier),
            "scheduled": _ts(c.scheduled),
            "describe": c.describe(),
        }
        for c in engine.fees.upcoming_changes(now, timedelta(days=14))[:40]
    ]
    model_status = []
    for m in models:
        try:
            st = m.status()
        except Exception as exc:  # status must never break the feed
            st = {"state": "unknown", "summary": f"status unavailable: {exc}"}
        model_status.append(
            {
                "name": m.name,
                **{k: v for k, v in st.items() if k in {"state", "summary", "passed"}},
                "gates": st.get("gates"),
            }
        )
    s = settings
    lp_mode = s.liquidity.mode
    lp_held = ledger.lp_positions(lp_mode)
    liquidity = {
        "enabled": s.liquidity.enabled,
        "mode": lp_mode,
        "day": ledger.lp_summary(lp_mode, now - timedelta(days=1)),
        "week": ledger.lp_summary(lp_mode, now - timedelta(days=7)),
        "total": ledger.lp_summary(lp_mode),
        "hourly": [{"hour": h, "usd": round(v, 4)} for h, v in ledger.lp_hourly(lp_mode, now - timedelta(days=7))],
        "by_series": [
            {"series": k, "usd": round(v, 4)}
            for k, v in ledger.lp_reward_by_series(lp_mode, now - timedelta(days=7))[:12]
        ],
        "inventory": [
            {"ticker": t, "side": side, "qty": q, "cost": round(c, 4)}
            for t, sides in sorted(lp_held.items())
            for side, (q, c) in sides.items()
            if q
        ],
        "fills": ledger.lp_recent_fills(lp_mode, 50),
        "settled": ledger.lp_recent_settlements(lp_mode, 50),
        "settings": {
            "sides": s.liquidity.sides,
            "capital_per_market_usd": s.liquidity.capital_per_market_usd,
            "max_total_capital_usd": s.liquidity.max_total_capital_usd,
            "max_markets": s.liquidity.max_markets,
        },
    }
    return {
        "version": 1,
        "omen_version": __version__,
        "generated_at": _ts(now),
        "host": socket.gethostname(),
        "plays": live,
        "activity": activity[:MAX_ACTIVITY],
        "paper": {
            "open": paper_open,
            "settled": paper_settled,
            "totals": [dict(r) for r in totals],
        },
        "models": model_status,
        "liquidity": liquidity,
        "runs": runs,
        "fees": {
            "schedule_version": engine.schedule.version,
            "verified_on": engine.schedule.verified_on.isoformat(),
            "upcoming": upcoming,
            "series_count": len(engine.fees.series),
        },
        "health": {
            "outbox": ledger.outbox_stats(),
            "execution_mode": s.execution.mode,
            "markets_in_store": len(engine.markets),
            "cadence": {
                "hot_seconds": s.scan.hot_interval_seconds,
                "hot_horizon_hours": s.scan.hot_horizon_hours,
                "full_seconds": s.scan.full_interval_seconds,
                "model_seconds": s.scan.model_interval_seconds,
            },
            "thresholds": {
                "min_profit_usd": float(s.lock.min_profit_usd),
                "min_return_pct": float(s.lock.min_return_pct),
                "min_apr_pct": float(s.lock.min_apr_pct),
                "max_capital_usd": float(s.lock.max_capital_usd),
                "max_plausible_return_pct": float(s.lock.max_plausible_return_pct),
            },
        },
    }


def _paper_row(r: Any) -> dict[str, Any]:
    return {
        "id": r["short_id"],
        "strategy": r["strategy"],
        "tier": r["tier"],
        "opened_at": r["opened_at"],
        "settles_at": r["settles_at"],
        "settled_at": r["settled_at"],
        "status": r["status"],
        "sets": _f(Decimal(r["sets"] or 0), 2),
        "capital": _f(Decimal(r["capital"] or 0)),
        "fees": _f(Decimal(r["fees"] or 0)),
        "expected_profit": _f(Decimal(r["expected_profit"] or 0)),
        "payout": _f(Decimal(r["payout"])) if r["payout"] else None,
        "pnl": _f(Decimal(r["pnl"])) if r["pnl"] else None,
        "legs": json.loads(r["legs_json"] or "[]"),
    }
