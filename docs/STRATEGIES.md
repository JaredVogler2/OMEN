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

### Validation gates and promotion

`omen report` and the daily digest show, from resolved predictions:

* Brier score of the model and of the market's mid price, and the skill between them;
* expected calibration error;
* for would-alert signals, after-fee ROI with a **cluster bootstrap** lower bound. Strikes of one event are
  correlated, so whole events are resampled together.

The default gates are ≥ 150 resolved signals over ≥ 30 events, a 95% lower ROI bound above 0, and calibration
error ≤ 0.05. `omen promote crypto_threshold` refuses until all of them pass. `--force --note "why"` overrides and
records the note.

## Paper ledger

Every play alerted to `#kalshi-plays` is recorded at its verified size, prices and fees. It is settled from Kalshi's
official result, using `settlement_value_dollars` for scalar or fair-price settlements. A LOCK that settles below its
guarantee raises a `#kalshi-status` error so the strategy can be investigated before anyone trusts it again.
