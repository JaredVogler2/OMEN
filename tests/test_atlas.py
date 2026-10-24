"""Calibration atlas: observations from price history, series-clustered statistics, validated edges only."""

import random

from omen import atlas

DAY = 86400


def market(i, category, series, result, ask, bid, close_ts, hours_before=24):
    t = close_ts - hours_before * 3600
    return {
        "ticker": f"{series}-{i}",
        "event_ticker": f"{series}-E{i}",
        "series": series,
        "category": category,
        "result": result,
        "close_ts": close_ts,
        "candles": [{"t": t, "bid": f"{bid:.2f}", "ask": f"{ask:.2f}"}],
    }


def test_observations_take_the_price_a_taker_pays():
    m = market(1, "Weather", "KXHIGHNY", "no", ask=0.12, bid=0.10, close_ts=100 * DAY)
    obs = atlas.observations([m])
    assert {(o.side, round(o.price, 2), o.won, o.horizon) for o in obs} == {
        ("yes", 0.12, False, "24h"),  # YES at the ask
        ("no", 0.90, True, "24h"),  # NO at 1 - bid
    }


def test_wide_or_one_sided_markets_are_ignored():
    wide = market(1, "Weather", "S", "yes", ask=0.60, bid=0.30, close_ts=100 * DAY)
    empty = market(2, "Weather", "S", "yes", ask=0.60, bid=0.0, close_ts=100 * DAY)
    assert atlas.observations([wide, empty]) == []


def _synthetic(edge_category, fake_category, n_series=30, per_series=6, seed=3):
    """Longshots at ~8c. In `edge_category` they win 20% of the time all along (a real edge); in `fake_category` they
    win 30% early and 0% late (a pattern that does not survive validation); elsewhere they win 8% (fair)."""
    rng = random.Random(seed)
    rows, i = [], 0
    for cat in (edge_category, fake_category, "Sports"):
        for s in range(n_series):
            for k in range(per_series):
                close = (10 + k * 5 + rng.random()) * DAY  # spread over ~40 days
                late = k >= 4
                p_win = 0.20 if cat == edge_category else (0.0 if late else 0.30) if cat == fake_category else 0.08
                result = "yes" if rng.random() < p_win else "no"
                rows.append(market(i, cat, f"{cat}{s}", result, ask=0.08, bid=0.07, close_ts=close))
                i += 1
    return rows


def test_only_edges_that_survive_validation_are_reported():
    obs = atlas.observations(_synthetic("Weather", "Politics"))
    cells, _ = atlas.build_cells(obs, min_n=40, min_series=10, min_validation=20)
    edges = {(c.category, c.band) for c in cells if c.edge}
    assert ("Weather", (0.05, 0.10)) in edges  # real: 20% wins at 8c
    assert all(cat != "Politics" for cat, _ in edges)  # looked great in discovery, failed validation
    assert all(cat != "Sports" for cat, _ in edges)  # fairly priced
    weather = next(c for c in cells if c.category == "Weather" and c.band == (0.05, 0.10))
    assert weather.discovery.ev_lo > 0 and weather.validation.ev > 0
    assert abs(weather.discovery.excess - 0.12) < 0.06  # wins ~20% at ~8c


def test_bootstrap_resamples_series_not_markets():
    # one lucky series with many wins must not make a cell look certain
    rows = [market(i, "Weather", "LUCKY", "yes", 0.08, 0.07, (10 + i % 30) * DAY) for i in range(60)]
    rows += [market(100 + i, "Weather", f"S{i}", "no", 0.08, 0.07, (10 + i % 30) * DAY) for i in range(60)]
    stats = atlas.cell_stats([o for o in atlas.observations(rows) if o.side == "yes"])
    assert stats.series == 61 and stats.ev > 0
    assert stats.ev_lo < 0  # the interval admits that it was one series


def test_collect_samples_settled_markets_and_reads_their_price_history():
    import json as _json
    from datetime import UTC, datetime
    from urllib.parse import parse_qs, urlsplit

    import httpx

    from omen.http import HttpClient

    now = datetime(2026, 10, 24, tzinfo=UTC)
    seen = []

    def handler(request):
        u = urlsplit(str(request.url))
        q = parse_qs(u.query)
        seen.append((u.path, q))
        if u.path.endswith("/markets"):
            day_end = int(q["max_close_ts"][0])
            markets = [
                {
                    "ticker": f"KXHIGHNY-{day_end}-{i}",
                    "event_ticker": f"KXHIGHNY-{day_end}",
                    "title": "High?",
                    "open_time": "2026-10-01T00:00:00Z",
                    "close_time": datetime.fromtimestamp(day_end - 3600, UTC).isoformat().replace("+00:00", "Z"),
                    "result": "yes" if i == 0 else "no",
                    "volume_fp": "100",
                }
                for i in range(3)
            ]
            return httpx.Response(200, json={"markets": markets, "cursor": ""})
        if "/candlesticks" in u.path:
            end = int(q["end_ts"][0])
            return httpx.Response(
                200,
                json={
                    "candlesticks": [
                        {
                            "end_period_ts": end - 86400,
                            "yes_bid": {"close_dollars": "0.30"},
                            "yes_ask": {"close_dollars": "0.33"},
                        }
                    ]
                },
            )
        return httpx.Response(404)

    http = HttpClient(
        "https://api.test/trade-api/v2",
        rate_per_second=1000,
        transport=httpx.MockTransport(handler),
        sleep=lambda _: None,
    )
    rows = atlas.collect(http, {"KXHIGHNY": "Climate and Weather"}, now, days=7, day_step=3, per_series=4, per_event=2)
    listing = [q for p, q in seen if p.endswith("/markets")]
    assert len(listing) == 2 and all(q["status"] == ["settled"] and q["mve_filter"] == ["exclude"] for q in listing)
    assert len(rows) == 4  # capped at 4 per series, 2 per event
    assert all(r["category"] == "Weather" and r["candles"] for r in rows)
    obs = atlas.observations(rows)
    assert obs and all(o.horizon == "24h" for o in obs)
    assert _json.dumps(atlas.atlas_document(rows, now))  # serializable
