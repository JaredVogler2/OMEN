# The OMEN website

A private dashboard on Vercel's free Hobby plan. It shows the plays worth playing right now, runs on-demand scans,
and explains the fees behind every number. It runs the same engine, fee registry and strategies as the Pi, so a play
looks and prices identically on the site, in Discord and in `omen scan`.

The site never holds Kalshi API keys and never places orders. It reads public market data, and it receives the Pi's
live state over a signed channel.

## Pages

| Page | What it shows |
|---|---|
| **Plays** | Every live play from the Pi and from the latest on-demand scan: KPIs (live plays, locked profit available, settled paper P&L, last scan), filters and sorting, and a detail drawer with the exact orders, fees per leg, rationale, risks, raw evidence, a **Re-verify now** button (re-prices a LOCK against fresh order books) and copy-as-JSON orders. Also shown: the structures that came **closest to profitable**, an activity feed (alerts, expiries, fee changes, errors) and a plays-over-time chart. |
| **Scan** | Run a scan now: horizon (6 h to all ~170k markets), structures, and your own thresholds (min profit, return, APR, max capital). Progress streams stage by stage. Results include plays, candidates that failed verification and why ("after fees and depth the best size loses $1.51"), near misses and structures rejected by the safety checks. Shows the free-plan budget. |
| **Fees** | The fee curve by price (taker, maker, multiplied terms), a calculator using any series' live terms (with series search), scheduled fee changes for the next 30 days, the formula constants and when they were verified, and every series on non-standard terms (maker fees, multipliers, fee-free). |
| **Crypto model** | Per asset: live spot from three exchanges, volatility used, and for each daily ladder the model's worst-case probability band against the market's bid–ask at every strike, with any positive edges. The model stays in shadow until its calibration gates pass. |
| **Performance** | The Pi's paper ledger: settled P&L over time, win rate, open positions and expected profit, model validation status. |
| **System** | Setup checklist, live-feed health (last push, host, version, cadence, Discord outbox), thresholds, this site's budget and recent scanner runs. |

Dark and light themes, phone layout, and keyboard focus states are built in.

## How it fits together

```
Raspberry Pi (omen run) ── every cycle, signed snapshot ──▶  Vercel: /api/ingest ──▶ Upstash Redis (free)
                                                                     ▲
Browser ── password session ──▶ Vercel static site + /api/* (one Python function) ──▶ Kalshi public API
```

* **Frontend:** React + TypeScript + Tailwind, built by Vite into `web/dist` and served from Vercel's CDN.
* **API:** `api/index.py` serves every `/api/*` route (`vercel.json` rewrites them to the one function). It imports
  `src/omen`, so the scan logic is the Pi's. Scans stream progress as NDJSON.
* **State:** Upstash Redis over REST holds the Pi's latest snapshot, the last on-demand scan, a 7-day history and the
  budget counters. Without it the site still works, but it forgets everything when the function goes cold.
* **Pi → site:** after each cycle the Pi pushes a snapshot when the plays changed, and at least every 2 minutes as a
  heartbeat (`OMEN_PUBLISH_SECONDS`). Each push is HMAC-signed with `OMEN_INGEST_SECRET` and timestamped. The site
  rejects bad signatures, timestamps more than 5 minutes off, and replays. A failed push is logged and retried with
  backoff. It never interrupts scanning or Discord alerts.

## Cost: $0, and how it stays that way

| Resource | Free allowance | OMEN's use |
|---|---|---|
| Vercel Hobby functions | 4 h active CPU and 360 GB-h memory a month; 300 s max per request | Page loads are milliseconds. A 24-hour scan takes about 12 s and 3 CPU-seconds; a full ~170k-market scan about 85–105 s and 16 CPU-seconds (measured 2026-10-23). The site caps on-demand scans at **30 a day** and **180 CPU-minutes a month**, at least 45 s apart, so the rest of the allowance is always free for page views. |
| Upstash Redis (Vercel Marketplace, free plan) | 500k commands a month, 256 MB | About 6 commands per Pi push (~130k a month at the 2-minute heartbeat) plus 2 per dashboard refresh while a tab is open. Snapshots are capped at 950 KB. |
| Bandwidth | 100 GB a month | Snapshots are a few KB to a few hundred KB; the app bundle is ~100 KB gzipped. |

