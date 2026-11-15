# v7.2.1 — Same-Millisecond and Routing Guard Fixes

## Fixed

- Execution Simulator rejects an order when the latest processed snapshot at the same millisecond is invalid. The previous `>` comparison treated a valid state and a later invalid event in the same millisecond as interchangeable.
- `execution_supported` now checks the Effective Token Reserve for `amm_virtual_tokens` pools (`tokens_in_pool + virtual_tokens_in_pool`). A negative virtual-token value could previously mark an unroutable pool as supported.
- `execution_supported` now also requires `pool_selection_status == ready`. A pool with conflicting quote-mint metadata could previously be marked supported when the resolved quote mint was WSOL.
- A Shadow Reserve that becomes non-positive now records `shadow_reserve_became_non_positive` instead of leaving `reason` empty.
- Regression tests cover both same-millisecond event orders, negative virtual-token reserves, conflicting metadata, and the Shadow Reserve reason.

## Clarification

- For `pump` rows, `execution_supported` means "this pool type is modeled", not "reserves were validated". Bonding-curve reserves are not carried into OHLCV, so the simulator remains the final authority for reserve and fee validation at order time.

## Validation

- Full unit test suite: 82/82 passed.
- Real full-hour rebuild and candidate integration re-run: routing invariant holds (no unsupported row fills; no supported row fails with a reserve-model error). The 34 order-time `unsupported_pool` rejects on supported rows now report `shadow_reserve_became_non_positive` instead of an empty reason.

# v7.2.0 — Executable Pool Routing

## Added

- `reserve_model` in `pool_selection_timeline.csv`: `bonding_curve` for `pump`, `amm_virtual_quote` or `amm_virtual_tokens` for `pump-amm`, `ambiguous` when both virtual components are non-zero, `unknown` when neither is present, and empty for unmodeled protocols.
- `execution_supported` in `pool_selection_timeline.csv`: `1` only for WSOL-quoted rows with a pool id, a modeled reserve identity, and positive reserves.
- Dataset report fields `execution_supported_rows` and `reserve_model_counts`.
- Raw OHLCV passthrough of `virtual_quote_in_pool` (open/close/prev-close) so the builder can identify the PumpSwap reserve style.
- Execution Simulator support for `pump-amm` pools that use `virtualTokensInPool` instead of `virtualQuoteInPool`.

## Fixed

- Execution Simulator now prices both PumpSwap reserve styles. `virtual token` pools previously failed as `missing_amm_reserves`, which silently excluded about a quarter of pump-amm signal rows.
- Buy and sell fees now share one convention: the quote-asset fee is deducted from the gross amount (`curve_quote = amount * (1 - fee_rate)`).
- Out-of-order `execute()` calls are rejected instead of advancing to a future pool state (look-ahead guard).
- `filled_rows_included` is computed from the source instead of being hardcoded to zero.
- `row_count_mismatch` compares mint/timestamp key sets, so a replaced row cannot cancel against an added row.
- Missing `pool_id` and missing `quote_mint` are now structural Pool Timeline failures, matching mandatory pool-type validation.
- Raw pipeline report version is `v6.2.2` instead of the stale `v6`.

## Compatibility

- The raw change is additive: all 282 pre-existing 1m columns were verified bit-identical against the v7.1.1 output. Only `open_virtual_quote_in_pool`, `close_virtual_quote_in_pool`, and `prev_close_virtual_quote_in_pool` are new.
- `pool_selection_status = ready` still means "metadata internally consistent". Use `execution_supported == 1` to gate order creation.

## Validation

- Full unit test suite: 77/77 passed.
- Real full-hour rebuild: `05.jsonl.zst` (1,907,859 events) regenerated to 112,388 one-minute rows, 44,837 real bars, and a 44,837-row pool timeline with every validation check at zero.
- Real candidate integration: 8,525 candidate rows. Of these, 1,183 bars are `amm_virtual_tokens` and now fill in the simulator; 1,223 buy and 886 sell orders still reject with `missing_amm_reserves`, and every one of them is `execution_supported == 0`.
- Routing invariant verified on real orders: no `execution_supported == 0` row fills, and no `execution_supported == 1` row fails with a reserve-model error.

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

The raw pipeline emits `pool` and `quote_mint` on selected-pool candle rows and forward-carries them only for compatibility on filled rows. The pool timeline filters filled rows, so carried metadata is never treated as a new selection. Numeric OHLCV, VWAP, volume, and directional execution calculations are unchanged.

## Unchanged

- Point-in-Time Universe qualification and delay logic.
- Spot and directional Execution OHLC calculations.
- Strategy/Backtester behavior; integration remains a later step.
