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

Three results, in order of decision value:

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

| lane | n | win | avg win | avg loss | break-even win | margin | PnL | P(PnL>0) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `scanner_pumpfun_amm` (fixed) | 870 | 79.8 % | +24.4 % | −92.1 % | 79.0 % | **+0.7 pp** | +0.757 | 0.69 |
| `scanner_pumpfun_amm_tp50` | 100 | 79.0 % | +23.1 % | −96.6 % | 80.7 % | **−1.7 pp** | −0.209 | 0.34 |

The tp50 lane is exactly the case being asked about: 79 % of trades win, and
the ledger is negative, because the lane sits **1.7 pp below break-even**. The
fixed lane sits 0.7 pp above it — inside the noise. Dropping any single run
from the fixed lane gives a leave-one-run-out margin of **−0.2 to +1.5 pp** and
PnL of **−0.160 to +1.440 SOL**; one of the six subsets is negative. The edge
is therefore real in sign but not separable from zero at this sample size.

The small-loss band is only **1.6 %** of trades (79.8 % wins + 18.6 %
catastrophic = 98.4 %), so the win rate is almost exactly
`1 − catastrophic rate`. Reporting both is redundant; the only informative
number is the margin over break-even.

Consequences (870 trades ≈ 88 SOL notional at 0.10 SOL/trade):

- A **0.7 pp rise** in the tail rate — about 6 more catastrophes in 870
  trades — erases the fixed lane's whole edge. The observed day-to-day rate
  already spans 17.9–20.8 %, so this is inside normal variation.
- Reaching the frozen **16 %** tail gate is worth about **+2.6 pp** of margin,
  ≈ +2.3 SOL per 870 trades (upper bound: assumes each avoided tail exits flat
  instead of −100 %).
- A **1.5 pp** round-trip cost cut is worth about **+1.3 SOL** per 870 trades,
  and it is the only one of the three that can be measured today (§2.4).

The tp50 variant is **not** disproven by its −0.209 SOL: TP fired 3 times for
+0.124 SOL, the non-TP mean is −3.39 %, and its tail rate (20.0 %) is worse
than the fixed lane's (18.6 %) because it ran on a different cohort. At n=100
it is not evidence either way.

`forward_gates.py` now prints this payoff line for every run as **report only**;
it does not change any gate or threshold.

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

Not yet satisfied, and the honest blocker:

- `TradeEvent` carries no reserves; `parse_pump_event` drops `tokensInPool`,
  `quoteInPool` and the virtual reserves.
- `entry_liquidity_usd` is populated on **2 of 197** pre-existing entries.
- No historical snapshot file exists, so the dataset must be collected forward
  or replayed from raw archives on the research host.

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
