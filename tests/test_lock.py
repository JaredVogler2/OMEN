from dataclasses import replace
from decimal import Decimal as D

from helpers import NOW, book, event, market, registry
from omen.config import LockSettings
from omen.strategies.lock import VerifyContext, screen_ladders, screen_no_baskets, screen_yes_partitions, verify
from omen.strategies.structure import check_partition, interval_of, ladder_groups, semantic_problem

REG = registry()


def fee(m):
    return REG.quote("KXTEST", m.event_ticker, NOW)


SETTINGS = LockSettings(min_profit_usd=D("0.50"), min_return_pct=D("0.5"), min_apr_pct=D("0"))


def ctx(books, settings=SETTINGS):
    return VerifyContext(books=books, fee=fee, settings=settings, now=NOW)


# ------------------------------------------------------------------------------------------------- NO basket
def three_way(me=True):
    ev = event(me=me)
    ms = [
        market("KXTEST-26OCT30-A", yes_bid="0.40", yes_ask="0.42", sub="A wins"),
        market("KXTEST-26OCT30-B", yes_bid="0.35", yes_ask="0.37", sub="B wins"),
        market("KXTEST-26OCT30-C", yes_bid="0.33", yes_ask="0.35", sub="C wins"),
    ]
    return ev, ms


def test_no_basket_found_and_priced_exactly():
    ev, ms = three_way()
    cands = screen_no_baskets({ev.event_ticker: ev}, {ev.event_ticker: ms}, fee)
    assert len(cands) == 1 and cands[0].payout_per_set == 2
    books = {m.ticker: book(m.ticker, yes=[(str(m.yes_bid), "100")], no=[("0.50", "100")]) for m in ms}
    opp = verify(cands[0], ctx(books))
    assert opp is not None
    assert opp.sets == 100
    # NO at 0.60 / 0.65 / 0.67 = $192 for 100 sets; fees 1.68 + 1.60 + 1.55 (each rounded up)
    assert opp.cost == D("192.00")
    assert opp.fees == D("1.68") + D("1.60") + D("1.55")
    assert opp.payout_min == D("200") and opp.payout_max == D("300")
    assert opp.profit == D("200") - D("192") - D("4.83")
    orders = [lp.order_v2("x") for lp in opp.legs]
    assert {o["side"] for o in orders} == {"ask"}  # buying NO = selling YES in the V2 book
    assert {o["price"] for o in orders} == {"0.4000", "0.3500", "0.3300"}


def test_no_basket_requires_mutual_exclusivity():
    ev, ms = three_way(me=False)
    assert screen_no_baskets({ev.event_ticker: ev}, {ev.event_ticker: ms}, fee) == []


def test_no_basket_rejected_when_fees_eat_the_edge():
    ev = event()
    ms = [market(f"KXTEST-26OCT30-{c}", yes_bid=p) for c, p in zip("ABC", ["0.34", "0.34", "0.33"], strict=True)]
    assert screen_no_baskets({ev.event_ticker: ev}, {ev.event_ticker: ms}, fee) == []  # 1.01 - fees < 1


def test_no_basket_depth_limits_size():
    ev, ms = three_way()
    cands = screen_no_baskets({ev.event_ticker: ev}, {ev.event_ticker: ms}, fee)
    books = {m.ticker: book(m.ticker, yes=[(str(m.yes_bid), "100")]) for m in ms}
    books[ms[2].ticker] = book(ms[2].ticker, yes=[("0.33", "30"), ("0.20", "500")])  # thin, then a cliff
    opp = verify(cands[0], ctx(books))
    assert opp is not None and opp.sets == 30  # past 30, C's NO costs 0.80 and the basket loses money
    assert opp.profit == D("60") - D("57.60") - (D("0.51") + D("0.48") + D("0.47"))
    # at 10 sets the per-fill fee rounding leaves $0.31, under the $0.50 floor: too small to alert
    books[ms[2].ticker] = book(ms[2].ticker, yes=[("0.33", "10"), ("0.20", "500")])
    assert verify(cands[0], ctx(books)) is None


