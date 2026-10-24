# Liquidity rewards (`omen lp`)

Kalshi pays people to keep resting orders in the book. Its Liquidity Incentive Program (LIP) had about **$182,000 a
day** of rewards active on 2026-10-24 across 6,220 markets, and paid out **$1.2 to $2.3 million a week** from
September to October 2026. `omen lp` rests small bids where those rewards are paid, counts every fill and every
reward, and settles the inventory. It starts in paper mode.

This is the strategy the research found worth running. The others are in [STRATEGIES.md](STRATEGIES.md), and most of
them lost to fees or found nothing.

## How the reward is computed

The rules below come from Kalshi's CFTC filing, the October 2026 amendment, Appendix A §5. `omen.liquidity.scoring`
implements them exactly, and the tests check its payout against Kalshi's own worked example.

1. Once a second, at a random moment, Kalshi takes a snapshot of each rewarded market.
2. For each side (YES bids, and NO bids; a YES ask counts as a NO bid), it walks the price levels from the best one
   down. Every order at a visited level qualifies; there is no time priority inside a level.
3. The **reference price** is the first level where the running size reaches one fifth of the **target size**,
   usually 1,000 contracts. The walk stops after the level where the running size reaches the full target. If one
   side never reaches the target, the snapshot pays nobody.
4. Each order scores `size × 0.5^(ticks below the reference price)`. A participant's share of a side is their score
   divided by the side's total score. Their snapshot score is their YES share plus their NO share.
5. **Payout** = your share of all snapshot scores × the period reward × (valid snapshots ÷ all snapshots).
   Amounts under $1 per period are not paid.

What follows: a bid of N contracts at the best price earns the same reward whichever side it is on. Its collateral,
and the most it can lose if it fills and expires worthless, is its price. A 4¢ bid earns what a 96¢ bid earns on
one twenty-fourth of the money. So the default policy rests one bid, at the best price, on the **cheaper side** of
each rewarded market.

## What the bot does every minute

* **Find programs.** It reads the active liquidity programs (`GET /incentive_programs`) and keeps the ones that
  pay at least `min_reward_per_day_usd`, last at least `min_program_hours`, and are not in `exclude_series`.
* **Read the books.** It fetches order books 100 at a time and reads the public tape, all markets in a few requests.
* **Fill orders.** In paper mode, a bid fills only after the size that was queued ahead of it at its price has
  traded, or at once when a trade goes through its price. In live mode, it reads its own fills from
  `/portfolio/fills`.
* **Quote.** It bids the best price on the cheap side. Size is capped by `capital_per_market_usd`, and inventory
  already held counts against that cap. Quotes are ranked by expected reward per dollar until
  `max_total_capital_usd` is used. No bid is left resting within `stop_before_close_minutes` (12 hours by default)
  of a market's close, because fills that late lose (Evidence §2).
* **Count rewards.** Each interval earns the average of its start and end reward rates under the filed formula. The
  end rate is computed in the new book, with our bid where it actually rested, so a fill or a better bid that
  overtook us cuts it.
* **Settle.** When a market settles it books the inventory P&L, and it posts a summary to `#kalshi-research` every
  `summary_every_hours`.

Live orders are post-only (they never take liquidity) and good-till-canceled, with `cancel_order_on_pause` set.
Stopping the service cancels every resting order: SIGTERM is caught. The live venue also cancels any orders a
crashed run left behind when it starts. The kill switch file (`<state_dir>/KILL`) cancels everything at the next
cycle.

## Evidence

### 1. Adverse selection in rewarded markets, from settled history

A resting bid gets filled when someone wants to trade, and that is often someone who knows more than the book.
`lip_bt` replayed settled markets that carried rewards: about 45 series × 40 markets, using 1-minute quotes and every
trade. It rested a bid on each side and filled it only when a trade went through its price, the most adverse kind of
fill. Each fill was then marked against the mid one hour later. Results are per contract per day of resting:

| Series (examples) | Fill mark-out (1 h) | Reward at the best bid (live books) |
|---|---|---|
| Rotten Tomatoes scores (KXRT) | −0.8¢ (−3.6, +2.7) | +0.7¢ |
| Trump approval (KXTRUMPAPPROVE), cheap side | +0.0¢ (−3.2, +3.7) | +4.1¢ |
| YouTube views (KXYTVIEWSW) | +1.5¢ (+0.2, +3.2) | (no live program sampled) |
| State gas prices (KXAAAGASD*) | −1 to −11¢ | +7 to +17¢ max |
| 15-minute FX / commodities / crypto lead | −4 to −30¢ **per fill**, ~190 fills a market-day | large, but far smaller than the losses |

Slow series lose little to fills. Fast series (15-minute markets, live sports, gas, and hourly temperature and rates)
are picked off, so they are excluded by default.

### 2. When fills hurt: the last 12 hours before close

Cheap-side fills at the best bid in the same settled markets, split by hours until the market closed. The older and
newer halves were checked separately, so the cutoff was not fitted to one period:

| Markets | Filled | Fills | 1-hour mark-out (95%) | Held to settlement |
|---|---|---|---|---|
| Older half | 12+ h before close | 279 | **+2.45¢ (+1.03, +4.63)** | +8.0¢ |
| Older half | last 12 h | 108 | −0.16¢ (−3.05, +3.02) | −7.4¢ |
| Newer half | 12+ h before close | 307 | **+2.05¢ (+0.65, +3.70)** | +0.1¢ |
| Newer half | last 12 h | 102 | −1.06¢ (−4.37, +2.04) | −7.0¢ |

Near the close the outcome becomes known, the losing side collapses toward 0, and resting bids on it fill on the way
down. The first forward paper run met exactly this on Trump-approval markets that settle on a 1 PM reading. So
`stop_before_close_minutes` defaults to 720: no resting bids in a market's last 12 hours.

### 3. The merged policy, replayed

This replays exactly what the default configuration does over the settled rewarded markets. It bids at the best
price on the cheaper side, only at 2–50¢, skips the excluded series, and never quotes in a market's last 12 hours.
A bid fills only when a trade goes through its price, which is the most adverse kind of fill. Each fill is marked
against the mid one hour later; settlement P&L on 1–5¢ bids is a lottery and too noisy to read. Rewards are the same
quote, sized to $25, scored with the filed formula in 36 live book snapshots (6 hours). Both are per dollar of
resting bid per day:

| Sample | Markets | Fills | Fill mark-out (95%) |
|---|---|---|---|
| Older half | 112 | 321 | **+14.9% (+3.2, +34.0)** |
| Newer half | 112 | 229 | **+6.9% (−0.9, +15.8)** |
| All | 224 | 550 | **+10.7% (+3.6, +19.5)** |

| Series | Fill mark-out | Reward (live books) | Net |
|---|---|---|---|
| Rotten Tomatoes (KXRT) | +17% (−16, +67) | +10% | +28% |
| Trump approval (KXTRUMPAPPROVE) | −24% (−87, +28) | +230% | +206% |
| Hormuz weekly (KXHORMUZWEEKLY) | −11% (−24, −3) | +7% | −4% |
| YouTube views (KXYTVIEWSW) | +26% (+10, +50) | not sampled | — |

On average, cheap bids filled more than 12 hours before close were bought below where the market settled an hour
later: impatient sellers dumping long shots. The rewards come on top. The percentages are large because a 2–10¢ bid
ties up very little money. In dollars, the per-market cap ($25) and per-series cap (30%) limit what any one market
can earn or lose.

### 4. Realized settlement on a broad sample (the sobering part)

The 1-hour mark-outs above are favorable, but **settlement** is what pays. The policy was replayed on a broad random
sample: 1,429 settled rewarded markets across 99 slow series, plus all 353 Trump-approval markets. Results are realized
fill P&L per contract per day of resting:

