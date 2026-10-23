import base64
from datetime import timedelta
from decimal import Decimal as D

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

from helpers import NOW
from omen.alerts.discord import Notifier
from omen.config import Settings
from omen.execution import LIVE_ACK, ExecutionRefused, Executor
from omen.kalshi.auth import KalshiSigner, signing_message
from omen.ledger import Ledger
from test_alerts import no_basket_opp


def pem(key) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )


def test_signing_message_strips_query_and_keeps_prefix():
    msg = signing_message(
        1700000000000, "get", "https://api.elections.kalshi.com/trade-api/v2/portfolio/orders?limit=5"
    )
    assert msg == b"1700000000000GET/trade-api/v2/portfolio/orders"


def test_rsa_pss_signature_verifies():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    signer = KalshiSigner("key-1", pem(key), clock=lambda: 1700000000.123)
    h = signer.headers("POST", "https://api.elections.kalshi.com/trade-api/v2/portfolio/events/orders")
    assert h["KALSHI-ACCESS-KEY"] == "key-1" and h["KALSHI-ACCESS-TIMESTAMP"] == "1700000000123"
    key.public_key().verify(
        base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"]),
        b"1700000000123POST/trade-api/v2/portfolio/events/orders",
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )


def test_ed25519_signature_verifies():
    key = ed25519.Ed25519PrivateKey.generate()
    signer = KalshiSigner("k", pem(key), clock=lambda: 1.0)
    h = signer.headers("GET", "/trade-api/v2/portfolio/balance")
    key.public_key().verify(base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"]), b"1000GET/trade-api/v2/portfolio/balance")


class FakeClient:
    def __init__(self, fills, balance_cents=100_000):
        self.fills = list(fills)
        self.orders = []
        self.balance_cents = balance_cents

    def balance(self):
        return {"balance": self.balance_cents}

    def create_order(self, body):
        self.orders.append(body)
        got = self.fills.pop(0)
        return {
            "order_id": "o",
            "fill_count": f"{got:.2f}",
            "remaining_count": "0.00",
            "average_fill_price": "0.60",
            "average_fee_paid": "0.0168",
        }


def executor(tmp_path, client, mode="demo", **cfg):
    s = Settings()
    s.state_dir = tmp_path
    s.execution.mode = mode
    for k, v in cfg.items():
        setattr(s.execution, k, v)
    ledger = Ledger(tmp_path / "x.sqlite3")
    notifier = Notifier(ledger, {}, None, dry_run=True)
    return Executor(s, ledger, notifier, client, clock=lambda: NOW), ledger, notifier


def test_live_mode_requires_explicit_ack(tmp_path):
    with pytest.raises(ExecutionRefused):
        executor(tmp_path, FakeClient([]), mode="live")
    s = Settings()
    s.execution.mode = "live"
    s.live_ack = LIVE_ACK
    Executor(s, Ledger(":memory:"), Notifier(Ledger(":memory:"), {}, None, dry_run=True), FakeClient([]))


def test_executes_all_legs_with_capped_size(tmp_path):
    opp = no_basket_opp()  # 100 sets, ~$197 capital
    client = FakeClient([D(50)] * 3)
    ex, ledger, notifier = executor(tmp_path, client, max_cost_per_play_usd=D("100"))
    ex.consider(opp, NOW)
    assert len(client.orders) == 3
    assert {o["count"] for o in client.orders} == {"50.00"}  # scaled to the $100 per-play cap
    assert all(o["time_in_force"] == "fill_or_kill" and o["side"] == "ask" for o in client.orders)
    assert ledger.spent_since(NOW - timedelta(days=1), "demo") > 0
    assert "Executed" in notifier.printed[-1]["payload"]["embeds"][0]["title"]


def test_leg_failure_raises_leg_risk_alert(tmp_path):
    client = FakeClient([D(50), D(0)])
    ex, _, notifier = executor(tmp_path, client, max_cost_per_play_usd=D("100"))
    ex.consider(no_basket_opp(), NOW)
    assert len(client.orders) == 2  # stopped after the failed leg
    assert "LEG RISK" in notifier.printed[-1]["payload"]["embeds"][0]["title"]


def test_kill_switch_daily_cap_and_balance(tmp_path):
    client = FakeClient([D(100)] * 3)
    ex, _, notifier = executor(tmp_path, client)
    (tmp_path / "KILL").touch()
    ex.consider(no_basket_opp(), NOW)
    assert client.orders == []
    (tmp_path / "KILL").unlink()

    ex, _, notifier = executor(tmp_path, client, max_cost_per_play_usd=D("1000"), max_daily_cost_usd=D("50"))
    ex.consider(no_basket_opp(), NOW)
    assert client.orders == [] and "daily cap" in notifier.printed[-1]["payload"]["embeds"][0]["title"]

    poor = FakeClient([D(100)] * 3, balance_cents=1000)
    ex, _, notifier = executor(tmp_path, poor, max_cost_per_play_usd=D("1000"), max_daily_cost_usd=D("5000"))
    ex.consider(no_basket_opp(), NOW)
    assert poor.orders == [] and "balance" in notifier.printed[-1]["payload"]["embeds"][0]["title"]


def test_off_mode_never_sends(tmp_path):
    client = FakeClient([])
    ex, _, _ = executor(tmp_path, client, mode="off")
    ex.consider(no_basket_opp(), NOW)
    assert client.orders == []
