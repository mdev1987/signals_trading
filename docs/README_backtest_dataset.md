# Backtest Dataset: Filtered Replay and Multi-Hour OHLCV

This stage turns the 32 hourly PumpAPI replay archives into a small, reproducible
dataset that a strategy backtest can run against without loading the full replay.

Entry signal: `2nd Vibe Buy Signal` on Solana, timestamped by the Telegram
message. Execution is simulated later against the event stream with the
configured latency.

> **See also:** `src/README_multi_day_backtest.md` for the five-day
> rerun (schema drift, routing, pool coverage, execution/marking rules and
> strategy results). This document covers the original single-period 11-08
> dataset stage.

## Pipeline

```text
messages/@avesignalmonitor-messages.csv
        |
        |  src/prepare_signal_inputs.py       (Phase 1.1)
        v
output/backtest/signals/{strategy_signals.csv,target_mints.txt}
        |
        |  src/filter_replay.py               (streaming universe filter)
        v
output/backtest/replay_filtered/target_mints_events.jsonl.zst
        |
        |  src/raw_to_clean_ohlcv.py                         (unchanged pipeline)
        v
output/backtest/ohlcv/ohlcv_{1m,5m}.csv
        |
        |  src/verify_backtest_dataset.py     (coverage + equivalence)
        v
output/backtest/ohlcv/dataset_verification_{1m,5m}.json
```

Run all steps with one command:

```bash
uv run python src/build_backtest_dataset.py \
  --replay-dir 2026-11-08-replay \
  --timeframes 1m,5m
```

Add `--reference output/ohlcv_clean_v7_1_1/ohlcv_1m.csv \
--reference-window 2026-11-08T05:00/2026-11-08T06:00` to include the
single-period equivalence check.

## 1. Streaming universe filter

`src/filter_replay.py` makes one pass over each archive, keeps events
whose top-level `mint` is in `target_mints.txt`, and writes one timestamp-ordered
zstd JSONL file.

Kept actions: `buy`, `sell`, `create`, `createPool`, `migrate`, `add`, `remove`.
These are the actions that carry pool reserve snapshots; `transfer`, `leaders`,
`distributeFees`, and `createConfig` do not.

Memory stays bounded by streaming one archive per worker and writing per-archive
parts. The merge step is a k-way merge over the sorted parts with a bounded
dedupe window.

Measured on 2026-11-08/09:

| Metric | Value |
|---|---:|
| Archives | 32 |
| Source lines scanned | 70,147,907 |
| Compressed source | 17.0 GiB |
| Events kept | 1,762,675 |
| Kept fraction | 2.5% |
| Filtered file | 384 MB (44x smaller) |
| Buy / sell events | 968,815 / 753,721 |
| add / remove | 20,186 / 19,367 |
| create / createPool / migrate | 234 / 246 / 106 |
| Target mints with events | 246 / 246 |
| Duplicates dropped | 0 |
| Ordering violations | 0 |
| Wall clock | 253 s |
| Peak RSS | 117 MB |

### Fields that must survive the filter

The feed uses three reserve styles:

| Pool type | Reserve fields | Reserve model |
|---|---|---|
| `pump` | `vTokensInBondingCurve`, `vQuoteInBondingCurve` | `bonding_curve` |
| `pump-amm` (this universe) | `tokensInPool`, `quoteInPool`, `virtualQuoteInPool` | `amm_virtual_quote` |
| `raydium-launchpad` | `tokensInPool`, `quoteInPool`, `vTokensInBondingCurve`, `vQuoteInBondingCurve` | bonding-curve metadata |
| other AMMs | `tokensInPool`, `quoteInPool` | vault reserves |

`src/raw_to_clean_ohlcv.py` only persists the AMM-style virtual fields, so the
bonding-curve fields would be lost if the filter dropped them. The filtered
replay therefore keeps `vTokensInBondingCurve` and `vQuoteInBondingCurve`
verbatim, and the execution simulator reads reserves from the event stream
rather than from the OHLCV CSV.

