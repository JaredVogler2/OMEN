# OMEN — high-confidence Kalshi scanner

OMEN scans every open Kalshi market and alerts only on plays that clear a high bar **after every fee**. It posts
them to the same private Discord server as STONKS2026/APEX, with the exact orders to place. It runs for $0 on the
Raspberry Pi you already have for APEX, or any always-on Linux box. GitHub Actions is an optional fallback.

It never places a real order unless you explicitly turn execution on ([docs/EXECUTION.md](docs/EXECUTION.md)).

A private **website** on Vercel's free plan shows every play worth playing live, runs scans on demand and explains
the fees behind each number ([docs/WEB.md](docs/WEB.md)).

## What counts as "high confidence"

OMEN has two tiers, and it never mixes them up.

| Tier | Meaning | Where it goes |
|---|---|---|
| 🔒 **LOCK** | The profit is **guaranteed by the contracts' own rules** at prices verified on the full order book, after fees rounded up. The remaining risks are execution (fill every leg) and exchange or rules events. These are listed on every alert. | `#kalshi-plays` + paper ledger |
| 📈 **MODEL** | Expected profit from a probability model, using the **worst case** over its uncertainty. Starts in **SHADOW**: logged and scored against real outcomes. It can be promoted only after it passes statistical gates and you run `omen promote`. | `#kalshi-research` while in shadow; `#kalshi-plays` once promoted |

LOCK strategies ([docs/STRATEGIES.md](docs/STRATEGIES.md)):

* **NO basket.** In a mutually exclusive event, at most one market can resolve YES, so NO on *k* of its markets pays at
  least *k − 1*. When the YES bids sum to more than 1 plus fees, the basket locks a profit.
* **Ladder.** If outcome A implies outcome B (for example, BTC > 90k implies BTC > 85k), then YES on B plus NO on A
  pays at least 1 in every outcome. When the market prices A above B by more than fees, that is a lock.
* **YES basket.** If an event's buckets provably cover every outcome, YES on all of them pays 1. This is used only
  when the rules have no "everything resolves NO" or "last fair price" clause.
* **Cross-series lock.** When different series settle on the same number (SOL "above $X" thresholds next to SOL
  "$X to $Y" buckets), the cheapest mix of YES and NO positions that covers every possible value pays at least 1.
  OMEN finds it as a shortest path over the strikes.

MODEL strategy: **crypto threshold**. This covers the BTC/ETH/SOL/XRP/DOGE price markets that settle on CF Benchmarks
indices. Spot is the median of Coinbase, Kraken and Bitstamp. Volatility comes from realized and Deribit implied
estimates. The model takes the worst case over the volatility range, normal versus fat tails, and a spot-basis band.
It only considers favorites, where the worst-case win probability is at least 85%.

Strictness is the point. On 2026-10-23 a naive scan of a live snapshot found **37,960** "ladder arbitrages". All but one
were false: they paired different players or opposite teams. The single real one offered 0.01 contracts. OMEN treats
two markets as one underlying only when wording, rules, timing, participant and strike type all match. It also
gives a market interval semantics only when its strike fields agree with its rules text. That second check was
added after the first full live scan: Kalshi tagged "exactly 4 Starship launches" as `less`, which produced a fake
29% "lock". As a last line of defense, any "lock" returning more than 20% goes to `#kalshi-status` as **SUSPICIOUS**,
never to `#kalshi-plays`.

## Fees: exact, live, and never understated

* Each series' fee type and multiplier are read from `GET /series` (all ~15k series in one call).
* Scheduled series fee changes (`/series/fee_changes`) and per-event overrides (`/events/fee_changes`) are applied
  when they take effect. For example, World Series games switch from ×0.5 to ×1. Within 60 minutes of a change, the
  more expensive terms are used.
* Fees are computed with the published formula `round_up(multiplier × rate × contracts × P × (1−P))`. Each fill on each
  price level is rounded up separately. These numbers match Kalshi's published examples exactly (tests).
* Every change OMEN observes goes to `#kalshi-status` and an audit log (`omen fee-log`). An unknown fee type is
  **fail-closed**: those markets are excluded until the formula is added. The formula constants live in
  [config/fee_schedule.toml](config/fee_schedule.toml), and OMEN reminds you to re-verify them against Kalshi's PDF.
  Details: [docs/FEES.md](docs/FEES.md).

## Recommended cadence

