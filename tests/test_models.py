import math
from datetime import UTC, datetime
from decimal import Decimal as D

import pytest

from helpers import market
from omen.calibration import evaluate_gates
from omen.config import CryptoModelSettings, ValidationGates
from omen.data.crypto_feeds import SpotQuote, VolEstimate
from omen.stats import betainc, expected_calibration_error, normal_cdf, student_t_cdf
from omen.strategies.crypto import prob_in_interval, probability_band
from omen.strategies.structure import Interval, interval_of


def t4_closed_form(t):
    x = t / math.sqrt(4 + t * t)
    return 0.5 + 0.75 * (x - x**3 / 3)


@pytest.mark.parametrize("t", [-6, -2.5, -1, -0.1, 0, 0.3, 1, 2, 4.5])
def test_student_t_matches_closed_form_for_four_dof(t):
    assert student_t_cdf(t, 4) == pytest.approx(t4_closed_form(t), abs=1e-10)


def test_student_t_tends_to_normal_and_beta_edges():
    assert student_t_cdf(1.5, 1e7) == pytest.approx(normal_cdf(1.5), abs=1e-9)
    assert student_t_cdf(1.5, 200) == pytest.approx(normal_cdf(1.5), abs=2e-3)
    assert betainc(2, 3, 0) == 0 and betainc(2, 3, 1) == 1
    assert betainc(1, 1, 0.3) == pytest.approx(0.3)


def test_interval_probabilities_sum_to_one():
    spot, sigma, tau = 80000.0, 0.5, 1 / 365
    parts = [
        Interval(None, False, D(79000), False),
        Interval(D(79000), True, D(81000), True),
        Interval(D(81000), False, None, False),
    ]
    for nu in (4.0, math.inf):
        assert sum(prob_in_interval(iv, spot, sigma, tau, nu) for iv in parts) == pytest.approx(1.0, abs=1e-9)


CFG = CryptoModelSettings()


LIVE_VOLS = VolEstimate(0.18, 0.27, 0.37)  # realized 2h / realized 7d / Deribit DVOL on 2026-10-23


def band(strike, spot=80000.0, vol=LIVE_VOLS, hours=24):
    iv = Interval(D(strike), False, None, False)
    q = SpotQuote("BTC", spot, {}, datetime.now(UTC), 0.0)
    return probability_band(iv, q, vol, hours / (365.25 * 24), CFG)


def test_band_brackets_central_and_widens_with_disagreeing_vols():
    central, lo, hi = band(81500)
    assert lo < central < hi
    _, lo_tight, hi_tight = band(81500, vol=VolEstimate(0.30, 0.30, 0.30))
    assert lo < lo_tight and hi > hi_tight


def test_both_tails_can_never_both_look_cheap():
    """The bug the live smoke test caught: with one high vol estimate, both OTM wings looked underpriced.
    Conservative YES on the upper wing and conservative NO on the lower wing must not both beat cheap prices."""
    _, up_lo, _ = band(82750, spot=81275)
    _, _, down_hi = band(80750, spot=81275)
    assert up_lo < 0.03  # YES above 82,750 at 3c is not a conservative edge
    assert 1 - down_hi < 0.20  # NO below 80,750 at 20c is not either


def test_deep_in_the_money_favorite():
    _, lo, _ = band(70000, spot=80000, hours=6)  # 13% below spot, 6 hours left
    assert lo > 0.97


def test_crypto_markets_have_interval_semantics():
    m = market("KXBTCD-26OCT3017-T68999.99", "KXBTCD-26OCT3017", strike_type="greater", floor="68999.99")
    assert interval_of(m).lo == D("68999.99")


# -------------------------------------------------------------------------------------------- calibration gates
def row(p_model, outcome, would, side="yes", price="0.90", fee="0.01", event="E1", mid=None):
    return {
        "p_model": p_model,
        "outcome": outcome,
        "would_alert": would,
        "side": side,
        "price": price,
        "fee": fee,
        "event_ticker": event,
        "ticker": "T",
        "features_json": "{}" if mid is None else f'{{"market_mid": {mid}}}',
    }


def test_gates_block_small_samples():
    v = evaluate_gates([row(0.95, 1.0, 1) for _ in range(10)], ValidationGates())
    assert not v.passed and any("resolved signals" in r for r in v.reasons)


def test_gates_pass_on_honest_profitable_record():
    rows = []
    for e in range(40):
        for i in range(5):
            rows.append(row(0.95, 1.0 if (e * 5 + i) % 20 else 0.0, 1, event=f"E{e}", mid=0.9))
    v = evaluate_gates(rows, ValidationGates(min_resolved_signals=150, bootstrap_samples=500))
    # wins 95% of the time at a 0.91 all-in cost: ROI ~ +4.4% with a positive lower bound
    assert v.passed, v.reasons
    assert v.roi == pytest.approx(0.95 / 0.91 - 1, abs=1e-9)


def test_gates_fail_on_losing_record():
    rows = [row(0.95, 1.0 if i % 5 else 0.0, 1, event=f"E{i % 40}") for i in range(200)]
    v = evaluate_gates(rows, ValidationGates(bootstrap_samples=300))
    assert not v.passed and v.roi < 0


def test_ece():
    assert expected_calibration_error([(0.9, 1.0)] * 9 + [(0.9, 0.0)]) == pytest.approx(0.0)
    assert expected_calibration_error([(0.9, 0.0)] * 10) == pytest.approx(0.9)
