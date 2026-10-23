# Discord (same server as STONKS / APEX)

OMEN posts to three channels in the **same private server** APEX uses:

| channel key | suggested channel | what is posted | who should see it |
|---|---|---|---|
| `plays` | `#kalshi-plays` | 🔒 LOCK plays and promoted 📈 MODEL plays, with exact orders; ⌛ GONE edits; leg-risk alerts | members you share plays with |
| `research` | `#kalshi-research` | 📈 shadow-model signals, labelled *SHADOW · unvalidated, not a recommendation* | owner (optional: leave unset to skip) |
| `status` | `#kalshi-status` | fee changes, errors, daily digest, execution notices | owner |

Create the channels by hand. As with APEX, only OMEN or the bot and the owner should be able to send in them.

Every OMEN message sets `allowed_mentions` to none, so it can never @everyone or ping anyone.

## Option A: webhooks (simplest; recommended)

For each channel: **Edit Channel → Integrations → Webhooks → New Webhook → Copy URL**. Then, in
`~/.config/omen/omen.env`:

```
OMEN_DISCORD_WEBHOOKS={"plays":"https://discord.com/api/webhooks/…","research":"https://discord.com/api/webhooks/…","status":"https://discord.com/api/webhooks/…"}
```

A single `DISCORD_WEBHOOK_URL=…` sends everything to one channel. Only `https://discord.com/api/webhooks/…` URLs
are accepted, as an SSRF guard like APEX's. Treat a webhook URL like a password: anyone holding it can post as OMEN.

## Option B: reuse the APEX bot token

OMEN can post through the same bot user as APEX. It uses only the REST API (`POST /channels/{id}/messages`) and
never opens a Gateway connection, so it does not interfere with the running `apex-discord-bot` service.

```
DISCORD_BOT_TOKEN=<same token as APEX>
OMEN_DISCORD_CHANNEL_MAP={"plays":"123…","research":"124…","status":"125…"}
```

The bot needs **View Channel, Send Messages and Embed Links** in the new channels, the same minimal permissions
APEX uses.

**Do not** add these keys to APEX's `APEX_CHANNEL_MAP`. APEX refuses to start on channel keys it does not know.
OMEN has its own map.

## Test

```bash
uv run omen --env-file ~/.config/omen/omen.env doctor        # validates routes (webhook URLs are redacted)
uv run omen --env-file ~/.config/omen/omen.env discord-test  # one test message per configured channel
```

## What a play looks like

```
🔒 LOCK · NO basket · Who will win the 2026 XYZ election?
Guaranteed profit $3.17 on $196.83 (1.61%, 98% APR) after all fees
Settles <date> (6.0 days)
• Event is flagged mutually exclusive: at most one of its markets can resolve YES.
• Buying NO on 3 of its markets therefore pays at least 2 per set (3 if none of them resolves YES).
• Their YES bids sum to more than 1 + fees, so the NO side is underpriced as a group.

Orders · fill-or-kill, in this order
1. BUY NO `KXELECT-26-A` ×100 @ ≤ 60¢ (avg 60¢, fee $1.68) — A
2. BUY NO `KXELECT-26-B` ×100 @ ≤ 65¢ (avg 65¢, fee $1.60) — B
3. BUY NO `KXELECT-26-C` ×100 @ ≤ 67¢ (avg 67¢, fee $1.55) — C
Money        Contracts $192.00 + fees $4.83 = $196.83 · Payout min $200.00 · max $300.00 · 100 sets
Fees used    quadratic (series) · each fill rounded up (never understated)
Residual risks
• Leg risk: fill every leg (fill-or-kill) or unwind; a partial basket is a directional bet.
OMEN 0.1.0 · id 1a2b3c4d · prices verified on the full order book at 15:00:02 UTC · re-check before trading
```

When the play disappears, OMEN edits the same message to **⌛ GONE**, so stale orders are never left looking live.
`omen show <id>` prints the full record, including the exact V2 order bodies.
