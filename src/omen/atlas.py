"""The calibration atlas: where Kalshi's prices have been right, and where they have been wrong after fees.

Method (all public data, no account):

1. Sample settled markets from whole days spread over the window (every ``day_step``-th day), capped per series and
   per event so no single ladder or game dominates; combo (multivariate) markets are excluded.
2. For each market, read its hourly bid/ask history and, at each horizon before the close (1 h, 6 h, 24 h, 72 h),
   take the price a buyer would actually have paid: YES at the ask, NO at 1 - bid, on a real two-sided market
   (spread at most 10c).
3. Group by category x horizon x price band and measure, per contract, win rate - price - taker fee.
4. Confidence intervals come from a bootstrap that resamples whole series (markets of one series are not
   independent). Cells are found on the older two thirds of the window ("discovery") and must also be profitable on
   the newest third ("validation"), which the search never saw.

Only cells that pass both become **edges**; everything else is still shown, because "the crowd is right here" is
useful too.
"""

from __future__ import annotations

import logging
import random
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from omen.http import HttpClient

log = logging.getLogger(__name__)

HORIZONS = {"1h": 3600, "6h": 6 * 3600, "24h": 86400, "72h": 3 * 86400}
BANDS = [
    (0.01, 0.05),
    (0.05, 0.10),
    (0.10, 0.20),
    (0.20, 0.40),
    (0.40, 0.60),
    (0.60, 0.80),
    (0.80, 0.90),
    (0.90, 0.95),
    (0.95, 0.99),
]
CATEGORIES = {
    "Climate and Weather": "Weather",
    "Sports": "Sports",
    "Crypto": "Crypto",
    "Economics": "Economics",
    "Financials": "Financials",
    "Commodities": "Financials",
    "Politics": "Politics",
    "Elections": "Politics",
    "Entertainment": "Entertainment",
    "Social": "Entertainment",
    "Companies": "Companies",
    "Science and Technology": "Science/Tech",
    "World": "World",
    "Health": "Health",
    "Mentions": "Mentions",
}
MAX_SPREAD = 0.10


def category_of(raw: str | None) -> str:
    return CATEGORIES.get(raw or "", "Other")


def taker_fee(p: float) -> float:
    """Kalshi's standard taker fee per contract (quadratic, x1), before rounding the fill up to the cent."""
    return 0.07 * p * (1 - p)


def band_of(p: float) -> tuple[float, float] | None:
    for lo, hi in BANDS:
        if lo <= p < hi:
            return (lo, hi)
    return None


