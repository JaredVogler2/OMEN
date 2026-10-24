"""MODEL strategy: calibration edges, i.e. price bands where Kalshi has been measurably wrong after fees.

The atlas (omen.atlas, rebuilt weekly by `omen atlas`) measures, per category x time-to-close x price band, how
often contracts bought at that price actually won. A band becomes an **edge** only if its after-fee profit was
positive with 95% confidence on the older weeks AND positive again on the newest weeks, which the search never saw.

For a live market in an edge band this model quantifies confidence explicitly:

* the measured mispricing (win rate - price) is shrunk toward zero with a normal prior (``prior_sd``): small samples
  and lucky streaks count for less;
* ``p_true`` = price + shrunk mispricing, with a standard error from the series-clustered bootstrap;
* ``confidence`` = P(after-fee edge > 0) under that posterior;
* the stake is a fraction of the Kelly bet for ``p_true`` at the all-in price, capped.

Like every model it starts in SHADOW: every evaluation is logged and scored when the market settles, and its
signals reach #kalshi-plays only after the calibration gates pass and the owner runs `omen promote`.
"""

from __future__ import annotations

import json
import logging
import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from omen import atlas
from omen.book import available, sweep
from omen.calibration import evaluate_gates
from omen.config import Settings
from omen.fees.schedule import FeeUnavailable
from omen.kalshi.models import Market
from omen.money import ONE, ZERO
from omen.stats import normal_cdf
from omen.strategies.base import MODEL, LegPlan, Opportunity

log = logging.getLogger(__name__)
NAME = "calibration_edge"


def horizon_for(hours: float) -> str | None:
    """The atlas horizon a live market's time-to-close falls into (None = outside every measured horizon)."""
    if hours < 0.5:
        return None
    if hours < 3:
        return "1h"
    if hours < 12:
        return "6h"
    if hours < 48:
        return "24h"
    if hours <= 120:
        return "72h"
    return None


def score(price: float, fee: float, excess: float, se: float, prior_sd: float) -> dict[str, float]:
    """Posterior for a contract bought at ``price`` (+ ``fee``) in a band whose measured mispricing is ``excess``
    (win rate - price) with standard error ``se``. Normal-normal shrinkage toward zero."""
    w = prior_sd**2 / (prior_sd**2 + se**2)
    mean = excess * w
    sd = math.sqrt(prior_sd**2 * se**2 / (prior_sd**2 + se**2))
    p_true = min(0.999, max(0.001, price + mean))
    cost = price + fee
    ev = p_true - cost
    confidence = normal_cdf((mean - fee) / sd) if sd > 0 else float(ev > 0)
    kelly = max(0.0, (p_true - cost) / (1 - cost)) if cost < 1 else 0.0
    return {
        "p_true": p_true,
        "p_low": max(0.0, price + mean - 1.645 * sd),
        "ev": ev,
        "confidence": confidence,
        "kelly": kelly,
        "payout_multiple": (1 - cost) / cost if cost > 0 else 0.0,
        "shrink": w,
    }


