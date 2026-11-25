# `scanner_pumpfun_amm_tp50`: pre-registered exit variant

Status: **declared 2026-11-25, paper only, experiment.** It is the only exit
policy from [`EXIT_POLICY_STUDY.md`](EXIT_POLICY_STUDY.md) that was positive on
the 2026-11-21/22 replay days. Those days are out of sample for the frozen entry
rule, but they are **selection data for the exit policy**: 22 variants were
compared on the same 982 entries before this one was chosen. The lane's own
forward sample is the independent test. It is deployed because the operator
asked for the best measured policy to run 24/7, **not** because it is an
established edge. Its result is fragile and must be read against the gate
below, never against the frozen lane's numbers.

## Rule

| item | value |
| --- | --- |
| lane / rule name | `scanner_pumpfun_amm_tp50` |
| channel | `@AveSolanaTokenScanner`, first card per mint |
| entry filter | `dex == Pumpfunamm` and `20,000 <= liquidity_usd < 100,000` |
| entry | market buy on the first BUY print at/after card + 5 s; retry to card + 60 s |
| size | 0.1 SOL, 10 SOL paper wallet, at most 10 concurrent positions |
| exit | sell **100 %** at the first observed price (**PumpAPI trade or Dexscreener mark**) `>= entry × 1.50` before `entry + 5 min`; otherwise the normal hard exit at `entry + 5 min` |
| stops / trail | none |
| preflight guard | unchanged 0.90× exit-value ratio |
| fees / slippage | unchanged 1.25 % per side model, 50 bps start, 1000 bps cap |
| recovery | `output/recovery/scanner_pumpfun_amm_tp50_trade.sqlite3` (separate file) |

Only the exit differs from the frozen `scanner_pumpfun_amm` lane. Entry timing,
sizing, guards and the liquidity band are identical, so a difference between the
two lanes is attributable to the take-profit.

## Evidence and its limits

On 2026-11-21/22 (982 entries that did not exist when the frozen rule was
designed), with entries unchanged and an executable-print exit model:

| policy | win | median | PF | catastrophic | PnL @0.1 SOL |
| --- | --- | --- | --- | --- | --- |
| sell all at +5 min (frozen) | 75.15 % | +18.28 % | 0.79 | 24.0 % | −5.11 |
| **sell all at +50 % (this lane)** | 79.63 % | +19.90 % | 1.09 | 19.7 % | **+1.76** |

Limits, all of which are why this is an experiment:

- The positive PnL is concentrated: the best 1 % of trades supply 144 % of it,
  removing the best three trades leaves −0.35 SOL, and the bootstrap 5th
  percentile is −1.59 SOL (P(>0) = 0.79). The bootstrap is a mint-cluster
  percentile bootstrap, seed `20261121`, 10,000 resamples; regenerate it with
  `src/exit_policy_study.py --artifact ...` (see
  [`EXIT_POLICY_STUDY.md`](EXIT_POLICY_STUDY.md#reproducibility)). Without the
  replay inputs those exact numbers remain the recorded VPS run, not something
  this archive can re-derive on its own.
- Under a stricter 5 s freshness assumption the same policy is **−2.11 SOL**.
  The sign depends on how dead pools are valued.
- The study and the running engine are **different execution models, not
  interchangeable**. The study triggers on the first qualifying trade print and
  prices the exit at that print. The engine exits on either a PumpAPI trade or
  a Dexscreener mark, then prices the sale with a Jupiter quote. At the five
  minute horizon the gap is structural: the engine quotes from the last
  in-horizon observation, while the study uses the first print at or after the
  horizon. A quote can land above or below the print that follows it, so the
  live lane is neither guaranteed better nor guaranteed worse; the two results
  measure different things and must not be compared as if they were the same
  experiment. Each trade now records which stream fired the exit, the trigger
  timestamp/price, the quote timestamp/amounts, whether the exit needed the
  fraction ladder, and whether the reference was the trigger observation or the
  last in-horizon observation (`exit_trigger_source`, `exit_trigger_ts_ms`,
  `exit_trigger_price_sol`, `exit_fill_evidence`, `exit_full_size`,
  `exit_reference_kind`).
- Selling at +50 % caps the right tail: the perfect 5-minute exit is worth
  +29.6 SOL versus −5.11 for the fixed exit, so the take-profit deliberately
  gives up most of that ceiling in exchange for a smaller left tail.

## Pre-registered acceptance gate

Fixed before the first trade. Evaluated on the lane's own forward sample only;
never pooled with samples B/A/E or with the frozen lane.

1. at least **300 closed trades over at least 5 days**;
2. **positive realized PnL**, with the cluster-bootstrap lower bound no worse
   than −5 % of starting capital;
3. realized **median return >= +10 %**;
4. catastrophic exits (`return_pct <= -0.90`) **under 16 %** — and this gate is
   only meaningful **together with gate 2**: a partial-exit scheme can lower the
   per-trade tail rate while still losing money (the 2/5/10-minute ladder in the
   study shows 14.5 % catastrophic and −15.26 SOL);
5. `no_route` / unsold write-offs **under 16 %**;
6. fill rate **>= 80 %** and entry-failure rate **< 10 %**;
7. run integrity `VALID` (not `DEGRADED` / `INVALID`).

If the lane fails gate 2 or 3, the correct action is to stop it and return to
the frozen lane. Do not move the +50 % threshold, add a stop, or lengthen the
hold on the same sample; that is a new pre-registration.

### How the gate is measured

`src/forward_gates.py` evaluates this list mechanically, on the lane's own run
directory only:

| gate | measurement |
| --- | --- |
| coverage | closed rows in `trades.jsonl` and distinct UTC days from `exit_ts_ms` |
| PnL / bootstrap | sum of `pnl_sol`; mint-cluster percentile bootstrap, seed `20261121`, 10,000 resamples, 5th percentile compared with −5 % of starting capital |
| median | median of `return_pct` |
| catastrophic | share of `return_pct <= -0.90`; fails at **16 % or above** (strict `<`) |
| write-off | share of `exit_status == "no_route"` or `unsold_size > 0`; fails at **16 % or above** (strict `<`) |
| entry failure | skips joined to an *allowed* signal by `message_id` (or mint + signal timestamp). The numerator is every entry-attempt outcome: `entry_delay_exceeded`, `entry_quote_failed`, `entry_no_exit_route`, `entry_no_exit_economics`, `entry_insufficient_sol`, `persistence_degraded`, `entry_timer_late`, missing/stale reference outcomes, and trade-mode expiries `no_buy_trade_after_signal` / `entry_dust_prints_only` / `entry_unverified_quote_notional`. Intake rejections (`allowed=false`) and their skips are censoring, not failures. Fails at **10 % or above** |
| integrity | `summary.json` must say `integrity == "VALID"` and `persistence_degraded == false`, and `meta.json` must say `integrity_at_start == "VALID"`. Missing or malformed metadata fails the gate; it is never read as valid |

The bootstrap in the gate and in the exit study is the same implementation
(`forward_gates.cluster_bootstrap`), so the selection artifact and the forward
verdict cannot drift apart methodologically.

`summary.json` is written when the lane shuts down, so run the evaluator on a
stopped run (or on a copy of its directory). A still-running lane has no final
integrity verdict; the gate treats that as a failure rather than assuming the
process is clean.

## Running it 24/7

`run_bot.sh` and `oxfile.toml` default to this lane:

```bash
# paper, first start of the lane: no --resume yet (there is no snapshot)
env PYTHONPATH=src RESUME=0 ./run_bot.sh paper

# after it has written output/recovery/scanner_pumpfun_amm_tp50_trade.sqlite3
oxmgr import ./oxfile.toml      # command ends with --resume
oxmgr start paper-bot
```

`--resume` is fail-closed. On a host that has never run this lane, starting the
oxmgr app with `--resume` will refuse to start until the lane has been seeded
once without it. Seed in the foreground (or with `RESUME=0`), confirm the
startup report shows `Recovery: fresh`, `integrity: VALID` and
`📌 Open positions: 0 / 10`, then let oxmgr take over.

### Paper-only is enforced

`run_bot.sh` exits with an error when `MODE=live` is combined with
`RULE=scanner_pumpfun_amm_tp50`, and `run_live()` raises before reading any
credential for the same combination. The `LIVE_TRADING_ENABLED=1` opt-in
protects the wallet; it does not turn this experiment into a live strategy.

### Switching back to the frozen lane

```bash
env PYTHONPATH=src RULE=scanner_pumpfun_amm RESUME=1 ./run_bot.sh paper
```

The frozen lane's snapshot (`scanner_pumpfun_amm_trade.sqlite3`) is untouched by
the variant, so switching back resumes that sample instead of starting a new
one. Do not delete either snapshot unless you intend to end that sample.

## What not to do

- Do not pool this lane's trades with the frozen sample, samples B/A/E, or the
  replay studies.
- Do not treat a green first week as validation: at ~100 trades the standard
  error on a 79 % win rate is about 4 pp, and the study's PnL sign already
  depends on the fill assumption.
- Do not add a stop, a trailing exit, a partial take-profit or a longer hold to
  this lane without a new pre-registration; every one of those was negative in
  [`EXIT_POLICY_STUDY.md`](EXIT_POLICY_STUDY.md).
