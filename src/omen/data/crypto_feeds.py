"""Free, key-less crypto market data: spot (median of three venues), realized volatility, Deribit implied vol.

Kalshi crypto markets settle on CF Benchmarks Real-Time Indices (60-second averages), which aggregate the same
venues. The median of Coinbase, Kraken and Bitstamp mid prices tracks them closely; the model adds a basis band.
"""

from __future__ import annotations

import itertools
import logging
import math
import statistics
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from omen.http import HttpClient, HttpError

log = logging.getLogger(__name__)

COINBASE = "https://api.exchange.coinbase.com"
KRAKEN = "https://api.kraken.com/0/public"
BITSTAMP = "https://www.bitstamp.net/api/v2"
DERIBIT = "https://www.deribit.com/api/v2/public"

KRAKEN_PAIRS = {"BTC": "XBTUSD", "ETH": "ETHUSD", "SOL": "SOLUSD", "XRP": "XRPUSD", "DOGE": "XDGUSD"}
BITSTAMP_PAIRS = {"BTC": "btcusd", "ETH": "ethusd", "SOL": "solusd", "XRP": "xrpusd", "DOGE": "dogeusd"}
DVOL = {"BTC", "ETH"}
MINUTES_PER_YEAR = 365 * 24 * 60


class FeedError(RuntimeError):
    pass


@dataclass(frozen=True)
class SpotQuote:
    asset: str
    price: float
    sources: dict[str, float]
    as_of: datetime
    dispersion_pct: float


@dataclass(frozen=True)
class VolEstimate:
    short: float  # annualized, last ~2h of 1-minute returns
    long: float  # annualized, last ~7d of hourly returns
    implied: float | None  # Deribit DVOL (30-day implied), BTC/ETH only

    @property
    def estimates(self) -> list[float]:
        return [v for v in (self.short, self.long, self.implied) if v is not None and v > 0]

    @property
    def central(self) -> float:
        """Median of the available estimates (implied vol carries a risk premium; short realized is noisy)."""
        return statistics.median(self.estimates)


class CryptoFeeds:
    def __init__(self, http: HttpClient, clock=lambda: datetime.now(UTC), cache_seconds: int = 300):
        self.http = http
        self.clock = clock
        self.cache_seconds = cache_seconds
        self._vol_cache: dict[str, tuple[float, VolEstimate]] = {}

    def spot(self, asset: str, max_dispersion_pct: float = 0.5) -> SpotQuote:
        sources: dict[str, float] = {}
        try:
            d = self.http.get_json(f"{COINBASE}/products/{asset}-USD/ticker")
            sources["coinbase"] = (float(d["bid"]) + float(d["ask"])) / 2
        except (HttpError, KeyError, ValueError) as exc:
            log.debug("coinbase %s: %s", asset, exc)
        if asset in KRAKEN_PAIRS:
            try:
                d = self.http.get_json(f"{KRAKEN}/Ticker", params={"pair": KRAKEN_PAIRS[asset]})
                row = next(iter((d.get("result") or {}).values()))
                sources["kraken"] = (float(row["b"][0]) + float(row["a"][0])) / 2
            except (HttpError, KeyError, ValueError, StopIteration) as exc:
                log.debug("kraken %s: %s", asset, exc)
        if asset in BITSTAMP_PAIRS:
            try:
                d = self.http.get_json(f"{BITSTAMP}/ticker/{BITSTAMP_PAIRS[asset]}/")
                if time.time() - float(d.get("timestamp", 0)) < 120:
                    sources["bitstamp"] = (float(d["bid"]) + float(d["ask"])) / 2
            except (HttpError, KeyError, ValueError) as exc:
                log.debug("bitstamp %s: %s", asset, exc)
        if len(sources) < 2:
            raise FeedError(f"{asset}: only {len(sources)} spot source(s) available")
        prices = list(sources.values())
        mid = statistics.median(prices)
        dispersion = (max(prices) - min(prices)) / mid * 100
        if dispersion > max_dispersion_pct:
            raise FeedError(f"{asset}: venues disagree by {dispersion:.2f}% ({sources})")
        return SpotQuote(asset, mid, sources, self.clock(), dispersion)

    def vol(self, asset: str) -> VolEstimate:
        hit = self._vol_cache.get(asset)
        if hit and time.monotonic() - hit[0] < self.cache_seconds:
            return hit[1]
        short = self._realized(asset, granularity=60, count=120)
        long = self._realized(asset, granularity=3600, count=168)
        implied = self._dvol(asset) if asset in DVOL else None
        est = VolEstimate(short, long, implied)
        self._vol_cache[asset] = (time.monotonic(), est)
        return est

    def _realized(self, asset: str, granularity: int, count: int) -> float:
        rows = self.http.get_json(f"{COINBASE}/products/{asset}-USD/candles", params={"granularity": granularity})
        closes = [float(r[4]) for r in sorted(rows, key=lambda r: r[0])][-(count + 1) :]
        if len(closes) < count // 2:
            raise FeedError(f"{asset}: not enough candles for volatility ({len(closes)})")
        rets = [math.log(b / a) for a, b in itertools.pairwise(closes) if a > 0 and b > 0]
        var = sum(r * r for r in rets) / len(rets)  # zero-mean estimator: robust for short windows
        periods_per_year = MINUTES_PER_YEAR / (granularity / 60)
        return math.sqrt(var * periods_per_year)

    def _dvol(self, asset: str) -> float | None:
        end = int(time.time() * 1000)
        try:
            d = self.http.get_json(
                f"{DERIBIT}/get_volatility_index_data",
                params={
                    "currency": asset,
                    "resolution": "3600",
                    "start_timestamp": end - 6 * 3600_000,
                    "end_timestamp": end,
                },
            )
            data = (d.get("result") or {}).get("data") or []
            return float(data[-1][4]) / 100 if data else None
        except (HttpError, KeyError, ValueError, IndexError) as exc:
            log.debug("deribit %s: %s", asset, exc)
            return None
