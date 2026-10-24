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
  `max_total_capital_usd` is used. No bid is left resting within `stop_before_close_minutes` of a market's close.
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

### 2. Forward paper test on live data

<!-- FORWARD_RESULTS -->

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
