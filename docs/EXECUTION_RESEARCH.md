# Execution research: cost, exit depth, and the near-zero tail

Status: **research only. No engine, strategy, or threshold change.** Written
2026-11-25 against the paper ledger and the deployed `0.90` preflight guard.

Two new measuring tools accompany this note:

| tool | what it measures |
| --- | --- |
| `src/cost_audit.py` | per-trade decomposition of the round-trip cost and PnL under three fill conventions |
| `src/preflight_study.py` | what each preflight exit-depth threshold would have accepted and rejected |

Neither tool changes a strategy field. Both are deterministic over existing
`output/paper/live_*/trades.jsonl` records and the bot log.

---

## 0. Summary

Four results, in order of decision value:

1. **The strategy's sign depends on the broker fee overlay, not on the exits.**
   Across 954 recorded trades the gross edge before the modeled broker fee is
   **+2.886 SOL (+3.02 % of notional)**; the `1.25 %/side` overlay removes
   **2.421 SOL (2.54 %)**; the recorded net is **+0.465 SOL**. Quote-embedded
   impact is already inside every fill, so the broker percentage is the only
   assumed cost left. At `0.50 %/side` the same fills net **+1.917 SOL**; at
   `1.00 %/side`, **+0.949 SOL**. Measuring the real all-in fee is worth more
   than every remaining exit experiment combined.