class CalibrationEdgeModel:
    name = NAME

    def __init__(self, settings: Settings, client, ledger, engine, atlas_doc: dict | None = None):
        self.s = settings
        self.cfg = settings.edge
        self.client = client
        self.ledger = ledger
        self.engine = engine
        self._doc = atlas_doc
        self._loaded_at: datetime | None = None

    # ---------------------------------------------------------------------------------------------- atlas
    def atlas_file(self) -> Path:
        return Path(self.cfg.atlas_path).expanduser() if self.cfg.atlas_path else self.s.state_dir / "atlas.json"

    def edges(self, now: datetime) -> dict[tuple[str, str, tuple[float, float]], dict]:
        """Validated cells from a fresh-enough atlas, keyed by (category, horizon, band)."""
        doc = self._doc
        if doc is None or (self._loaded_at and now - self._loaded_at > timedelta(hours=6)):
            try:
                doc = json.loads(self.atlas_file().read_text())
                self._doc, self._loaded_at = doc, now
            except (OSError, ValueError):
                return {}
        try:
            generated = datetime.fromisoformat(doc["generated_at"])
        except (KeyError, ValueError):
            return {}
        if now - generated > timedelta(days=self.cfg.max_atlas_age_days):
            return {}
        return {(e["category"], e["horizon"], tuple(e["band"])): e for e in doc.get("edges") or []}

    # -------------------------------------------------------------------------------------------------- run
    def run(self, now: datetime) -> tuple[list[Opportunity], set[str]]:
        if not self.cfg.enabled:
            return [], set()
        edges = self.edges(now)
        if not edges:
            return [], set()
        series = self.engine.fees.series
        scope: set[str] = set()
        candidates = []
        for m in list(self.engine.markets.values()):
            hz = horizon_for((m.close_time - now).total_seconds() / 3600)
            meta = series.get(m.series_ticker)
            if hz is None or meta is None or not m.tradable:
                continue
            category = atlas.category_of(meta.category)
            for side, price, ok in (("yes", m.yes_ask, m.has_yes_ask), ("no", ONE - m.yes_bid, m.has_yes_bid)):
                if not ok:
                    continue
                band = atlas.band_of(float(price))
                cell = edges.get((category, hz, band)) if band else None
                if cell is None:
                    continue
                scope.add(m.ticker)
                cand = self._evaluate(m, side, price, cell, now)
                if cand is not None:
                    candidates.append(cand)
        return self._size(candidates, now), scope

    def _evaluate(self, m: Market, side: str, price: Decimal, cell: dict, now: datetime):
        try:
            fee = self.engine.fee_for(m)
        except FeeUnavailable:
            return None
        fee_c = float(fee.taker_rate_per_contract(price))
        d = cell["discovery"]
        se = max(1e-4, (d["ev_hi"] - d["ev_lo"]) / 3.92)
        sc = score(float(price), fee_c, d["excess"], se, self.cfg.prior_sd)
        would = sc["confidence"] >= self.cfg.min_confidence and sc["ev"] >= self.cfg.min_ev
        p_yes = sc["p_true"] if side == "yes" else 1 - sc["p_true"]
        mid = float((m.yes_bid + m.yes_ask) / 2) if m.has_yes_bid and m.has_yes_ask else None
        last = self.ledger.last_prediction_ts(NAME, m.ticker)
        if last is None or now - last >= timedelta(minutes=60 if not would else 30):
            self.ledger.add_prediction(
                NAME,
                m.ticker,
                m.event_ticker,
                now,
                side,
                p_yes,
                sc["p_low"],
                price,
                fee.taker_rate_per_contract(price),
                sc["ev"],
                would,
                m.close_time,
                {"cell": [cell["category"], cell["horizon"], cell["band"]], "market_mid": mid, **sc},
            )
        return (m, side, sc, cell) if would else None

    def _size(self, candidates, now: datetime) -> list[Opportunity]:
        if not candidates:
            return []
        books = self.client.orderbooks({m.ticker for m, *_ in candidates})
        out = []
        for m, side, sc, cell in candidates:
            book = books.get(m.ticker)
            if book is None:
                continue
            fee = self.engine.fee_for(m)
            stake = min(
                self.cfg.max_capital_usd,
                Decimal(str(round(self.cfg.kelly_fraction * sc["kelly"], 6))) * self.cfg.bankroll_usd,
            )
            levels = book.asks(side)
            n = _largest_within(levels, fee, stake)
            best = sweep(levels, n, fee) if n >= 1 else None
            if best is not None and Decimal(str(round(sc["p_true"], 6))) - best.total / n < Decimal(
                str(self.cfg.min_ev)
            ):
                best = None  # deeper levels erode the edge below the floor
            if best is None:
                continue
            d, v = cell["discovery"], cell["validation"] or {}
            hours = (m.close_time - now).total_seconds() / 3600
            out.append(
                Opportunity(
                    strategy=NAME,
                    tier=MODEL,
                    title=f"{m.title} · {side.upper()} {m.yes_sub_title}",
                    event_ticker=m.event_ticker,
                    legs=[LegPlan(m, side, best, fee, available(levels))],
                    sets=best.count,
                    payout_min=ZERO,
                    payout_max=best.count * ONE,
                    settles_at=m.close_time,
                    found_at=now,
                    confidence=round(sc["confidence"], 4),
                    probability=round(sc["p_true"], 4),
                    model_probability=round(sc["p_true"], 4),
                    rationale=[
                        f"{cell['category']} markets {cell['horizon']} before close priced "
                        f"{cell['band'][0] * 100:.0f}-{cell['band'][1] * 100:.0f}¢ won {d['win_rate']:.1%} of the time "
                        f"at an average {d['avg_price'] * 100:.1f}¢ ({d['n']} contracts over {d['series']} series), "
                        f"and {v.get('ev', 0) * 100:+.1f}¢ per contract on held-out recent weeks.",
                        f"Shrunk toward zero for caution: estimated true win probability {sc['p_true']:.1%} "
                        f"(90% lower bound {sc['p_low']:.1%}) against an all-in price of "
                        f"{(float(best.avg_price) + float(best.fees / best.count)) * 100:.1f}¢.",
                        f"P(edge after fees > 0) = {sc['confidence']:.0%}; expected {sc['ev'] * 100:+.1f}¢ per "
                        f"contract; pays {sc['payout_multiple']:.2f}x the stake if it wins; "
                        f"stake = {self.cfg.kelly_fraction:g} Kelly.",
                    ],
                    risks=[
                        "Statistical edge, not a lock: individual plays lose, and past mispricing can disappear.",
                        f"Settles in {hours:.0f} h. The edge was measured on taker prices; slippage reduces it.",
                    ],
                    evidence={"model": NAME, "cell": cell, "score": sc},
                )
            )
        return out

    # --------------------------------------------------------------------------------------------- status
    def status(self) -> dict:
        verdict = evaluate_gates(self.ledger.resolved_predictions(NAME), self.s.gates)
        state = self.ledger.model_state(NAME)
        edges = self.edges(datetime.now(UTC))
        summary = verdict.summary
        if not edges:
            summary = "Dormant: no validated calibration edge in a fresh atlas (run `omen atlas`).\n" + summary
        return {"state": state, "summary": summary, "passed": verdict.passed}


def _largest_within(levels, fee, cap: Decimal) -> Decimal:
    """Most whole contracts whose all-in cost (walking the book, fees rounded up) fits within ``cap``."""
    lo, hi = 0, int(available(levels))
    while lo < hi:
        mid = (lo + hi + 1) // 2
        f = sweep(levels, Decimal(mid), fee)
        if f is not None and f.total <= cap:
            lo = mid
        else:
            hi = mid - 1
    return Decimal(lo)


def describe(doc: dict[str, Any]) -> str:
    return f"{doc.get('markets', 0)} markets, {len(doc.get('edges') or [])} validated edges"
