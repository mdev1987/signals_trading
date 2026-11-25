# Exit-policy study: trailing stop, partial TP, dynamic hold

Status: **research only. No engine or strategy change.** Run 2026-11-25 on the two
replay days that did not exist when the rule was frozen (2026-11-21 and
2026-11-22), so the entry cohort is out of sample for every policy tested here.

Question: can the exit be improved enough to turn the lane positive, and do
trailing stops, partial take-profits or a dynamic hold capture the pumps better
than the fixed 5-minute sell?

Short answer: **no policy tested beats the fixed exit reliably.** Trailing stops
and dynamic holds lose heavily; partial take-profits reduce the measured tail
rate but also lose money; the only positive policy (sell everything at +50 %) is
carried by three trades and fails under a stricter fill assumption.

## Pre-registered design

Declared before the run, and enforced by
[`src/exit_policy_study.py`](../src/exit_policy_study.py) with tests in
[`src/tests/test_exit_policy_study.py`](../src/tests/test_exit_policy_study.py):

| item | value |
| --- | --- |
| entries | unchanged, taken from `alerts_priced_canon.csv` (first BUY print at card + 5 s, `Pumpfunamm`, 20–100k) |
| cohort | 2026-11-21 (519) + 2026-11-22 (463) = **982 trades** |
| exit execution | the first trade print at or after the policy's sell time; **no print within `fresh_ms` means the pool was dead and the leg is written off at −100 %** |
| freshness | 60 s primary; 5 s as a sensitivity check |
| cost | 1.25 % per side per leg |
| per-trade cap | +999 % (the research convention; otherwise one impossible print dominates) |
| metrics | win rate, mean, median, profit factor, catastrophic rate (return ≤ −90 %), PnL at 0.1 SOL |

The fixed baseline is `base5`: sell 100 % at `entry + 5 min`. `trailNN` trails
`NN %` from the peak print after a 60 s activation and holds to a 15-minute cap;
`trailNN_5` is the same stop with the deployed 5-minute deadline. `tpNNall`
sells everything at the first print ≥ `entry × (1 + NN/100)` inside 5 min;
`tpNNh5` sells half there and the rest at +5 min. `stopNN_5` sells everything at
the first print ≤ `entry × (1 − NN/100)` inside 5 min (the print-side form of
the pre-horizon liquidity exit). `dynN` extends in 30 s steps
while the print is fresh and ≥ `entry × (1 + N/100)`, up to 15 min. `ladder`
sells thirds at +2/+5/+10 min. `oracle5` / `oracle15` are perfect-timing ceilings.

## Result (60 s freshness, 982 trades, 0.1 SOL)

| policy | win | mean | median | PF | cat | PnL SOL |
| --- | --- | --- | --- | --- | --- | --- |
| **base5 (deployed)** | 75.15 % | −5.20 % | +18.28 % | 0.79 | 24.0 % | **−5.11** |
| hold15 | 39.31 % | −37.80 % | −100 % | 0.38 | 60.7 % | −37.12 |
| trail25 (15 m cap) | 40.12 % | −32.10 % | −100 % | 0.46 | 58.7 % | −31.52 |
| trail35 (15 m cap) | 40.22 % | −32.06 % | −100 % | 0.46 | 59.0 % | −31.48 |
| trail50 (15 m cap) | 39.92 % | −32.80 % | −100 % | 0.45 | 59.4 % | −32.21 |
| trail25_5 (5 m cap) | 75.15 % | −3.80 % | +18.29 % | 0.84 | 23.5 % | −3.73 |
| trail35_5 (5 m cap) | 75.25 % | −3.99 % | +18.30 % | 0.83 | 23.7 % | −3.92 |
| trail50_5 (5 m cap) | 75.25 % | −4.04 % | +18.29 % | 0.83 | 23.8 % | −3.97 |
| stop50_5 (absolute) | 75.15 % | −5.25 % | +18.28 % | 0.78 | 24.0 % | −5.16 |
| stop70_5 (absolute) | 75.15 % | −5.25 % | +18.28 % | 0.78 | 24.0 % | −5.15 |
| tp50all | 79.63 % | +1.79 % | +19.90 % | 1.09 | 19.7 % | **+1.76** |
| tp100all | 75.56 % | −3.13 % | +18.39 % | 0.87 | 23.6 % | −3.07 |
| tp150all | 75.36 % | −3.38 % | +18.30 % | 0.86 | 23.8 % | −3.32 |
| tp50h5 | 75.56 % | −1.71 % | +18.35 % | 0.92 | 19.7 % | −1.67 |
| tp75h5 | 75.46 % | −4.20 % | +18.35 % | 0.82 | 23.4 % | −4.12 |
| tp100h5 | 75.56 % | −4.16 % | +18.32 % | 0.83 | 23.6 % | −4.09 |
| tp50 + trail30 rest | 40.33 % | −29.14 % | −100 % | 0.47 | 53.7 % | −28.62 |
| dyn5 (extend ≥ +5 %) | 39.61 % | −37.45 % | −100 % | 0.37 | 59.3 % | −36.77 |
| dyn10 (extend ≥ +10 %) | 40.84 % | −36.40 % | −100 % | 0.38 | 58.0 % | −35.75 |
| ladder (2/5/10 min) | 45.11 % | −15.54 % | −16.12 % | 0.42 | 14.5 % | −15.26 |
| oracle5 (perfect 5 m exit) | 99.19 % | +30.14 % | +24.81 % | 270 | 0.1 % | +29.60 |
| oracle15 (perfect 15 m exit) | 99.19 % | +53.31 % | +39.02 % | 478 | 0.1 % | +52.35 |

