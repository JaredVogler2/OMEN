"""Measuring Kalshi's actual reward credits from the account's cash."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from omen.ledger import Ledger
from omen.liquidity.reconcile import Flows, Snapshot, account_flows, credits, holds_in_balance, reconcile, take_snapshot

T0 = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


class FakeAccount:
    """The signed portfolio endpoints, paged two rows at a time."""

    def __init__(self, balance: float, orders=(), fills=(), settlements=(), deposits=(), withdrawals=()):
        self.bal = balance
        self.rows = {
            "/portfolio/orders": ("orders", list(orders)),
            "/portfolio/fills": ("fills", list(fills)),
            "/portfolio/settlements": ("settlements", list(settlements)),
            "/portfolio/deposits": ("deposits", list(deposits)),
            "/portfolio/withdrawals": ("withdrawals", list(withdrawals)),
        }

    def balance(self) -> dict:
        return {"balance": round(self.bal * 100), "balance_dollars": f"{self.bal:.4f}"}

    def _signed(self, method, path, body=None, params=None):
        key, rows = self.rows[path]
        start = int((params or {}).get("cursor") or 0)
        page = rows[start : start + 2]
        more = start + 2 < len(rows)
        return {key: page, "cursor": str(start + 2) if more else ""}


def fill(ts: datetime, count: float, price: float, fee: float = 0.0, side: str = "no") -> dict:
    return {
        "fill_id": f"f{ts.timestamp()}",
        "outcome_side": side,
        "count_fp": f"{count:.2f}",
        "yes_price_dollars": f"{1 - price if side == 'no' else price:.4f}",
        "no_price_dollars": f"{price if side == 'no' else 1 - price:.4f}",
        "fee_cost": f"{fee:.4f}",
        "created_time": ts.isoformat().replace("+00:00", "Z"),
    }


def test_snapshot_counts_what_resting_buy_orders_hold():
    acct = FakeAccount(
        100.0,
        orders=[
            {
                "outcome_side": "no",
                "no_price_dollars": "0.0100",
                "yes_price_dollars": "0.9900",
                "remaining_count_fp": "1100.00",
            },
            {
                "outcome_side": "yes",
                "yes_price_dollars": "0.0500",
                "no_price_dollars": "0.9500",
                "remaining_count_fp": "200.00",
            },
            {"outcome_side": "yes", "yes_price_dollars": "0.0300", "remaining_count_fp": "50.00", "action": "sell"},
        ],
    )
    snap = take_snapshot(acct, T0)
    assert snap.balance == 100.0 and snap.holds == pytest.approx(11.0 + 10.0)


def test_flows_add_up_fills_fees_settlements_and_transfers_inside_the_window():
    stamp = int(at(30).timestamp())
    acct = FakeAccount(
        0.0,
        fills=[fill(at(5), 300, 0.01, fee=0.01), fill(at(6), 100, 0.01), fill(at(-5), 999, 0.01)],
        settlements=[
            {"revenue": 4500, "settled_time": at(20).isoformat()},
            {"revenue": 100, "settled_time": at(90).isoformat()},
        ],
        deposits=[
            {"status": "applied", "amount_cents": 5000, "finalized_ts": stamp},
            {"status": "pending", "amount_cents": 7, "finalized_ts": None},
        ],
        withdrawals=[{"status": "applied", "amount_cents": 1000, "finalized_ts": stamp}],
    )
    f = account_flows(acct, T0, at(60))
    assert (f.fills, f.fees, f.settlements, f.deposits, f.withdrawals) == pytest.approx((4.0, 0.01, 45.0, 50.0, 10.0))
    assert len(f.events) == 5  # the fill before the window and the settlement after it are left out


def test_credits_are_the_cash_nothing_else_explains():
    a, b = Snapshot(T0, 100.0, 0.0), Snapshot(at(1440), 119.0, 0.0)
    flows = Flows(fills=4.0, fees=0.01, settlements=0.0)
    # 100 -> 119 after paying 4.01 for fills: Kalshi credited 23.01
    assert credits(a, b, flows, True) == pytest.approx(23.01)


def test_resting_orders_out_of_the_balance_are_added_back():
    a, b = Snapshot(T0, 100.0, 0.0), Snapshot(at(1440), 89.0, 11.0)  # an 11-dollar bid took 11 out of the balance
    assert credits(a, b, Flows(), False) == pytest.approx(0.0)
    assert credits(a, b, Flows(), True) == pytest.approx(-11.0)


@pytest.mark.parametrize("in_balance", [True, False])
def test_which_balance_convention_kalshi_uses_is_read_off_quiet_snapshot_pairs(in_balance):
    snaps, bal, holds = [], 100.0, 0.0
    for minute, delta in enumerate([11.0, 0.0, -11.0, 5.0, 0.0]):  # we place and cancel bids, nothing fills
        holds += delta
        snaps.append(Snapshot(at(minute), bal if in_balance else bal - holds, holds))
    # a fill between minutes 4 and 5 makes that pair unusable; a reward credit lands in a pair of its own
    snaps.append(Snapshot(at(5), snaps[-1].balance - 3.0, holds))
    snaps.append(Snapshot(at(6), snaps[-1].balance + 7.5, holds))
    got, votes = holds_in_balance(snaps, [at(4.5)])
    assert got is in_balance and votes == 2  # the two pairs where our bids changed and nothing else happened


def test_reconcile_reports_the_credits_under_the_convention_the_data_shows():
    # the balance excludes resting-order cash: placing an 11-dollar bid drops it by 11
    snaps = [Snapshot(at(0), 100.0, 0.0), Snapshot(at(1), 89.0, 11.0), Snapshot(at(1440), 112.0, 11.0)]
    acct = FakeAccount(112.0, fills=[fill(at(30), 200, 0.01)])  # paid 2.00 for fills along the way
    r = reconcile(acct, snaps)
    assert r.holds_in_balance is False and r.votes == 1
    assert r.paid == pytest.approx(112.0 - 100.0 + 11.0 + 2.0)  # 25.00 of rewards


def test_cash_snapshots_round_trip_through_the_ledger():
    ledger = Ledger(":memory:")
    ledger.lp_add_cash("live", T0, 100.0, 11.0)
    ledger.lp_add_cash("live", at(1), 89.0, 11.0)
    ledger.lp_add_cash("paper", at(2), 1.0, 0.0)
    assert ledger.lp_cash("live") == [(T0, 100.0, 11.0), (at(1), 89.0, 11.0)]
    assert ledger.lp_cash("live", at(1)) == [(at(1), 89.0, 11.0)]
