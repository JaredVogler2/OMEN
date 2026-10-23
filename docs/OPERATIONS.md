# Operations

## Where it runs (zero cost)

**Primary: the Raspberry Pi 5 that runs APEX** (or any always-on Linux machine). OMEN is a single Python process
under systemd. Its marginal cost is a few watts and some bandwidth. It shares only the Discord server with APEX,
never a database, lock or token file.

| Option | Cost | Cadence | Notes |
|---|---|---|---|
| Raspberry Pi / home Linux box | $0 | 5 min hot, 60 min full, 60 s models | recommended |
| Oracle Cloud "Always Free" ARM VM | $0 (needs a card on file) | same | if the Pi is not on 24/7 |
| GitHub Actions (`omen-scan.yml`) | $0 within the free minutes | every 3 h by default | fallback only; see below |

### Why not GitHub Actions as the primary runtime

GitHub Free includes 2,000 Actions minutes a month for *all* private repositories on the account. STONKS2026
already reserves about 1,100 of them (see its `docs/COST_AND_QUOTAS.md`). A full OMEN scan bills 3–5 minutes, so even
an hourly schedule would exceed the remaining budget, and Actions schedules can be delayed or dropped. The workflow
ships **disabled**: it runs on schedule only when the repository variable `OMEN_ACTIONS_SCAN` is `true`, and a
skipped job bills nothing. If OMEN is ever made public, Actions minutes become free. Either way, set
**GitHub → Billing → Spending limits → Actions = $0** so nothing can ever be charged.

## Install on the Pi

```bash
git clone https://github.com/JaredVogler2/OMEN.git ~/OMEN && cd ~/OMEN
scripts/install-pi.sh                       # deps from uv.lock, ~/.config/omen/omen.env (600), systemd unit
nano ~/.config/omen/omen.env                # Discord routes (docs/DISCORD.md), optional health check URL
scripts/install-pi.sh --with-backup-timer   # re-render, start, nightly ledger backup (keeps 14)
uv run omen --env-file ~/.config/omen/omen.env discord-test
journalctl -u omen -f
```

Update: `cd ~/OMEN && git pull && scripts/install-pi.sh`. The service restarts with the new code.

The unit runs as your user with systemd hardening: read-only home except `~/.omen` and the venv, no new privileges,
and at most 900 MB of memory. Exit code 2 (bad configuration) and 3 (another instance holds the lock) are not
restarted, so a typo cannot hammer Kalshi or Discord.

## What it costs to run

Measured from the build environment on 2026-10-23, through a shared and rate-limited proxy:

* Hot sweep: 34,873 markets, ~35 market pages plus a one-time event backfill. 173 requests on the first run; later
  runs need about 35 pages plus a few event lookups.
* Full sweep: 170,159 open markets in 222 requests and 132 s with the event cache warm. The first run also backfills
  event metadata and took 450 requests. Pages hold 1,000 markets each, about 160 KB gzipped.
* Bandwidth at the defaults: about 2–3 GB of compressed JSON a day. Setting `hot_interval_seconds = 600` roughly
  halves it.
* Kalshi rate limit: the Basic tier allows 20 reads per second. OMEN caps itself at 8 per second and averages under 1.
  It backs off on HTTP 429 and honors `Retry-After`.
* CPU and memory: parsing about 35k markets takes a few seconds on a Pi 5. The resident set is a few hundred MB
  during a full sweep.

## Health and alerting about OMEN itself

* `#kalshi-status` receives: startup, fee changes, scheduled fee changes, unknown fee types, fee-document changes, the
  re-verify reminder, cycle errors (at most one per hour per kind), LOCK plays that settled below their guarantee,
  execution events, and the **daily digest at 8 pm New York time**.
* A dead-man's switch: set `OMEN_HEALTHCHECK_URL` to a free healthchecks.io check. OMEN pings it after every good
  cycle and pings `/fail` on errors. If the Pi dies, healthchecks.io alerts you, which OMEN cannot do for itself.
* Discord outages: messages wait in the ledger outbox and are retried each cycle, up to 8 attempts. `omen report`
  shows pending and dead messages.

## Files

| Path | What |
|---|---|
| `~/.config/omen/omen.env` | secrets and runtime environment (600) |
| `~/.omen/omen.sqlite3` | ledger: events cache, fee terms and fee log, opportunities, paper positions, predictions, outbox, runs |
| `~/.omen/omen-dryrun.sqlite3` | used by `omen scan` and `omen once --dry-run`, so test runs never mark real alerts as sent |
| `~/.omen/backups/` | nightly copies (with `--with-backup-timer`) |
| `~/.omen/KILL` | create this file to stop all order execution immediately |
| `config/omen.toml` | thresholds and cadence (unknown keys are rejected) |
| `config/fee_schedule.toml` | fee formula constants (see FEES.md) |

## Tuning

* Fewer, bigger plays: raise `[lock].min_profit_usd` or `min_return_pct`.
* More plays: lower `min_apr_pct`, but long-dated locks tie up capital.
* Quieter Discord: raise `[alerts].cooldown_minutes` and `improvement_pct`, or set `post_gone_updates = false`.
* Turn the crypto model off with `[crypto].enabled = false`.
