"""Model validation: is a model's probability honest, and do its would-be alerts make money after fees?

A model starts in SHADOW: every evaluation is logged, outcomes are filled in from Kalshi's results, and its
signals go only to #kalshi-research. It may be promoted to LIVE (signals to #kalshi-plays and the paper ledger) by
the owner once every gate below passes on resolved data:

* enough resolved signals, spread over enough distinct events (strikes of one event are strongly correlated);
* the lower bound of a cluster-bootstrap confidence interval for after-fee ROI of the signals is above zero;
* expected calibration error of the central probability is small.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from omen.config import ValidationGates
from omen.stats import cluster_bootstrap_lower, expected_calibration_error


@dataclass(frozen=True)
class GateVerdict:
    passed: bool
    resolved_predictions: int
    resolved_signals: int
    distinct_events: int
    roi: float
    roi_lower: float
    ece: float
    brier_model: float
    brier_market: float
    reasons: tuple[str, ...]

    @property
    def summary(self) -> str:
        if self.resolved_predictions == 0:
            return "No resolved predictions yet."
        skill = 1 - self.brier_model / self.brier_market if self.brier_market > 0 else float("nan")
        lines = [
            f"{self.resolved_predictions} resolved predictions; Brier {self.brier_model:.4f} vs market "
            f"{self.brier_market:.4f} (skill {skill:+.1%}); calibration error {self.ece:.3f}",
            f"{self.resolved_signals} resolved signals over {self.distinct_events} events: after-fee ROI "
            f"{self.roi:+.2%} (lower bound {self.roi_lower:+.2%})",
            "Gates: PASSED" if self.passed else "Gates: not yet — " + "; ".join(self.reasons),
        ]
        return "\n".join(lines)


def evaluate_gates(rows: list[Any], gates: ValidationGates) -> GateVerdict:
    preds: list[tuple[float, float]] = []
    market: list[tuple[float, float]] = []
    by_event: dict[str, list[tuple[float, float]]] = defaultdict(list)
    signals = 0
    pnl = cost = 0.0
    for r in rows:
        y = float(r["outcome"])
        preds.append((float(r["p_model"]), y))
        feats = json.loads(r["features_json"] or "{}")
        if feats.get("market_mid") is not None:
            market.append((float(feats["market_mid"]), y))
        if r["would_alert"]:
            win = y if r["side"] == "yes" else 1 - y
            c = float(r["price"]) + float(r["fee"])
            by_event[r["event_ticker"] or r["ticker"]].append((win - c, c))
            signals += 1
            pnl += win - c
            cost += c
    brier_model = sum((p - y) ** 2 for p, y in preds) / len(preds) if preds else float("nan")
    brier_market = sum((p - y) ** 2 for p, y in market) / len(market) if market else float("nan")
    ece = expected_calibration_error(preds)
    roi = pnl / cost if cost else 0.0
    roi_lower = (
        cluster_bootstrap_lower(by_event, gates.bootstrap_confidence, gates.bootstrap_samples)
        if by_event
        else float("-inf")
    )
    reasons = []
    if signals < gates.min_resolved_signals:
        reasons.append(f"{signals}/{gates.min_resolved_signals} resolved signals")
    if len(by_event) < gates.min_distinct_events:
        reasons.append(f"{len(by_event)}/{gates.min_distinct_events} distinct events")
    if not roi_lower > gates.min_after_fee_roi_lower_bound:
        reasons.append(f"ROI lower bound {roi_lower:+.2%} not above {gates.min_after_fee_roi_lower_bound:+.2%}")
    if math.isnan(ece) or ece > gates.max_calibration_error:
        reasons.append(f"calibration error {ece:.3f} above {gates.max_calibration_error}")
    return GateVerdict(
        not reasons, len(preds), signals, len(by_event), roi, roi_lower, ece, brier_model, brier_market, tuple(reasons)
    )
