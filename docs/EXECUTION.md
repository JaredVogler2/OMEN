# Execution (optional, off by default)

OMEN scans and alerts without any Kalshi account. Each alert carries the exact orders, and `omen show <id>` prints
the exact request bodies for `POST /portfolio/events/orders`, Kalshi's V2 order endpoint. Any execution system can
consume them.

OMEN can also place the orders itself. That is **off** unless you change it.

| mode | what happens | needs |
|---|---|---|
| `off` (default) | nothing is ever sent | — |
| `demo` | orders go to Kalshi's demo exchange (fake money) to prove keys, signing and payloads | demo API key |
| `live` | real orders | production API key **and** `OMEN_LIVE_ACK="I accept real-money risk"` |

## Guards (all modes)

* **LOCK plays only.** MODEL plays are executed only if `[execution].allow_models = true`. Do not do this before the
  model passes its gates.
* **Per-play cap.** A larger play is scaled down to `max_cost_per_play_usd` (default $50). It is executed only if it
  is still profitable when every leg fills at its worst limit price, with fees recomputed for the smaller size.
* **Daily cap** (`max_daily_cost_usd`, default $200, counted per New York day). There is also a balance check before
  every play.
* **Kill switch.** `touch ~/.omen/KILL` stops all execution at once. Scanning and alerts continue.
* **Order type.** Every leg is a **fill-or-kill** limit order at the verified worst price, with
  `self_trade_prevention_type = taker_at_cross` and `cancel_order_on_pause = true`.
* **Thinnest book first.** If the first leg cannot fill, nothing has been bought. If a later leg fails after earlier
  legs filled, OMEN stops and posts a **LEG RISK** alert listing exactly what you hold, so you can unwind it.
* **No retries on order POSTs.** Network errors and 5xx responses are never retried automatically, because a
  duplicate order is worse than a missed one.
* Every request and response is written to the `executions` table in the ledger.

## Setting it up

1. Kalshi → Account → **API keys**. Create a key; RSA and Ed25519 are both supported. Save the private key as
   `~/.config/omen/kalshi-private-key.pem` and `chmod 600` it. OMEN refuses keys that group or others can read.
2. `scripts/install-pi.sh --with-exec` installs the signing library.
3. Run on demo first. In `omen.env` set `OMEN_EXECUTION_MODE=demo`, `KALSHI_API_KEY_ID=…` and
   `KALSHI_PRIVATE_KEY_PATH=…`, using a key from Kalshi's demo environment. Watch `#kalshi-status`. Many production
   tickers do not exist on demo; their rejections are expected and harmless.
4. Live, only when you are ready: `OMEN_EXECUTION_MODE=live` and `OMEN_LIVE_ACK="I accept real-money risk"`. Start
   with small caps.

Requests are signed as `timestamp_ms + METHOD + path`, where the path includes `/trade-api/v2` and excludes the
query string. RSA uses RSA-PSS with SHA-256 and a salt equal to the digest length; Ed25519 signs directly. Both are
covered by tests that verify the signatures.

**Status: untested against Kalshi's real or demo exchange.** The orchestration, caps and leg-risk handling are tested
against a fake exchange. The first real run must be in demo mode.