def _ts(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


# ------------------------------------------------------------------------------------------------------ collect
def collect(
    http: HttpClient,
    series_categories: dict[str, str],
    now: datetime,
    days: int = 45,
    day_step: int = 3,
    per_series: int = 6,
    per_event: int = 2,
    min_volume: float = 50,
    max_markets: int | None = None,
    seed: int = 42,
    progress: Callable[[str], None] = lambda _: None,
) -> list[dict[str, Any]]:
    """Sampled settled markets with hourly bid/ask candles over their last 8 days (one request per market)."""
    pool: list[dict[str, Any]] = []
    for k in range(1, days, day_step):
        day_end = int((now - timedelta(days=k)).timestamp())
        cursor = ""
        while True:
            params: dict[str, Any] = {
                "status": "settled",
                "limit": 1000,
                "mve_filter": "exclude",
                "min_close_ts": day_end - 86400,
                "max_close_ts": day_end,
            }
            if cursor:
                params["cursor"] = cursor
            d = http.get_json("/markets", params=params)
            for m in d.get("markets") or []:
                if m.get("result") in ("yes", "no") and float(m.get("volume_fp") or 0) >= min_volume:
                    pool.append(
                        {
                            "ticker": m["ticker"],
                            "event_ticker": m["event_ticker"],
                            "title": m.get("title") or "",
                            "open_time": m["open_time"],
                            "close_time": m["close_time"],
                            "result": m["result"],
                        }
                    )
            cursor = d.get("cursor") or ""
            if not cursor:
                break
        progress(f"listed day -{k}: {len(pool)} settled markets so far")
    rng = random.Random(seed)
    rng.shuffle(pool)
    seen_series: dict[str, int] = defaultdict(int)
    seen_event: dict[str, int] = defaultdict(int)
    sample = []
    for m in pool:
        series = m["event_ticker"].split("-")[0]
        if seen_series[series] >= per_series or seen_event[m["event_ticker"]] >= per_event:
            continue
        seen_series[series] += 1
        seen_event[m["event_ticker"]] += 1
        m["series"] = series
        m["category"] = category_of(series_categories.get(series))
        sample.append(m)
        if max_markets and len(sample) >= max_markets:
            break
    progress(f"sampled {len(sample)} markets from {len(seen_series)} series")
    out = []
    for i, m in enumerate(sample):
        close = _ts(m["close_time"])
        start = max(_ts(m["open_time"]), close - 8 * 86400)
        try:
            d = http.get_json(
                f"/series/{m['series']}/markets/{m['ticker']}/candlesticks",
                params={"start_ts": start, "end_ts": close, "period_interval": 60},
            )
        except Exception as exc:  # one bad market never sinks the study
            log.debug("skip %s: %s", m["ticker"], exc)
            continue
        m["close_ts"] = close
        m["candles"] = [
            {
                "t": c["end_period_ts"],
                "bid": c["yes_bid"].get("close_dollars"),
                "ask": c["yes_ask"].get("close_dollars"),
            }
            for c in d.get("candlesticks") or []
        ]
        out.append(m)
        if i % 500 == 0:
            progress(f"price history {i}/{len(sample)}")
    return out


# ------------------------------------------------------------------------------------------------------ analyse
@dataclass(frozen=True)
class Obs:
    category: str
    horizon: str
    band: tuple[float, float]
    side: str
    price: float
    won: bool
    series: str
    event: str
    close_ts: int


def observations(markets: list[dict[str, Any]]) -> list[Obs]:
    """What a taker would have paid at each horizon, and whether it won."""
    out = []
    for m in markets:
        candles = m.get("candles") or []
        if not candles:
            continue
        for name, h in HORIZONS.items():
            target = m["close_ts"] - h
            c = min(candles, key=lambda x: abs(x["t"] - target))
            if abs(c["t"] - target) > 1800 or c["bid"] is None or c["ask"] is None:
                continue
            bid, ask = float(c["bid"]), float(c["ask"])
            if not (0 < bid < ask < 1) or ask - bid > MAX_SPREAD:
                continue
            for side, price in (("yes", ask), ("no", 1 - bid)):
                band = band_of(price)
                if band is not None:
                    out.append(
                        Obs(
                            m["category"],
                            name,
                            band,
                            side,
                            price,
                            m["result"] == side,
                            m["series"],
                            m["event_ticker"],
                            m["close_ts"],
                        )
                    )
    return out


@dataclass
class CellStats:
    n: int
    series: int
    win_rate: float
    avg_price: float
    ev: float  # per contract after the taker fee
    ev_lo: float  # series-bootstrap 95% interval
    ev_hi: float
    excess: float  # win rate - average price (calibration error, before fees)


def cell_stats(items: list[Obs], boot: int = 400, seed: int = 1) -> CellStats | None:
    if not items:
        return None
    pnl = [(1.0 if o.won else 0.0) - o.price - taker_fee(o.price) for o in items]
    by_series: dict[str, list[float]] = defaultdict(list)
    for o, x in zip(items, pnl, strict=True):
        by_series[o.series].append(x)
    groups = list(by_series.values())
    rng = random.Random(seed)
    means = []
    for _ in range(boot):
        draw = [groups[rng.randrange(len(groups))] for _ in groups]
        means.append(sum(sum(g) for g in draw) / sum(len(g) for g in draw))
    means.sort()
    n = len(items)
    win = sum(o.won for o in items) / n
    avg = sum(o.price for o in items) / n
    return CellStats(
        n, len(groups), win, avg, sum(pnl) / n, means[int(0.025 * boot)], means[int(0.975 * boot) - 1], win - avg
    )


@dataclass
class Cell:
    category: str
    horizon: str
    band: tuple[float, float]
    discovery: CellStats
    validation: CellStats | None
    edge: bool


def build_cells(
    obs: list[Obs], min_n: int = 60, min_series: int = 12, min_validation: int = 25, by_category: bool = True
) -> tuple[list[Cell], int]:
    """Cells keyed by (category, horizon, band). Discovery = closes before the 2/3 time split; validation = after.
    A cell is an **edge** when discovery's whole 95% interval is above zero, over enough distinct series, and the
    validation period (never used for the search) is profitable too."""
    if not obs:
        return [], 0
    closes = sorted(o.close_ts for o in obs)
    split = closes[int(len(closes) * 2 / 3)]
    groups: dict[tuple, list[Obs]] = defaultdict(list)
    for o in obs:
        groups[(o.category if by_category else "All", o.horizon, o.band)].append(o)
    cells = []
    for (cat, hz, band), items in groups.items():
        disc = cell_stats([o for o in items if o.close_ts < split])
        if disc is None or disc.n < min_n:
            continue
        val_items = [o for o in items if o.close_ts >= split]
        val = cell_stats(val_items, boot=200) if len(val_items) >= min_validation else None
        edge = disc.ev_lo > 0 and disc.series >= min_series and val is not None and val.ev > 0
        cells.append(Cell(cat, hz, band, disc, val, edge))
    cells.sort(key=lambda c: (c.category, list(HORIZONS).index(c.horizon), c.band))
    return cells, split


def atlas_document(markets: list[dict[str, Any]], generated: datetime) -> dict[str, Any]:
    obs = observations(markets)
    by_cat, split = build_cells(obs)
    overall, _ = build_cells(obs, by_category=False)
    return {
        "version": 1,
        "generated_at": generated.astimezone(UTC).isoformat(timespec="seconds"),
        "method": __doc__.split("\n\n")[1] if __doc__ else "",
        "markets": len(markets),
        "series": len({m["series"] for m in markets}),
        "observations": len(obs),
        "validation_from": datetime.fromtimestamp(split, UTC).isoformat(timespec="seconds") if split else None,
        "cells": [_cell_json(c) for c in by_cat],
        "overall": [_cell_json(c) for c in overall],
        "edges": [_cell_json(c) for c in by_cat if c.edge],
    }


def _cell_json(c: Cell) -> dict[str, Any]:
    return {
        "category": c.category,
        "horizon": c.horizon,
        "band": list(c.band),
        "discovery": asdict(c.discovery),
        "validation": asdict(c.validation) if c.validation else None,
        "edge": c.edge,
    }


def run(http: HttpClient, series_categories: dict[str, str], days: int, progress=print) -> dict[str, Any]:
    t0 = time.monotonic()
    now = datetime.now(UTC)
    markets = collect(http, series_categories, now, days=days, progress=progress)
    doc = atlas_document(markets, now)
    doc["seconds"] = round(time.monotonic() - t0)
    return doc
