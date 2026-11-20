# Point-in-Time Universe Dataset Builder v7.2.1

This package extends the v7.0 Point-in-Time VectorBT dataset with a separate, auditable Pool Selection Timeline. The raw JSONL-to-OHLCV pipeline remains numerically unchanged; it also passes the selected pool protocol (`pool`), quote mint (`quote_mint`), and virtual reserve components (`virtual_tokens_in_pool`, `virtual_quote_in_pool`) through each real candle for downstream routing. Pool metadata is mandatory for validation, and the timeline now records the reserve model and whether the execution simulator can route each row.

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
- Writes `pool_selection_timeline.csv`, preserving the selected `pool_id`, `pool_key`, pool protocol, quote mint, and pool-switch flag for each real candle.
- Records each selected pool's `reserve_model` and an `execution_supported` routing flag so the dataset and the execution simulator agree.
- Marks the pool selection as available only at the candle close (`bar_start_timestamp_ms + bar_ms`); filled candles are excluded.

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
|-- pool_selection_timeline.csv
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

### `pool_selection_timeline.csv`

One row per mint and real candle. It preserves the selected-pool metadata from the raw OHLCV pipeline:

- `pool_id`: executable pool identifier; blank IDs remain blank and are never fabricated.
- `pool_key`: grouping identity used by the raw pipeline, including its missing-ID fallback.
- `pool`: pool protocol/type (for example, `pump` or `pump-amm`).
- `quote_mint`: quote asset used by that selected pool.
- `pool_changed`: whether the selected pool differs from the previous observed candle for the mint.
- `reserve_model`: the reserve identity the execution simulator uses: `bonding_curve` for `pump`, `amm_virtual_quote` or `amm_virtual_tokens` for `pump-amm`, `ambiguous` when both virtual reserves are non-zero, `unknown` when neither is present, and empty for protocols the simulator does not model.
- `execution_supported`: `1` only when `pool_selection_status == ready`, the row is WSOL-quoted, has a pool id, and uses a reserve model the simulator implements (plus positive effective reserves for PumpSwap). This is the flag to gate order creation; `pool_selection_status = ready` alone is not a routing guarantee, and the simulator remains the final authority for reserve and fee validation at order time.
- `pool_selection_status`: `ready`, `missing_pool_id`, `missing_quote_mint`, `missing_pool_type`, or a metadata-conflict status.

Selected-pool metadata is mandatory for execution routing: the simulator needs a pool identifier, a protocol (to dispatch `pump` vs `pump-amm` reserve models), and a quote mint. A missing `pool_id`, `quote_mint`, or pool type now causes Pool Timeline validation to fail; it can no longer silently pass as `ready`. If you are using a legacy OHLCV CSV without the selected `pool` field, regenerate the raw-to-OHLCV output with the `src/raw_to_clean_ohlcv.py` included in this package before running the builder.

**Timing rule:** `timestamp_ms` and `bar_start_timestamp_ms` identify the start of the candle. The selected pool is based on all events inside that candle, so it becomes known only at `available_at_timestamp_ms`, which equals `bar_end_timestamp_ms = bar_start_timestamp_ms + bar_ms`. For a 1-minute bar starting at 05:05:00 UTC, the selected pool is available at 05:06:00 UTC, not at 05:05:00.

When creating an order, select the latest timeline row for the mint where `available_at_timestamp_ms <= order_timestamp_ms`. Never join by candle start and assume the selected pool was known at that time. The execution simulator must still validate the pool snapshot, age, reserves, and quote mint at order time.

Filled candles are excluded from this timeline: a carried-forward `pool_id` is not a new observed pool selection.

## Repository layout

```text
src/            source modules, the Telegram bot, and the on-chain feature package
src/tests/      unittest suite
docs/           method and experiment documentation
pyproject.toml  project metadata and test paths
oxfile.toml     oxmgr deployment for the Telegram listener
```