If Kalshi is slow, a scan stops paging at 200 s (`OMEN_WEB_SWEEP_SECONDS`) and reports a **partial scan** rather than
hitting Vercel's 300 s limit. The Pi always scans everything.

## Setup (about 10 minutes)

1. **Import the repository.** In Vercel, choose *Add New → Project*, pick `JaredVogler2/OMEN`, keep the framework
   preset as *Other* (`vercel.json` sets the build), and deploy. The first deploy shows the "one step left" screen.
2. **Add free storage.** In the project, go to *Storage → Create Database → Upstash for Redis* (Marketplace), choose
   the free plan and connect it to the project. It adds `KV_REST_API_URL` and `KV_REST_API_TOKEN`.
3. **Set environment variables** in *Settings → Environment Variables* (Production):

   | Name | Value |
   |---|---|
   | `OMEN_WEB_PASSWORD` | Required. The site's password, 8+ characters. Changing it signs everyone out. |
   | `OMEN_INGEST_SECRET` | Required for the Pi feed. A long random string, e.g. the output of `openssl rand -hex 32`. |
   | `OMEN_SESSION_SECRET` | Optional. Signs session cookies (defaults to the password). |
   | `OMEN_WEB_DAILY_SCANS`, `OMEN_WEB_MONTHLY_CPU_SECONDS`, `OMEN_WEB_MIN_SCAN_GAP_SECONDS`, `OMEN_WEB_SWEEP_SECONDS` | Optional budget overrides (defaults 30, 10800, 45, 200). |

4. **Redeploy** (*Deployments → ⋯ → Redeploy*) so the variables take effect, open the site and sign in.
5. **Connect the Pi.** Add these two lines to `~/.config/omen/omen.env` on the Pi, with the same secret as in Vercel:

   ```bash
   OMEN_WEB_URL=https://<your-project>.vercel.app
   OMEN_INGEST_SECRET=<same value as in Vercel>
   ```

   Then check and restart:

   ```bash
   cd ~/OMEN
   uv run omen --env-file ~/.config/omen/omen.env doctor     # [ok] website: ... ingest_enabled=True
   uv run omen --env-file ~/.config/omen/omen.env publish    # pushes one snapshot now
   sudo systemctl restart omen
   ```

   The System page's checklist turns green and the sidebar shows **Pi live**.

**Deployment Protection.** Vercel's default protection covers preview deployments, not the production URL, so the Pi
can reach `/api/ingest` directly. If you turn on protection for production too, create a *Protection Bypass for
Automation* secret in Vercel and put it on the Pi as `OMEN_VERCEL_BYPASS`.

## Security

* One owner password (`OMEN_WEB_PASSWORD`). Sessions are HMAC-signed cookies: HttpOnly, Secure, SameSite=Strict,
  30 days. Every state-changing request needs a CSRF header. Logins are throttled to 8 attempts per 15 minutes per IP.
  With no password configured, only `/api/health` answers.
* Strict Content-Security-Policy (scripts only from the site itself), `X-Frame-Options: DENY`, no referrer,
  `noindex`, and `no-store` on every API response.
* The ingest endpoint accepts only snapshots signed with `OMEN_INGEST_SECRET`, at most 5 minutes old and newer than
  the last one, up to 950 KB.
* No Kalshi keys, Discord webhooks or Pi secrets are ever sent to the site. Snapshots contain plays, paper results,
  run statistics and fee terms only.

## Working on it locally

```bash
uv sync                                    # includes uvicorn (dev group)
npm --prefix web ci && npm --prefix web run build
OMEN_WEB_PASSWORD=choose-a-password uv run omen web     # http://127.0.0.1:8787 (API + built site)

npm --prefix web run dev                   # hot-reloading frontend on :5173, proxying /api to omen web
```

`omen web` applies the same security headers as `vercel.json`, so CSP problems show up locally. Without Upstash
variables it uses an in-memory store. The API is tested in `tests/test_web.py` (auth, CSRF, throttling, signed
ingest, publisher round trip, streamed scans, budgets, re-verify and fee quotes).
