# Ave hot-money continuation strategy

Single source of truth for the strategy that came out of the PIT-safe pattern
search. The executable definition lives in
[`ave_hot_money_strategy.py`](ave_hot_money_strategy.py); the exploratory
evidence lives in `output/backtest/ave_signals/patterns/` (gitignored) and is
summarised in [`README_ave_signals.md`](README_ave_signals.md).

> **Status:** research, validated out of sample. This is **not** the execution
> simulator and does not model slots, cash, routing, liquidity impact or PIT
> eligibility.

## The rule

At the entry decision (`signal_timestamp_ms + 5 s`), enter only when all three
conditions hold:

```text
ev_flow_1m      > 9.46 SOL   # train-day q80 net buys-minus-sells, prior minute
alert_total_buy > 3 SOL      # "Total Buy" printed in the Telegram alert
pre_vol_30m     > 600 SOL    # prior 30-minute traded volume
```

Exit after **5 minutes**. One fixed stake per signal (the validation uses
0.1 SOL and 1.25 % per side).

The 9.46 SOL flow cutoff is the 80th percentile of `ev_flow_1m` on the train
days. It is fitted from data; the other two floors are round, interpretable
values. The threshold-fitting code only ever reads `is_train` rows.

## Why this pattern

The unconditional alerts lose money at 30 minutes. The profitable subset is a
**short-horizon continuation**: aggressive net buying has just hit a mint that
is already liquid and actively traded. The mirror state - late pumps, many
small wallets, blow-off volume, crowded high-price entries - is the rug state.

## PIT guarantees

| Input | Source | Cutoff |
| --- | --- | --- |
| `alert_total_buy` | Telegram alert text | signal timestamp |
| `ev_flow_1m` | on-chain events (`events.parquet`) | `timestamp_ms <= decision` |
| `pre_vol_30m` | real 1m bars (`ohlcv_1m.parquet`) | `close_ts <= decision` |
| Entry price | real 1m close | `close_ts <= decision` |
| Exit price | real 1m close | `close_ts <= exit decision` |

`is_filled == 1` bars are ignored. Unpriced signals are excluded, never
force-closed. There is no backdating and no use of post-decision data.

## Validated result (5-minute exit)

| | Baseline | Rule |
| --- | ---: | ---: |
| Trades | 677 | **74** (10.9 %) |
| Win rate | 41.4 % | **62.2 %** |
| Mean net | +4.8 % | **+47.7 %** |
| Median net | −9.1 % | **+22.5 %** |
| 2×+ net | 8.7 % | 27.0 % |
| Rug (≤ −90 %) | 0.9 % | 1.4 % |
| Days positive | 2/5 | **5/5** |
| Train mean | −3.9 % | +26.2 % |
| Holdout mean | +14.1 % | +70.3 % |
| Net PnL (0.1 SOL stakes) | — | +3.53 SOL |

Per day:

| Day | n | Win | Mean | Median |
| --- | ---: | ---: | ---: | ---: |
| 2026-08-09 | 16 | 56 % | +80.9 % | +13.6 % |
| 2026-10-03 | 12 | 67 % | +47.5 % | +34.9 % |
| 2026-10-04 | 13 | 46 % | +28.3 % | −15.9 % |
| 2026-10-05 | 13 | 46 % | +4.5 % | −18.3 % |
| 2026-11-08 | 20 | 85 % | +61.8 % | +49.1 % |

The rule keeps a tenth of the alerts but captures **33.9 % of all 5 m 2×+
winners** and 30.9 % of the top 5 m decile.

The edge decays with holding time: the median is still near zero at 15 m and
−41 % at 30 m with a 16 % rug rate. A 5-minute exit is the best mean/median
compromise; 1–3 minutes has a higher win rate and lower variance.

## Robustness

- **Threshold grid:** every flow × alert-buy × volume cell with ≥ 40 trades
  has a positive 5 m mean; most also have a positive median, and many are
  positive on all five days.
