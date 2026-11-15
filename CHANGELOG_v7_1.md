# v7.1.1 — Mandatory Pool Type Validation

## Fixed

- Pool Timeline rows with an empty selected `pool` protocol/type are now marked `missing_pool_type`.
- `missing_pool_type_rows` is included in the Pool Timeline validation and dataset report.
- Pool Timeline validation now fails when the selected pool type is missing, preventing a false `passed: true` for data that cannot be routed to the correct execution model.
- Added regression coverage for missing pool type metadata.

## Required input pipeline

The v7.1.1 raw-to-OHLCV script passes selected `pool` and `quote_mint` into real candles. Legacy `ohlcv_clean_v6_2/ohlcv_1m.csv` files must be regenerated using the packaged raw script if their `pool` column is empty.

# v7.1 — Pool Selection Timeline

## Added

- `pool_selection_timeline.csv`: one row per mint/real candle with selected `pool_id`, `pool_key`, pool protocol, quote mint, pool switch metadata, and selection status.
- `bar_start_timestamp_ms`, `bar_end_timestamp_ms`, and `available_at_timestamp_ms`; pool metadata is available only at bar close.
- Structural validation for duplicate keys, alignment, bar-end timing, fill exclusion, source pool-change parity, and quote/pool metadata consistency.
- Dataset report section `pool_selection_timeline` with coverage and validation counts.

## Raw OHLCV metadata passthrough

The raw pipeline now emits `pool` and `quote_mint` on selected-pool candle rows and forward-carries them only for compatibility on filled rows. The pool timeline filters filled rows, so carried metadata is never treated as a new selection. Numeric OHLCV, VWAP, volume, and directional execution calculations are unchanged.

## Unchanged

- Point-in-Time Universe qualification and delay logic.
- Spot and directional Execution OHLC calculations.
- Execution Simulator pricing, reserve, and fee logic.
- Strategy/Backtester behavior; integration remains a later step.

## Validation

- Full unit test suite: 65/65 passed.
- Python compilation: passed.
- Pool timeline unit tests: real-bar-only behavior and post-close availability passed.
- Actual full-hour dataset rebuild: not run in this environment because the source raw `.jsonl.zst` and original `ohlcv_1m.csv` were not included in the uploaded archives.
