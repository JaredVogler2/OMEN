"""MODEL strategy: Kalshi crypto price markets ("BTC above $X at 5pm ET", ranges) priced from live spot and vol.

Probability that the settlement value lands in a market's interval, with log returns drawn from a fat-tailed
Student-t scaled to the volatility. The probability the play relies on is the *worst* over a grid of
volatilities (central / safety, central, central x safety) and spot shifts (± the basis band between our venue
median and CF Benchmarks' index). A play needs that conservative probability to beat price + fee by ``min_edge``.

Starts in SHADOW (see omen.calibration): signals go to #kalshi-research and every evaluation is logged so its
calibration and after-fee ROI can be measured before anyone relies on it.
"""

from __future__ import annotations

import logging
import math
import statistics
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal

from omen.book import available, sweep
from omen.calibration import evaluate_gates
from omen.config import Settings
from omen.data.crypto_feeds import CryptoFeeds, SpotQuote, VolEstimate
from omen.fees.schedule import FeeUnavailable
from omen.http import HttpClient
from omen.kalshi.client import KalshiClient
from omen.kalshi.models import Market
from omen.ledger import Ledger
from omen.money import ONE, ZERO
from omen.stats import normal_cdf, scaled_t_cdf
from omen.strategies.base import MODEL, LegPlan, Opportunity
from omen.strategies.structure import Interval, interval_of

log = logging.getLogger(__name__)
NAME = "crypto_threshold"
SECONDS_PER_YEAR = 365.25 * 24 * 3600


def prob_in_interval(iv: Interval, spot: float, sigma: float, tau_years: float, nu: float) -> float:
    """P(settlement in ``iv``); log returns are Student-t(``nu``) or normal (nu = inf) with sd sigma*sqrt(tau)."""
    sd = sigma * math.sqrt(max(tau_years, 1e-9))
    mu = -0.5 * sigma * sigma * tau_years

    def cdf(level: Decimal | None, default: float) -> float:
        if level is None:
            return default
        lv = float(level)
        if lv <= 0:
            return 0.0
        z = math.log(lv / spot) - mu
        return normal_cdf(z / sd) if math.isinf(nu) else scaled_t_cdf(z, sd, nu)

    return min(1.0, max(0.0, cdf(iv.hi, 1.0) - cdf(iv.lo, 0.0)))


def probability_band(
    iv: Interval, spot: SpotQuote, vol: VolEstimate, tau_years: float, cfg
) -> tuple[float, float, float]:
    """(central, lowest, highest) P(YES).

    The band is the min/max over every combination of
      * volatility: lowest estimate / safety, median estimate, highest estimate x safety (floored);
      * spot: venue median shifted by -band, 0, +band (basis to the CF Benchmarks index);
      * tail shape: normal and Student-t.
    Low volatility and thin tails are the conservative case for "the price will get there" bets, high volatility and
    fat tails for "it will stay put" bets; taking the worst case over all of them covers both.
    """
    est = vol.estimates
    k = cfg.vol_safety_multiplier
    central_sigma = max(statistics.median(est), cfg.vol_floor_annual)
    sigmas = (min(est) / k, central_sigma, max(max(est) * k, cfg.vol_floor_annual))
    b = cfg.spot_basis_band_pct / 100
    spots = (spot.price * (1 - b), spot.price, spot.price * (1 + b))
    shapes = (math.inf, cfg.student_t_dof)
    central = prob_in_interval(iv, spot.price, central_sigma, tau_years, cfg.student_t_dof)
    grid = [prob_in_interval(iv, s, sg, tau_years, nu) for s in spots for sg in sigmas for nu in shapes]
    return central, min(grid), max(grid)


