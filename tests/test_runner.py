from dataclasses import replace
from decimal import Decimal as D

from omen.config import Settings
from omen.ledger import Ledger
from omen.runner import Runner, tickers_of
from test_alerts import no_basket_opp


def runner():
    return Runner(Settings(), engine=None, ledger=Ledger(":memory:"), notifier=None)  # type: ignore[arg-type]


def test_locks_route_to_plays_unless_implausible():
    r = runner()
    opp = no_basket_opp()  # 1.6% return
    assert r._route(opp) == ("plays", None)
    r.s.lock.max_plausible_return_pct = D("1")
    channel, note = r._route(opp)
    assert channel == "status" and "SUSPICIOUS" in note


def test_models_route_to_research_until_promoted():
    r = runner()
    opp = replace(no_basket_opp(), tier="MODEL", strategy="crypto_threshold", probability=0.9, model_probability=0.95)
    assert r._route(opp)[0] == "research"
    r.ledger.set_model_state("crypto_threshold", "live", "test", opp.found_at)
    assert r._route(opp)[0] == "plays"


def test_tickers_of_key():
    assert tickers_of("ladder|KXA-1:yes,KXA-2:no") == {"KXA-1", "KXA-2"}
