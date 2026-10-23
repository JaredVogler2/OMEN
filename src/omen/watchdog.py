"""Fee-schedule document watch: the formula constants in config/fee_schedule.toml come from Kalshi's PDF, which has
no API. OMEN checks the PDF's HTTP validators daily (when kalshi.com lets a bot see them) and reminds the owner to
re-verify the constants on a fixed cadence regardless."""

from __future__ import annotations

import hashlib
import logging
from datetime import date, datetime

import httpx

from omen.engine import Notice
from omen.fees.schedule import FeeSchedule
from omen.ledger import Ledger

log = logging.getLogger(__name__)
BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"


def check_fee_document(
    ledger: Ledger,
    schedule: FeeSchedule,
    reminder_days: int,
    now: datetime,
    transport: httpx.BaseTransport | None = None,
) -> list[Notice]:
    notices: list[Notice] = []
    today = now.date()
    if ledger.get_meta("fee_doc_checked_on") == today.isoformat():
        return notices
    ledger.set_meta("fee_doc_checked_on", today.isoformat())

    age = (today - schedule.verified_on).days
    last_reminder = ledger.get_meta("fee_verify_reminded_on")
    if age >= reminder_days and (last_reminder is None or (today - date.fromisoformat(last_reminder)).days >= 7):
        ledger.set_meta("fee_verify_reminded_on", today.isoformat())
        notices.append(
            Notice(
                "fee_verify",
                "Re-verify Kalshi fee formula constants",
                f"config/fee_schedule.toml (schedule {schedule.version}) was last verified {age} days ago. Open "
                f"{schedule.source_url}, confirm the taker/maker rates, then update verified_on. Per-series fee types "
                "and multipliers are already tracked live; this is only the formula behind them.",
                "warning",
            )
        )

    try:
        with httpx.Client(
            timeout=20, transport=transport, headers={"User-Agent": BROWSER_UA}, follow_redirects=True
        ) as http:
            resp = http.get(schedule.source_url)
    except httpx.HTTPError as exc:
        log.info("fee schedule document not reachable: %s", exc)
        return notices
    if resp.status_code != 200 or not resp.content.startswith(b"%PDF"):
        log.info("fee schedule document not readable by the bot (HTTP %s)", resp.status_code)
        return notices
    digest = hashlib.sha256(resp.content).hexdigest()
    previous = ledger.get_meta("fee_doc_sha256")
    ledger.set_meta("fee_doc_sha256", digest)
    if previous and previous != digest:
        ledger.log_fee("document_changed", None, f"fee schedule PDF changed (sha256 {digest[:12]})", now)
        notices.append(
            Notice(
                "fee_document",
                "Kalshi fee schedule document changed",
                f"{schedule.source_url} has new content. Review it and update config/fee_schedule.toml if the formula "
                "constants changed. Until then OMEN keeps using the per-series terms from the API with the current "
                "formula.",
                "error",
            )
        )
    return notices
