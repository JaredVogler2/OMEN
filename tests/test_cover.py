"""Cross-series cover locks: thresholds and buckets from different series that settle on the same number."""

import itertools
import random
from datetime import timedelta
from decimal import Decimal as D

from helpers import NOW, book, market, registry
from omen.config import LockSettings
from omen.strategies import cover
from omen.strategies.lock import VerifyContext, near_misses, screen_covers, verify

REG = registry()
SETTINGS = LockSettings(min_profit_usd=D("0.50"), min_return_pct=D("0.5"), min_apr_pct=D("0"))
SOURCE = "If the simple average of the sixty seconds of CF Benchmarks' SOLUSD_RTI before 5 PM EDT is"
TAIL = "at 5 PM EDT on Oct 30, 2026, then the market resolves to Yes."


def fee(m):
    return REG.quote("KXTEST", m.event_ticker, NOW)


def above(floor, bid="0", ask="0", source=SOURCE, event="KXSOLD-26OCT30", **kw):
    return market(
        f"{event}-T{floor}",
        event,
        strike_type="greater",
        floor=floor,
        yes_bid=bid,
        yes_ask=ask,
        rules=f"{source} above ${floor} {TAIL}",
        sub=f"above {floor}",
        **kw,
    )


def bucket(lo, hi, bid="0", ask="0", event="KXSOLE-26OCT30", rules2="", **kw):
    return market(
        f"{event}-B{lo}",
        event,
        strike_type="between",
        floor=lo,
        cap=hi,
        yes_bid=bid,
        yes_ask=ask,
        rules=f"{SOURCE} between ${lo}-{hi} {TAIL}",
        rules2=rules2,
        sub=f"{lo} to {hi}",
        **kw,
    )


def mispriced_bucket():
    """P(> 70.2499) ~ 0.75 and P(> 70.4999) ~ 0.45 imply ~0.30 for the bucket between, but it is offered at 0.20."""
    return [
        above("70.2499", bid="0.75", ask="0.77"),
        above("70.4999", bid="0.43", ask="0.45"),
        bucket("70.25", "70.4999", bid="0.18", ask="0.20"),
    ]


def test_settlement_key_pairs_series_on_the_same_number_only():
    t, _, b = mispriced_bucket()
    assert cover.settlement_key(t) == cover.settlement_key(b) is not None
    other_index = above("70.2499", source=SOURCE.replace("SOLUSD", "ETHUSD"))
    assert cover.settlement_key(other_index) != cover.settlement_key(t)
    later = above("70.2499", close=NOW + timedelta(days=3))
    assert cover.settlement_key(later) != cover.settlement_key(t)


def test_three_leg_cover_across_series_is_a_lock():
    ms = mispriced_bucket()
    cands, rejected = screen_covers({}, ms, fee, allow_step_gaps=True)
    assert rejected == [] and len(cands) == 1
    c = cands[0]
    assert c.strategy == "cover" and c.payout_per_set == 1
    assert {(m.ticker, side) for m, side in c.legs} == {
        ("KXSOLD-26OCT30-T70.2499", "no"),
        ("KXSOLE-26OCT30-B70.25", "yes"),
        ("KXSOLD-26OCT30-T70.4999", "yes"),
    }
    assert any("reporting step" in r for r in c.risks)  # 70.2499 / 70.25 meet one step apart
    books = {
        "KXSOLD-26OCT30-T70.2499": book("KXSOLD-26OCT30-T70.2499", yes=[("0.75", "100")], no=[("0.23", "100")]),
        "KXSOLD-26OCT30-T70.4999": book("KXSOLD-26OCT30-T70.4999", yes=[("0.43", "100")], no=[("0.55", "100")]),
        "KXSOLE-26OCT30-B70.25": book("KXSOLE-26OCT30-B70.25", yes=[("0.18", "100")], no=[("0.80", "100")]),
    }
    opp = verify(c, VerifyContext(books=books, fee=fee, settings=SETTINGS, now=NOW))
    assert opp is not None and opp.sets == 100
    assert opp.payout_min == D("100")  # 1 per set whatever SOL settles at
    assert opp.cost == D("90")  # NO 0.25 + YES 0.20 + YES 0.45 per set
    assert opp.profit == D("100") - D("90") - opp.fees and opp.profit > 0


