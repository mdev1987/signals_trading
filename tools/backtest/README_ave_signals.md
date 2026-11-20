# AveSignalMonitor signal backtest

A standalone evaluation of the `@AveSignalMonitor` Telegram alerts. It answers a
single question: **after a signal is published, what did the mint's price do?**

It is deliberately *not* a portfolio simulation:

- no VectorBT, no numba, no event loop over pools;
- no slots, cash limits, routing or PIT eligibility;
- no AMM impact, no liquidity limits, no forced closes.

Accounting is plain pandas over the validated 1-minute OHLCV parquet. The
authoritative execution numbers remain in `execution_simulator.py`; nothing here
changes them.

## Method

| Step | Rule |
| --- | --- |
| Signal | `source_username == "@AveSignalMonitor"` from `output/backtest/signals/<day>/strategy_signals.csv` |
| Entry decision | `signal_timestamp_ms + --latency-ms` (default 5 s) |
| Entry price | close of the latest **real** 1-minute bar with `close_ts <= entry decision` |
| Exit decision | entry decision + horizon |
| Exit price | close of the latest real bar with `close_ts <= exit decision` |
| Fees | `--fee-rate` on each side (default 1.25 %) |
| Stake | equal `--stake-sol` per signal (default 0.1 SOL) |
| Stale bars | `is_filled == 1` rows are ignored |

Bar `close_ts` is the bar open plus 60 s, matching the rest of the project. A
signal is never priced from a bar that closed after the decision, so there is no
look-ahead. If no real close exists, the signal is reported as
`no_bars` / `no_entry_price` / `no_exit_price` and excluded from returns rather
than force-closed.

MFE/MAE use the real bar highs/lows strictly after the entry close through the
exit close. Signals whose bar history ends before the horizon are kept, and
their exit age is reported: `Stale exits` is the share of exits resting on a
close more than 60 s old.

## Run

From the repository root:

```bash
.venv/bin/python tools/backtest/backtest_ave_signals.py
```

Useful options:

```bash
# one day, finer horizons
.venv/bin/python tools/backtest/backtest_ave_signals.py \
  --days 2026-11-08 --horizons 1 3 5 15 30

# drop mints with any flagged candle-range anomaly that day
.venv/bin/python tools/backtest/backtest_ave_signals.py \
  --exclude-flagged-mints --output-dir output/backtest/ave_signals_unflagged
```

Outputs are written to `output/backtest/ave_signals/`:

| File | Content |
| --- | --- |
| `signals_backtest.csv` | one row per signal × horizon |
| `summary_by_horizon.csv` | return distribution per horizon |
| `summary_by_day.csv` | per-day stats at 30 m |
| `sensitivity_flagged.csv` | all vs. ex-flagged-mint subset |
| `coverage_by_day.csv` | signals / closed / excluded per day |
| `summary.json` | machine-readable copy of the above |
| `summary.md` | human-readable report |
| `patterns/` | PIT-safe feature table, screens, rule results and placebo test |

## Results (five days, 1.25 % fee, 0.1 SOL stake)

730 alerts on 730 distinct mints; 680 are priceable at the 30 m horizon.

| Horizon | Trades | Win rate | Mean net | Median net | Mean ex-top 1 % | Stale exits | Total PnL (SOL) |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 m | 669 | 48.7 % | +5.30 % | −0.97 % | +1.83 % | 0.0 % | +3.5437 |
| 3 m | 676 | 43.5 % | +5.47 % | −4.35 % | +1.30 % | 1.8 % | +3.6947 |
| 5 m | 677 | 41.4 % | +4.78 % | −9.07 % | +0.49 % | 1.6 % | +3.2354 |
| 15 m | 680 | 34.0 % | −1.55 % | −28.37 % | −8.22 % | 6.6 % | −1.0549 |
| 30 m | 680 | 26.2 % | −11.93 % | −47.83 % | −20.18 % | 14.6 % | −8.1158 |
| 60 m | 680 | 19.0 % | −11.79 % | −55.95 % | −26.62 % | 29.9 % | −8.0164 |

Reading:

- The **median** signal loses money at every horizon; the mean is positive only
  at 1–5 m and only because of a small right tail (many 2–10× runners).
- Removing the best 1 % of trades turns every horizon negative except 1–5 m,
  where the edge shrinks to roughly zero after fees.
- All five days are negative at 30 m (mean −7.1 % to −19.4 %, total −8.12 SOL).
- Excluding mints with flagged candle-range anomalies changes the 30 m mean
  from −11.93 % to −11.02 % (652 trades), so the conclusion is not driven by
  flagged candles.

## Pattern search: what separates the profitable signals?

The unconditional 30 m result is negative, but a small subset of alerts is
consistently profitable. Two exploratory scripts test this out of sample:

```bash
# 1. PIT-safe feature table + train/holdout screens (writes patterns/)
.venv/bin/python tools/backtest/analyze_ave_signal_patterns.py
.venv/bin/python tools/backtest/analyze_ave_signal_patterns.py --horizon 5 \
  --output-dir output/backtest/ave_signals/patterns_5m

# 2. interpretable rules, threshold grid, placebo test
.venv/bin/python tools/backtest/ave_signal_pattern_rules.py
```

