"""Calibration-edge model: confidence quantification, dormancy without a fresh validated atlas, shadow logging."""

from datetime import timedelta
from decimal import Decimal as D
from types import SimpleNamespace

from helpers import NOW, ROOT, book, market, registry
from omen.config import load_settings
from omen.kalshi.models import Series
from omen.ledger import Ledger
from omen.strategies.base import MODEL
from omen.strategies.edge import CalibrationEdgeModel, horizon_for, score

REG = registry()


def test_score_shrinks_noisy_edges_and_reports_confidence():
    # measured: wins 20% at 8c (excess +12c) with a tight interval -> confident
    tight = score(price=0.08, fee=0.005, excess=0.12, se=0.02, prior_sd=0.03)
    assert 0.08 < tight["p_true"] < 0.20  # shrunk toward the price, never above the raw measurement
    assert tight["confidence"] > 0.95 and tight["ev"] > 0.03
    assert tight["payout_multiple"] > 10  # asymmetric: ~11x if it wins
    assert 0 < tight["kelly"] < 0.2
    # the same measured excess from a tiny sample (wide interval) earns much less belief
    noisy = score(price=0.08, fee=0.005, excess=0.12, se=0.15, prior_sd=0.03)
    assert noisy["shrink"] < 0.05 and noisy["confidence"] < 0.65
    # no measured excess: never confident, never a Kelly stake
    flat = score(price=0.50, fee=0.0175, excess=0.0, se=0.01, prior_sd=0.03)
    assert flat["confidence"] < 0.1 and flat["kelly"] == 0


def test_horizon_buckets():
    assert [horizon_for(h) for h in (0.2, 1, 5, 20, 70, 200)] == [None, "1h", "6h", "24h", "72h", None]


def _atlas(generated, edges):
    return {"generated_at": generated.isoformat(), "edges": edges, "markets": 1000}


EDGE = {
    "category": "Weather",
    "horizon": "24h",
    "band": [0.05, 0.10],
    "discovery": {
        "n": 400,
        "series": 60,
        "win_rate": 0.20,
        "avg_price": 0.08,
        "ev": 0.115,
        "ev_lo": 0.08,
        "ev_hi": 0.15,
        "excess": 0.12,
    },
    "validation": {
        "n": 150,
        "series": 30,
        "win_rate": 0.18,
        "avg_price": 0.08,
        "ev": 0.09,
        "ev_lo": 0.03,
        "ev_hi": 0.15,
        "excess": 0.10,
    },
    "edge": True,
}


def _model(tmp_path, doc):
    s = load_settings(ROOT / "config" / "omen.toml", env={"OMEN_PROJECT_ROOT": str(ROOT)})
    s.edge.bankroll_usd = D("1000")
    m = market(
        "KXHIGHNY-26OCT24-B70",
        "KXHIGHNY-26OCT24",
        yes_bid="0.06",
        yes_ask="0.08",
        strike_type="between",
        floor="70",
        cap="71",
        close=NOW + timedelta(hours=20),
        sub="70 to 71°",
    )
    fees = SimpleNamespace(
        series={"KXHIGHNY": Series("KXHIGHNY", "NYC high", "Climate and Weather", "quadratic", D(1), None)}
    )
    engine = SimpleNamespace(
        markets={m.ticker: m}, fees=fees, fee_for=lambda mk: REG.quote("KXTEST", mk.event_ticker, NOW)
    )
    client = SimpleNamespace(
        orderbooks=lambda tickers: {m.ticker: book(m.ticker, yes=[("0.06", "500")], no=[("0.92", "500")])}
    )
    ledger = Ledger(tmp_path / "l.sqlite3")
    return CalibrationEdgeModel(s, client, ledger, engine, atlas_doc=doc), ledger, m


def test_live_market_in_a_validated_edge_becomes_a_quantified_model_play(tmp_path):
    model, ledger, m = _model(tmp_path, _atlas(NOW - timedelta(days=2), [EDGE]))
    opps, scope = model.run(NOW)
    assert scope == {m.ticker}
    assert len(opps) == 1
    o = opps[0]
    assert o.tier == MODEL and o.strategy == "calibration_edge" and o.legs[0].side == "yes"
    assert o.confidence >= 0.9 and 0.08 < o.probability < 0.2
    assert o.capital <= D("200")  # quarter Kelly of $1,000, capped
    assert any("P(edge after fees > 0)" in r for r in o.rationale)
    rows = ledger.db.execute("SELECT * FROM predictions WHERE model='calibration_edge'").fetchall()
    assert len(rows) == 1 and rows[0]["would_alert"] == 1  # every evaluation is logged for the gates


def test_dormant_without_a_fresh_validated_atlas(tmp_path):
    stale, _, _ = _model(tmp_path, _atlas(NOW - timedelta(days=40), [EDGE]))
    assert stale.run(NOW) == ([], set())
    empty, _, _ = _model(tmp_path, _atlas(NOW - timedelta(days=1), []))
    assert empty.run(NOW) == ([], set())
    assert "Dormant" in empty.status()["summary"]
