# Strategies

Each strategy produces plays in two steps. First, a cheap **screen** uses the top-of-book quotes from the market list.
Then **verification** fetches the full order books for every leg in one batch request (`/markets/orderbooks`, 100
tickers per call). It walks the book level by level, applies each fill's fee rounded up, and sizes the play to the
number of sets that maximizes the guaranteed profit. A play is alerted only if it clears every floor
(`config/omen.toml [lock]`):

* guaranteed profit ≥ `min_profit_usd` (default $1);
* guaranteed profit ÷ capital ≥ `min_return_pct` (default 0.75%);
* annualized ≥ `min_apr_pct` (default 15%), because capital locked for a year at 2% is not worth it;
* capital ≤ `max_capital_usd` (default $1,000) and each leg ≤ `max_contracts_per_leg`;
* every limit price lies on the market's tick grid (`price_ranges`, including the sub-cent bands).

Markets are excluded when they are not `active`, are provisional or multivariate (combo) markets, close within
`min_minutes_to_close`, or have a fee type with no configured formula.

## 🔒 NO basket (mutually exclusive events)

Kalshi flags some events `mutually_exclusive`: at most one of their markets can resolve YES. Buy NO on *k* of them,
one contract each, and the payout is at least *k − 1* (*k* if none of them wins).

* NO costs `1 − YES bid`. Leg *i* contributes `YES_bid_i − fee_i` to the edge, so the basket is profitable when
  `Σ (YES_bid_i − fee_i) > 1`.
* The screen picks every market whose contribution is positive. Verification also tries dropping the weakest legs,
  because a thin leg can cost more than it adds at depth.
* Robust to "everything resolves NO" outcomes (missing data, cancellation): those pay *k*, more than the guarantee.
* Residual risks: leg risk (use fill-or-kill and place the thinnest book first), and reliance on the exchange's
  mutually-exclusive flag. That flag is re-read from the API (incrementally, every 30 minutes) and never assumed.

## 🔒 Ladder (implied outcomes on one underlying)

When outcome A implies outcome B, P(A) ≤ P(B). Example: "BTC above 90k at 5pm" implies "above 85k at 5pm". Buy YES on
B and NO on A:

| Outcome | YES B | NO A | Total |
|---|---|---|---|
| A happens (so B does) | 1 | 0 | 1 |
| B but not A | 1 | 1 | 2 |
| neither | 0 | 1 | 1 |

The profit is `YES_bid(A) − YES_ask(B) − fees`. It is positive only when the book prices A above B.

**Fields must agree with the rules text.** Kalshi's strike metadata is not always self-consistent. In the live
data on 2026-10-23:

* "exactly *N* Starship launches" markets were tagged `strike_type: less` with floor = cap = *N*;
* "#3 seed or better" was tagged `less` with cap 3, which really means ≤ 3;
* some totals read "score more 1.5 runs".

Taken at face value, the first case turned "exactly 4" vs "exactly 7" into a fake 29% "guaranteed" ladder. The first
build of OMEN reported it in a read-only scan. A market now gets interval semantics only if both checks pass:

* its strike fields are consistent (a one-sided strike carries no opposite bound);
* its settlement rules use matching direction words: above / over / at least / *N*+ for upper strikes, below / under
  / less than / at most for lower strikes, a range for `between`. They must contain no opposite-direction words and
  no "exactly" (allowed only for single-point buckets).

About 99.6% of interval-tagged markets pass. The ~0.4% that fail are excluded from every strategy, not guessed at.
Regression tests: `test_exactly_n_markets_tagged_less_are_not_a_ladder` and
`test_rules_text_must_agree_with_strike_type`.

**Plausibility guard.** A LOCK whose guaranteed return exceeds `max_plausible_return_pct` (default 20%) is far more
likely a data problem than free money. It is posted to `#kalshi-status` as **SUSPICIOUS** for a manual rules check.
It never goes to `#kalshi-plays`, is never paper-traded and is never executed.

**Same-underlying test (the important part).** Two markets count as the same underlying only if *all* of these match:
event, title and settlement rules with every number masked, strike type, close time, settlement time and
participant. Implication is then checked from the strike intervals:

* `greater`: (floor, ∞)
* `greater_or_equal`: [floor, ∞)
* `less`: (−∞, cap)
* `less_or_equal`: (−∞, cap]
* `between`: [floor, cap]

A group with duplicate strikes is discarded as ambiguous.

On the 2026-10-23 snapshot, grouping by event alone produced 37,960 false "arbitrages". Grouping by ticker prefix
still produced 8,590, all of them opposite teams' spreads such as "California wins by over 1.5" against "SMU wins by
over 2.5". The full test above left one real inversion, and it was too small to trade.

Robust to all-NO outcomes: NO on A pays 1.

## 🔒 YES basket (provable partitions)

