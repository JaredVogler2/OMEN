"""Discord embeds (and plain text for the CLI) for plays, model signals, notices and the daily digest.

Every embed is trimmed to Discord's limits: title 256, description 4096, 25 fields of 1024, 6000 characters total.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from omen import __version__
from omen.money import ONE, cents, fmt_count, usd
from omen.strategies.base import LOCK, LegPlan, Opportunity

GREEN = 0x2ECC71
BLUE = 0x3498DB
GREY = 0x95A5A6
AMBER = 0xF39C12
RED = 0xE74C3C

STRATEGY_NAMES = {
    "no_basket": "NO basket",
    "ladder": "Ladder",
    "yes_partition": "YES basket",
    "cover": "Cross-series lock",
    "crypto_threshold": "Crypto model",
    "calibration_edge": "Calibration edge",
}


def _cut(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def fit_embed(embed: dict) -> dict:
    embed = dict(embed)
    if "title" in embed:
        embed["title"] = _cut(embed["title"], 256)
    if "description" in embed:
        embed["description"] = _cut(embed["description"], 4096)
    fields = []
    for f in embed.get("fields", [])[:25]:
        fields.append(
            {"name": _cut(f["name"], 256), "value": _cut(f["value"] or "-", 1024), "inline": f.get("inline", False)}
        )
    embed["fields"] = fields
    if "footer" in embed:
        embed["footer"] = {"text": _cut(embed["footer"]["text"], 2048)}

    def total() -> int:
        return (
            len(embed.get("title", ""))
            + len(embed.get("description", ""))
            + len(embed.get("footer", {}).get("text", ""))
            + sum(len(f["name"]) + len(f["value"]) for f in embed["fields"])
        )

    while total() > 5900 and embed["fields"]:
        embed["fields"].pop()
    if total() > 5900:
        embed["description"] = _cut(
            embed.get("description", ""), max(100, 5900 - (total() - len(embed.get("description", ""))))
        )
    return embed


def when(ts: datetime, now: datetime) -> str:
    secs = (ts - now).total_seconds()
    if secs < 3600:
        rel = f"{max(0, int(secs // 60))} min"
    elif secs < 72 * 3600:
        rel = f"{secs / 3600:.1f} h"
    else:
        rel = f"{secs / 86400:.1f} days"
    return f"<t:{int(ts.timestamp())}:f> ({rel})"


def leg_line(i: int, lp: LegPlan) -> str:
    side = lp.side.upper()
    fill = lp.fill
    avg = cents(fill.avg_price.quantize(Decimal("0.0001")))
    return (
        f"{i}. **BUY {side}** `{lp.market.ticker}` ×{fmt_count(fill.count)} @ ≤ {cents(lp.limit_price)} "
        f"(avg {avg}, fee {usd(fill.fees)}) — {_cut(lp.market.yes_sub_title or lp.market.title, 80)}"
    )


def _chunk_lines(lines: list[str], limit: int = 1000) -> list[str]:
    chunks, cur = [], ""
    for line in lines:
        if len(cur) + len(line) + 1 > limit and cur:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur:
        chunks.append(cur)
    return chunks


def play_embed(opp: Opportunity, now: datetime, note: str | None = None) -> dict:
    name = STRATEGY_NAMES.get(opp.strategy, opp.strategy)
    if opp.tier == LOCK:
        icon, color = "🔒", GREEN
        headline = (
            f"Guaranteed profit **{usd(opp.profit)}** on **{usd(opp.capital)}** "
            f"(**{opp.return_pct:.2f}%**, {opp.apr_pct:,.0f}% APR) after all fees"
        )
    else:
        icon, color = "📈", BLUE
        headline = (
            f"Expected profit **{usd(opp.profit)}** on **{usd(opp.capital)}** "
            f"(**{opp.return_pct:.2f}%** edge) using the *conservative* probability "
            f"{opp.probability:.1%} (model {opp.model_probability:.1%})"
        )
    desc = [headline, f"Settles {when(opp.settles_at, now)}", ""]
    desc += [f"• {r}" for r in opp.rationale]
    lines = [leg_line(i + 1, lp) for i, lp in enumerate(_execution_order(opp))]
    fields = []
    for j, chunk in enumerate(_chunk_lines(lines)):
        fields.append({"name": "Orders · fill-or-kill, in this order" + (" (cont.)" if j else ""), "value": chunk})
    payout = (
        f"min {usd(opp.payout_min)} · max {usd(opp.payout_max)}"
        if opp.tier == LOCK
        else f"{usd(opp.payout_max)} if it wins"
    )
    fields.append(
        {
            "name": "Money",
            "value": (
                f"Contracts {usd(opp.cost)} + fees {usd(opp.fees)} = **{usd(opp.capital)}**\n"
                f"Payout {payout} · {fmt_count(opp.sets)} sets"
            ),
            "inline": False,
        }
    )
    fee_terms = sorted({lp.fee.describe() for lp in opp.legs})
    fields.append({"name": "Fees used", "value": "\n".join(fee_terms) + "\nEach fill rounded up (never understated)."})
    if opp.risks:
        fields.append({"name": "Residual risks", "value": "\n".join(f"• {r}" for r in opp.risks)})
    if note:
        fields.insert(0, {"name": "Status", "value": note})
    found = opp.found_at.astimezone(UTC).strftime("%H:%M:%S UTC")
    return fit_embed(
        {
            "title": f"{icon} {opp.tier} · {name} · {opp.title}",
            "description": "\n".join(desc),
            "color": color,
            "fields": fields,
            "footer": {
                "text": f"OMEN {__version__} · id {opp.short_id} · prices verified on the full order book at "
                f"{found} · re-check before trading · not financial advice"
            },
        }
    )


def _execution_order(opp: Opportunity) -> list[LegPlan]:
    """Thinnest book first: if that leg fails to fill, nothing else has been bought yet."""
    return sorted(opp.legs, key=lambda lp: (lp.depth, lp.market.ticker))


def play_payload(opp: Opportunity, now: datetime, note: str | None = None) -> dict:
    return {"username": "OMEN · Kalshi", "embeds": [play_embed(opp, now, note)]}


def gone_payload(title: str, short_id: str, first_seen: datetime, gone_at: datetime, best_profit: Decimal) -> dict:
    minutes = max(1, int((gone_at - first_seen).total_seconds() // 60))
    return {
        "username": "OMEN · Kalshi",
        "embeds": [
            fit_embed(
                {
                    "title": f"⌛ GONE · {title}",
                    "description": (
                        f"No longer available at a profit after ~{minutes} min. "
                        f"Best observed: {usd(best_profit)}. Do not place these orders now."
                    ),
                    "color": GREY,
                    "footer": {"text": f"OMEN · id {short_id}"},
                }
            )
        ],
    }


def superseded_payload(title: str, short_id: str) -> dict:
    return {
        "username": "OMEN · Kalshi",
        "embeds": [
            fit_embed(
                {
                    "title": f"↻ UPDATED · {title}",
                    "description": "This play improved; a newer alert with fresh prices and sizes replaces this one.",
                    "color": GREY,
                    "footer": {"text": f"OMEN · id {short_id}"},
                }
            )
        ],
    }


def notice_payload(title: str, body: str, severity: str) -> dict:
    color = {"info": BLUE, "warning": AMBER, "error": RED}.get(severity, BLUE)
    icon = {"info": "ℹ️", "warning": "⚠️", "error": "🛑"}.get(severity, "ℹ️")
    return {
        "username": "OMEN · Kalshi",
        "embeds": [fit_embed({"title": f"{icon} {title}", "description": body, "color": color})],
    }


def digest_payload(lines: list[str], fields: list[dict]) -> dict:
    return {
        "username": "OMEN · Kalshi",
        "embeds": [
            fit_embed(
                {
                    "title": "📊 OMEN daily digest",
                    "description": "\n".join(lines),
                    "color": BLUE,
                    "fields": fields,
                    "footer": {"text": f"OMEN {__version__}"},
                }
            )
        ],
    }


def play_text(opp: Opportunity, now: datetime) -> str:
    """Plain-text rendering for the terminal."""
    out = [
        f"[{opp.tier}] {STRATEGY_NAMES.get(opp.strategy, opp.strategy)} · {opp.title}  (id {opp.short_id})",
        f"  profit {usd(opp.profit)} on {usd(opp.capital)} = {opp.return_pct:.2f}% "
        f"({opp.apr_pct:,.0f}% APR), settles {opp.settles_at:%Y-%m-%d %H:%M} UTC",
    ]
    for i, lp in enumerate(_execution_order(opp), 1):
        price = lp.limit_price if lp.side == "yes" else ONE - lp.limit_price
        out.append(
            f"  {i}. BUY {lp.side.upper()} {lp.market.ticker} x{fmt_count(lp.fill.count)} "
            f"limit {cents(lp.limit_price)} (V2 {'bid' if lp.side == 'yes' else 'ask'} @ {price}) "
            f"cost {usd(lp.fill.notional)} fee {usd(lp.fill.fees)}"
        )
    out += [f"  - {r}" for r in opp.risks]
    return "\n".join(out)
