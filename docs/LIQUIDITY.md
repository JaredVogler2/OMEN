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

The 1-hour mark-outs above are favorable, but **settlement** is what pays. The policy was replayed on 5,215 settled
rewarded markets across 1,245 series, plus all 353 Trump-approval markets:

* **Sample:** up to 6 random markets per series, closing between October 2025 and October 2026, fetched from Kalshi's
  live and historical endpoints.
* **Exclusions:** the speech and insider series below are already excluded.
* **Unit:** realized fill P&L per contract per day of resting.
* **Intervals:** 95%, resampling whole series, because markets in one series share news.

| Sample | Markets | Series | Fills | Settled fill P&L (95%) |
|---|---|---|---|---|
| All except Trump approval | 5,215 | 1,245 | 9,546 | **−2.3¢ (−3.5, −1.1)** |
| …closed before 2026-09-08 | 2,606 | 822 | 6,729 | −3.8¢ (−5.7, −1.9) |
| …closed after | 2,609 | 609 | 2,817 | −0.1¢ (−1.2, +1.1) |
| …scheduled-data categories (economics, financials, weather, commodities, crypto) | 2,105 | 549 | 3,051 | −1.5¢ (−3.2, +0.1) |
| …announcement and decision categories | 3,212 | 701 | 6,704 | −2.8¢ (−4.4, −1.2) |
| Trump approval | 353 | 1 | 731 | −12.4¢ (−35.4, +10.9) |

The live reward at the same placement:

* **Outside Trump approval:** 1.1¢ per contract-day on average (median 0.4¢). Only about one snapshot in six reaches
  2¢, and those average 4.5¢.
* **Trump approval:** 22.8¢.

The worst losses come from markets where someone usually knows the answer first:

* **Speech markets:** Trump mentions −23¢, "Trump says" −39¢, Truth Social −47¢.
* **Award and show results:** VMAs −31¢, Netflix top movie −31¢, Big Brother eliminations −73¢.
* **Announcements and decisions:** coach hires, draft picks, trades, government shutdown length, Iran and Hormuz news,
  from −80¢ to −320¢.

The speech and show series are excluded by default.

**Honest status:**

* **Plain quoting loses.** At the best bid it loses more on fills than the average reward pays.
* **Only high-reward markets can pay.** The `min_reward_per_contract_day` floor (2¢) keeps only markets where the
  reward is well above the fill cost.
* **That can't be checked from history.** Kalshi publishes no historical order-book depth, so whether fills cost the
  same in those markets has to come from the forward test.
* **Small samples mislead.** An earlier 1,429-market sample showed +0.4¢ for the same group, and the larger sample
  overturned it. Trust nothing here that rests on fewer than a few thousand markets.
* **Keep this policy in paper.** Section 5 is the part that holds up.

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

**Measuring the flow.** A first replay charged our bid with every 99¢ favorite purchase while the market was
decided. That understates the cost. Those purchases can only happen while someone bids 1¢ on the dead side, and the
side we complete is the one with no such bid. The corrected replay measures sales per minute that a 1¢ bid rested (a
"supply minute") and charges that rate on every minute we bid.

**The filter.** The bot bids only while a market's trailing 24 hours of sales into 1¢ bids, priced at 1¢ each, cost
at most `dead_side_max_flow_ratio` (default 0.5) times the reward the bid earns:

* **Inputs:** the rate is per supply minute and comes from the public tape (`DeadSideFlow`).
* **Before history:** until 30 supply minutes were seen, it assumes `dead_side_flow_prior`, 1 contract a minute ($14.40
  a day).
* **No lookahead:** the decision uses only the past.

| 5,062 decided market-days in settled rewarded markets (favorite bid 97–98¢, 12+ h before close) | Net per decided market-day (95%, resampling series) |
|---|---|
| Bid whenever the side is short | −$10.52 (−$29.51, +$0.25) |
| Filter at 1.0 | +$3.80 (+$1.87, +$6.34) |
| **Filter at 0.5 (default)** | **+$3.56 (+$1.66, +$6.24)** |
| Filter at 0.25 | +$3.47 (+$1.69, +$6.01) |
| Default, markets that closed before 2026-09-08 | +$2.81 (+$1.49, +$4.53) |
| Default, markets that closed after | +$4.59 (+$1.14, +$9.87) |

Without the filter, people dumping the loser all day cost $21.87 per decided market-day against $11.35 of reward. The
filter gives up a third of the reward and cuts the fill cost by more than four fifths. The result holds in both halves
of the sample and across filter settings from 0.25 to 1.0.

**Closer to the close.** A dead-side bid loses at most 1¢ a contract, so it gets its own stop,
`dead_side_stop_before_close_minutes`. The default is 6 hours; cheap-side quotes keep 12 hours. With the filter on,
the replay gave these totals:

| Stop before close | Net per decided market-day (95%) | Older half, total | Newer half, total |
|---|---|---|---|
| 12 h | +$3.52 (+$1.84, +$5.95) | +$8,357 | +$11,824 |
| **6 h (default)** | +$4.16 (+$1.80, +$7.69) | +$7,955 | +$16,828 |
| 3 h | +$4.06 (+$1.66, +$8.11) | +$7,678 | +$16,842 |
| 1 h | +$4.58 (+$1.54, +$10.21) | +$7,041 | +$20,947 |

* **Every stop pays:** all four make money in both halves.
* **Shorter stops are uneven:** each step shorter costs the older half a little and helps the newer half more.
* **Why 6 hours:** it adds 22% over 12 hours and gives up less than 5% in the older half. The 1-hour gain comes mostly
  from the newer half alone, so it isn't used.

**How to read these numbers:**

* **Fills, worst case:** our bid absorbs every sale.
* **Reward, best case:** half the program reward while we bid. That needs three things: we hold the whole dead side,
  the favorite's side reaches the target, and Kalshi pays.
* **What history can't check:** with no historical order-book depth, the replay can't test the first two conditions.
  The bot checks them from the live book before every bid.
* **Why the sign should hold:** the bot bids only when completing the side pays, so reward and fill cost shrink
  together when that state is rarer than the replay assumes. The third condition only a live account can confirm.

**Paper charges the expected dead-side fills.** The public tape cannot show sales into a bid that does not exist.
So while our paper 1¢ bid is the only one on its side, the paper venue would never fill it. Instead, paper mode
books the expected fills each minute at the market's measured selling rate, as the replay does. Where someone else
also bids 1¢, the real trades and the queue model decide.

**Compliance caveat, plainly:**

* **Within the published rules:** Kalshi's help center sets no price limit on qualifying orders and does not exclude
  1¢ bids or decided markets. It scores "every resting order that helps reach the Target Size on its side".
* **Real bids:** these are real, fillable, fair-priced bids that make a market meet the two-sided requirement the
  program pays for.
* **The risk:** Kalshi's filing lets it end eligibility for activity it judges "inconsistent with the purpose of the
  Program". Its help center says it monitors for abuse and can revoke participation. Liquidity in a decided market is
  of little use to traders, so Kalshi could decide this does not deserve rewards.
* **What to do:** read Kalshi's current program terms (kalshi.com/regulatory/notices), and set
  `complete_dead_sides = false` if you would rather not take that risk.
* **Status:** it is paper-only until you go live.

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