When an event's buckets cover every outcome, exactly one pays $1, so buying YES on all of them for less than $1 plus
fees locks a profit. OMEN requires all of the following:

* the event is flagged mutually exclusive, and the *complete* market list is re-read from the API just before
  verification. If any bucket is closed or paused, the play is dropped;
* the intervals cover (−∞, ∞). Touching boundaries must have at least one closed end. A gap is allowed only when it
  equals exactly one reporting step of the event's strikes: whole degrees (`59–60` / `61–62`), tenths (`8.1` /
  `8.2`) or cents (`84,999.99` / `85,000`). The alert names the step it relies on, and
  `allow_step_gap_partitions = false` disables these;
* the rules contain no clause under which every bucket can resolve NO or settle to a "last fair price" or "fair
  value". Example: crypto rules say "if no data is available … resolve to No". If such a clause is present, the
  basket is rejected and the reason is logged.

A YES basket pays 0 if the exchange voids or resolves every bucket NO. That residual risk is printed on every alert,
and these plays carry a lower confidence (0.95–0.97) than NO baskets and ladders (0.99).

## 🔒 Cross-series lock (covers)

Kalshi often lists one number in several series. For example, SOL's 5 pm price appears both as "above $X"
thresholds (`KXSOLD`) and as "$X to $Y" buckets (`KXSOLE`), and both settle on the same 60-second CF Benchmarks
average. Each market pays YES on an interval of that number:

* YES on a market buys its interval;
* NO on a one-sided market buys the other half-line (NO on "$70.25 or above" pays on anything below $70.25).

Any set of positions whose intervals together cover the whole number line pays at least $1, whatever the
settlement value. OMEN finds the **cheapest** such set as a shortest path over interval end points (Dijkstra). That
covers the 3-leg "NO on $70.25 or above + YES on $70.25–70.4999 + YES on $70.50 or above" combination, longer chains,
and anything else that covers the line. A second shape covers it **twice**: NO on a bucket [L, U] plus YES on
"at least a" (a ≤ L) plus NO on "above b" (b ≥ U) pays at least $2 everywhere. It never relies on reporting
precision.

Markets are combined only when all of these match:

* their settlement wording, with every number masked and the strike phrase removed (same index, same averaging
  window, same wording);
* units, close time and settlement time.

Single-event combinations are left to ladders and YES baskets, so nothing is reported twice. As with YES baskets:

* joins one reporting step apart (70.2499 / 70.25) need `allow_step_gap_partitions`, and the alert says so;
* a "fair price" settlement clause rejects the cover;
* a "resolves NO if data is missing" clause rejects it unless a NO leg would then pay.

A randomized test checks that every reported cover pays its minimum at every possible settlement value.

Live check, 2026-10-24: 12 universes across BTC, ETH, SOL and XRP, each pairing the threshold and bucket series at
3 settlement times. None was profitable at the top of the book; the closest, a BTC double cover, was 0.28¢ per set
away. These are separate order books quoted by different traders, so OMEN watches them every sweep for the moments
they drift apart.

## 📈 Crypto threshold model (shadow until validated)

These markets settle on a CF Benchmarks 60-second index average for BTC, ETH, SOL, XRP and DOGE: daily "above X at
5pm ET" ladders and range buckets.

* **Spot**: median of the Coinbase, Kraken and Bitstamp mid prices. OMEN refuses to price when fewer than two venues
  answer or the venues disagree by more than 0.5%.
* **Volatility**: realized over the last 2 hours of 1-minute returns, realized over 7 days of hourly returns, and
  Deribit DVOL (30-day implied, BTC and ETH only).