# -------------------------------------------------------------------------------------------------- ladders
def btc(strike, bid, ask, close=None):
    return market(
        f"KXBTCD-26OCT3017-T{strike}",
        "KXBTCD-26OCT3017",
        yes_bid=bid,
        yes_ask=ask,
        strike_type="greater",
        floor=strike,
        title="Bitcoin price on Oct 30, 2026?",
        sub=f"${strike} or above",
        rules=f"If the BRTI average is above {strike} at 5 PM EDT on Oct 30, 2026, then the market resolves to Yes.",
        close=close,
    )


def test_ladder_inversion_is_a_lock():
    lo, hi = btc("85000", "0.60", "0.62"), btc("90000", "0.70", "0.72")  # P(>90k) priced above P(>85k)
    cands = screen_ladders({}, [lo, hi], fee)
    assert len(cands) == 1
    c = cands[0]
    assert [(m.ticker, s) for m, s in c.legs] == [(lo.ticker, "yes"), (hi.ticker, "no")]
    books = {lo.ticker: book(lo.ticker, no=[("0.38", "50")]), hi.ticker: book(hi.ticker, yes=[("0.70", "50")])}
    opp = verify(c, ctx(books))
    assert opp is not None and opp.sets == 50
    assert opp.cost == D("0.62") * 50 + D("0.30") * 50
    assert opp.payout_min == 50 and opp.payout_max == 100


def test_ladder_respects_fees():
    lo, hi = btc("85000", "0.60", "0.62"), btc("90000", "0.63", "0.65")  # 1 cent inversion < fees
    assert screen_ladders({}, [lo, hi], fee) == []


def test_ladder_ignores_different_underlyings():
    # Opposite teams' spread markets in one event look like a ladder by ticker but are different questions.
    cal = market(
        "KXSPREAD-26OCT24CALSMU-CAL2",
        "KXSPREAD-26OCT24CALSMU",
        yes_bid="0.05",
        yes_ask="0.06",
        strike_type="greater",
        floor="1.5",
        title="California wins by over 1.5 points",
        rules="If California wins by over 1.5 points, then the market resolves to Yes.",
    )
    smu = market(
        "KXSPREAD-26OCT24CALSMU-SMU3",
        "KXSPREAD-26OCT24CALSMU",
        yes_bid="0.83",
        yes_ask="0.85",
        strike_type="greater",
        floor="2.5",
        title="SMU wins by over 2.5 points",
        rules="If SMU wins by over 2.5 points, then the market resolves to Yes.",
    )
    assert ladder_groups([cal, smu]) == []
    assert screen_ladders({}, [cal, smu], fee) == []


def test_ladder_ignores_different_close_times():
    a = btc("85000", "0.60", "0.62")
    b = replace(btc("90000", "0.70", "0.72"), close_time=a.close_time.replace(hour=1))
    assert screen_ladders({}, [a, b], fee) == []


def test_less_ladder_direction():
    def lt(cap, bid, ask):
        return market(
            f"KXT-L{cap}",
            "KXT-1",
            yes_bid=bid,
            yes_ask=ask,
            strike_type="less",
            cap=cap,
            title="Value?",
            rules=f"If the value is below {cap}, Yes.",
        )

    low, high = lt("100", "0.50", "0.52"), lt("110", "0.40", "0.42")  # P(<100) priced above P(<110)
    cands = screen_ladders({}, [low, high], fee)
    assert len(cands) == 1
    assert [(m.ticker, s) for m, s in cands[0].legs] == [(high.ticker, "yes"), (low.ticker, "no")]


