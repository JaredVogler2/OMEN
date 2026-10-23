"""Paper ledger: every play OMEN alerts is recorded at the exact prices and fees it verified, then settled from
Kalshi's official results. This is the track record that tells you whether "high confidence" is earned."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from decimal import Decimal

from omen.kalshi.client import KalshiClient
from omen.kalshi.models import parse_ts
from omen.ledger import Ledger
from omen.money import ONE, ZERO
from omen.strategies.base import Opportunity

log = logging.getLogger(__name__)


def legs_json(opp: Opportunity) -> list[dict]:
    return [
        {
            "ticker": lp.market.ticker,
            "side": lp.side,
            "count": str(lp.fill.count),
            "notional": str(lp.fill.notional),
            "fees": str(lp.fill.fees),
            "limit": str(lp.limit_price),
            "close_time": lp.market.close_time.isoformat(),
            "fee_terms": lp.fee.describe(),
        }
        for lp in opp.legs
    ]


def open_position(ledger: Ledger, opp: Opportunity, now: datetime) -> int | None:
    if ledger.has_open_paper(opp.key):
        return None
    return ledger.open_paper(
        opp.key,
        opp.short_id,
        opp.strategy,
        opp.tier,
        now,
        opp.settles_at,
        opp.sets,
        opp.capital,
        opp.fees,
        opp.profit,
        opp.payout_min,
        legs_json(opp),
    )


def settle_positions(ledger: Ledger, client: KalshiClient, now: datetime) -> list[dict]:
    """Settle open paper positions whose markets have all resolved. Returns a summary row per settled position."""
    due = []
    for row in ledger.open_paper_positions():
        legs = json.loads(row["legs_json"])
        if all((parse_ts(leg["close_time"]) or now) <= now for leg in legs):
            due.append((row, legs))
    if not due:
        return []
    tickers = {leg["ticker"] for _, legs in due for leg in legs}
    markets = {m.ticker: m for m in client.markets_by_ticker(tickers)}
    settled = []
    for row, legs in due:
        payout = ZERO
        complete = True
        for leg in legs:
            m = markets.get(leg["ticker"])
            yes = m.payout_yes() if m is not None else None
            if yes is None:
                complete = False
                break
            per = yes if leg["side"] == "yes" else ONE - yes
            payout += per * Decimal(leg["count"])
        if not complete:
            continue
        pnl = payout - Decimal(row["capital"])
        ledger.settle_paper(row["id"], payout, pnl, now)
        settled.append(
            {
                "id": row["id"],
                "short_id": row["short_id"],
                "strategy": row["strategy"],
                "tier": row["tier"],
                "payout": payout,
                "pnl": pnl,
                "expected": Decimal(row["expected_profit"]),
            }
        )
        log.info("paper %s settled: payout %s pnl %s", row["short_id"], payout, pnl)
    return settled


def resolve_predictions(ledger: Ledger, client: KalshiClient, now: datetime) -> int:
    tickers = ledger.unresolved_prediction_tickers(now)
    if not tickers:
        return 0
    n = 0
    for m in client.markets_by_ticker(tickers):
        yes = m.payout_yes()
        if yes is not None:
            n += ledger.resolve_predictions(m.ticker, yes, now)
    return n