2. **Pre-entry exitability cannot remove the tail.** On the 287 trades that
   recorded both quotes, every pool that passed the `0.90` guard quoted an
   immediate full-size sale at **0.9617–0.9912** of entry cost. The trades
   that later collapsed did **not** quote worse at entry: catastrophic exits
   had a *slightly higher* median preflight ratio (AUC 0.458 for "catastrophic
   quotes lower"). The collapse happens after entry, in both the quote and the
   tape.

3. **The proposed V3 thresholds cannot work as specified.** `0.95` is *below
   the fee floor* of the metric and rejects exactly nothing; `0.98` rejects
   94.8 % of entries and still leaves a **33 %** catastrophic rate among the
   survivors. No threshold in the observed range lowers the tail while keeping
   the opportunity set.

4. **The recorded profit is a knife-edge, not an edge.** The fixed lane wins
   79.8 % of trades and still earns only +0.757 SOL, because the break-even
   win rate at its own payoff (+24.4 % / −92.1 %) is 79.0 %: a margin of
   **0.7 pp**. The tail rate is 18.6 % (Wilson 16.2–21.3 %) against a
   break-even of 19.3 %; **one extra point of tail rate costs 1.1 SOL**, more
   than the whole profit. The live tp50 lane sits on the same line and crossed
   it in both directions inside two hours of wall-clock: **−0.7 pp** at 104
   closes, **+1.1 pp** at 120, **−0.25 pp** at 122. The last leg was two
   catastrophic tails. That is the noise band measured on the live lane, not
   asserted. Its four TP triggers are the first evidence on the rule itself,
   and one of them filled at **−100 %** because the +50 % mark was not
   executable (§2.5.5). The win rate is the least informative number in the
   report.

What follows is the pre-registration for the parts that remain, and the
evidence that closes the rest.

---

## 1. Pre-registration

Frozen before any evaluation of the partitions below.

**Partitions.** Chosen by day, never by row:

| partition | days | role |
| --- | --- | --- |
| selection | 2026-11-21, 2026-11-22 | choose at most one threshold/variant |
| holdout | 2026-11-18, 2026-11-19, 2026-11-20 | evaluate the frozen choice and the `0.90` baseline |
| live-only | 2026-11-24, 2026-11-25 | forward sample; never used to choose |

Rules:

- A variant is selected on the **selection** partition only. The selection is
  recorded in this file (or a dated successor) *before* the holdout is priced.
- The holdout is reported for **both** the selected variant and the deployed
  `0.90` baseline. Holdout results never revise the selection.
- Mints are partitioned with the day: a mint that appears on both sides of a
  split is removed from the holdout, not moved. (Pool overlap between
  partitions would leak the outcome.)
- Any dataset built from position snapshots is split by **mint and day**, never
  by snapshot row. Rows from one pool are one observation for splitting.
- A variant "passes" only if it clears all of: catastrophic rate ≤ 16 %,
  profit factor ≥ 1.0, positive net PnL under the `otherAmountThreshold` fill,
  and ≥ 60 accepted trades in the holdout. A PnL improvement on a handful of
  rejected losers is not a pass.

**Metrics reported for every variant** (the table requested for V3):

| metric | definition |
| --- | --- |
| signals received | rows in `signals.jsonl` (all decisions) |
| entries accepted | trades opened |
| rejected but profitable | rejected candidates whose counterfactual return > 0 |
| catastrophic rate | share of closes with `return_pct ≤ −0.90` |
| near-zero loss | sum of `pnl_sol` over catastrophic closes |
| net PnL | summed `pnl_sol` |
| profit factor | gross wins / \|gross losses\| |
| quote availability | preflight attempts without a route or an evaluable ratio |
| max drawdown | peak-to-trough of cumulative `pnl_sol` in entry order |
| cost sensitivity | PnL at `otherAmountThreshold` and at 0.30/0.50/1.00/1.25 %/side |

---

## 2. Measured: the cost decomposition

Command:

```bash
env PYTHONPATH=src .venv/bin/python src/cost_audit.py
```

954 trades, 95.400 SOL of entry notional, 287 with both quotes recorded
(30.1 %).

### 2.1 What is measured and what is assumed

The paper engine fills at the Jupiter quote's `outAmount` and then subtracts its
own `--fee`. That means:

| component | status |
| --- | --- |
| platform fee (`feeBps` = 10) | **measured**, 20 bps round trip, inside `outAmount` |
| pool fee + price impact | **measured**, inside `outAmount` |
| slippage tolerance (`otherAmountThreshold`) | **not applied** by paper; 50 bps/side |
| priority / Jito / signature fee | **not modeled** in paper; quotes report 0 lamports |
| ATA rent | not spent in paper; locked per live trade until the account is closed |
| broker `--fee` 1.25 %/side | **assumed**, applied on top of every fill |

The two categories must not be added twice. Quote-embedded impact is already in
the fill price and therefore already in the recorded PnL.

### 2.2 Cost components (trades with both quotes)

| component | mean (bps) | median (bps) |
| --- | --- | --- |
| quote platform fee, both sides | 20.00 | 20.00 |
| quote price impact, both sides | 2843.09 | 293.29 |
| — non-tail only (`return > −90 %`, n = 227) | — | 233.51 |
| — embedded (impact + platform), non-tail | — | 253.51 |
| broker fee overlay (1.25 %/side) | 253.78 | — |
| slippage tolerance, both sides | — | 100.00 (not applied) |

The mean impact is dominated by the 60 catastrophic exits, where a sale quotes
near-zero and `priceImpact` saturates at −100 % (10 000 bps). That is the loss
event, not an execution cost, and it must not be averaged into the cost model.

### 2.3 PnL under fill conventions

| convention | n | SOL |
| --- | --- | --- |
| paper, as recorded | 954 | **+0.4647** |
| paper, quoted subset only | 287 | **−0.3332** |
| exit at `otherAmountThreshold` | 287 | **−0.6237** (−0.2905) |
| plus 2 × 12 000 lamports/trade | 287 | **−0.6306** (−0.0069) |

The 954-trade ledger and the 287-trade quoted subset are different cohorts and
must not be pooled. The threshold fill costs **0.29 SOL** on the same 287 trades —
almost the whole of that subset's recorded result (−0.333 SOL).

### 2.4 Gross edge versus the fee overlay

| line | SOL | % of notional |
| --- | --- | --- |
| gross of broker fee | **+2.8857** | +3.02 % |
| broker fee overlay | **−2.4211** | −2.54 % |
| recorded net | **+0.4647** | +0.49 % |

Fee sensitivity, holding the measured fills fixed:

| per-side fee | net SOL (954 trades) |
| --- | --- |
| 1.25 % (deployed) | +0.4647 |
| 1.00 % | +0.9489 |
| 0.75 % | +1.4331 |
| 0.50 % | +1.9173 |
| 0.30 % | +2.3047 |

This is a **sensitivity, not a recommendation to lower the assumption**. The
deployed `1.25 %` is a conservative buffer; the explicit platform fee is
`0.1 %`. The gap between the two is the single largest unknown in the strategy,
and it can only be resolved on a funded fill (or, at minimum, by confirming the
Pump AMM fee schedule), not by choosing a flattering number.

ATA rent is `0.00204 SOL` per trade if the account is neither reused nor
closed: **1.95 SOL locked** across 954 trades. It is recoverable capital, not
an expense, but it is real working capital at 0.1 SOL/trade.

### 2.5 Why a 79 % win rate is compatible with a negative ledger

The recurring observation — median trade +20 %, win rate 79 %, PnL negative —
is arithmetically consistent, because the average loss is about **3.8×** the
average win:

```
break-even win rate = |avg loss| / (avg win + |avg loss|)
```

| sample | n | win | avg win | avg loss | break-even win | margin | PnL | P(PnL>0) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `scanner_pumpfun_amm` (fixed), 6 restarted runs 11-22 → 11-25 | 870 | 79.8 % | +24.4 % | −92.1 % | 79.0 % | **+0.7 pp** | +0.757 | 0.69 |
| `scanner_pumpfun_amm_tp50`, **separate cohort** (source run not recorded) | 105 | 79.0 % | +23.0 % | −96.7 % | 80.8 % | **−1.7 pp** | −0.222 | 0.34 |
| `scanner_pumpfun_amm_tp50`, **live lane**, snapshot 11-26 04:07 UTC | 122 | 80.3 % | +23.4 % | −97.0 % | 80.6 % | **−0.25 pp** | −0.037 | 0.48 |

**Provenance.** Each row is only decision-grade with the data that produced
it. `forward_gates.py` stamps its JSON and its human report with a
`provenance` block — run directory, rule, lane, mode, run start, recovery
status, trade count, and the first/last `exit_ts_ms` — so a quoted row can be
tied back to one `trades.jsonl`.

The fixed row is the union of six `scanner_pumpfun_amm` runs, not one file
(the bot restarted, and each restart opens a new directory):

| run dir | trades |
| --- | --- |
| `live_20261122_055930` | 76 |
| `live_20261122_094804` | 348 |
| `live_20261123_052635` | 76 |
| `live_20261123_122916` | 122 |
| `live_20261124_045122` | 87 |
| `live_20261124_160122` | 161 |

Concatenated, they reproduce every figure above exactly: 870 trades,
+0.7569 SOL, 79.77 % win, 162 tails, break-even 79.03 %, margin +0.74 pp,
`P(PnL>0) = 0.6865`, best mint +0.2415 SOL, and the four day rows of §2.5.3.

The live row is a **frozen copy** of
`output/paper/live_20261125_121607/trades.jsonl` taken at 2026-11-26
04:07:38 UTC: 122 closes, 98 W / 24 L, 23 tails, −0.0365 SOL, bootstrap
`P(PnL>0) = 0.48` (5th percentile −0.944 SOL). The lane kept trading while
this was written — its margin had already gone −0.7 pp (n=104) → +1.1 pp
(n=120) → −0.25 pp (n=122) in under two hours — so this row is a snapshot,
not the lane's state; re-run the command for the current one. The 105-trade
row is a **separate cohort whose source run directory was not recorded**; an
earlier draft presented it as the running lane, which it is not. It is kept
only because it is the one sample in which the TP rule fired before the live
lane did. **A row without a stated source and last close is not evidence.**

The tp50 case is the one worth stating plainly: the live margin has already
read −0.7 pp, +1.1 pp and −0.25 pp on the same rule, and the last leg was two
catastrophic tails. That is the same one-trade-wide variance the fixed lane
shows, now demonstrated on the live lane; the verdict is "indistinguishable
from the frozen lane so far", not "1.7 pp worse". The fixed lane's
leave-one-run-out margin is **−0.2 to +1.5 pp** with PnL **−0.160 to +1.440
SOL**; one of six subsets is negative.

The small-loss band is only **1.6 %** of trades (79.8 % wins + 18.6 %
catastrophic = 98.4 %), so the win rate is almost exactly
`1 − catastrophic rate`. Reporting both is redundant; the only informative
number is the margin over break-even.

`P(PnL>0)` comes from resampling **mints** on one forward sample. It is not a
calibrated probability of future profitability: trades inside a day share a
regime that mint clustering does not remove, so the true uncertainty is wider.

### 2.5.1 How precisely is the tail rate known?

Fixed lane: 162 catastrophes in 870 trades, `q = 18.62 %`. Wilson 95 % interval
(independent trades): **16.2 % – 21.3 %**. The break-even tail rate is
**19.3 %**, so the interval straddles break-even and its upper half is
unprofitable. That is the whole fragility in one line.

Two caveats on the interval:

- Wilson assumes independent trades. Catastrophes are not independent — they
  cluster in hours and launches — so the effective sample is smaller and the
  true interval is **wider**, not narrower.
- The break-even tail rate (19.3 %) differs from the win-rate break-even
  (79.0 % ⇒ 21.0 % of tails) because the win-rate version uses the empirical
  average loss (−92.1 %, small losers included) while this one assumes every
  catastrophe is total. The tail-rate version is the conservative one.

The tp50 separate cohort: 20.0 % tail (13.5 % – 28.6 % Wilson) against a
break-even of 18.3 %. The live tp50 lane: 19.2 % tail, 20 in 104 (12.8 % –
27.9 % Wilson), against a break-even of 18.5 %. In both the interval straddles
break-even and the point estimate is on the wrong side by less than one tail;
that is a warning, not a measurement.

The live lane has since moved to 23 tails in 122 (**18.9 %**, Wilson
12.9 % – 26.7 %, break-even 18.6 %) — back across break-even without leaving
the interval. That is what an interval that wide means in practice.

### 2.5.2 Tail-rate sensitivity

Holding the observed non-tail payoff fixed (`m = +23.9 %`) and replacing trades
at random with the observed catastrophe (`t = −99.9 %`), expectancy is
`(1−q)·m + q·t`:

| tail rate | fixed lane (870) | tp50 cohort (105) | tp50 live lane (122) |
| --- | --- | --- | --- |
| observed (18.6 / 20.0 / 18.9 %) | **+0.86 %**/trade (+0.757 SOL) | −2.09 %/trade (−0.222 SOL) | −0.30 %/trade (−0.036 SOL) |
| +1 pp | −0.38 %/trade (−0.334 SOL) | −3.31 %/trade (−0.352 SOL) | −1.52 %/trade (−0.188 SOL) |
| +2 pp | −1.62 %/trade (−1.425 SOL) | −4.53 %/trade (−0.482 SOL) | −2.75 %/trade (−0.340 SOL) |
| +3 pp | −2.86 %/trade (−2.516 SOL) | −5.75 %/trade (−0.611 SOL) | −3.98 %/trade (−0.491 SOL) |
| break-even | 19.3 % | 18.3 % | 18.6 % |

One extra point of tail rate costs about **1.1 SOL per 870 trades** — more than
the entire recorded profit. The fixed lane is profitable in exactly one
0.7-pp-wide band. The live tp50 ledger (`m = +22.8 %`, `t = −99.8 %`, 12.3 SOL
book) sits on the other side of the same break-even: one extra point costs
**0.15 SOL per 122 trades**, four times its current loss of 0.036 SOL.

### 2.5.3 Concentration: days and mints

Fixed lane, four days (11-22 → 11-25):

| day | trades | PnL | PnL without best trade | profitable only via its best trade |
| --- | --- | --- | --- | --- |
| 11-22 | 340 | −0.364 | −0.483 | no |
| 11-23 | 255 | +0.823 | +0.582 | no |
| 11-24 | 183 | −0.044 | −0.166 | no |
| 11-25 | 92 | +0.342 | +0.249 | no |

**2 of 4 days are profitable**; no day depends on a single trade. But the
11-23 day alone (+0.823) is larger than the whole lane's net (+0.757), and the
best single mint (+0.242) is a third of the profit — without it the lane is
+0.515 SOL. The positive result is one good day plus one good mint.

The tp50 samples are worse on every one of these axes. Separate cohort: 1 of 2
days profitable, the profitable day (11-26, +0.021) is **negative without its
best trade**, and removing the best mint turns the lane from −0.222 to −0.273
SOL. Live lane: 1 of 2 profitable days (11-25 −0.108, 11-26 +0.072), neither
flipped by its best trade. Without the best mint the lane goes from −0.037 to
−0.087 SOL — the best mint halves the loss but there is no profit for it to
concentrate. In the separate cohort whatever profit exists is one day and one
mint.

**pp convention.** "pp" here means percentage points of traded notional
(median **0.101 SOL** per trade), not of starting capital. On the 88 SOL
fixed-lane book, 1 pp ≈ 0.88 SOL; the 1.5 pp cost cut is 1.32 SOL, and the
0.5 pp cut is 0.44 SOL.

Finer launch cohorts (hour of entry) are **not** reported: 870 trades over four
days is ~9 trades per hour, below any resolution that could separate signal
from noise. Day is the coarsest split that is still meaningful, mint the
finest.

### 2.5.4 The levers, with assumptions stated

| lever | upper bound | assumptions it needs |
| --- | --- | --- |
| exit half the tail at −50 % | +8.2 SOL | exits actually fill at −50 % during the collapse — unproven, and the thing the pool-health dataset must test |
| 16 % tail gate | +2.3 SOL | every rejected trade would have lost 100 % of notional; every rejected trade exits at 0 %, not at a partial loss; **no rejected winner**; no added cost from the gate |
| 0.5 pp round-trip cost cut | +0.44 SOL | the 1.25 %/side buffer overstates the real all-in cost |

Every row is an **upper bound, not an expected improvement**. The gate row in
particular assumes a perfect classifier: a gate that rejects a 20 % tail of the
signal set while also rejecting some winners is worth far less, and the
preflight work (§3.3) already shows the available ratio has no predictive
power (AUC 0.458).

The tp50 variant is **not** disproven by either sample, and it is no longer
unexercised. In the separate cohort TP fired 3 times for +0.124 SOL (non-TP
mean −3.39 %). In the live lane it fired **4 times in 122 closes for +0.023
SOL net**, because one of the four filled at −100 % (§2.5.5). Neither sample
is evidence for or against the +50 % exit on its own.

### 2.5.5 What a TP trigger actually realizes

The engine fires `take_profit` when the *mark* crosses +50 %, then sells at the
next full-size quote. Those are not the same price. The live lane's four TP
exits:

| mint | mark at trigger | realized | hold |
| --- | --- | --- | --- |
| `9NXD…` | +51.0 % | +45.7 % | 3m43s |
| `7LgW…` | +54.5 % | +50.0 % | 1m38s |
| `2HHE…` | +50.4 % | +26.6 % | 4m18s |
| `3WdJ…` | +38,400 % (one print) | **−100.0 %** | 4m04s |

Net across the four: **+0.023 SOL**. Three filled 4–24 pp below their trigger.
The fourth is the one that matters: a single print marked the mint at ~384×
entry, TP fired, and the sell quote returned ~0 (`exit_price_sol` fell to
~4×10⁻¹¹, `exit_quote_price_impact_pct = −99.98 %`) while `exit_liquidity_usd`
still read **$31k**. That field is not the SOL side of the pool; rank exits by
quoted out amount, never by reported liquidity. Two consequences:

1. The +50 % rule is not a +50 % realization. Comparing it to the 5-minute
   fallback must use realised returns, as every table above does.
2. The tail TP was meant to avoid can still be taken **on the TP path**,
   through a mark that cannot be sold. That is exactly the quantity the V1
   quote guard measures. It is a reason to measure the guard, not to change a
   frozen rule.

A `no_route` write-off in the same sample (488k tokens unsold, 10 quote
attempts, `quote_bad_request`) is counted separately by the write-off gate:
1 in 122.

`forward_gates.py` prints the payoff line, the exit-reason table, the Wilson
interval, the tail sensitivity table and the concentration summary for every
run, and the per-day/per-mint tables with `--detail`, all as **report only**;
no gate or threshold changes.

---

## 3. Measured: preflight exit depth (V3 feasibility screen)

Command:

```bash
env PYTHONPATH=src .venv/bin/python src/preflight_study.py \
  --thresholds 0.900 0.950 0.960 0.965 0.968 0.972 0.975 0.980
```

At the 2026-11-25 16:09 cut, 287 preflight quotes join one-to-one to 287
trades (0 unmatched). Cohort: 2026-11-24 05:02 → 2026-11-25 16:09, the only
period that logged the ratio. Across all retained runs the tool also reports
the funnel: 8,220 signal decisions → 979 accepted → 954 closed trades, with 5
rejected by preflight economics and none by route failure. It
spans the fixed-exit lane and part of the tp50 lane.

### 3.1 The metric is dominated by the fee model

The logged ratio is

```
ratio = size × exit_price × (1 − slippage) × (1 − fee) / (entry value + entry fee)
```

For a zero-move trade it equals the **fee floor**:

```
floor = (1 − 0.005) × (1 − 0.0125) / (1 + 0.0125) = 0.9704
```

Observed distribution: min **0.9617**, median **0.9672**, max **0.9912**. The
median sits 33 bps *below* the floor, i.e. the average pool quotes a 0.33 %
drawdown in the five seconds between the entry quote and the preflight quote.

Consequences:

- The whole observable range is about **300 bps wide**.
- `0.95` and `0.90` are below the floor. They reject only a pool that quotes a
  >2 % instant drop, which did not occur once in 287 attempts.
- `0.98` requires the pool to be **up ~1 % at entry**, which is a momentum
  filter in disguise, not a depth filter.

### 3.2 Threshold counterfactual

Every rejected trade has a known outcome, because a stricter threshold can only
remove trades the deployed `0.90` already accepted.

| threshold | depth vs floor | accepted | catastrophic | cat % | near-zero loss | net PnL | PF | rejected cat | rejected winners | rejected PnL | PnL @ threshold fill |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| **0.900 (deployed)** | −726 bps | 287 | 60 | **20.9 %** | 6.071 | **−0.333** | 0.95 | 0 | 0 | 0.000 | −0.072 |
| 0.950 | −211 bps | 287 | 60 | 20.9 % | 6.071 | −0.333 | 0.95 | 0 | 0 | 0.000 | −0.072 |
| 0.960 | −107 bps | 287 | 60 | 20.9 % | 6.071 | −0.333 | 0.95 | 0 | 0 | 0.000 | −0.072 |
| 0.965 | −56 bps | 286 | 60 | 21.0 % | 6.071 | −0.357 | 0.94 | 0 | 1 | +0.024 | −0.095 |
| 0.968 | −25 bps | 72 | 18 | 25.0 % | 1.822 | −0.544 | 0.70 | 42 | 172 | +0.211 | −0.476 |
| 0.972 | +16 bps | 47 | 14 | 29.8 % | 1.417 | −0.614 | 0.57 | 46 | 193 | +0.281 | −0.635 |
| 0.975 | +47 bps | 31 | 9 | 29.0 % | 0.911 | −0.347 | 0.62 | 51 | 204 | +0.014 | −0.361 |
| 0.980 | +99 bps | 15 | 5 | **33.3 %** | 0.506 | −0.205 | 0.59 | 55 | 216 | −0.128 | −0.212 |

Reading it:

- `0.95` is **bit-identical to the deployed gate**. There is nothing to learn
  from running that lane; the result is already known to be identical.
- `0.98` keeps 15 trades and the catastrophic *rate* rises to a third. The
  absolute tail shrinks only because almost nothing trades.
- At every effective threshold the gate rejects **about 4× more winners than
  catastrophic trades** (e.g. 0.968: 172 winners, 42 catastrophes).
- PnL is worse at every stricter threshold, under both the recorded and the
  threshold fill.

### 3.3 The ratio is not predictive

- AUC for "a catastrophic trade quotes lower at entry" = **0.458**. The sign is
  opposite to the hypothesis: catastrophic trades quoted marginally *better*.
- Splitting by day, an upper-bound gate (reject `ratio ≥ 0.975`) looks useful
  on 11-24 (PnL −0.323 → −0.027) and does nothing on 11-25 (−0.010 → +0.041),
  and at `0.98` it *hurts* on 11-25. With 19 and 12 removed trades this is
  noise, not a signal.

The deployed guard rejected 5 entries in total across the retained runs
(`entry_no_exit_economics` in `skips.jsonl`); it never rejected by route
failure (`entry_no_exit_route` = 0). It is not doing the work it was expected
to do, and no threshold in this metric can.

---

## 4. Verdict on V3

**Do not run the 0.95 and 0.98 lanes.** One is provably a no-op and the other
is a near-total entry ban that concentrates the tail rather than removing it.

The experiment is not merely unpromising, it is mis-parameterized:

- A ratio threshold is only meaningful relative to the fee floor. Change the
  fee assumption from 1.25 % to 0.50 % and the floor moves from 0.9704 to
  0.9925; every ratio threshold in this document becomes a different rule.
- If the idea is ever revisited, the parameter must be **depth in bps over the
  floor** (`ratio / floor − 1`), not a raw ratio. The observed range is −90 to
  +214 bps; the sane grid is −75, −50, −25, 0, +25, +50.
- Even then, the measured AUC says the ordering is wrong at entry.

The deeper reason is now measured, not argued: **at entry, the pools that will
collapse are indistinguishable from the ones that will not.** They all absorb
the full position at 96–99 % of cost. The failure mode is a post-entry
liquidity withdrawal that is invisible in the quote at `t = 0` and, per the
exit-policy study, also invisible in the tape before the collapse.

**Prerequisite for any further threshold work:** measure the real all-in fee
first. A gate tuned against a fee assumption that is 2.5× the real cost is
tuned against the wrong objective.

---

## 5. Pool-health dataset (observational only)

Purpose: answer "which execution features precede a near-zero exit" **before**
any of them becomes a rule. No gate, no auto-exit.

### 5.1 Three hard-separated column groups

**Observed** — written at snapshot time, no derivation:

| column | source |
| --- | --- |
| `mint`, `run_id`, `entry_ts_ms`, `snapshot_ts_ms` | clock |
| `pool_age_ms` | first observed print |
| `token_reserve`, `quote_reserve`, `virtual_token_reserve`, `virtual_quote_reserve` | PumpAPI event |
| `reserve_exit_value_sol` | reserve model for the position size |
| `jupiter_exit_quote_sol` | live read-only quote, or null |
| `jupiter_quote_status` | `available` / `no_route` / `timeout` / `error` |
| `quote_timestamp_ms` | clock |
| `entry_cost_sol`, `size_tokens` | position |

**Derived** — computed from observed history up to the snapshot timestamp only:

`liquidity_drawdown_from_entry`, `liquidity_drawdown_from_peak`, `net_flow_1m`,
`trade_count_1m`, `activity_ratio`, `reserve_model_error`
(`jupiter_exit_quote_sol / reserve_exit_value_sol − 1`), `time_since_last_print`.

**Labels** — joined at the entry level *after* the hold ends, from the realized
exit only:

| label | definition |
| --- | --- |
| `NORMAL_EXIT` | closed at the horizon with `exit_value / entry_cost ≥ 0.01` |
| `NEAR_ZERO_EXIT` | closed with `exit_value / entry_cost < 0.01` |
| `ROUTE_FAILURE` | no executable exit route before the horizon |
| `LIQUIDITY_COLLAPSE` | reserve-derived exit value fell below 1 % of entry cost before the horizon |

No label may use a predictor threshold. `NEAR_ZERO_EXIT` is the engine's own
`NEAR_ZERO_EXIT_RATIO = 0.01`, not a tuned cut.

### 5.2 Splitting and minimum size

Split by mint and day per §1. Report per-pool survival, not per-row accuracy.
A useful first check is the day-split stability of the top feature; if the sign
flips between days (as the card features did in `EXIT_POLICY_STUDY.md`), the
feature is not a candidate.

### 5.3 Data prerequisites

Partly satisfied. The parsing half landed; the collection half has not.

- **Done** (`a94db30`): `TradeEvent` and `parse_pump_event` now carry the
  post-event reserves (`tokens_in_pool`, `quote_in_pool`, the virtual reserves
  and the pool/route fee rates), and `paper_bot.pool_health` turns them into a
  constant-product `ReserveState` with `sell_quote_sol()` and `classify_exit()`.
  Nothing in the engine imports it yet; it is observational only.
- **Still missing:** a writer. No code appends `snapshots.jsonl`, so the
  dataset is not being collected by the running lane.
- `entry_liquidity_usd` is populated on **2 of 197** pre-existing entries.
- No historical snapshot file exists — the parser arrived after the recorded
  runs — so the dataset must be collected forward or replayed from raw
  archives on the research host.

---

## 6. Live collection plan (no exits)

Phase 1 — **reserve snapshots only**, every 15 s per open position, written to
`output/paper/live_*/snapshots.jsonl`. This needs the reserve parsing above.
No API calls, no strategy effect.

Phase 2 — **Jupiter sampling, budgeted.** Do not poll every position every
15 s. Measure the request rate first. Start with a single quote at fixed
checkpoints (entry + 15 s, +60 s, and at the horizon) for a subset of
positions, and use the reserve model for the continuous series. Record
`jupiter_quote_status` on every attempt so refusals are data, not gaps.

Phase 3 — **divergence report.** `reserve_model_error` per snapshot. If the
reserve model is accurate except on collapsing pools, that is itself the
finding: it would mean the proxy is unsafe exactly where it matters, and no
reserve-based gate can be trusted there.

No automatic exit, no health-score threshold, and no TP/SL change is part of
this phase.

---

## 7. Reproduction

```bash
# every row of §2.5 is a `forward_gates.py` report over stated data: the
# provenance block prints rule, lane, mode, run start, trade count and last
# exit timestamp, so a quoted row can be tied back to one ledger.
env PYTHONPATH=src .venv/bin/python src/forward_gates.py \
  --run-dir output/paper/<run> --starting-sol 10 --detail
env PYTHONPATH=src .venv/bin/python src/forward_gates.py \
  --run-dir output/paper/<run> --starting-sol 10 --json

# cost decomposition over every recorded run
env PYTHONPATH=src .venv/bin/python src/cost_audit.py
env PYTHONPATH=src .venv/bin/python src/cost_audit.py --json

# preflight threshold counterfactual (thresholds are CLI arguments, so a
# pre-registered set can be frozen in the command)
env PYTHONPATH=src .venv/bin/python src/preflight_study.py \
  --thresholds 0.900 0.950 0.960 0.965 0.968 0.972 0.975 0.980
env PYTHONPATH=src .venv/bin/python src/preflight_study.py --json

env PYTHONPATH=src .venv/bin/python -m pytest -q \
  src/tests/test_cost_audit.py src/tests/test_preflight_study.py
```

On a **running** lane `run_integrity` fails by design: `summary.json` is
written only at shutdown, so a live directory has no final integrity verdict
and cannot pass the acceptance gate. Its provenance and report blocks are
still valid; read those, not the overall verdict, until the lane stops.

---

## 8. Limitations

- The cost audit is a **forward paper ledger**, not a funded fill. Every
  "measured" component is measured *inside a Jupiter quote*; nothing here
  proves the transaction would have landed.
- Quote coverage is 30.1 % because the entry/exit raw quotes were only retained
  from 2026-11-24 onward. The 954-trade ledger and the 287-trade quoted
  subset are different cohorts; the audit reports them separately for that
  reason.
- The preflight cohort ends on the selection days' successors and cannot be
  used to choose a threshold. It is a feasibility screen, and it fails on
  mechanism, not on sample size.
- The threshold-fill PnL assumes the exit lands at `otherAmountThreshold` with
  no failed transaction. Real failures make it worse, never better.
- The `1.25 %/side` overlay is the deployed conservative assumption. Nothing in
  this note should be read as evidence that the real fee is lower.
