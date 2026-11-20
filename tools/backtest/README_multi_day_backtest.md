# Multi-day 2nd Vibe Buy backtest

This document covers the five-day backtest of the Ave **2nd Vibe Buy Signal**
on Solana memecoins:

| Role    | Day        | Why                                    |
| ------- | ---------- | -------------------------------------- |
| Holdout | 2026-08-09 | oldest archive, thinnest signal volume |
| Train   | 2026-10-03 | full day                              |
| Train   | 2026-10-04 | full day                              |
| Train   | 2026-10-05 | full day                              |
| Holdout | 2026-11-08 | newest archive (32 h window)           |

Each day is an independent 10 SOL paper account. Results are **not**
compounded across days; the multi-day figure is the sum of five separate runs,
which is also why `mean_return_per_day_pct` is the number to look at.

The pipeline is plain Python/NumPy/Pandas. There is no vectorbt and no numba:
the strategy is an event loop over replayed pool snapshots.

---

## 1. Pipeline

```
Telegram ──► fetch_signals.py ──► prepare_signal_inputs.py ──► filter_replay.py
                                                                    │
                                              raw hourly .jsonl.zst │ (target mints only)
                                                                    ▼
                                                          raw_to_clean_ohlcv.py
                                                                    │
                                                          verify_backtest_dataset.py
                                                                    │
                                                        export_parquet.py (optional)
                                                                    │
                                                          backtest_strategy.py
                                                                    ▼
                                                     execution_simulator.py
```

The canonical artifacts stay in their original formats:

- `replay/` — read-only source archives (`/mnt/HDD/PumpAPI_Replay_Zst`, 5 day
  directories, 128 files, 68 GiB). Never modified.
- `output/backtest/days/<day>/replay_filtered/target_mints_events.jsonl.zst` —
  filtered stream, still plain JSONL + zstd. This is what the simulator reads.
- `output/backtest/days/<day>/ohlcv/ohlcv_1m.csv` — the audit pipeline's
  285-column candle CSV.
- `output/backtest/days/<day>/parquet/events.parquet` and
  `ohlcv/ohlcv_1m.parquet` — derived, read-optimised copies. Nothing consumes
  them yet; they exist for research loads.

### Day layout

```
output/backtest/days/2026-10-03/
├── replay_filtered/
│   ├── target_mints_events.jsonl.zst
│   └── replay_filter_report.json
├── ohlcv/
│   ├── ohlcv_1m.csv
│   ├── ohlcv_1m.parquet
│   ├── cleaning_report.json
│   └── dataset_verification_1m.json
├── parquet/events.parquet
├── signal_routing.csv
└── dataset_report.json          # passed/failed + timings + routing + verify
```

`2026-11-08` predates this layout. Its replay/OHLCV live in the original
single-period locations (`output/backtest/replay_filtered/`,
`output/backtest/ohlcv/`), and
`output/backtest/days/2026-11-08/{replay_filtered,ohlcv,parquet}` are symlinks
into them. Its `signal_routing.csv` is real and lives in the day directory, so
`backtest_strategy.py` treats all five days the same way. The 11-08 replay
window is 32 h (04:00 → next-day 12:00 UTC); the four rebuilt days are
00:00–24:00 UTC.

---

## 2. Signals

`fetch_signals.py` derives each day's Telegram window from the replay directory
name, fetches the configured channel(s) with one hour of extra lookback, and
writes `messages/<day>/…`. `prepare_signal_inputs.py` then keeps Solana
`second_vibe_buy` messages inside the window and writes:

- `output/backtest/signals/<day>/strategy_signals.csv`
- `output/backtest/signals/<day>/target_mints.txt`
- `output/backtest/signals/<day>/signal_ingestion_report.json`

Signals outside the replay window are counted and dropped; `--keep-outside`
retains them instead. The signal timestamp is the Telegram message timestamp.

| Day        | Messages | Signals |
| ---------- | -------- | ------- |
| 2026-08-09 | 181      | 93      |
| 2026-10-03 | 327      | 129     |
| 2026-10-04 | 314      | 121     |
| 2026-10-05 | 301      | 141     |
| 2026-11-08 | 496      | 246     |