- **Placebo:** 10,000 random 74-signal subsets from the same 5 m pool reach
  the rule's mean/median/2×+ rate at the 100th percentile and its win rate at
  the 99.98th (seed 7).
- **Holdout:** thresholds are fitted on Oct 3–5 only. Aug 9 + Nov 8 are never
  used to choose a threshold.

## Reproduce

```bash
# canonical rule + re-computed validation
.venv/bin/python tools/backtest/ave_hot_money_strategy.py

# machine-readable
.venv/bin/python tools/backtest/ave_hot_money_strategy.py --json

# rebuild the exploratory evidence (needs the local dataset)
.venv/bin/python tools/backtest/analyze_ave_signal_patterns.py
.venv/bin/python tools/backtest/ave_signal_pattern_rules.py
```

`ave_hot_money_strategy.py` re-computes the headline statistics from the saved
tables, so the numbers above cannot silently drift.

## Relationship to the execution pipeline

The strategy is intentionally standalone. `backtest_strategy.py` and
`execution_simulator.py` remain the authoritative execution path and are not
modified by this module. To run the filter in an execution-aware backtest, use
`hot_money_mask()` (or `decide()`) to subset `strategy_signals.csv` before the
run; that integration is left as a separate, explicit step.

## Helius historical data: where it helps

The Helius historical-data docs are useful for this strategy, but they are
**optional**. The validated result reproduces entirely from the local replay
(`ohlcv_1m.parquet` + `events.parquet`). Helius adds value in three places:

1. **Backfill / independent verification** - pull the mint's transactions in
   the 60 seconds before a decision and compare `ev_flow_1m` with the local
   events. This is the strongest check that the feature is correct.
2. **Extend beyond the five replayed days** - rebuild the flow feature for days
   or mints the local replay does not cover.
3. **Live feature computation** - at alert time, query the last minute of mint
   activity instead of depending on a replay file.

Relevant methods (Helius mainnet):

| Need | Method | Notes |
| --- | --- | --- |
| Mint transaction history with time filters | `getTransactionsForAddress` | 10+ credits, metered by results; no batching; `transactionDetails: "signatures"` or `"full"`; `filters.blockTime` |
| Parsed SOL/token transfers for a wallet | `getTransfersByAddress` | 10 credits, Developer+; limit 1–100; useful for wallet-level forensics |
| Raw block / signatures | `getBlock`, `getSignaturesForAddress` | 1 credit each; standard archival RPC |

Practical constraints:

- Enhanced-API rate limits are separate from standard RPC: 2 req/s on Free,
  10 req/s on Developer. Do not batch `getTransactionsForAddress`.
- For a backtest, query with `blockTime <= decision` and use `finalized`
  commitment. For live use, `confirmed` plus the 5 s entry latency is the
  realistic trade-off; clock skew and finality must be handled explicitly.
- `getTransactionsForAddress` takes an address (wallet, token mint or program).
  Passing the mint returns transactions that reference the mint; parse the
  token-transfer instructions to recover signer and SOL amount. The local
  `events.parquet` already stores the fields the rule needs (`tx_signer`,
  `quote_amount`), so Helius is a verification/backfill source, not a new
  feature definition.
- Costs are small at this scale: 74 selected signals × a couple of requests is
  well within a 1M-credit monthly allocation, but a broad scan of thousands of
  mints should be budgeted and rate-limited.

The credential lives in `.env` as `HELIUS_API_KEY` (ignored by git, never
committed). The module in this repository does not make network calls.

## Caveats

- 74 signals is a small sample; a single 10× runner moves the mean. The median
  and the five-day sign test are the more robust statistics.
- The holdout is two days, not a random sample.
- 1-minute close sampling means real fills can differ inside the minute.
- `alert_inflow_sum` (sum of SOL-denominated wallet lines) is missing for the
  few alerts that print token units. The rule uses `alert_total_buy`, which is
  always present.