def test_step_gap_join_needs_permission():
    cands, _ = screen_covers({}, mispriced_bucket(), fee, allow_step_gaps=False)
    assert cands == []


def test_double_cover_pays_two_without_precision_risk():
    ms = [
        above("70.2499", bid="0.68", ask="0.70"),
        above("70.4999", bid="0.45", ask="0.47"),
        bucket("70.25", "70.4999", bid="0.40", ask="0.42"),  # bucket bid far above the ~0.23 the thresholds imply
    ]
    cands, _ = screen_covers({}, ms, fee, allow_step_gaps=False)
    assert len(cands) == 1
    c = cands[0]
    assert c.payout_per_set == 2 and not any("reporting step" in r for r in c.risks)
    assert {(m.ticker, side) for m, side in c.legs} == {
        ("KXSOLE-26OCT30-B70.25", "no"),
        ("KXSOLD-26OCT30-T70.2499", "yes"),
        ("KXSOLD-26OCT30-T70.4999", "no"),
    }


def test_single_event_structures_are_left_to_ladders_and_baskets():
    ms = [
        above("70.2499", bid="0.75", ask="0.77", event="KXSOLD-26OCT30"),
        above("70.4999", bid="0.43", ask="0.45", event="KXSOLD-26OCT30"),
        bucket("70.25", "70.4999", bid="0.18", ask="0.20", event="KXSOLD-26OCT30"),
    ]
    assert screen_covers({}, ms, fee, allow_step_gaps=True)[0] == []


def test_fair_price_settlement_clause_rejects_the_cover():
    ms = mispriced_bucket()
    ms[2] = bucket("70.25", "70.4999", bid="0.18", ask="0.20", rules2="Otherwise it settles at the last fair price.")
    cands, rejected = screen_covers({}, ms, fee, allow_step_gaps=True)
    assert cands == [] and "fair price" in rejected[0][1]


def test_fairly_priced_markets_show_up_as_near_misses_only():
    ms = [
        above("70.2499", bid="0.74", ask="0.76"),
        above("70.4999", bid="0.44", ask="0.46"),
        bucket("70.25", "70.4999", bid="0.29", ask="0.31"),
    ]
    assert screen_covers({}, ms, fee, allow_step_gaps=True)[0] == []
    rows = [r for r in near_misses({}, {}, ms, fee) if r["strategy"] == "cover"]
    assert len(rows) == 1 and rows[0]["edge"] < 0 and rows[0]["legs"] == 3


def test_every_reported_cover_really_pays_its_minimum_on_every_outcome():
    """Randomized safety net: whatever the prices, a reported cover never pays less than promised at any value the
    settlement can take (multiples of the reporting step)."""
    rng = random.Random(7)
    step = D("0.0001")
    found = 0
    for _ in range(300):
        edges = sorted({D(rng.randint(0, 12)) * D("0.25") + D("70") for _ in range(6)})
        ms = []
        for e in edges:  # "e or above" thresholds, Kalshi style: > e - step
            p = D(rng.randint(2, 97)) / 100
            ms.append(above(f"{e - step}", bid=str(p), ask=str(min(D("0.99"), p + D("0.02")))))
        for lo, hi in itertools.pairwise(edges):
            p = D(rng.randint(2, 60)) / 100
            ms.append(bucket(f"{lo}", f"{hi - step}", bid=str(p), ask=str(p + D("0.01"))))
        for c in screen_covers({}, ms, fee, allow_step_gaps=True)[0]:
            found += 1
            ivs = [(cover.interval_of(m), side) for m, side in c.legs]
            for k in range(-200, 1500):
                x = D("70") + step * 4 * k + step * (k % 4)  # values on the reporting grid, incl. boundaries
                pays = 0
                for iv, side in ivs:
                    inside = (iv.lo is None or x > iv.lo or (x == iv.lo and iv.lo_closed)) and (
                        iv.hi is None or x < iv.hi or (x == iv.hi and iv.hi_closed)
                    )
                    pays += inside if side == "yes" else not inside
                assert pays >= c.payout_per_set, (x, [(m.ticker, s) for m, s in c.legs])
    assert found > 20  # the random books do produce covers, so the check above is exercised
