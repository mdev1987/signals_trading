# Point-in-Time Universe Dataset Builder v7.0

This package keeps the raw JSONL-to-OHLCV pipeline from v6.2 unchanged and upgrades the VectorBT dataset builder to use a point-in-time (PIT) universe.

## What changed

The previous builder selected mint columns using metrics aggregated over the complete input window. That can leak future information into earlier backtest decisions. Version 7.0 no longer uses full-window eligibility to filter historical rows.

The new builder:

- Retains every mint found in the input in the aligned dataset.
- Calculates cumulative eligibility metrics for each mint and each bar.
- Uses only completed bars available before the current decision bar.
- Shifts eligibility forward by at least one complete bar.
- Writes a time-aligned `universe_eligible` mask for Wide and Long datasets.
- Writes full decision history and entry/exit transition audit files.
- Keeps full-window metrics only as descriptive summary fields.

## Point-in-time eligibility rules

At each decision bar, the builder evaluates the existing universe rules using cumulative information available through the configured cutoff bar:

| Rule | Point-in-time metric |
|---|---|
| `few_trades` | Cumulative event trade count |
| `few_bars` | Cumulative count of real candles |
| `low_volume` | Cumulative valid gross SOL volume |
| `price_out_of_range` | Expanding median of observed close prices |
| `pegged_or_flat` | Cumulative maximum high divided by cumulative minimum low, after the minimum trade count |
| `non_wsol_dominant` | Cumulative non-WSOL event fraction |
| `dust_dominant` | Cumulative zero-quote event fraction |

Default thresholds remain compatible with v6.2:

- `--min-trades 20`
- `--min-bars 3`
- `--min-volume-sol 0.5`
- `--min-price 1e-11`
- `--max-price 0.05`
- `--pegged-ratio 1.02`
- `--pegged-min-trades 50`
- `--max-non-wsol-frac 0.5`
- `--max-zero-quote-frac 0.2`

### Eligibility timing

The default `--universe-delay-bars 1` means the eligibility shown on bar `t` is based only on data through the previous completed bar. A mint cannot become eligible in the same bar that first causes it to pass the thresholds.

The delay must be at least one bar. A larger value applies an additional conservative delay.

Eligibility can enter and exit as cumulative ratios and expanding price metrics change. The mask controls new entries. It must not force-close an already open position; existing positions must continue to be managed by the strategy's exit rules.

### Do not use full-window summary fields as historical filters

`universe.csv` contains descriptive summary fields such as:

- `included_at_window_end`
- `ever_eligible`
- `eligible_at_window_end`
- `first_eligible_utc`
- `last_eligible_utc`

These fields summarize the complete input window. They are useful for reporting and audit, but they are not the point-in-time trading mask. Do not use them to decide whether a mint was eligible at an earlier timestamp.

The historical mask is `universe_eligible` in:

- `wide/universe_eligible.csv`
- `ohlcv_clean_<timeframe>.csv`

All mints are present as columns in aligned frames. Future mints remain ineligible before their first observed bar and before their PIT filters pass. Strategies must apply `universe_eligible == 1` when evaluating new entries at each timestamp. Apply freshness and directional execution checks separately.

## Output files

The builder creates:

```text
output_directory/
|-- universe.csv
|-- universe_timeline.csv
|-- universe_transitions.csv
|-- ohlcv_clean_1m.csv
|-- dataset_report.json
`-- wide/
    |-- open.csv
    |-- high.csv
    |-- low.csv
    |-- close.csv
    |-- spot_open.csv
    |-- spot_high.csv
    |-- spot_low.csv
    |-- spot_close.csv
    |-- buy_execution_open.csv
    |-- buy_execution_high.csv
    |-- buy_execution_low.csv
    |-- buy_execution_close.csv
    |-- sell_execution_open.csv
    |-- sell_execution_high.csv
    |-- sell_execution_low.csv
    |-- sell_execution_close.csv
    |-- spot_mark_valid.csv
    |-- stale_bars.csv
    |-- age_bars.csv
    |-- universe_eligible.csv
    `-- ... other frames
```

### `universe_timeline.csv`

One row per mint and visible bar, beginning at the mint's first observed bar. It records:

- The eligibility decision for the current bar.
- The decision cutoff timestamp.
- Cumulative trades, real bars, and valid SOL volume known at that cutoff.
- Expanding median close and cumulative price range.
- Cumulative non-WSOL and zero-quote fractions.
- Exclusion reasons at that decision time.

### `universe_transitions.csv`

A sparse audit of each `entered` and `exited` transition, including the metrics available at the transition decision time.

## Install

Extract the package in the project root and copy the files into place:

```bash
unzip -oq jsonl_to_ohlcv_point_in_time_v7.zip

cp ohlcv_point_in_time_v7_package/raw_to_clean_ohlcv.py ./raw_to_clean_ohlcv.py
cp ohlcv_point_in_time_v7_package/build_vbt_dataset.py ./build_vbt_dataset.py
cp ohlcv_point_in_time_v7_package/test_raw_to_clean_ohlcv.py ./test_raw_to_clean_ohlcv.py
cp ohlcv_point_in_time_v7_package/test_build_vbt_dataset.py ./test_build_vbt_dataset.py
```

The raw pipeline is copied from v6.2 without modification. Only the VectorBT builder adds PIT universe behavior.

## Run tests

```bash
uv run python -m unittest -v test_raw_to_clean_ohlcv.py test_build_vbt_dataset.py
```

## Build the point-in-time VectorBT dataset

Use the v6.2 one-minute OHLCV output as input:

```bash
uv run python build_vbt_dataset.py output/ohlcv_clean_v6_2/ohlcv_1m.csv \
    -o output/vbt_dataset_pit_v7_1m \
    --max-stale-bars 0 \
    --candle-range-ratio 10.0 \
    --universe-delay-bars 1 \
    --require-execution-ohlc
```

`--require-execution-ohlc` now requires at least one PIT-eligible BUY execution bar and at least one PIT-eligible SELL execution bar. It fails rather than silently accepting a dataset with no eligible directional execution observations.

If you want to inspect PIT selection without requiring execution OHLC, omit `--require-execution-ohlc`. This is useful for legacy spot-only files, but such a dataset is not sufficient for an execution-based TP/SL evaluation.

## Strategy integration requirements

For a new entry at timestamp `t`, the strategy should require at least:

```python
can_enter = (
    universe_eligible.loc[t, mint] == 1
    and spot_mark_valid.loc[t, mint] == 1
    and buy_execution_valid.loc[t, mint] == 1
)
```

The actual order-fill model must still account for order size, pool depth, fees, price impact, and slippage. Historical observed execution OHLC is not a guaranteed fill for a hypothetical order.

For an exit from an existing long position, use the sell-side execution and risk rules even if the mint later leaves the entry universe. A PIT universe exit is not, by itself, an exit signal.

## Validation policy

The builder validates:

- Spot OHLC and freshness masks.
- Filled-bar behavior and stale metadata.
- Candle-range anomaly flags.
- Directional execution OHLC validity and non-forward-fill behavior.
- Binary universe masks.
- No universe eligibility before at least one prior completed real bar.
- Regular time-grid spacing.

The report records the builder version, configured thresholds, PIT delay, eligibility counts, transition counts, and validation results.

## Important limitation

PIT universe selection removes the full-window universe-filter leak. It does not by itself remove every possible source of look-ahead or guarantee executable PnL. Pool selection is still based on the completed current bucket, and historical fills are not hypothetical fills. Signal timing, next-bar order submission, pool choice, order size, reserve state, fees, and slippage must be modeled separately in the execution/backtest engine.
