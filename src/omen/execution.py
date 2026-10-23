"""Optional automated execution of alerted plays. OFF by default.

Modes (``[execution].mode`` or ``OMEN_EXECUTION_MODE``):
  off   - nothing is ever sent (default). Alerts carry the exact order payloads for a human or another system.
  demo  - orders go to Kalshi's demo exchange (fake money) to prove keys, signing and payloads.
  live  - real orders. Also requires ``OMEN_LIVE_ACK="I accept real-money risk"``.

Guards: LOCK plays only (unless ``allow_models``), per-play and per-day cost caps, balance check, a kill-switch file
(``<state_dir>/KILL``), fill-or-kill orders placed thinnest book first, and an immediate leg-risk alert if a later
leg fails after an earlier one filled.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from omen.alerts.discord import Notifier
from omen.alerts.render import notice_payload
from omen.config import DEMO_BASE_URL, PROD_BASE_URL, Settings
from omen.http import HttpClient, HttpError
from omen.kalshi.auth import KalshiSigner
from omen.kalshi.client import KalshiClient
from omen.ledger import Ledger
from omen.money import ONE, ZERO, floor_to, usd
from omen.strategies.base import LOCK, LegPlan, Opportunity

log = logging.getLogger(__name__)
LIVE_ACK = "I accept real-money risk"
ET = ZoneInfo("America/New_York")


class ExecutionRefused(RuntimeError):
    pass


def guaranteed_profit_at_limits(opp: Opportunity, sets: Decimal) -> Decimal:
    """Profit if every leg fills ``sets`` contracts at its limit price (the worst case for a fill-or-kill order)."""
    per_set_payout = opp.payout_min / opp.sets if opp.sets else ZERO
    cost = ZERO
    for lp in opp.legs:
        cost += lp.limit_price * sets + lp.fee.taker_fee(lp.limit_price, sets)
    return per_set_payout * sets - cost


class Executor:
    def __init__(
        self,
        settings: Settings,
        ledger: Ledger,
        notifier: Notifier,
        client: KalshiClient,
        clock=lambda: datetime.now(UTC),
    ):
        self.s = settings
        self.cfg = settings.execution
        self.ledger = ledger
        self.notifier = notifier
        self.client = client
        self.clock = clock
        if self.cfg.mode == "live" and settings.live_ack != LIVE_ACK:
            raise ExecutionRefused(f'live execution needs OMEN_LIVE_ACK="{LIVE_ACK}"')

    def _say(self, title: str, body: str, severity: str, channel: str = "status") -> None:
        self.notifier.queue(channel, notice_payload(title, body, severity), "execution", None, self.clock())

    def _killed(self) -> bool:
        return (self.s.state_dir / self.cfg.kill_switch_file).exists()

    def consider(self, opp: Opportunity, now: datetime) -> None:
        if self.cfg.mode == "off":
            return
        if self._killed():
            log.warning("kill switch present: not executing %s", opp.short_id)
            return
        if opp.tier != LOCK and not self.cfg.allow_models:
            return
        sets = opp.sets
        if opp.capital > self.cfg.max_cost_per_play_usd:
            sets = floor_to(opp.sets * self.cfg.max_cost_per_play_usd / opp.capital, ONE)
        if sets < 1:
            return
        cost_cap = sum((lp.limit_price * sets + lp.fee.taker_fee(lp.limit_price, sets) for lp in opp.legs), ZERO)
        if opp.tier == LOCK and guaranteed_profit_at_limits(opp, sets) <= 0:
            log.info("%s: not profitable at %s sets with worst-case limits; skipping", opp.short_id, sets)
            return
        day_start = now.astimezone(ET).replace(hour=0, minute=0, second=0, microsecond=0)
        spent = self.ledger.spent_since(day_start, self.cfg.mode)
        if spent + cost_cap > self.cfg.max_daily_cost_usd:
            self._say(
                "Execution skipped: daily cap",
                f"{opp.short_id} needs up to {usd(cost_cap)}; {usd(spent)} of {usd(self.cfg.max_daily_cost_usd)} "
                "already used today.",
                "warning",
            )
            return
        balance = self._balance()
        if balance is not None and balance < cost_cap:
            self._say(
                "Execution skipped: balance",
                f"{opp.short_id} needs {usd(cost_cap)}, balance {usd(balance)}.",
                "warning",
            )
            return
        self._execute(opp, sets, now)

    def _balance(self) -> Decimal | None:
        try:
            b = self.client.balance()
        except HttpError as exc:
            raise ExecutionRefused(f"cannot read balance: {exc}") from exc
        if "balance_dollars" in b:
            return Decimal(str(b["balance_dollars"]))
        if "balance" in b:
            return Decimal(int(b["balance"])) / 100
        return None

    def _execute(self, opp: Opportunity, sets: Decimal, now: datetime) -> None:
        order = sorted(opp.legs, key=lambda lp: (lp.depth, lp.market.ticker))  # thinnest book first
        filled: list[tuple[LegPlan, Decimal]] = []
        for i, lp in enumerate(order):
            body = lp.order_v2(f"omen-{opp.short_id}-{i}-{uuid.uuid4().hex[:8]}")
            body["count"] = f"{sets:.2f}"
            try:
                resp = self.client.create_order(body)
            except HttpError as exc:
                resp = {"error": str(exc), "status": exc.status, "body": exc.body}
            got = Decimal(str(resp.get("fill_count", "0")))
            leg_cost = Decimal(str(resp.get("average_fill_price") or 0)) * got
            leg_cost += Decimal(str(resp.get("average_fee_paid") or 0)) * got
            status = "filled" if got >= sets else ("partial" if got > 0 else "rejected")
            self.ledger.record_execution(opp.key, self.cfg.mode, now, lp.market.ticker, body, resp, status, leg_cost)
            if status != "filled":
                self._leg_failure(opp, lp, filled, resp, got)
                return
            filled.append((lp, got))
            time.sleep(0.05)
        self._say(
            f"Executed ({self.cfg.mode}) · {opp.title}",
            f"{opp.short_id}: {len(filled)} legs × {sets} filled with fill-or-kill orders.",
            "info",
            "plays",
        )

    def _leg_failure(
        self, opp: Opportunity, lp: LegPlan, filled: list[tuple[LegPlan, Decimal]], resp: dict, got: Decimal
    ) -> None:
        if not filled and got == 0:
            self._say(
                f"Execution aborted ({self.cfg.mode}) · {opp.short_id}",
                f"First leg {lp.market.ticker} did not fill (book moved). Nothing was bought.",
                "warning",
            )
            return
        held = "\n".join(f"• {p.side.upper()} {p.market.ticker} ×{n}" for p, n in filled)
        self._say(
            f"LEG RISK ({self.cfg.mode}) · {opp.short_id}",
            f"Leg {lp.market.ticker} filled {got} of the planned size after earlier legs filled. You now hold:\n"
            f"{held}\nThis is a directional position, not a lock. Unwind (sell what filled) or complete the missing "
            f"leg manually. Response: {str(resp)[:300]}",
            "error",
            "plays",
        )


def build_executor(settings: Settings, ledger: Ledger, notifier: Notifier) -> Executor:
    if not (settings.kalshi_api_key_id and settings.kalshi_private_key_path):
        raise ExecutionRefused("execution needs KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH")
    base = DEMO_BASE_URL if settings.execution.mode == "demo" else PROD_BASE_URL
    signer = KalshiSigner.from_file(settings.kalshi_api_key_id, settings.kalshi_private_key_path)
    client = KalshiClient(HttpClient(base, rate_per_second=5, max_retries=2), signer)
    return Executor(settings, ledger, notifier, client)
