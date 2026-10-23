# Fees

OMEN must know the fee on every contract it recommends, including when Kalshi changes the fee. It uses three live
sources and one versioned file.

## 1. Live per-series terms: `GET /series`

Every series carries a `fee_type` and a `fee_multiplier`. One unauthenticated request returns all ~15,400 series.
OMEN refreshes them every 6 hours, stores them in the ledger and diffs them against the previous snapshot. A changed
series posts **"Fee terms changed: SERIES · old → new"** to `#kalshi-status` and is written to the fee log.

Terms on 2026-10-23 (`omen fees`):

| fee_type × multiplier | series |
|---|---|
| quadratic ×1 | 15,190 |
| quadratic_with_maker_fees ×1 | 159 |
| quadratic ×0.5 (e.g. MLB props) | 18 |
| quadratic ×0 (fee-free promos) | 14 |
| quadratic_with_combo_maker_fees ×1 | 3 |
| quadratic_with_maker_fees ×0.5 (MLB game winner) | 1 |

## 2. Scheduled changes: `GET /series/fee_changes?show_historical=true` and `GET /events/fee_changes`

Kalshi publishes fee changes ahead of time, both per series and as per-event overrides. For example, the World Series
games' events switch from the regular-season ×0.5 to ×1 when they start. OMEN reloads both feeds every 30 minutes and
announces any change it has not seen before. It resolves the terms for a market at time *t* as follows:

1. Start from the series snapshot.
2. Apply the latest series change scheduled after the snapshot was taken and at or before *t*.
3. Apply the latest event override at or before *t*. A null override means the override was cleared.
4. **Guard:** if any change takes effect within 60 minutes, use whichever of now and then is more expensive. An order
   placed a few minutes after the alert must still be profitable.

## 3. The formula: `config/fee_schedule.toml`

```
fee = round_up( multiplier × rate × contracts × price × (1 − price) )
```

| fee_type | taker rate | maker rate |
|---|---|---|
| quadratic | 0.07 | 0 |
| quadratic_with_maker_fees | 0.07 | 0.0175 (¼ of taker) |
| quadratic_with_combo_maker_fees | 0.07 | 0.035 (½ of taker) |
| flat | from the PDF's "Specific Trading Fees Table". Not available through the API, so OMEN refuses to price these until you enter it | |

Sources: [Kalshi fee schedule PDF](https://kalshi.com/docs/kalshi-fee-schedule.pdf) (July 7, 2026 update), the
[Series API reference](https://docs.kalshi.com/api-reference/market/get-series) (fee_type meanings), and
[fee rounding](https://docs.kalshi.com/getting_started/fee_rounding).

**Rounding.** Kalshi computes each fill's fee to 6 decimals, then aligns balances to $0.0001 for direct members or
$0.01 for FCM-cleared members, with an accumulator that rebates over-rounding. OMEN rounds **every fill at every
price level up to a full cent** (`[fees].rounding_increment`). It can only overstate the cost, never understate it.
Set `"0.0001"` if you are a direct member and want tighter numbers.

Checks (tests/test_fees.py): 100 contracts at 50¢ cost $1.75, at 10¢ or 90¢ cost $0.63, and at 55¢ cost $1.74. These
match Kalshi's published examples exactly.

**Settlement** has no fee, and every play is held to settlement, so there is no exit fee. Deposits and withdrawals
are outside OMEN.

## When the formula itself changes

The constants above come from a PDF with no API. OMEN does two things about that:

* Once a day it fetches the PDF, when kalshi.com lets a bot see it, and hashes it. If the content changes, it posts
  **"Kalshi fee schedule document changed"** as an error.
* Every `verify_reminder_days` (30) it reminds you to re-check the constants and bump `verified_on`, whether or not
  the fetch worked.

A fee type OMEN has no formula for (for example, a new enum value) posts **"Unknown Kalshi fee type"**. Markets of
that type are excluded until the formula is added: fail closed.

## Commands

```bash
omen fees                                   # formula, live term counts, changes in the next 14 days
omen fees --series KXMLBGAME --event KXMLBGAME-26OCT272000MILTB --price 0.5 --count 100
omen fee-log                                # everything observed, newest first
```