Every feature is cut off at the entry decision (`signal_timestamp_ms +
--latency-ms`): the alert text, 1 m bars that closed at or before the decision,
and on-chain events with `timestamp_ms <= decision`. Thresholds are fitted on
the train days only (Oct 3–5); Aug 9 and Nov 8 are never used to choose a
threshold. `patterns/features.csv` is one row per signal.

### The rule

```text
ev_flow_1m      > 9.5 SOL   # train q80 of net buys-minus-sells in the prior minute
alert_total_buy > 3 SOL     # buy size printed in the Telegram alert
pre_vol_30m     > 600 SOL   # 30-minute traded volume
```

It selects **74 of 680 priceable signals (10.9 %)** and is best used with a
**5-minute exit**. At that horizon:

| | Baseline | Rule |
| --- | ---: | ---: |
| Trades | 677 | 74 |
| Win rate | 41.4 % | **62.2 %** |
| Mean net | +4.8 % | **+47.7 %** |
| Median net | −9.1 % | **+22.5 %** |
| 2×+ net | 8.7 % | 27.0 % |
| Rug (≤ −90 %) | 0.9 % | 1.4 % |
| Days positive | 2/5 | **5/5** |
| Mean, train | −3.9 % | +26.2 % |
| Mean, holdout | +14.1 % | +70.3 % |

The rule keeps only a tenth of the alerts but captures **33.9 % of all 5 m 2×+
winners** and 30.9 % of the top 5 m decile. It is a short-horizon continuation
pattern: the median is still positive at 15 m, but by 30 m it is −41 % with a
16 % rug rate. Holding longer destroys the edge.

The exact 5 m numbers by day (all five positive):

| Day | n | Win | Mean | Median |
| --- | ---: | ---: | ---: | ---: |
| 2026-08-09 | 16 | 56 % | +80.9 % | +13.6 % |
| 2026-10-03 | 12 | 67 % | +47.5 % | +34.9 % |
| 2026-10-04 | 13 | 46 % | +28.3 % | −15.9 % |
| 2026-10-05 | 13 | 46 % | +4.5 % | −18.3 % |
| 2026-11-08 | 20 | 85 % | +61.8 % | +49.1 % |

### Robustness

- **Threshold grid** (`patterns/threshold_grid.csv`): every one of the 75
  flow × alert-buy × volume cells that keeps ≥ 40 trades has a positive mean
  at 5 m; 47 also have a positive median, and 30 are positive on all five
  days. The result is not a knife-edge threshold.
- **Placebo** (`patterns/rules.json`): 10,000 random subsets of 74 signals
  from the same 5 m outcome pool reach the rule's mean, median and 2x+ rate at
  the 100.0th percentile, and its win rate at the 99.98th percentile.
- **Holdout**: the rule is fitted on train days only. Holdout mean is +70.3 %
  at 5 m and holdout 2×+ rate is 27.8 %, so the pattern is not a train artefact.
- **Fees**: all returns include 1.25 % per side. `net_pnl_sol` at 5 m is
  +3.53 SOL on 0.1 SOL stakes.

The canonical, executable definition of the rule is
[`ave_hot_money_strategy.py`](ave_hot_money_strategy.py); its spec, PIT
guarantees, failure modes and Helius data options are documented in
[`AVE_HOT_MONEY_STRATEGY.md`](AVE_HOT_MONEY_STRATEGY.md). The module re-computes
the headline statistics from the saved tables, so the numbers cannot silently
drift.

### What predicts rugs

The mirror image is equally clear: late pumps and crowded, diffuse flow are
the rugs. In both train and holdout, `alert_max_pump >= 5`, `alert_inflow_n >=
5`, top-quintile 15 m volume (> ~2,750 SOL) and top-quintile entry price each
raise the near-total-loss rate from ~2 % to 13–20 %. Excluding those states
avoids most rugs, but does **not** create a positive 30 m expectancy on its
own: the winners remain tail-driven. That is why the hot-money rule, not the
rug filter, is the selection rule.

### Pattern caveats

- This is exploratory research, not the execution simulator. Slot limits,
  cash, routing, liquidity impact and PIT eligibility are not modelled.
- The holdout is two days, not a random sample; five-day consistency is
  encouraging but not a guarantee.
- Only 74 signals pass the strict rule, so a single 10× runner can move the
  mean. The median and the five-day sign test are the more robust statistics.
- The rule is evaluated on 1 m closes. A real execution can do better or worse
  inside the minute.
- `alert_inflow_sum` (sum of SOL-denominated wallet lines) is missing for the
  few alerts that print token units; the rule uses `alert_total_buy`, which is
  always present. Wallet-line concentration (`alert_inflow_hhi`,
  `alert_inflow_top_share`) is unit-agnostic and remains usable.

## Tests

```bash
.venv/bin/python -m unittest test_backtest_ave_signals -v
.venv/bin/python -m unittest test_ave_signal_patterns -v
```

The tests pin the decision-timestamp pricing rules, the two-sided fee formula,
the no-look-ahead/`no_move_window` handling, stale exit reporting, flagged-mint
marking, and the coverage/sensitivity summaries. `test_ave_signal_patterns`
adds alert-text parsing (including the inflow/total-buy double-count guard),
feature cutoff tests (a future bar or event never changes a feature), rank
statistics, rule construction, and placebo determinism.