`preBalances`/`postBalances` are the largest per-event payload. They are
retained only for the events that trigger the participant-balance audit in
`src/raw_to_clean_ohlcv.py`, which leaves that audit's results unchanged.

## 2. OHLCV from the reduced replay

The existing pipeline runs unmodified on the filtered file:

```bash
uv run python src/raw_to_clean_ohlcv.py \
  output/backtest/replay_filtered/target_mints_events.jsonl.zst \
  -o output/backtest/ohlcv -t 1m,5m --fill
```

| Timeframe | Rows | Real bars | Filled bars | Mints | Window |
|---|---:|---:|---:|---:|---|
| `1m` | 213,963 | 55,567 | 158,396 | 246 | 2026-11-08 04:00 → 2026-11-09 11:59 UTC |
| `5m` | 42,972 | 19,353 | 23,619 | 246 | 2026-11-08 04:00 → 2026-11-09 11:55 UTC |

Cleaning summary for both timeframes: 1,722,536 buy/sell rows → 1,721,427 clean
rows, 1,109 invalid rows dropped (`bad_token_amount`), 0 duplicates.

Real 1m bars by pool:

| Pool | Real 1m bars |
|---|---:|
| `pump-amm` | 26,231 |
| `pump` | 13,475 |
| `raydium-cpmm` | 7,526 |
| `raydium-launchpad` | 6,770 |
| `meteora-damm-v2` | 792 |
| `meteora-launchpad` | 426 |
| `meteora-dlmm` | 335 |
| `orca-whirlpool` | 12 |

Resource profile: 3 min 48 s wall clock and **6.83 GB peak RSS**. The full replay
would need well over 100 GB in one frame; the universe filter reduces that by
roughly 40x. If a future window or a lower timeframe pushes the peak too high,
build one timeframe per process, or partition the filtered replay by mint with
`src/shard_filtered_replay.py`.

## 3. Verification

`src/verify_backtest_dataset.py` checks:

- all 246 signaled mints are present
- no untracked mints
- `(mint, timestamp_ms)` is unique
- timestamps are aligned to the timeframe grid
- `high >= low` and `close > 0`
- every signal has at least one bar at or after its timestamp

All checks pass for `1m` and `5m`.

### Equivalence with the accepted single-period pipeline

The reference `output/ohlcv_clean_v7_1_1/ohlcv_1m.csv` covers 05:00–06:00 UTC.
Re-deriving that hour from the multi-hour dataset gives the same 667 real bars
with the same keys. Of the 280 comparable columns:

- **214 match exactly** (spot OHLC, VWAP, volumes, trade counts, pool selection,
  reserve snapshots, execution OHLC, and every validation flag)
- **66 differ only for scope reasons**
- **0 unexplained mismatches**

The scope-dependent columns are:

| Column group | Why it differs |
|---|---|
| `*event_index` | Sequential counter over the loaded row set, not an event identity |
| `age_bars` | Counted from each mint's first bar in the loaded window |
| `prev_close*`, `price_gap_elapsed_ms`, `price_discontinuity_ratio` | Depend on whether a predecessor bar exists inside the window |
| `routed_trades` | Counts events per transaction signature across the whole loaded universe, so it changes when the universe is filtered |

`routed_trades` is audit-only. It does not feed OHLC, volume, VWAP, reserves, or
execution prices.

The earlier 32-file filtered build (`output/backtest/filtered_replay_32h`, built
before this stage) produces an identical 1m dataset: same 213,963 rows and same
keys, with one cell differing by `8.9e-16` in
`open_participant_quote_balance_delta_residual_sol`. That column sums trader
balances over a Python `set`, so its rounding order depends on hash seed. It is
a diagnostic, not a price.

### Signal coverage

| Metric | Value |
|---|---:|
| Signals | 246 |
| Signals with a bar at or after the signal | 246 |
| Bars after signal: min / median / max | 13 / 778.5 / 1,462 |
| Signals with less than 30 minutes of bars | 2 |
| Last signal | 2026-11-09T11:31:45Z |
| Time left to replay end | 28.25 min |