class CryptoThresholdModel:
    name = NAME

    def __init__(
        self, settings: Settings, client: KalshiClient, ledger: Ledger, engine, feeds: CryptoFeeds | None = None
    ):
        self.s = settings
        self.cfg = settings.crypto
        self.client = client
        self.ledger = ledger
        self.engine = engine  # for fee quotes (engine.fee_for) and fee refresh
        self.feeds = feeds or CryptoFeeds(HttpClient(rate_per_second=6, timeout=15))
        self.last_status: dict = {}

    # ------------------------------------------------------------------------------------------------------ run
    def run(self, now: datetime) -> tuple[list[Opportunity], set[str]]:
        self.engine.refresh_fees(now)
        by_asset: dict[str, list[Market]] = defaultdict(list)
        lo = now + timedelta(minutes=self.cfg.min_minutes_to_close)
        hi = now + timedelta(hours=self.cfg.max_hours_to_close)
        for series, asset in self.cfg.series.items():
            for m in self.client.iter_markets(series_ticker=series, status="open"):
                if m.tradable and lo < m.close_time <= hi and interval_of(m) is not None:
                    by_asset[asset].append(m)
        scope: set[str] = set()
        candidates: list[tuple[Market, str, float, float, float]] = []
        for asset, markets in by_asset.items():
            try:
                spot = self.feeds.spot(asset)
                vol = self.feeds.vol(asset)
            except Exception as exc:  # any broken feed skips the asset: never guess a price
                log.warning("crypto model: skipping %s (%s)", asset, exc)
                continue
            if (now - spot.as_of).total_seconds() > self.cfg.max_spot_age_seconds:
                continue
            for m in markets:
                scope.add(m.ticker)
                cand = self._evaluate(m, spot, vol, now)
                if cand is not None:
                    candidates.append(cand)
        opps = self._size(candidates, now)
        return opps, scope

    def _evaluate(self, m: Market, spot: SpotQuote, vol: VolEstimate, now: datetime):
        iv = interval_of(m)
        assert iv is not None
        tau = (m.close_time - now).total_seconds() / SECONDS_PER_YEAR
        central, p_lo, p_hi = probability_band(iv, spot, vol, tau, self.cfg)
        try:
            fee = self.engine.fee_for(m)
        except FeeUnavailable:
            return None
        sides = []
        if m.has_yes_ask:
            cost = m.yes_ask + fee.taker_rate_per_contract(m.yes_ask)
            sides.append(("yes", p_lo, m.yes_ask, cost, central))
        if m.has_yes_bid:
            price = ONE - m.yes_bid
            cost = price + fee.taker_rate_per_contract(price)
            sides.append(("no", 1 - p_hi, price, cost, 1 - central))
        if not sides:
            return None
        side, p_cons, price, cost, p_side_central = max(sides, key=lambda x: x[1] - float(x[3]))
        edge = p_cons - float(cost)
        would = (
            edge >= self.cfg.min_edge
            and price <= self.cfg.max_price
            and p_cons >= self.cfg.min_conservative_probability
        )
        self._log(m, now, side, central, p_cons, price, cost - price, edge, would, spot, vol, tau)
        if not would:
            return None
        return (m, side, p_cons, p_side_central, edge)

    def _log(self, m, now, side, central, p_cons, price, fee, edge, would, spot, vol, tau) -> None:
        last = self.ledger.last_prediction_ts(NAME, m.ticker)
        if would:
            if last is not None and now - last < timedelta(minutes=30):
                return
        else:
            if not 0.02 <= central <= 0.98:
                return
            if last is not None and now - last < timedelta(minutes=self.cfg.prediction_log_interval_minutes):
                return
        mid = float((m.yes_bid + m.yes_ask) / 2) if m.has_yes_bid and m.has_yes_ask else None
        self.ledger.add_prediction(
            NAME,
            m.ticker,
            m.event_ticker,
            now,
            side,
            central,
            p_cons,
            price,
            fee,
            edge,
            would,
            m.close_time,
            {
                "spot": spot.price,
                "sources": spot.sources,
                "sigma_short": vol.short,
                "sigma_long": vol.long,
                "sigma_implied": vol.implied,
                "tau_years": tau,
                "interval": interval_of(m).describe(),  # type: ignore
                "market_mid": mid,
                "yes_bid": str(m.yes_bid),
                "yes_ask": str(m.yes_ask),
            },
        )

    def _size(self, candidates, now: datetime) -> list[Opportunity]:
        if not candidates:
            return []
        books = self.client.orderbooks({m.ticker for m, *_ in candidates})
        out = []
        min_edge = Decimal(str(self.cfg.min_edge))
        for m, side, p_cons, p_central, _edge in candidates:
            book = books.get(m.ticker)
            if book is None:
                continue
            fee = self.engine.fee_for(m)
            levels = [lv for lv in book.asks(side) if lv.price <= self.cfg.max_price]
            p = Decimal(str(round(p_cons, 6)))
            best = None
            total = ZERO
            for lv in levels:
                total += lv.size
                n = total.to_integral_value(rounding="ROUND_FLOOR")
                if n < 1:
                    continue
                f = sweep(levels, n, fee)
                if f is None:
                    break
                if f.total > self.cfg.max_capital_usd:
                    n = self._max_within(levels, fee, self.cfg.max_capital_usd)
                    f = sweep(levels, n, fee) if n >= 1 else None
                    if f is None:
                        break
                if p - f.total / n < min_edge:
                    break
                if best is None or p * n - f.total > p * best.count - best.total:
                    best = f
                if f.total >= self.cfg.max_capital_usd:
                    break
            if best is None:
                continue
            plan = LegPlan(m, side, best, fee, available(levels))
            tau_h = (m.close_time - now).total_seconds() / 3600
            out.append(
                Opportunity(
                    strategy=NAME,
                    tier=MODEL,
                    title=f"{m.title} · {side.upper()} {m.yes_sub_title}",
                    event_ticker=m.event_ticker,
                    legs=[plan],
                    sets=best.count,
                    payout_min=ZERO,
                    payout_max=best.count * ONE,
                    settles_at=m.close_time,
                    found_at=now,
                    confidence=p_cons,
                    probability=p_cons,
                    model_probability=p_central,
                    rationale=[
                        f"Settles in {tau_h:.1f} h on a CF Benchmarks 60-second average.",
                        "Probability used is the worst case over the volatility range (lowest estimate ÷"
                        f" {self.cfg.vol_safety_multiplier:g} to highest × {self.cfg.vol_safety_multiplier:g}), normal"
                        f" and fat (Student-t ν={self.cfg.student_t_dof:g}) tails, and a ±"
                        f"{self.cfg.spot_basis_band_pct:g}% spot basis band.",
                        f"Conservative edge after fees ≥ {self.cfg.min_edge:.0%} per contract; worst-case win "
                        f"probability ≥ {self.cfg.min_conservative_probability:.0%}.",
                    ],
                    risks=[
                        "Model risk: crypto can gap on news faster than any volatility estimate.",
                        "Spot is a median of Coinbase/Kraken/Bitstamp, not the CF Benchmarks index itself.",
                    ],
                    evidence={"model": NAME},
                )
            )
        return out

    @staticmethod
    def _max_within(levels, fee, cap: Decimal) -> Decimal:
        lo, hi = 0, int(available(levels))
        while lo < hi:
            mid = (lo + hi + 1) // 2
            f = sweep(levels, Decimal(mid), fee)
            if f is not None and f.total <= cap:
                lo = mid
            else:
                hi = mid - 1
        return Decimal(lo)

    # --------------------------------------------------------------------------------------------------- status
    def status(self) -> dict:
        verdict = evaluate_gates(self.ledger.resolved_predictions(NAME), self.s.gates)
        state = self.ledger.model_state(NAME)
        return {"state": state, "summary": verdict.summary, "passed": verdict.passed}