# ------------------------------------------------------------------------------------------------ partitions
def temp(ticker, st, floor=None, cap=None, ask="0.10", rules2=""):
    return market(
        f"KXHIGHNY-26OCT24-{ticker}",
        "KXHIGHNY-26OCT24",
        yes_ask=ask,
        yes_bid="0.01",
        strike_type=st,
        floor=floor,
        cap=cap,
        title="Highest temperature in NYC on Oct 24?",
        rules2=rules2,
    )


def weather(asks=("0.10", "0.20", "0.30", "0.20", "0.10"), rules2=""):
    return [
        temp("T59", "less", cap="59", ask=asks[0], rules2=rules2),
        temp("B59.5", "between", "59", "60", asks[1], rules2),
        temp("B61.5", "between", "61", "62", asks[2], rules2),
        temp("B63.5", "between", "63", "64", asks[3], rules2),
        temp("T64", "greater", floor="64", ask=asks[4], rules2=rules2),
    ]


def test_partition_with_whole_degree_buckets():
    ms = weather()
    check = check_partition(ms, allow_step_gaps=True)
    assert check.ok and check.step == 1
    assert not check_partition(ms, allow_step_gaps=False).ok


def test_partition_detects_a_hole_and_missing_tail():
    ms = weather()
    assert not check_partition([m for m in ms if not m.ticker.endswith("B61.5")], True).ok
    assert not check_partition(ms[:-1], True).ok
    # less-than 59 and greater-than 59 leave exactly 59 uncovered
    assert not check_partition([temp("T59", "less", cap="59"), temp("T59b", "greater", floor="59")], True).ok


def test_cent_gap_partition():
    ms = [
        market("KXBTC-L", "KXBTC-1", yes_ask="0.2", strike_type="less", cap="80000"),
        market("KXBTC-B1", "KXBTC-1", yes_ask="0.2", strike_type="between", floor="80000", cap="84999.99"),
        market("KXBTC-B2", "KXBTC-1", yes_ask="0.2", strike_type="between", floor="85000", cap="89999.99"),
        market("KXBTC-G", "KXBTC-1", yes_ask="0.2", strike_type="greater", floor="89999.99"),
    ]
    check = check_partition(ms, allow_step_gaps=True)
    assert check.ok and check.step == D("0.01")
    # a 2-cent hole is not a reporting step
    ms[2] = market("KXBTC-B2", "KXBTC-1", yes_ask="0.2", strike_type="between", floor="85000.01", cap="89999.99")
    assert not check_partition(ms, allow_step_gaps=True).ok


def test_tenths_partition():
    ms = [
        market("KXV-L", "KXV-1", strike_type="less", cap="8.1"),
        market("KXV-B", "KXV-1", strike_type="between", floor="8.1", cap="8.1"),
        market("KXV-B2", "KXV-1", strike_type="between", floor="8.2", cap="8.3"),
        market("KXV-G", "KXV-1", strike_type="greater", floor="8.3"),
    ]
    check = check_partition(ms, allow_step_gaps=True)
    assert check.ok and check.step == D("0.1")


def test_yes_basket_found_and_fair_price_clause_rejected():
    ev = event("KXHIGHNY-26OCT24")
    ms = weather(asks=("0.08", "0.18", "0.30", "0.18", "0.08"))  # 0.82 + fees < 1
    cands, rejected = screen_yes_partitions({ev.event_ticker: ev}, {ev.event_ticker: ms}, fee, True)
    assert len(cands) == 1 and not rejected
    risky = weather(
        asks=("0.08", "0.18", "0.30", "0.18", "0.08"),
        rules2="If no data is available, all markets will resolve to the last fair price.",
    )
    cands, rejected = screen_yes_partitions({ev.event_ticker: ev}, {ev.event_ticker: risky}, fee, True)
    assert cands == [] and "fair price" in rejected[0][1]