---

## 3. Dataset build

`build_backtest_dataset.py` runs filter → OHLCV → verify → report. Only target
mints are ever decompressed at full width; the raw corpus is not re-processed.

```bash
uv run python tools/backtest/build_backtest_dataset.py \
  --replay-dir replay/2026-10-03 \
  --signals output/backtest/signals/2026-10-03/strategy_signals.csv \
  --mints   output/backtest/signals/2026-10-03/target_mints.txt \
  --output-dir output/backtest/days/2026-10-03 \
  --timeframes 1m --normalize-legacy
```

Measured on a 2.0–2.3 M-event day: filter 275–295 s, OHLCV 153–175 s,
verification ~3 s, with 8 archive workers (the default `min(8, CPU count)`).

### 3.1 Schema drift: `--normalize-legacy`

The pre-November archives do not carry SOL-denominated price columns. On those
files `priceSol` and `marketCapSol` are absent; `price` (`marketCapQuote`) are
already SOL for WSOL-quoted events.

`--normalize-legacy` is an opt-in shim applied only to WSOL-quoted events:

```
priceSol      := price
marketCapSol  := marketCapQuote
```

Everything else is left byte-for-byte. On 2026-10-03 this fired on all 62,739
WSOL events that needed it and zero non-WSOL events. On 2026-11-08 the shim is
a no-op (the archive already has the fields), so passing the flag is harmless.

Without the flag, the older days would fail pool pricing with
`missing_price_sol` for `meteora-launchpad`, and any SOL price taken from the
missing column would be silently wrong.

### 3.2 Routability: `signal_routing.csv`

Dataset-level routability answers one question only: *can this filtered stream
produce SOL-denominated bars at and after the signal?* Pool-model support is a
simulator concern and is deliberately checked at order time instead.

| `reason`                     | Meaning                                                                 | Rows excluded (5 days) |
| ---------------------------- | ----------------------------------------------------------------------- | ---------------------- |
| `no_replay_events`           | The mint appears nowhere in the replay (venue the feed does not cover).  | 3                      |
| `no_wsol_quoted_trades`      | The mint trades, but never against WSOL (e.g. only USDC pairs).          | 80                     |
| `no_wsol_trades_after_signal`| Last WSOL trade predates the signal, so no bar can follow it.            | 2                      |

Without that check, a market that went quiet before the signal could be
"entered" at a price that was already dead. Two signals are affected, one each
on 2026-10-05 (`telegram:60166`, last WSOL trade 676.9 s before the signal) and
2026-11-08 (`telegram:71636`, 154.9 s before). No other day has a signal after
its last WSOL trade.

Mints with zero replay events are tolerated explicitly: the builder takes
`--allow-missing-mints N` and fails if more than `N` signal mints are absent.
The occurrences are:

- 2026-10-04: `61BkPLSPrgzYdJL1n2eVfauWRy92qd3bE7yjUBzc4fiD` (`hookedpad.com`)
- 2026-10-05: `3ykvcjrzbb5nyJRnzHVEA12sQsZfnTijwxUusN4f8R3R`,
  `GMWdgqfnkjLhzoerrDVHLUNamYiGVfLTXvEoVM5tCQrg` (`hookedpad.com`)

All other mints are present in the replay. `verify_backtest_dataset.py` receives
the excluded mints via `--exclude-mints-file`, so "every signal has a bar"
applies to routable signals rather than to every Telegram signal.

Routing is evaluated **before** the strategy runs, which is why a signal in a
known un-routable mint is reported as `dataset_not_routable` rather than as a
liquidity failure later.

### 3.3 Verification gates

Per day, `dataset_verification_1m.json` must show:

- all routable signaled mints present, no untracked mints;
- unique `(mint, timestamp)` keys, aligned timestamps, positive prices;
- at least one bar at or after every routable signal.