| Sample | Markets | Fills | Settled fill P&L (95%) | Live reward | Net |
|---|---|---|---|---|---|
| All slow series | 1,429 | 2,304 | **−2.5¢ (−4.7, −0.05)** | 3.6¢ mean, 0.55¢ median | marginal |
| …except speech / insider markets and Trump approval | 1,203 | 1,316 | +0.4¢ (−1.7, +2.6) | 1.1¢ | +1.5¢ |
| Trump approval, at the best bid | 353 | 731 | −12.4¢ (−35.4, +10.9) | 22.8¢ | +10.4¢ |
| Trump approval, 2 ticks behind | 321 | 207 | −2.7¢ (−13.7, +9.7) | 8.1¢ | +5.5¢ |

Most of the loss comes from markets that resolve on what someone says or on insider-decided outcomes:

* Trump mentions: −23¢
* "Trump says": −39¢
* Truth Social: −47¢
* VMAs: −31¢
* Netflix top movie: −31¢
* Big Brother eliminations: −73¢

Someone usually knows the answer first. These are now excluded by default, and quotes need a formula reward of at
least `min_reward_per_contract_day` (2¢), above the pessimistic end of the remaining fill cost.

**Honest status:** after these rules, the expected net is positive but small, and no configuration is yet positive
at 95% confidence. Outside Trump approval, the chance that the net is positive is about 92%, and the speech/insider
exclusion was chosen after seeing this data. Trump approval carries most of the reward, and its fill cost is too
noisy to pin down. Keep it in paper.

### 5. Dead sides: completing books that pay nobody

Rewards pay only while both sides reach Target Size. Once a market is decided (one side bid at 97¢ or more), the
other side is worth about 1¢ and often has too few bids, so every snapshot is excluded and nobody is paid. In 6
hours of sampled live books, 31% of daily and weekly program snapshots were short of the target on a side, which
left about $21,900 a day of rewards unpaid.

`complete_dead_sides` (on by default in paper) rests a 1¢ bid on such a side, sized to complete the target, and
only when that side is short of it:

* **Value:** the bid costs about what it is worth.
* **Downside:** it can lose at most 1¢ per contract. It fills when someone buys the favorite at 99¢.
* **Upside:** if the "dead" outcome happens anyway, it pays 99¢.
* **Cap:** `dead_side_max_loss_usd` (default $50) limits inventory per market.
* **Crossing:** prices are whole cents, so while the favorite is bid at 99¢ a 1¢ bid would trade instead of resting
  (a post-only order is rejected). The bot only completes a side while the favorite's best bid is 97¢ or 98¢.

The replay assumed the worst case for fills: our 1¢ bid absorbs every trade that bought the favorite at 99¢. It counts
only the minutes when the favorite's best bid was 97–98¢ (an earlier version also counted 99¢ minutes, when the bid
could not have rested; that overstated the decided time by 70% and understated the cost per day).

| Settled rewarded markets while decided (12+ h before close, favorite bid 97–98¢) | Value |
|---|---|
| Markets / decided market-days | 1,053 / 1,302 |
| Contracts that would have filled the 1¢ bid | 1,069 per market-day |
| Settled cost of those fills | **−$10.66 per market-day (95% interval −$13.21 to −$8.65)** |
| Dead side won anyway | 2 of 1,053 markets |
| Program reward while decided | $30.66 per market-day; half of it ($15.33) when our bid alone completes the target |

Even in that worst case the reward exceeds the cost by about $4.67 per decided market-day. The fill cost is measured
tightly because it depends only on how many people buy favorites at 99¢. The uncertain parts are the share (half the
reward only while nobody else bids on the dead side; in the live sample that was the case in 31% of snapshots) and
whether Kalshi pays it at all, which only a live account can confirm.

**Compliance caveat, plainly:** these are real, fillable, fair-priced bids that make a market meet the two-sided
requirement the program pays for. But Kalshi's filing lets it end eligibility for activity it judges "inconsistent
with the purpose of the Program", and liquidity in a decided market is of little use to traders. Kalshi could decide
this does not deserve rewards. Read Kalshi's current program terms (kalshi.com/regulatory/notices) and set
`complete_dead_sides = false` if you would rather not take that risk. It is paper-only until you go live.

### 6. Forward paper test on live data