* **Probability**: P(settlement in the market's interval), with log returns scaled to σ√τ. The probability the play
  relies on is the **worst case** over a grid:
  * volatility from (lowest estimate ÷ 1.3) to (highest × 1.3);
  * normal and Student-t (ν=4) tails;
  * spot shifted ±0.15% for the basis between the venue median and the CF index.

  Low volatility and thin tails are the conservative case for "it will get there" bets. High volatility and fat tails
  are the conservative case for "it will stay put" bets. Taking the worst case covers both.
* A play needs a worst-case win probability ≥ 85% (favorites only) and must beat price plus fee by ≥ 3 points.
* Every evaluation is logged (at most once per market per 30 minutes), together with the market's own mid price, so
  the model's skill can be measured against the market.

The live build of 2026-10-23 tested an earlier version that used the *highest* volatility estimate as its center. It
flagged cheap tails on both sides at once. That is the classic sign of an overstated volatility, and the reason for
the grid above. A regression test (`test_both_tails_can_never_both_look_cheap`) keeps it fixed.

**Replayed over two years** (`probability_band()` on hourly BTC/ETH/SOL prices, Oct 2024 – Oct 2026, 138,384
evaluations at 6 h and 24 h): the worst case is deliberately conservative. Favorites it put at 80–85% won 93.9% of
the time, at 85–90% won 95.1%, and at 90–95% won 96.7%. The central estimate is honest to about 1.5 points (slightly
overconfident at the extremes, because real tails are a little fatter). Every crypto signal now quotes the measured
win rate of its confidence level. Kalshi's crypto prices were also fair in the atlas, so honest probabilities do not
by themselves create many bets.

### Validation gates and promotion

`omen report` and the daily digest show, from resolved predictions:

* Brier score of the model and of the market's mid price, and the skill between them;
* expected calibration error;
* for would-alert signals, after-fee ROI with a **cluster bootstrap** lower bound. Strikes of one event are
  correlated, so whole events are resampled together.

The default gates are ≥ 150 resolved signals over ≥ 30 events, a 95% lower ROI bound above 0, and calibration
error ≤ 0.05. `omen promote crypto_threshold` refuses until all of them pass. `--force --note "why"` overrides and
records the note.

## 📈 Calibration edges (the atlas; shadow until validated)

Instead of assuming where the crowd is wrong, OMEN measures it. `omen atlas` (weekly on the Pi with
`scripts/install-pi.sh --with-atlas-timer`) builds the atlas from Kalshi's public data:

1. **Sample.** Settled markets from 15 whole days spread over the last 45, at most 6 per series and 2 per event,
   so no ladder or game dominates. Combo markets are excluded.
2. **Price.** From each market's hourly bid/ask history, take what a buyer would really have paid 1, 6, 24 and
   72 hours before the close: YES at the ask, NO at 1 − bid, only on a real two-sided market (spread ≤ 10¢).
3. **Score.** Group by category × time-to-close × price band and compute the profit per contract after the taker
   fee.
4. **Interval.** 95% intervals come from a bootstrap that resamples whole **series**, because markets of one series
   rise and fall together.
5. **Edge test.** Edges are searched for on the older two thirds of the window. A cell becomes an **edge** only if
   its whole interval is above zero over ≥ 12 series, *and* the newest third (never used in the search) is
   profitable too.

For a live market in an edge, `calibration_edge` puts a number on confidence:

* the measured mispricing (win rate − price) is shrunk toward zero with a normal prior (`[edge].prior_sd`, 3¢);
* that gives an estimated true win probability, a 90% lower bound and **P(edge after fees > 0)**. A signal needs
  ≥ 90% and ≥ 1¢ expected profit per contract;
* the alert shows the **payout multiple** (how asymmetric the bet is) and a **¼-Kelly stake** on
  `[edge].bankroll_usd`, capped by `max_capital_usd` and by what the order book supports at a profitable price.

Every evaluation is logged and scored at settlement with the same gates as the crypto model. Signals stay in
`#kalshi-research` until `omen promote calibration_edge`. An atlas older than `max_atlas_age_days` (21) is ignored,
so stale edges switch themselves off. The website's **Edges** page shows the whole atlas, including the cells
where the crowd is right.

**First atlas (2026-10-24): no validated edge.** It covered 9,119 settled markets in 2,025 series (26,116 prices).
In every category, time-to-close and price band, buying at Kalshi's prices either lost money after the taker fee
and spread or gained within noise. What it showed:

* **Cheap longshots are overpriced.** At 1–5¢ six or more hours out, they won 0–1.1% of the time against a 3¢ price.
  The other side (NO at 95–99¢) only broke even once the spread and fee were paid.
* **Coin flips cost takers about 3–4¢ per contract** (40–60¢, every horizon): roughly the spread plus the fee. That
  is what market makers earn.
* **Validation earned its keep.** On the first third of the sample, 60–80¢ favorites a day out looked like
  +4.7¢ per contract. With the full sample the estimate was −1.2¢ (interval −4.2¢ to +1.4¢), and −0.4¢ on the
  held-out weeks.

The snapshot is `config/atlas.json`, and the Edges page shows every cell.

Two other ideas failed against data and were not built:

* **Support/resistance on SOL.** Over two years of hourly prices, the next-day move from near support or
  resistance was a coin flip. Ranges "held" no more often than random bands of the same width.
* **Buying 90–99¢ favorites in the last minutes of the 15-minute crypto markets.** Across 2,746 settled markets
  they were priced almost exactly right, so the fee made the strategy slightly negative.

## 💧 Liquidity rewards (not a play: a service)

Kalshi pays rewards to resting orders in thousands of markets. `omen lp` collects them with small cheap-side bids and
accounts for every fill. The rules, the evidence and the risks are in [LIQUIDITY.md](LIQUIDITY.md).

## Paper ledger

Every play alerted to `#kalshi-plays` is recorded at its verified size, prices and fees. It is settled from Kalshi's
official result, using `settlement_value_dollars` for scalar or fair-price settlements. A LOCK that settles below its
guarantee raises a `#kalshi-status` error so the strategy can be investigated before anyone trusts it again.
