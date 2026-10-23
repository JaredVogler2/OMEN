from datetime import timedelta
from decimal import Decimal as D

import pytest

from helpers import NOW, registry, schedule
from omen.fees import FeeUnavailable
from omen.fees.schedule import FeeQuote


def q(fee_type="quadratic", mult="1", inc="0.01"):
    sch = schedule(inc)
    return FeeQuote(fee_type, D(mult), sch.formula(fee_type), D(inc), "test")


@pytest.mark.parametrize(
    ("price", "count", "expected"),
    [
        # Kalshi's published examples: 100 contracts at 50c = $1.75, at 10c or 90c = $0.63, at 55c = $1.74
        ("0.50", "100", "1.75"),
        ("0.10", "100", "0.63"),
        ("0.90", "100", "0.63"),
        ("0.55", "100", "1.74"),
        # one contract at 50c: 0.0175 rounds UP to 2 cents
        ("0.50", "1", "0.02"),
        ("0.01", "1", "0.01"),
    ],
)
def test_taker_fee_matches_published_schedule(price, count, expected):
    assert q().taker_fee(D(price), D(count)) == D(expected)


def test_fee_is_never_understated_with_finer_rounding():
    cent = q(inc="0.01").taker_fee(D("0.37"), D("13"))
    fine = q(inc="0.0001").taker_fee(D("0.37"), D("13"))
    exact = D("0.07") * 13 * D("0.37") * D("0.63")
    assert cent >= fine >= exact


def test_multiplier_and_zero_fee_series():
    assert q(mult="0.5").taker_fee(D("0.50"), D("100")) == D("0.88")  # 0.875 rounded up
    assert q(mult="0").taker_fee(D("0.50"), D("100")) == D("0")


def test_maker_fees_by_type():
    assert q("quadratic").maker_fee(D("0.50"), D("100")) == D("0")
    assert q("quadratic_with_maker_fees").maker_fee(D("0.50"), D("100")) == D("0.44")  # 0.4375 up
    assert q("quadratic_with_combo_maker_fees").maker_fee(D("0.50"), D("100")) == D("0.88")


def test_flat_and_unknown_fee_types_fail_closed():
    sch = schedule()
    with pytest.raises(FeeUnavailable):
        sch.formula("flat")  # not configured from the PDF table
    with pytest.raises(FeeUnavailable):
        sch.formula("cubic_new_thing")


def test_registry_series_terms_and_unknown_series():
    reg = registry({"KXA": ("quadratic", "0.5")})
    quote = reg.quote("KXA", "KXA-1", NOW)
    assert quote.multiplier == D("0.5") and quote.source == "series"
    with pytest.raises(FeeUnavailable):
        reg.quote("KXNOPE", None, NOW)


def _change(cid, series, ft, mult, at):
    return {
        "id": cid,
        "series_ticker": series,
        "fee_type": ft,
        "fee_multiplier": mult,
        "scheduled_ts": at.isoformat().replace("+00:00", "Z"),
    }


def test_series_change_after_snapshot_is_applied():
    reg = registry({"KXA": ("quadratic", "1")})  # snapshot taken NOW-1h
    reg.load_changes([_change("c1", "KXA", "quadratic", 0.5, NOW - timedelta(minutes=30))], [])
    assert reg.quote("KXA", None, NOW).multiplier == D("0.5")
    # a change from before the snapshot is already reflected in it and must not be re-applied
    reg.load_changes([_change("c0", "KXA", "quadratic", 2, NOW - timedelta(hours=5))], [])
    assert reg.quote("KXA", None, NOW).multiplier == D("1")


def test_guard_uses_the_more_expensive_upcoming_terms():
    reg = registry({"KXA": ("quadratic", "0.5")})
    reg.load_changes([_change("c1", "KXA", "quadratic", 1, NOW + timedelta(minutes=20))], [])
    quote = reg.quote("KXA", None, NOW)
    assert quote.multiplier == D("1") and "guard" in quote.source
    # a cheaper upcoming change does not lower the price we assume
    reg = registry({"KXA": ("quadratic", "1")})
    reg.load_changes([_change("c1", "KXA", "quadratic", 0, NOW + timedelta(minutes=20))], [])
    assert reg.quote("KXA", None, NOW).multiplier == D("1")
    # outside the guard window the change is ignored until it happens
    reg.load_changes([_change("c1", "KXA", "quadratic", 2, NOW + timedelta(hours=5))], [])
    assert reg.quote("KXA", None, NOW).multiplier == D("1")


def test_event_override_and_clear():
    reg = registry({"KXMLBGAME": ("quadratic_with_maker_fees", "0.5")})
    ev = "KXMLBGAME-26OCT272000MILTB"
    rows = [
        {
            "id": "e1",
            "event_ticker": ev,
            "series_ticker": "KXMLBGAME",
            "fee_type_override": "quadratic",
            "fee_multiplier_override": 1,
            "scheduled_ts": (NOW - timedelta(minutes=5)).isoformat(),
        }
    ]
    reg.load_changes([], rows)
    quote = reg.quote("KXMLBGAME", ev, NOW)
    assert (quote.fee_type, quote.multiplier, quote.source) == ("quadratic", D("1"), "event override")
    assert reg.quote("KXMLBGAME", "KXMLBGAME-OTHER", NOW).multiplier == D("0.5")
    rows.append(
        {
            "id": "e2",
            "event_ticker": ev,
            "series_ticker": "KXMLBGAME",
            "fee_type_override": None,
            "fee_multiplier_override": None,
            "scheduled_ts": (NOW - timedelta(minutes=1)).isoformat(),
        }
    )
    reg.load_changes([], rows)
    assert reg.quote("KXMLBGAME", ev, NOW).multiplier == D("0.5")


def test_unknown_fee_types_reported():
    reg = registry({"KXA": ("quadratic", "1"), "KXB": ("flat", "1"), "KXC": ("mystery", "1")})
    assert reg.unknown_fee_types() == {"flat", "mystery"}
    with pytest.raises(FeeUnavailable):
        reg.quote("KXB", None, NOW)


def test_http_client_backs_off_and_adapts_rate_on_429():
    import httpx

    from omen.http import HttpClient

    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] <= 2:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={"error": "too many requests"})
        return httpx.Response(200, json={"ok": True})

    sleeps = []
    http = HttpClient("https://x.test", rate_per_second=8, transport=httpx.MockTransport(handler), sleep=sleeps.append)
    assert http.get_json("/a") == {"ok": True}
    assert calls["n"] == 3
    assert http.limiter.rate == 2  # halved twice