First day, Saturday 2026-10-24, about 4–5 hours, $1,000 cap, paper only. Rewards are estimated with the filed
formula. "Settled" is real settlement P&L on the paper positions, and "open" is the remaining positions marked at
the mid.

| Run | Rewards | Settled P&L | Open (marked) | Net | Realized (rewards + settled) |
|---|---|---|---|---|---|
| `stop_before_close_minutes = 120` (5.2 h) | +$40.02 | −$49.96 | +$73.02 | +$63.08 | **−$9.94** |
| `stop_before_close_minutes = 720` (3.9 h) | +$37.20 | $0.00 | +$22.19 | +$59.39 | **+$37.20** |

Fees were $0: the bot only places post-only maker orders, and these series charge makers nothing.

The 2-hour run's bids on Trump-approval markets filled in the hours before the 1 PM reading that settles them. They
lost more at settlement than all the rewards earned, which is the late-fill effect in §2. The 12-hour run did not
hold those positions.

This is one weekend day. Rewards came mostly from Trump approval (about 60%), Rotten Tomatoes and EO-week markets,
and the open positions settle over the following days. Treat it as a direction, not proof. Run paper on the Pi for
several weekdays before risking money, then start live small (see below) and compare Kalshi's actual reward credits
with `omen lp status`.

## Risks, plainly

* **The program changes.** An amendment that takes effect on 2026-10-28 makes every reward period last at least one
  day, so the 15-minute and hourly programs end, and pools may become per sub-category. The program expires on
  2027-01-01 unless Kalshi files to renew it. The bot reads whatever programs exist each cycle, so it adapts, but the
  money depends on Kalshi continuing to pay.
* **Competition.** Other bots farm the same rewards. Cheap-side queues of 20,000 to 60,000 contracts at 1¢ appear
  and vanish within minutes. Your share is what remains after them. Re-check `omen lp programs` and the realized
  rewards often.
* **Fills.** A filled bid becomes a position. On the cheap side the loss per contract is capped at its price, but
  long shots are, if anything, overpriced (see the atlas), so expect small losses per fill. The per-market cap stops
  quoting once inventory reaches it.
* **Estimates vs. payouts.** Paper rewards are computed with the filed formula from the books the bot sees once a
  minute. Kalshi samples every second and pays after each period ends. Start live small and compare Kalshi's actual
  reward credits with `omen lp status` before scaling.
* **Kalshi's discretion.** The filing lets Kalshi end eligibility for activity "inconsistent with the purpose of the
  Program", and it surveils for non-bona fide orders. The bot's orders are real, fillable bids at the best price,
  which is what the program pays for. It never places orders it intends to cancel before they can trade, and it does
  not chase snapshot timing.
* **Taxes and eligibility.** Rewards are income. Kalshi needs a verified SSN to pay rewards above the IRS reporting
  thresholds. Kalshi affiliates, Introducing Brokers, FCM customers and contracted market makers are not eligible.

## Running it

```bash
omen lp programs              # what the policy would earn per dollar in each live program right now
omen lp run                   # the loop (paper by default); Ctrl-C or SIGTERM cancels every resting order
omen lp status                # rewards, fills, inventory, settled P&L (day / week / total)
scripts/install-pi.sh --with-lp   # run it as the omen-lp systemd service next to omen.service
```

Going live, after paper looks right for several days:

1. Install the signing extra (`install-pi.sh --with-exec`) and put `KALSHI_API_KEY_ID` and `KALSHI_PRIVATE_KEY_PATH`
   in the environment file. Keep the key file `chmod 600`, on the Pi only.
2. Start small: in `config/omen.toml`, set `[liquidity] max_total_capital_usd = 100` and
   `capital_per_market_usd = 10`.
3. Set `[liquidity] mode = "live"` (or `OMEN_LP_MODE=live`) and
   `OMEN_LIVE_ACK="I accept real-money risk"`, then restart `omen-lp`.
4. After the first reward periods end, compare Kalshi's reward credits with `omen lp status`. Scale up only if they
   agree.

Settings live in the `[liquidity]` section of `config/omen.toml`; every key is commented there.