def test_yes_basket_needs_me_flag_and_full_coverage():
    ev = event("KXHIGHNY-26OCT24", me=False)
    ms = weather(asks=("0.08", "0.18", "0.30", "0.18", "0.08"))
    assert screen_yes_partitions({ev.event_ticker: ev}, {ev.event_ticker: ms}, fee, True)[0] == []
    ev = event("KXHIGHNY-26OCT24")
    cands, rejected = screen_yes_partitions({ev.event_ticker: ev}, {ev.event_ticker: ms[:-1]}, fee, True)
    assert cands == [] and "high tail" in rejected[0][1]


def test_interval_semantics():
    assert interval_of(temp("T59", "less", cap="59")).describe() == "(-∞, 59)"
    assert interval_of(temp("B", "between", "59", "60")).describe() == "[59, 60]"
    g = interval_of(btc("90000", "0", "0"))
    assert g.describe() == "(90000, +∞)"
    assert interval_of(btc("85000", "0", "0")).contains_interval(g)
    assert not g.contains_interval(interval_of(btc("85000", "0", "0")))


# ------------------------------------------------------------------------------------------------- sizing caps
def test_capital_cap_and_thresholds():
    ev, ms = three_way()
    cands = screen_no_baskets({ev.event_ticker: ev}, {ev.event_ticker: ms}, fee)
    books = {m.ticker: book(m.ticker, yes=[(str(m.yes_bid), "1000")]) for m in ms}
    capped = replace(SETTINGS, max_capital_usd=D("100"))
    opp = verify(cands[0], ctx(books, capped))
    assert opp is not None and opp.capital <= 100 and opp.sets == 50  # 51 sets would cost $100.39
    strict = replace(SETTINGS, min_return_pct=D("5"))
    assert verify(cands[0], ctx(books, strict)) is None


# ------------------------------------------------------------------------------- real-world metadata traps (live)
def starship(n, bid, ask):
    # Kalshi tagged these "exactly N" markets strike_type="less" with floor = cap = N (seen live on 2026-10-23)
    return market(
        f"KXSTARSHIPSPACE-26-{n}.0",
        "KXSTARSHIPSPACE-26",
        yes_bid=bid,
        yes_ask=ask,
        strike_type="less",
        floor=str(n),
        cap=str(n),
        title=f"Will  exactly {n} Starship launches reach space in 2026?",
        rules=f"If exactly {n} Starship launches reach Space in 2026, then the market resolves to Yes.",
    )


def test_exactly_n_markets_tagged_less_are_not_a_ladder():
    four, seven = starship(4, "0.40", "0.44"), starship(7, "0.01", "0.11")
    assert interval_of(four) is None and interval_of(seven) is None
    assert "floor=4" in semantic_problem(four)
    assert screen_ladders({}, [four, seven], fee) == []


def test_rules_text_must_agree_with_strike_type():
    seed = market(
        "KXNBASEEDOU-27-BOS3",
        "KXNBASEEDOU-27",
        strike_type="less",
        cap="3",
        rules="If the Boston team is the #3 seed or better at the conclusion of the season, Yes.",
    )
    assert interval_of(seed) is None  # "or better" means <= 3, not < 3: excluded rather than guessed
    exact = market(
        "KXGRANDSLAM-JSIN27-4",
        "KXGRANDSLAM-JSIN27",
        strike_type="greater_or_equal",
        floor="4",
        rules="If Jannik Sinner wins exactly 4 singles tennis Grand Slams in 2027, Yes.",
    )
    assert interval_of(exact) is None
    props = market(
        "KXNBAPTS-1-X15",
        "KXNBAPTS-1",
        strike_type="greater",
        floor="14.5",
        rules="If Tre Jones records 15+ points in the game, then the market resolves to Yes.",
    )
    assert interval_of(props) is not None
    point = market(
        "KXFEDCHGCOUNT-1",
        "KXFEDCHGCOUNT-26",
        strike_type="between",
        floor="1",
        cap="1",
        rules="If the number of rate changes before 2027 is exactly 1, then the market resolves to Yes.",
    )
    assert interval_of(point).describe() == "[1, 1]"