| Day        | Events kept | Mints | 1m bars | Real bars | Signals routed |
| ---------- | ----------- | ----- | ------- | --------- | -------------- |
| 2026-08-09 | 2,024,879   | 93    | 60,369  | 28,126    | 93 / 93        |
| 2026-10-03 | 1,996,600   | 129   | 86,965  | 44,136    | 124 / 129      |
| 2026-10-04 | 2,290,452   | 120   | 80,498  | 42,720    | 114 / 121      |
| 2026-10-05 | 1,897,705   | 139   | 91,207  | 40,340    | 132 / 141      |
| 2026-11-08 | 1,762,675   | 246   | 213,963 | —         | 182 / 246      |

(The 11-08 row was built by the earlier single-period pipeline; "real bars" was
not recorded in that report.)

---

## 4. Execution model

`execution_simulator.py` replays pool reserve snapshots and applies simulated
orders to a **shadow** state. Historical events are never rewritten. Fills are
always SOL-denominated, including when the pool quotes a different asset.

### 4.1 Pool coverage

| `pool`                             | Reserve identity used                                        | Model                  |
| ---------------------------------- | ------------------------------------------------------------ | ---------------------- |
| `pump`, `raydium-launchpad`        | `vQuoteInBondingCurve / vTokensInBondingCurve`                | constant product       |
| `pump-amm`                         | `(quoteInPool + virtualQuoteInPool) / tokensInPool` **or** `quoteInPool / (tokensInPool + virtualTokensInPool)` | constant product |
| `raydium-cpmm`, `meteora-damm-v2`  | `quoteInPool / tokensInPool`                                  | constant product       |
| `meteora-launchpad`                | linear in quote raised; slope estimated from local observations | linear curve        |
| `raydium-clmm`, `meteora-dlmm`, `orca-whirlpool` | —                                            | **rejected**           |

`pump-amm` feeds carry exactly one non-zero virtual reserve per pool; if both
sides are non-zero the price identity is ambiguous and the state is rejected
(`ambiguous_virtual_reserves`).

Concentrated-liquidity venues report vault balances that are unrelated to the
tradable depth inside the active tick range. Approximating them with constant
product would invent liquidity, so they are rejected as
`unsupported_concentrated_liquidity`. On 2026-11-08 the dominant WSOL pools
were: `pump` 47 mints, `pump-amm` 42, `meteora-damm-v2` 38, `raydium-launchpad`
24, and `meteora-dlmm` 25 (the 25 unsupported ones).

Non-WSOL quotes are converted with the event's `quotePriceSol`, and the all-in
SOL fee is

```
solRouteFeeRate = 1 - (1 - poolFeeRate) * (1 - routeFeeRate)
```

On WSOL pools `poolFeeRate == solRouteFeeRate` on 100% of events, so the fallback
for archives that only carry `poolFeeRate` is exact there. `meteora-damm-v2`
has 2 events with `poolFeeRate = 1.04`, which are rejected as
`missing_or_invalid_pool_fee_rate` rather than given a negative fee.

### 4.2 Reserve floors

A drained/closed pool reports dust reserves (`remove` leaving
`quoteInPool = tokensInPool = 1e-9`), whose ratio is a meaningless 1.0
SOL/token. Such states are invalid:

```
MIN_QUOTE_RESERVE_SOL = 1e-4
MIN_TOKEN_RESERVE     = 1e-3
```

Violations are rejected as `pool_reserves_below_floor` and never mark a
position. This matters: before the floor existed, one drained `meteora-damm-v2`
pool marked a 0.1 SOL position at 30,422 SOL, turning one day into a +304,208%
equity result.

### 4.3 Pool resolution

At order time the simulator advances the stream to just before the timestamp
and picks among `mint` pools that have a valid, fresh snapshot:

1. prefer a WSOL-quoted pool;
2. then prefer the deepest (largest quote reserve).

Freshness is `max_state_age_ms` (default 60 s; `0` disables). Re-resolving at
exit is what handles migration: a token that moved `pump` → `pump-amm` inside
the holding period exits in the new pool. If the re-resolved pool cannot absorb
the position, the exit falls back to the entry pool once before failing.

### 4.4 Fills and rejection statuses

A buy is quote-sized (0.1 SOL), a sell is token-sized. Each fill records the
pre/post reserves, fee, average price, and price impact in bps. Non-`filled`
outcomes are recorded with their reason, e.g.:

- `stale_pool_state` — no snapshot within `max_state_age_ms`;
- `insufficient_liquidity` — sell size ≥ token reserve (curve bounds);
- `unsupported_pool` — concentrated liquidity, missing price/slope, etc.;
- `price_impact_exceeded` — only with `--max-price-impact-bps` set.

---

## 5. Strategy

`backtest_strategy.py` is a single event loop over signals and scheduled exits.

| Parameter         | Default | Note                                        |
| ----------------- | ------- | ------------------------------------------- |
| Cash per day      | 10 SOL  | independent account per day                 |
| Stake per trade   | 0.1 SOL | fixed, not size-weighted                    |
| Max concurrent    | 10      | fills 1 SOL of the 10 SOL, so cash is rarely binding |
| Entry latency     | 5 s     | signal timestamp + 5,000 ms                 |
| Hold              | 30 min  | wall clock from the fill                    |
| Max state age     | 60 s    | entry and exit                              |
| Price-impact cap  | off     | `--max-price-impact-bps` to enable          |

Per signal, in timestamp order:

1. If routing excluded it → `dataset_not_routable` (with the dataset reason).
2. If 10 positions are open → `max_concurrent_positions`.
3. If cash < stake → `insufficient_cash`.
4. Resolve a fresh pool; none → `no_fresh_pool`.
5. Buy 0.1 SOL. At `entry + 30 min`, re-resolve and sell all tokens.

Spend order is greedy by signal time, so with this sizing the binding
constraint is the 10-slot cap, not cash. That is intentional: it measures the
signal's edge per unit stake rather than letting the account bleed dry on
early losers.

### 5.1 Open positions and marking

Positions are **never force-closed**. At the end of the replay each remaining
position is written with `status = open_at_end` (or `open_exit_failed` if an
exit was attempted) and marked to market:

- the mark is the **most recent valid pool state at or before the mark time**
  (`mark_price_sol`), never a future snapshot;
- a position whose exit was attempted freezes its mark at that attempt, so
  later pool activity cannot inflate the book;
- `open_exit_failed` rows keep the rejection reason in `note`.

Equity = cash + Σ(tokens × mark). `unrealized_pnl_sol` is reported separately
from `realized_pnl_sol`, and `equity_end_sol` uses both.

### 5.2 Outputs

`output/backtest/runs/<day>/`:

- `trades.csv` — one row per closed or open position;
- `fills.csv` — every entry/exit attempt with full simulator diagnostics;
- `skipped.csv` — every signal not traded, with reason and detail;
- `equity_curve.csv` — cash/open value/equity after each entry or exit;
- `run_report.json` — config, inputs, skip/fill counters, pool mix, PnL.

`output/backtest/runs/summary.json` aggregates the multi-day run. It reads
`dataset_report.json` when present and refuses to run a day whose dataset did
not pass.

---

## 6. Results

Five independent 10 SOL accounts, 0.1 SOL per trade, 30 min hold, 5 s latency.

| Day        | Signals | Routed | Entries | Closed | Open | Win rate | Realized | Unrealized | Return | Equity | Max DD | Fees |
| ---------- | ------- | ------ | ------- | ------ | ---- | -------- | -------- | ---------- | ------ | ------ | ------ | ---- |
| 2026-08-09 | 93      | 93     | 16      | 6      | 10   | 0.0%     | −1.438   | −0.290     | −7.28% | 9.272  | 8.51%  | 0.231 |
| 2026-10-03 | 129     | 124    | 96      | 87     | 9    | 29.9%    | −1.749   | −0.412     | −12.61% | 8.739 | 14.82% | 0.272 |
| 2026-10-04 | 121     | 114    | 105     | 100    | 5    | 27.0%    | −2.826   | −0.401     | −27.27% | 7.273 | 28.30% | 0.433 |
| 2026-10-05 | 141     | 132    | 101     | 91     | 10   | 24.2%    | −1.506   | −0.437     | −9.43% | 9.057  | 10.12% | 0.443 |
| 2026-11-08 | 246     | 182    | 62      | 52     | 10   | 30.8%    | −1.943   | −0.578     | −15.22% | 8.478 | 16.26% | 0.366 |

Grouped:

| Group            | Days | Signals | Entries | Closed | Open | Realized | Unrealized | Equity / start | Mean day | Total |
| ---------------- | ---- | ------- | ------- | ------ | ---- | -------- | ---------- | -------------- | -------- | ----- |
| Train (Oct 3–5)  | 3    | 391     | 302     | 278    | 24   | −6.081   | −1.250     | 25.070 / 30    | −16.43%  | −16.43% |
| Holdout (Aug 9, Nov 8) | 2 | 339   | 78      | 58     | 20   | −3.382   | −0.868     | 17.751 / 20    | −11.25%  | −11.25% |
| All five         | 5    | 730     | 380     | 336    | 44   | −9.462   | −2.118     | 42.820 / 50    | −14.36%  | −14.36% |

Interpretation, from `trades.csv` across the 336 closed trades:

- mean trade return −15.1%, median −52.6%; the distribution is "many small
  losses, a few 2× wins": p10 −89.6%, p90 +71.3%;
- the underlying spot move alone is mean −8.9%, median −46.8%, so most of the
  damage is the token falling, not execution;
- fees are 1.9% of stake at the median (3.8% mean), and price impact is
  ~10 bps entry / ~7 bps exit at the median on 0.1 SOL — small relative to the
  price move;
- win rate 24–31% per day, and no day is profitable at a 30 min horizon.

These are paper results on a 0.1 SOL size. Nothing here says the signal cannot
work with a different hold, a momentum filter, or adaptive sizing; it says that
buying every signal and holding 30 minutes lost money on all five days.

### 6.1 Where entries landed

| Day        | pump | pump-amm | raydium-launchpad | meteora-damm-v2 | meteora-launchpad |
| ---------- | ---- | -------- | ----------------- | --------------- | ----------------- |
| 2026-08-09 | 10   | 0        | 0                 | 6               | 0                 |
| 2026-10-03 | 29   | 63       | 0                 | 3               | 1                 |
| 2026-10-04 | 15   | 82       | 0                 | 8               | 0                 |
| 2026-10-05 | 12   | 80       | 0                 | 9               | 0                 |
| 2026-11-08 | 37   | 13       | 8                 | 3               | 1                 |

Exits can migrate: on 2026-11-08, 3 exits landed in `raydium-cpmm` even though
no entry did. Multi-pool mints (e.g. a WSOL `meteora-damm-v2` pool plus a USDC
`raydium-cpmm` pool) route by WSOL preference, so "entry pool" is the deepest
WSOL pool at that instant, not the only pool the mint has.

### 6.2 Skip accounting

| Day        | max concurrent | no fresh pool | dataset not routable | rejected fills |
| ---------- | -------------- | ------------- | -------------------- | -------------- |
| 2026-08-09 | 49             | 28            | 0                    | 10             |
| 2026-10-03 | 24             | 4             | 5                    | 7              |
| 2026-10-04 | 0              | 9             | 7                    | 5              |
| 2026-10-05 | 17             | 14            | 9                    | 8              |
| 2026-11-08 | 120            | 0             | 64                   | 10             |

2026-08-09 rejects an `unsupported_pool` and an `insufficient_liquidity` fill
as well as stale states; those are real pool states, not dataset gaps.

The 11-08 day is cap-bound (120 skips) because 246 signals arrive against 10
slots and a 30 min hold. That is the strategy's throughput limit, not missing
data.

---

## 7. Parquet derivatives

`export_parquet.py` writes read-optimised copies. It does not replace the
canonical files, and nothing in the pipeline reads its output yet.

```bash
uv run python tools/backtest/export_parquet.py \
  --ohlcv output/backtest/days/2026-10-03/ohlcv/ohlcv_1m.csv --overwrite
uv run python tools/backtest/export_parquet.py \
  --events output/backtest/days/2026-10-03/replay_filtered/target_mints_events.jsonl.zst \
  --out-dir output/backtest/days/2026-10-03/parquet --overwrite
```

OHLCV (285 columns, all preserved):

