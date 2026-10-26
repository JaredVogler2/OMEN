"""What Kalshi actually paid in liquidity rewards, measured from the account's cash.

Kalshi has no endpoint for reward credits: they arrive as cash in the balance. Every other cash movement has one
(fills, settlements, deposits, withdrawals), so the part of the balance change those do not explain is the reward
credit. ``omen lp run`` in live mode records the balance and the cash held by resting orders every cycle; ``omen lp
reconcile`` does the arithmetic and sets it against OMEN's own formula estimate.

It assumes the account trades only through OMEN, which only buys: every fill and every resting order is read as a
purchase of its ``outcome_side`` at that side's price (the deprecated ``action`` field is not used). A manual sale
would be counted as a purchase.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import Any

MAX_PAGES = 200
TOLERANCE = 0.011  # a residual within a cent (plus rounding) is zero


@dataclass(frozen=True)
class Snapshot:
    ts: datetime
    balance: float  # GET /portfolio/balance, dollars
    holds: float  # what the account's resting buy orders would cost if they all filled, dollars


@dataclass
class Flows:
    """Cash movements Kalshi reports between two moments (dollars; + is cash in)."""

    fills: float = 0.0  # paid for contracts (sales count as negative)
    fees: float = 0.0
    settlements: float = 0.0  # paid out when markets settled
    deposits: float = 0.0
    withdrawals: float = 0.0
    netted: float = 0.0  # YES+NO pairs bought in one market inside the window: Kalshi pays $1 back for each at once
    events: list[datetime] = field(default_factory=list)  # when each of these happened

    def explained(self) -> float:
        return self.settlements + self.deposits - self.withdrawals - self.fills - self.fees + self.netted


def _ts(v: Any) -> datetime | None:
    if v is None or v == "":
        return None
    if isinstance(v, int | float):
        return datetime.fromtimestamp(v, UTC)
    return datetime.fromisoformat(str(v).replace("Z", "+00:00"))


def _pages(client: Any, path: str, key: str, params: dict) -> list[dict]:
    out: list[dict] = []
    cursor = ""
    for _ in range(MAX_PAGES):
        q = dict(params)
        if cursor:
            q["cursor"] = cursor
        data = client._signed("GET", path, params=q)
        out.extend(data.get(key) or [])
        cursor = data.get("cursor") or ""
        if not cursor:
            break
    return out


def take_snapshot(client: Any, now: datetime) -> Snapshot:
    bal = client.balance()
    balance = float(bal["balance_dollars"]) if bal.get("balance_dollars") is not None else bal["balance"] / 100
    holds = 0.0
    for o in _pages(client, "/portfolio/orders", "orders", {"status": "resting", "limit": 200}):
        # every resting order is a purchase of its outcome side (a NO bid is a V2 "ask"; ``action`` is deprecated)
        side = o.get("outcome_side")
        if side not in ("yes", "no"):
            side = "yes" if o.get("book_side") == "bid" else "no"
        price = o.get("yes_price_dollars") if side == "yes" else o.get("no_price_dollars")
        holds += float(price or 0) * float(o.get("remaining_count_fp") or 0)
    return Snapshot(now, balance, round(holds, 4))


def account_flows(client: Any, start: datetime, end: datetime) -> Flows:
    f = Flows()
    window = {"min_ts": int(start.timestamp()), "max_ts": int(end.timestamp()) + 1}
    bought: dict[str, dict[str, float]] = {}
    for x in _pages(client, "/portfolio/fills", "fills", {**window, "limit": 200}):
        ts = _ts(x.get("created_time") or x.get("ts"))
        if ts is None or not start < ts <= end:
            continue
        side = x.get("outcome_side")
        price = x.get("yes_price_dollars") if side == "yes" else x.get("no_price_dollars")
        qty = float(x.get("count_fp") or 0)
        cost = float(price or 0) * qty
        f.fills += cost
        f.fees += float(x.get("fee_cost") or 0)
        if side in ("yes", "no"):
            sides = bought.setdefault(x.get("ticker", ""), {"yes": 0.0, "no": 0.0})
            sides[side] += qty
        f.events.append(ts)
    # positions held before the window are not known here; OMEN never quotes the side opposite one it holds
    f.netted = sum(min(s["yes"], s["no"]) for s in bought.values())
    for x in _pages(client, "/portfolio/settlements", "settlements", {**window, "limit": 200}):
        ts = _ts(x.get("settled_time"))
        if ts is None or not start < ts <= end:
            continue
        f.settlements += float(x.get("revenue") or 0) / 100
        f.events.append(ts)
    for key, path in (("deposits", "/portfolio/deposits"), ("withdrawals", "/portfolio/withdrawals")):
        for x in _pages(client, path, key, {"limit": 500}):
            ts = _ts(x.get("finalized_ts"))
            if x.get("status") != "applied" or ts is None or not start < ts <= end:
                continue
            amount = float(x.get("amount_cents") or 0) / 100
            setattr(f, key, getattr(f, key) + amount)
            f.events.append(ts)
    return f


def credits(a: Snapshot, b: Snapshot, flows: Flows, holds_in_balance: bool) -> float:
    """Cash that arrived between ``a`` and ``b`` and that no fill, settlement or transfer explains."""
    held = 0.0 if holds_in_balance else b.holds - a.holds  # if resting orders take cash out of the balance
    return (b.balance - a.balance) + held - flows.explained()


def holds_in_balance(snaps: list[Snapshot], events: list[datetime]) -> tuple[bool | None, int]:
    """Does Kalshi's balance still count cash that resting orders hold? Read it off quiet pairs of snapshots.

    A pair is quiet when no fill, settlement or transfer happened between the two snapshots and our resting orders
    changed by at least 50 cents. If the balance moved with the holds it excludes them; if it stayed put it includes
    them. Returns the answer most quiet pairs agree on (None if none do) and how many pairs voted."""
    votes = {True: 0, False: 0}
    for a, b in pairwise(snaps):
        if abs(b.holds - a.holds) < 0.5 or any(a.ts < t <= b.ts for t in events):
            continue
        for convention in (True, False):
            if abs(credits(a, b, Flows(), convention)) <= TOLERANCE:
                votes[convention] += 1
    if votes[True] == votes[False]:
        return None, 0
    winner = votes[True] > votes[False]
    return winner, votes[winner]


@dataclass
class Reconciliation:
    start: datetime
    end: datetime
    balance_change: float
    flows: Flows
    holds_in_balance: bool | None
    votes: int
    credits: dict[bool, float]  # by convention: True = balance includes resting-order cash
    cutoff: datetime | None = None  # Kalshi's archive cutoff for fills and settlements, if it could be read

    @property
    def paid(self) -> float | None:
        if self.holds_in_balance is not None:
            return self.credits[self.holds_in_balance]
        if abs(self.credits[True] - self.credits[False]) <= TOLERANCE:  # resting-order cash unchanged: either way
            return self.credits[True]
        return None


def live_since(client: Any) -> datetime | None:
    """The latest of Kalshi's archive cutoffs for fills, markets and settled positions: before it, records leave the
    live endpoints (``/portfolio/settlements`` follows the positions cutoff)."""
    try:
        data = client.http.get_json("/historical/cutoff")
    except Exception:  # unknown cutoff: reconcile the whole window (the output says so)
        return None
    keys = ("trades_created_ts", "market_settled_ts", "market_positions_last_updated_ts")  # fills, markets, settlements
    stamps = [_ts(data.get(k)) for k in keys]
    known = [t for t in stamps if t is not None]
    return max(known) if known else None


def reconcile(client: Any, snaps: list[Snapshot]) -> Reconciliation:
    cutoff = live_since(client)
    if cutoff is not None:  # older fills and settlements are archived: start at the first snapshot after the cutoff
        snaps = [s for s in snaps if s.ts >= cutoff] or snaps[-1:]
    if len(snaps) < 2:
        raise ValueError("no two cash snapshots after Kalshi's archive cutoff")
    a, b = snaps[0], snaps[-1]
    flows = account_flows(client, a.ts, b.ts)
    convention, votes = holds_in_balance(snaps, flows.events)
    return Reconciliation(
        a.ts,
        b.ts,
        b.balance - a.balance,
        flows,
        convention,
        votes,
        {c: credits(a, b, flows, c) for c in (True, False)},
        cutoff,
    )


@dataclass
class PeriodCheck:
    """OMEN's estimate for the reward periods Kalshi should have paid inside a window, by its own rule: a period is
    paid up to a day after it ends, and only if the account's share comes to at least $1.00 (rounded down)."""

    closed: int  # periods that ended in time to be paid inside the window
    formula: float  # OMEN's estimate for them
    expected: float  # the same after the $1 minimum and rounding down
    open_periods: int  # periods still running at the window's end, or ended too late to be paid inside it
    open_estimate: float  # what OMEN estimates they had earned by the window's end (paid in a later window)


def period_check(ledger: Any, mode: str, start: datetime, end: datetime) -> PeriodCheck:
    """Periods that ended between a day before ``start`` and a day before ``end`` are the ones Kalshi pays inside
    [start, end]; a weekly window does not line up with 1-7 day periods otherwise."""
    closed = ledger.lp_programs_ended(mode, start - timedelta(days=1), end - timedelta(days=1))
    formula = expected = 0.0
    for ticker, s, e in closed:
        est = ledger.lp_reward_in_period(mode, ticker, s, e)
        formula += est
        expected += math.floor(est * 100 + 1e-9) / 100 if est >= 1.0 else 0.0
    never = datetime.max.replace(tzinfo=UTC)
    later = [(t, s, e) for t, s, e in ledger.lp_programs_ended(mode, end - timedelta(days=1), never) if s < end]
    open_estimate = sum(ledger.lp_reward_in_period(mode, t, s, min(e, end)) for t, s, e in later)
    return PeriodCheck(len(closed), formula, expected, len(later), open_estimate)
