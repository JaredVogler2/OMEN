# Weekly playbook: liquidity rewards with $100 a week at risk

This is the routine for a $100-a-week risk budget on Kalshi's Liquidity Incentive Program (LIP) after the
Oct 28, 2026 amendment. It was set on Oct 26, 2026 from the analyses in `docs/LIQUIDITY.md`, then corrected after two
independent adversarial reviews.

**The short version:**
* **Default:** $0 deployed.
* **Each day:** a free scan (`omen lp scout`) checks whether Kalshi has posted enough of what OMEN can actually trade.
* **On GO:** money goes in only as one capped measurement of real fill costs, not a scale-up ladder.
* **Expectation:** a GO week is worth roughly **−$5 to +$10**, sign unknown. The worst case is about **$15–35** in
  total before the stops end it.
* **Not a path to big returns:** nothing here turns $100 into hundreds of dollars a week.

## Why the default is $0

**What the amendment changed** (CFTC filing of Oct 14, 2026, effective Oct 28):
* Reward periods last 1–7 days, so hourly and 15-minute programs end.
* Rewards are set per sub-category, $10–$10,000 per business day.
* Contracts are chosen for being new and having wide spreads, and are posted at least 24 h ahead.
* Kalshi still pays only when both sides reach the target, and only at least $1.00 per period.
* The program ends by Jan 1, 2027.

**What that does to the opportunity:**
* The old programs wind down, from about $70k/day of rewards on Oct 28 to about $4k/day by Nov 3.
* As of Oct 26, only 21 new-rule programs were posted (about $775/day), and the first starts Oct 31.
* 37% of the dead-side replay profit came from 16-hour daily gas programs, which the 1-day minimum rules out. 61%
  came from 1–7 day programs (mean $131/week before live haircuts), mostly ones that ran into the market's close.

**Why the strategies don't pay at this size:**
* Quoting new markets at the best bid is about break-even: fills cost 1.4c per contract-day against 1.3c of reward.
  It turns negative once a follower bot reacts. Half of those markets settle months later, which locks up the budget.