| Day        | CSV MB | Parquet MB | Full read | Narrow read |
| ---------- | ------ | ---------- | --------- | ----------- |
| 2026-08-09 | 100.6  | 37.4       | 0.66 s    | 0.014 s     |
| 2026-10-03 | 153.5  | 58.9       | 0.96 s    | 0.016 s     |
| 2026-10-04 | 146.2  | 57.2       | 0.92 s    | 0.016 s     |
| 2026-10-05 | 148.2  | 54.1       | 1.03 s    | 0.014 s     |
| 2026-11-08 | 267.7  | 77.0       | 2.39 s    | 0.019 s     |

(Narrow = `mint, timestamp_ms, close` only. Sizes are decimal MB from the
exporter; 267.7 MB is ≈255 MiB, which is the "256 MB" figure quoted in earlier
notes.)

Events (29 top-level columns; nested `breakdown` is reduced to leg counts,
unique traders, and signed leg sizes — the raw payload remains in the replay):

| Day        | Rows      | zst MB | Parquet MB | Export time |
| ---------- | --------- | ------ | ---------- | ----------- |
| 2026-08-09 | 2,024,879 | 376.1  | 320.1      | 44.2 s      |
| 2026-10-03 | 1,996,600 | 383.1  | 318.9      | 45.5 s      |
| 2026-10-04 | 2,290,452 | 436.5  | 362.8      | 51.2 s      |
| 2026-10-05 | 1,897,705 | 365.1  | 303.4      | 42.9 s      |
| 2026-11-08 | 1,762,675 | 401.7  | 295.8      | 42.1 s      |

Event Parquet is only ~15–20% smaller than the zstd stream, because the columns
are mostly short strings that do not compress much better than zstd already
does. Its value is column pruning and typed scans, not size. The simulator still
streams `.jsonl.zst`; a full pass over 1.76 M events (11-08) takes ~15 s, and
the whole 5-day strategy run takes ~102 s end to end.

---

## 8. Reproduction

From a clean checkout, with `config.ini` (Telegram credentials) in place:

```bash
# 1. Signals (all five days)
uv run python tools/backtest/fetch_signals.py \
  --days 2026-08-09 2026-10-03 2026-10-04 2026-10-05 2026-11-08

# 2. One manifest per day (`--end` is exclusive).
for day in 2026-08-09 2026-10-03 2026-10-04 2026-10-05; do
  next=$(date -u -d "$day +1 day" +%F)
  uv run python tools/backtest/prepare_signal_inputs.py \
    --signals-csv "messages/$day/@avesignalmonitor-messages.csv" \
    --start "${day}T00:00:00+00:00" \
    --end   "${next}T00:00:00+00:00" \
    --out-dir "output/backtest/signals/$day"
done
uv run python tools/backtest/prepare_signal_inputs.py \
  --signals-csv "messages/2026-11-08/@avesignalmonitor-messages.csv" \
  --start 2026-11-08T04:00:00+00:00 \
  --end   2026-11-09T12:00:00+00:00 \
  --out-dir output/backtest/signals/2026-11-08

# 3. Datasets
for day in 2026-08-09 2026-10-03 2026-10-04 2026-10-05; do
  uv run python tools/backtest/build_backtest_dataset.py \
    --replay-dir "replay/$day" \
    --signals "output/backtest/signals/$day/strategy_signals.csv" \
    --mints   "output/backtest/signals/$day/target_mints.txt" \
    --output-dir "output/backtest/days/$day" \
    --timeframes 1m --normalize-legacy
done
# 2026-10-04 and 2026-10-05 need --allow-missing-mints 1 / 2 (hookedpad.com).

# 4. 2026-11-08 has no day build. Point the day layout at its legacy paths,
#    then classify its routing directly.
mkdir -p output/backtest/days/2026-11-08
for sub in replay_filtered ohlcv parquet; do
  ln -sfn "../../$sub" "output/backtest/days/2026-11-08/$sub"
done
uv run python - <<'PY'
import io, sys
from pathlib import Path
import pandas as pd, zstandard as zstd
sys.path.insert(0, "tools/backtest")
import filter_replay
from build_backtest_dataset import classify_wsol_routability

events = Path("output/backtest/replay_filtered/target_mints_events.jsonl.zst")
signals = pd.read_csv("output/backtest/signals/2026-11-08/strategy_signals.csv")
wanted = set(signals["mint"].astype(str))
present = set()
with events.open("rb") as fh:
    reader = zstd.ZstdDecompressor().stream_reader(fh, read_across_frames=True)
    for line in io.BufferedReader(reader, 1 << 20):
        event = filter_replay._loads(line) if line.strip() else None
        if isinstance(event, dict) and event.get("mint") in wanted:
            present.add(event["mint"])
routing = classify_wsol_routability(events, signals, missing_mints=wanted - present)
out = Path("output/backtest/days/2026-11-08")
out.mkdir(parents=True, exist_ok=True)
routing.to_csv(out / "signal_routing.csv", index=False)
PY

# 5. Backtest
uv run python tools/backtest/backtest_strategy.py --all-days \
  --days 2026-08-09 2026-10-03 2026-10-04 2026-10-05 2026-11-08
```