Sensitivity at 5 s freshness (a stricter definition of "there was a live
market"): base5 −8.98 SOL, trail25_5 −7.61, stop50_5 −9.02, tp50all −2.11,
ladder −19.27, hold15 −39.76. **Every policy is negative**; ordering is
unchanged.

## What the numbers say

1. **The trailing stop is not the problem; the extension is.** Holding to 15 min
   costs −37 SOL because 60.7 % of pools have no print by then (the evidence doc
   already showed 59.4 % stale at 15 min). Even with the deployed 5-minute
   deadline, a trailing stop only improves the baseline by 1.2–1.4 SOL
   (−3.7 to −4.0 vs −5.1) and stays negative: the 25–50 % pullbacks it fires on
   are ordinary noise in these pools.
   An absolute −50 % / −70 % stop is indistinguishable from the baseline
   (−5.16 / −5.15 SOL): the collapses that matter produce no print at all before
   the pool is dead, so there is nothing to stop into. That is also why a
   pre-horizon liquidity exit fails on the print side — by the time the collapse
   is observable, the value is already gone.
2. **Partial take-profit reduces the tail metric without creating PnL.** The
   2/5/10-minute ladder has a 14.5 % catastrophic rate — it would *pass* the
   frozen 16 % tail gate — while losing 15 SOL. Partial exits spread a rug over
   three legs so the per-trade return is no longer ≤ −90 %, but the money is
   still gone. **A variant could game gate 4 this way; keep the tail gate
   coupled to realized PnL.**
3. **Selling into the pump is the only positive policy, and it is one-trade
   concentrated.** `tp50all` (+1.76 SOL) has a top 1 % of trades contributing
   144 % of the PnL; removing the best 3 trades leaves −0.35 SOL, the bootstrap
   5th percentile is −1.59 SOL (P(>0) = 0.79), and at 5 s freshness it is
   −2.11 SOL. It is not evidence of an edge.
4. **The upside ceiling is timing, not holding.** A perfect exit inside the
   existing 5-minute window is worth +29.6 SOL versus the baseline, and that
   value is broadly distributed (best single trade = 3.4 % of it). Extending to
   15 minutes adds another +23 SOL on paper — and is exactly the −37 SOL
   unexecutable variant when the pool must actually print.
5. **Card features do not predict the dead pools.** On 21/22,
   `top_holder_share` looked strong (catastrophic 14.7 % in the lowest quartile
   vs 30.5 % in the highest), but across all five days the stale-exit rate by
   bucket is 12.3 / 17.8 / 21.3 / 20.1 % and the effect disappears on 2026-11-18
   (14.2 / 11.0 / 9.4 / 10.2 %) while dominating 2026-11-21 (15.7 / 22.3 /
   31.5 / 31.5 %). `insiders`, `rushers` and `insider_holdings_pct` are
   degenerate (about three quarters of the cards are zero) and their apparent
   effect is a tie-ordering artifact. On 2,520 trades there is no stable
   card-feature tail filter.

## Where the PnL actually is

The baseline model here is harsher than the paper bot (75 % vs 78.6–79.7 % win)
because a market sell needs a print, not a quote. That makes the comparison
conservative, but it also points at the cheapest lever: at 982 trades, **1
percentage point of round-trip cost is 1.96 SOL** — more than every exit policy
tested. Before changing any rule, measure the real round-trip cost (priority
fee, ATA rent, Jupiter route fee, slippage) of the paper fills on the VPS. If
the realized cost is below the 1.25 %/side assumption, the frozen rule earns
more without a single parameter change; if it is above, the lane is worse than
reported.

## Verdict

- Trailing stop: **rejected** (negative with and without a 15-minute cap).
- Absolute stop: **rejected** (no print arrives while there is still value).
- Partial take-profit: **rejected** (negative, or positive only via 3 trades;
  can also game the catastrophic gate).
- Dynamic hold / extend while pumping: **rejected** (−37 SOL; the pools die).
- Keep the deployed 5-minute full exit for the frozen lane.
- The +50 % policy is the only positive one, so it is available as the separate
  pre-registered lane `scanner_pumpfun_amm_tp50`
  ([`TP50_VARIANT.md`](TP50_VARIANT.md)). It is an experiment: its PnL is
  concentrated in three trades and its sign flips under a stricter fill
  assumption. No engine change was made for it; the engine already supported
  `--tp-pct`. The next useful experiments are execution-cost measurement and
  more days on a fixed lane, not another exit rule.

## Reproduction

```bash
# 2026-11-21 and 2026-11-22, 60 s freshness (primary) and 5 s (sensitivity)
env PYTHONPATH=src .venv/bin/python src/exit_policy_study.py
env PYTHONPATH=src .venv/bin/python src/exit_policy_study.py --fresh-ms 5000

# unit tests for the simulator contract
env PYTHONPATH=src .venv/bin/python -m pytest -q src/tests/test_exit_policy_study.py
```

The raw prints are cached under `output/backtest/exit_study/<day>.json` after the
first pass (≈3 min per day, streaming 24 hourly archives); the simulation itself
takes seconds.