The two short horizons are data limitations, not filter errors:

| Signal | Mint | Bars after | Last bar |
|---|---|---:|---|
| `telegram:71565` | `BVRxEfWue7La6bgyBiGkCY323WtGEohUpKauqz6Cx2Bq` | 13 | 2026-11-09 02:13 |
| `telegram:71701` | `GLEpJAdQQ6hEyqfa571RQtnpYsAL1MTNK894QHi37mtF` | 28 | 2026-11-09 11:59 |

The first token stopped trading 13 minutes after its signal. The second is the
last signal in the file and therefore has only the remaining 28 minutes of
replay.

## Outputs

```text
output/backtest/
├── signals/
│   ├── strategy_signals.csv            246 signal records
│   ├── target_mints.txt                246 mints
│   └── signal_ingestion_report.json
├── replay_filtered/
│   ├── target_mints_events.jsonl.zst   1,762,675 events, timestamp ordered
│   └── replay_filter_report.json
├── ohlcv/
│   ├── ohlcv_1m.csv                    213,963 rows x 285 columns
│   ├── ohlcv_5m.csv                     42,972 rows x 285 columns
│   ├── cleaning_report.json
│   ├── dataset_verification_1m.json
│   ├── dataset_verification_5m.json
│   └── flagged_*.csv                   audit outputs from raw_to_clean_ohlcv
└── dataset_report.json                 consolidated stage report
```

`output/backtest/filtered_replay_32h/`, `ohlcv_1s/`, `ohlcv_1m/`, `mint_shards/`,
and `vbt_dataset_pit_1m/` are earlier artifacts from the same stage. They are
left untouched. `output/backtest/ohlcv/` is the canonical multi-hour output for
this stage.

## Limits for the backtest

- **Open positions at replay end.** The last signal leaves 28 minutes. Positions
  still open at 2026-11-09 12:00 UTC must be recorded as open and marked to
  market at the last available close, not force-closed.
- **Filled bars are marks only.** `is_filled == 1` carries a forward-filled spot
  price and zero volume. Use them to value an open position, never as an entry
  or exit fill.
- **OHLCV is not a liquidity model.** Candle volume is the observed gross volume
  of the selected pool, not the depth available to a new order. Fills, fees, and
  slippage must come from the execution simulator against the event stream.
- **Bonding-curve reserves are not in the CSV.** For `pump` pools the CSV carries
  vault `tokens_in_pool`/`quote_in_pool`; the curve reserves live only in the
  filtered event stream. Route `pump` executions from events.
- **`routed_trades` is universe-scoped** and should not be compared across
  different mint sets.
- **Pool selection is per closed bucket.** The selected pool for a bar is known
  only after that bar closes, so entry decisions must use the previous closed
  bar's pool, exactly as `pool_selection_timeline.csv` does.

## Next stage

The filtered replay is directly consumable by the existing simulator. A smoke
test on the first signal (`telegram:71230`, `pump` bonding curve) with 5 s
latency filled both orders:

```text
source_events_read             1,120
source_pool_snapshots_seen     1,119
source_valid_snapshots_applied 1,116
filled                         2 / 2
reserve_model                  bonding_curve
```

```bash
uv run python src/execution_simulator.py \
  output/backtest/replay_filtered/target_mints_events.jsonl.zst \
  orders.csv -o execution_fills.csv
```

The backtester should:

1. Load `strategy_signals.csv` and apply the configured execution latency to
   each `signal_timestamp_ms`.
2. Stream `target_mints_events.jsonl.zst` once with `ExecutionSimulator`, in
   timestamp order, so BUY and SELL fills share the same shadow reserves.
3. Size positions from strategy state rather than the audit's fixed
   `0.0001 SOL` / `100 token` sizes.
4. Mark remaining positions to the last `close` at or before
   2026-11-09 12:00 UTC and report them separately from closed trades.