`--all-days` without `--days` enumerates `output/backtest/days/`; with the
11-08 symlinks that is the same five days.

A single day, with explicit paths and knobs:

```bash
uv run python tools/backtest/backtest_strategy.py \
  --events output/backtest/days/2026-10-04/replay_filtered/target_mints_events.jsonl.zst \
  --signals output/backtest/signals/2026-10-04/strategy_signals.csv \
  --routing output/backtest/days/2026-10-04/signal_routing.csv \
  --out-dir output/backtest/runs/2026-10-04-hold60 \
  --hold-minutes 60 --latency-ms 5000
```

`backtest_strategy.py --day <day>` uses the day layout and needs no explicit
paths. Re-running a dataset stage is cheap with `--skip-filter --skip-ohlcv`
(for example after a routing change) because only verification is repeated.

---

## 9. Caveats

- **Independent accounts.** Each day starts at 10 SOL; the five-day total is a
  sum, not a compounding curve. Compounding would shrink stakes after losses,
  so it would change the path without making the edge positive.
- **One signal source, five days.** 730 signals is a small sample, and all days
  are 2026 memecoin regimes. Treat the train/holdout split as a sanity check,
  not as out-of-sample evidence.
- **30 min is a fixed assumption.** No stop loss, no take profit, no momentum
  filter. The negative result is for *this* exit rule.
- **Entry uses the deepest fresh WSOL pool** at signal + 5 s. A real trade would
  also pay priority fees and slippage beyond modeled price impact.
- **Concentrated liquidity is excluded**, not approximated. Mints whose only
  WSOL market is a DLMM/CLMM pool (25 mints on 11-08) are untradeable in this
  simulator.
- **`pump-amm` virtual reserves** are read from the feed. The model is exact
  only while the feed's virtual reserve matches the on-chain curve.
- **`meteora-launchpad`** uses a locally estimated slope; the no-impact fill
  error is 0.15–0.18% median, and the last-slope estimate is much tighter
  (4e-6 median). It affects 2 entries across the five days.
- **Marking is bookkeeping.** `open_at_end` rows are valued at the last valid
  state at or before the mark time; they are not sold, so the mark is not a
  realized price and may be optimistic for illiquid names.
- **2026-11-08 layout.** Legacy single-period paths plus symlinks; a future
  rebuild of that day should use the `days/<day>` layout like the others.
- **Parquet is derived only.** Regenerating it is safe; deleting it loses
  nothing.

---

## 10. Tests

```bash
uv run --with pytest pytest -q
ruff check . --exclude .venv
```

Relevant suites:

- `test_execution_simulator.py` — reserve identities, quote conversion, fees,
  pool resolution, marking, floors, fills.
- `test_backtest_strategy.py` — entry latency, cap/cash skips, routing
  exclusions, migration at exit, open-at-end marking, drained-pool regression.
- `test_build_backtest_dataset.py` — routability reasons, including
  `no_wsol_trades_after_signal`.
- `test_export_parquet.py` — event mapping, breakdown reduction, OHLCV fidelity.
- `test_filter_replay.py`, `test_verify_backtest_dataset.py` — legacy
  normalisation and verification gates.

`tools/backtest/shard_filtered_replay.py` retains two pre-existing unused-import
ruff errors from the earlier sharding work; they are not part of this pipeline
and are left untouched.
