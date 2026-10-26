from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from omen.config import Settings
from omen.http import HttpClient
from omen.kalshi.client import KalshiClient
from omen.liquidity.scout import scout

START = datetime(2026, 10, 28, 4, tzinfo=UTC)  # Wed Oct 28 00:00 ET


def iso(t: datetime) -> str:
    return t.isoformat().replace("+00:00", "Z")


def program(ticker: str, start: datetime, hours: float, reward: float, target: float = 1000) -> dict:
    return {
        "market_ticker": ticker,
        "incentive_type": "liquidity",
        "start_date": iso(start),
        "end_date": iso(start + timedelta(hours=hours)),
        "period_reward": int(reward * 10_000),
        "target_size_fp": f"{target:.2f}",
        "discount_factor_bps": 5000,
    }


class FakeKalshi:
    def __init__(self, upcoming: list[dict], active: list[dict], closes: dict[str, datetime], books: dict[str, dict]):
        self.programs = {"upcoming": upcoming, "active": active}
        self.closes = closes
        self.books = books

    def handler(self, request: httpx.Request) -> httpx.Response:
        u = urlsplit(str(request.url))
        q = parse_qs(u.query)
        if u.path.endswith("/incentive_programs"):
            return httpx.Response(200, json={"incentive_programs": self.programs[q["status"][0]], "next_cursor": ""})
        if u.path.endswith("/markets/orderbooks"):
            rows = [{"ticker": t, "orderbook_fp": self.books[t]} for t in q.get("tickers", []) if t in self.books]
            return httpx.Response(200, json={"orderbooks": rows})
        if u.path.endswith("/markets"):
            tickers = q.get("tickers", [""])[0].split(",")
            rows = [
                {
                    "ticker": t,
                    "event_ticker": t.rsplit("-", 1)[0],
                    "status": "active",
                    "market_type": "binary",
                    "close_time": iso(self.closes[t]),
                }
                for t in tickers
                if t in self.closes
            ]
            return httpx.Response(200, json={"markets": rows, "cursor": ""})
        return httpx.Response(404)


def client(fake: FakeKalshi) -> KalshiClient:
    return KalshiClient(
        HttpClient("https://api.test/trade-api/v2", rate_per_second=1000, transport=httpx.MockTransport(fake.handler))
    )


def settings(**overrides) -> Settings:
    s = Settings()
    lp = s.liquidity
    lp.min_program_hours, lp.max_program_hours = 24.0, 168.0
    lp.dead_side_min_period_reward_usd, lp.dead_side_max_target, lp.dead_side_close_gap_hours = 30.0, 1000.0, 24.0
    for k, v in overrides.items():
        setattr(lp, k, v)
    return s


DECIDED_EMPTY = {"yes_dollars": [["0.9700", "2000.00"]], "no_dollars": []}  # favorite at 97c, nobody on NO
DECIDED_SHARED = {"yes_dollars": [["0.9800", "2000.00"]], "no_dollars": [["0.0100", "50.00"]]}


def world() -> FakeKalshi:
    day = START
    active = [
        program("KXA-1", day, 24, 70),  # 1-day, $70/day, ends at close: counts at full weight
        program("KXB-1", day, 168, 210),  # 7-day, $30/day, ends a day before close: counts at 0.3
        program("KXC-1", day, 168, 210),  # ends 3 days before its market closes: does not count
        program("KXD-1", day, 24, 20),  # $20 a period: under the $30 floor
        program("KXE-1", day, 24, 70, target=2500),  # a side the $12 cap cannot complete
        program("KXCRYPTOLEAD15M-1", day, 24, 70),  # an excluded fast series
        program("KXF-1", day, 12, 70),  # shorter than a day
    ]
    closes = {
        "KXA-1": day + timedelta(hours=24),
        "KXB-1": day + timedelta(hours=168 + 20),
        "KXC-1": day + timedelta(hours=168 + 72),
        "KXD-1": day + timedelta(hours=24),
        "KXE-1": day + timedelta(hours=24),
        "KXCRYPTOLEAD15M-1": day + timedelta(hours=24),
        "KXF-1": day + timedelta(hours=12),
    }
    return FakeKalshi([], active, closes, {"KXA-1": DECIDED_EMPTY, "KXB-1": DECIDED_SHARED})


def test_the_scout_counts_only_what_omen_could_trade_and_weights_long_programs():
    r = scout(client(world()), settings().liquidity, START)
    assert sorted(p.ticker for p in r.qualifying) == ["KXA-1", "KXB-1"]
    # KXA: $70/day for 1 of 7 days; KXB: $30/day all week, at 0.3
    assert r.raw_pool_per_day == pytest.approx(70 / 7 + 30)
    assert r.pool_per_day == pytest.approx(70 / 7 + 30 * 0.3)
    assert r.empty_dead_sides == ["KXA-1"]  # KXB's dead side already has a bid
    assert not r.go and len(r.reasons) == 3  # pool, breadth and the unconfirmed per-market rewards


def test_the_scout_goes_only_when_every_condition_holds():
    lp = settings(scout_go_pool_per_day_usd=15.0, scout_go_markets=2, scout_go_series=2).liquidity
    assert scout(client(world()), lp, START).reasons == [
        "per-market rewards not confirmed against Kalshi's Liquidity Rewards page (scout_rewards_confirmed)"
    ]
    lp.scout_rewards_confirmed = True
    assert scout(client(world()), lp, START).go


def test_the_scout_flags_groups_that_look_like_one_pooled_reward_and_counts_daily_fuel():
    upcoming = [program(f"KXP-{i}", START + timedelta(days=1), 168, 300) for i in range(6)]
    upcoming.append(program("KXAAAGASDTX-1", START, 24, 100))
    fake = FakeKalshi(upcoming, [], {}, {})
    r = scout(client(fake), settings().liquidity, START)
    assert r.pooled_groups == 1 and r.daily_fuel == 1 and not r.qualifying