Runtime inputs are not committed: `messages/`, `output/`, `.env`, `config.ini`,
Telegram sessions, and replay archives (`replay/`, `*.jsonl.zst`) are gitignored.
The replay examples below assume a local `05.jsonl.zst` or the `replay/` tree.

## Run tests

```bash
uv run python -m unittest discover -s src/tests -t src -p 'test_*.py' -v
```

## Build the point-in-time VectorBT dataset

Regenerate the one-minute OHLCV input with the packaged raw script (it adds the pool metadata columns):

```bash
uv run python src/raw_to_clean_ohlcv.py 05.jsonl.zst \
    -o output/ohlcv_clean_v7_2 \
    -t 5s,15s,1m \
    --fill
```

Then build the dataset:

```bash
uv run python src/build_vbt_dataset.py output/ohlcv_clean_v7_2/ohlcv_1m.csv \
    -o output/vbt_dataset_pit_v7_2_1m \
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
- Pool timeline key-set equality with the real-bar source (not just row counts), real-timestamp alignment, post-close availability, fill exclusion, source pool-change parity, and mandatory `pool_id` / `quote_mint` / pool-type metadata.

The report records the builder version, configured thresholds, PIT delay, eligibility counts, transition counts, and validation results.

## Important limitation

PIT universe selection removes the full-window universe-filter leak. It does not by itself remove every possible source of look-ahead or guarantee executable PnL. The pool timeline makes the selected pool available only after the candle closes, but an adapter must still select it as-of order time and reject stale or unsupported metadata. Historical fills are not hypothetical fills. Signal timing, next-bar order submission, order size, reserve state, fees, and slippage must be modeled separately in the execution/backtest engine.

## Telegram bot

`src/paper_bot/` is the live listener for the AVE channel. It consumes Telegram
messages, rebuilds the pre-signal flow and volume features from the PumpAPI
firehose, applies the canonical hot-money rule, and manages the 5-minute exit.

The deployed rule is the one in `src/ave_hot_money_strategy.py` (see
`docs/AVE_HOT_MONEY_STRATEGY.md`):

```text
hist_flow_1m_sol   > 9.456   # net buys - sells in the 60 s before the alert
total_buy_sol      > 3.0     # "Total Buy" printed in the Ave alert
hist_pre_vol_30m_sol > 600   # gross traded volume in the prior 30 min
```

Two execution modes share the same state machine and accounting:

| Mode | Quotes | Signs / broadcasts | Requirement |
|---|---|---|---|
| `paper` | Jupiter `GET /order` without a taker | never | none |
| `live` | Jupiter `GET /order` with the wallet taker | Jupiter `POST /execute` | `--mode live` **and** `LIVE_TRADING_ENABLED=1` |

Live mode is single-lane (no A/B), caps every entry at `--notional-sol`, can
restrict mints with `LIVE_MINT_ALLOWLIST`, and books the executed amounts as-is
(the on-chain swap already paid its own fee and slippage). Paper runs use the
research cost model: `--fee 0.0125 --slippage 0`.

```bash
# Paper: quotes only, safe default
PYTHONPATH=src uv run python -m paper_bot live --mode paper

# Live: requires SOLANA_PRIVATE_KEY and LIVE_TRADING_ENABLED=1 in the env
PYTHONPATH=src uv run python -m paper_bot live --mode live --notional-sol 0.1

# Inspect one read-only quote
PYTHONPATH=src uv run python -m paper_bot quote --mint <MINT> --notional-sol 0.1

# Summarize a run directory
PYTHONPATH=src uv run python -m paper_bot report output/paper_runs/<run>
```

`oxfile.toml` deploys the paper lane with the same rule and horizon. To move
the deployed app to live execution, change `--mode paper` to `--mode live` and
add the live keys to the app environment. Secrets stay in `.env`, `config.ini`,
and the Telethon session file; all three are gitignored.