* The one independent live run of 1c dead-side bids
  ([mirabilis-agent/lip-ledger](https://github.com/mirabilis-agent/lip-ledger), Oct 2–10, 2026) lost money. Kalshi
  paid rewards within 1–3% of the formula, but fills cost more than the rewards. Tape-based fill models, like the
  ones behind every replay here, underestimated fills on those bids 10–30x.
* The replays break even at 2.6–4.7x their modeled fills.
* Buying favorites at 99c into those bids pays fast traders. A once-a-minute bot holding one market per event breaks
  even.

## The weekly routine

| When (ET) | What |
|---|---|
| Daily, 6:41 PM | `omen lp scout --notify` (systemd timer, `scripts/install-pi.sh --with-scout-timer`) posts GO or NO-GO to Discord. |
| On GO | Run one measurement with the trial settings below. |
| Monday 9 AM, during a trial | `omen lp status` (real fills against modeled) and `omen lp reconcile --since <trial start>` (what Kalshi paid). |

### The GO test (`omen lp scout`)

**What counts as qualifying.** Only programs OMEN's dead-side filters accept count. The values are in
`config/omen.toml`:
* 1–7 days long (`min_program_hours = 24`, `max_program_hours = 168`);
* at least $30 per period (`dead_side_min_period_reward_usd`), so the $1 minimum stays reachable;
* Target Size up to 1,000 (`dead_side_max_target`): 1,100 contracts at 1c fit a $12 per-market loss cap;
* ending within 24 h of the market's close or after it (`dead_side_close_gap_hours = 24`);
* not in OMEN's dead-side excludes.

**How they're weighted.** Each program counts its reward per day over the hours it overlaps the week. Programs of 48 h
or more count at 0.3x, because that is what they earned per reward dollar in the replays.

**GO needs all of these:**
* a weighted pool of at least **$20,000/day**, about twice the replay's break-even;
* at least **30 markets across 5 series**;
* **`scout_rewards_confirmed = true`**. The amendment sets rewards per sub-category, while the API still lists a
  reward per market. If Kalshi repeats a pooled amount on every market, every OMEN estimate is several times too high,
  the fill guard never trips, and the flow filter passes everything. Before setting this to true, check that the
  per-market amounts on Kalshi's Liquidity Rewards page match the API. The scout flags groups of 5 or more markets
  that share one reward, start, end and target.

The scout also reports decided markets with an empty dead side right now, whether daily gas or diesel programs came
back, and any program entries OMEN cannot read.

**First result.** For Oct 28–Nov 3 it read 354 qualifying markets in 32 series and a pool of $878/day weighted
($2,914/day unweighted): **NO-GO**.

### The measurement (only after a GO)

Override these in `config/omen.toml`, and set `OMEN_LIVE_ACK`, `KALSHI_API_KEY_ID` and `KALSHI_PRIVATE_KEY_PATH`:

```toml
[liquidity]
mode = "live"
max_total_capital_usd = 50.0   # the whole trial; 0.30 x $50 = $15 per series, enough for one $11 side
dead_side_max_loss_usd = 12.0  # 1,100 contracts at 1c
max_series_share = 0.30
exclude_series = "."           # no cheap-side quoting at all
max_fill_loss_usd = 15.0       # fills minus settlement payouts: the trial's hard stop
fill_guard_ratio = 0.4
fill_guard_hours = 168
fill_guard_min_usd = 8.0       # about three typical sweeps before the ratio is judged
```

**Before starting:**
* Use a fresh `OMEN_STATE_DIR`, so the loss cap and the modeled-fill total start at zero.
* Use a dedicated Kalshi subaccount, so reconcile sees only this bot's cash.
* Route a Discord research or default channel, so warnings arrive.
* Make sure the API key's location attestation is current. Without it Kalshi refuses sports, elections and
  entertainment markets, and OMEN holds off such a market for 6 hours after a 403.
* Keep `omen lp run` going after a stop. Killed cycles still record cash, so later payouts are reconciled.

**What OMEN does in the trial:**
* bids only empty dead sides in qualifying programs;
* sets every order to expire 6 h before its market closes, so a Pi that goes down cannot leave bids into the close;
* records each reward period so payouts can be checked period by period.

### Decision rules

**Judge on totals since the trial started, not week by week.** Single weeks are too noisy: in the replay's own good
case the fill guard tripped in 69% of weeks.

* **Fills.** `omen lp status` prints real fills against what the replays' model expected for the same orders. Decide
  once the model expected at least about $8 (three typical sweeps).
  * **2x or less:** the replays' fill model holds for this account. Continue.
  * **3x or more:** the live case study's world. Stop.
* **Payouts.** `omen lp reconcile --since <start> --until <now>` counts only the reward periods Kalshi should already
  have paid: those that ended at least a day before the window's end. It applies the $1 minimum and prints paid
  against expected, net (paid + settlements − fills − fees) and fills/paid.
  * **Paid under 50% of expected:** stop.
  * **No answer** (the balance convention is unknown): count it as a failure.
* **Hard stops.**
  * Fills minus settlement payouts reach $15 (`max_fill_loss_usd`, enforced in code).
  * The fill guard trips.
  * The program ends on Jan 1, 2027.
* **"Stop" is permanent** for this program. Restarting after a stop is what turns a $15 worst case into $35.
* **Raising the cap to $100** is worth it only if the trial passes and the $50 cap was binding. In the replays, the
  $100 stake had about 2 bids resting at once.

## What the scout cannot tell you

* **Fills:** whether this account's real fills are close to the model's. Only the measurement shows that.
* **Payouts:** how Kalshi will pay a pooled sub-category reward. Check its Liquidity Rewards page.
* **Time:** whether a good answer arrives before Jan 1, 2027. About nine weeks remain, and a measurement needs a few
  of them.