| Loop | Default | Why |
|---|---|---|
| Hot sweep: markets closing within 72 h (~35k) | **every 5 min** | Short-dated markets are where mispricings appear, and where small locks earn a real annualized return. About 35 requests per sweep, under 1 request/s on average. Kalshi's Basic limit is 20/s. |
| Full sweep: every open market (~170k) | every 60 min | Long-dated prices move slowly. A lock that ties up capital for months rarely clears the 15% APR floor anyway. |
| Crypto model | every 60 s | Spot moves continuously, and each pass costs only ~20 small requests. |
| Fee terms / fee changes | 6 h / 30 min | One request each, plus a 60-minute guard window before any scheduled change. |
| Settlement and calibration | 30 min | Paper positions and model predictions are settled from official results. |

Truly fleeting arbitrage is gone in seconds and no polling bot catches it. Five minutes catches the persistent
mispricings a person can act on. Enabling execution catches more of them.

## Quick start

```bash
# On the Pi (or any Linux box) that runs APEX; uv is already installed there
git clone https://github.com/JaredVogler2/OMEN.git ~/OMEN && cd ~/OMEN
uv sync --frozen
uv run omen scan                        # read-only: prints verified plays now (no keys, no Discord)
uv run omen fees --series KXBTCD        # the fee terms in force, scheduled changes, a fee quote

scripts/install-pi.sh                   # creates ~/.config/omen/omen.env (chmod 600) and the systemd service
nano ~/.config/omen/omen.env            # add Discord webhooks (docs/DISCORD.md), then:
scripts/install-pi.sh --with-backup-timer
uv run omen --env-file ~/.config/omen/omen.env discord-test
journalctl -u omen -f
```

## Website

Plays, on-demand scans, a fee explorer and calculator, the crypto model's view, paper performance and system health,
on Vercel Hobby with free Upstash Redis: $0. The Pi pushes its live state to the site after each cycle (signed with a
shared secret); the site never holds Kalshi keys or places orders. Setup takes about 10 minutes:
[docs/WEB.md](docs/WEB.md).

## Commands

| Command | What it does |
|---|---|
| `omen run` | The service loop. systemd runs this. |
| `omen once [--kind full\|hot] [--dry-run]` | One complete cycle with alerts, for cron or Actions. `--dry-run` prints the Discord payloads instead. |
| `omen scan [--orders]` | Read-only scan. Prints verified plays and their exact V2 order payloads. |
| `omen fees [--series S --event E --price P --count N]` | Fee terms in force, scheduled changes, and a fee quote. |
| `omen fee-log` | Every fee change OMEN has observed. |
| `omen report` | Paper ledger, model validation status, and recent runs. |
| `omen show <id>` | The full record of an alerted play (the id is in the alert footer). |
| `omen promote crypto_threshold [--state shadow]` | Moves a model to live once its gates pass, or demotes it. |
| `omen doctor` / `omen discord-test` / `omen digest` / `omen backup <file>` | Operations. |
| `omen publish` | Pushes the live feed to the website once (setup check; `omen run` publishes on its own). |
| `omen web` | Runs the website locally (API + built frontend) for previews. |

## Documentation

* [docs/STRATEGIES.md](docs/STRATEGIES.md): the math, the structural checks, and the residual risks.
* [docs/FEES.md](docs/FEES.md): fee formula, live terms, rounding, and change detection.
* [docs/OPERATIONS.md](docs/OPERATIONS.md): Pi install, the Actions fallback, costs, health checks, backups, and the kill switch.
* [docs/DISCORD.md](docs/DISCORD.md): channels in the STONKS server, webhooks or the APEX bot token.
* [docs/EXECUTION.md](docs/EXECUTION.md): optional demo and live order placement and its guards.
* [docs/WEB.md](docs/WEB.md): the website: pages, architecture, free-plan budget, setup and security.
* [docs/PI_HARDWARE.md](docs/PI_HARDWARE.md): one Raspberry Pi for STONKS and OMEN: shopping list and step-by-step plan.

## Status and honesty

* Tested: 93 unit and end-to-end tests. They cover fee math against Kalshi's published examples, structural traps
  (opposite-team spreads, holes in partitions, fair-price clauses), a fake-exchange pipeline (alert → GONE edit →
  settlement → fee-change notice) and the website API (auth, signed ingest, streamed scans, budgets). Read-only scans
  against the live Kalshi API ran from the build environment. The latest full scan (2026-10-23) covered 169,024
  markets in 85 s: 4 structures looked profitable at the top of the book, and none survived full depth, fees and the
  APR floor. That is normal on efficient books.
* Not yet done: running on the Pi, posting to the real Discord server, or placing any order (demo or live). Those
  need your accounts.
* Real LOCK plays are rare and often small. Most cycles will find nothing, and that is the correct output. The paper
  ledger and the daily digest show what OMEN found and whether it held up.
* Nothing here is financial advice. Prediction-market trading can lose money, and execution and leg risk are real.
