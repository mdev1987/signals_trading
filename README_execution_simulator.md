# Execution Simulator v1.1

A first-pass historical, event-driven execution simulator for SOL-quoted PumpAPI pools. It reads the original PumpAPI JSONL or Zstandard JSONL event stream directly. It does not require or read `trades.parquet`.

## Supported pool models

| Pool | Reserve fields | Status |
|---|---|---|
| `pump` | `vTokensInBondingCurve`, `vQuoteInBondingCurve` | Supported as a virtual-reserve constant-product bonding curve |
| `pump-amm` (virtual quote) | `tokensInPool`, `quoteInPool`, `virtualQuoteInPool` | Supported constant-product AMM using effective quote reserves |
| `pump-amm` (virtual tokens) | `tokensInPool`, `quoteInPool`, `virtualTokensInPool` | Supported constant-product AMM using effective token reserves |
| Other pool types | Protocol-specific reserves and/or curve data | Rejected as unsupported |

Only WSOL-quoted pools are supported. USDC and other quote mints are rejected rather than silently treating their amounts as SOL.

Pump AMM pools use exactly one virtual reserve style, so the two are mutually exclusive:

```text
virtual quote style:  effective_quote_reserve = quoteInPool + virtualQuoteInPool
                      effective_token_reserve = tokensInPool
virtual token style:  effective_quote_reserve = quoteInPool
                      effective_token_reserve = tokensInPool + virtualTokensInPool
```

Both styles reproduce the feed's reported price exactly. Signed virtual values are accepted; `virtualTokensInPool` is typically negative. A snapshot with both virtual components non-zero is rejected as `ambiguous_virtual_reserves`, and a snapshot with neither is rejected as `missing_amm_reserves` rather than silently falling back to vault reserves. Each fill reports the detected `reserve_model` (`bonding_curve`, `amm_virtual_quote`, or `amm_virtual_tokens`).

## Order input

Provide a CSV with these columns:

```csv
timestamp_ms,mint,pool_id,side,amount
1794432878000,TokenMintAddress,PoolAddress,buy,0.05
1794432885000,TokenMintAddress,PoolAddress,sell,250000
```

- `timestamp_ms` is the **execution time**, not the original signal time. Add the modeled signal-to-order latency before writing the order.
- `side=buy`: `amount` is gross SOL/WSOL budget, including the modeled pool fee.
- `side=sell`: `amount` is the number of tokens offered for sale.
- `pool_id` must be selected using information available at the decision time. Do not select a pool using a future candle.
- When using `pool_selection_timeline.csv`, join by mint and choose the latest row with `available_at_timestamp_ms <= timestamp_ms` (the order execution time). The timeline selection for a candle is only known at that candle's close, not at its start.
- Prefer rows where `execution_supported == 1`. That flag means the pool is WSOL-quoted and uses a reserve model this simulator implements. Rows with `pool_selection_status != ready`, a non-WSOL `quote_mint`, or `execution_supported == 0` will be rejected or misrouted; the simulator does not convert non-WSOL quote reserves into SOL.
- Amounts use the human-readable units used by PumpAPI events: SOL for WSOL quote amounts and token units for token amounts.

## Run

```bash
uv add zstandard
uv run python execution_simulator.py 05.jsonl.zst orders.csv \
    --output output/execution_fills.csv \
    --max-state-age-ms 60000
```

For an uncompressed `.jsonl` file, `zstandard` is not required. `orjson` is optional.

Set `--max-state-age-ms 0` to disable snapshot-age rejection. An optional `--max-price-impact-bps` guard rejects orders whose estimated curve impact exceeds the configured cap.

## Execution rules

1. The simulator streams source events in timestamp order.
2. It uses the latest valid snapshot with `event_timestamp < execution_timestamp`. Events at the exact same millisecond as the order are excluded to avoid ambiguous ordering and look-ahead.
3. It requires positive effective reserves and an explicit `poolFeeRate` in the range `[0, 1)`.
4. For buys, `amount` is the gross quote budget. The modeled fee is deducted from that budget, and the remainder is the curve input: `curve_quote = amount * (1 - fee_rate)`. The fee is charged in the quote asset.
5. For sells, the curve calculates gross quote output first, then the modeled fee is deducted from proceeds. This is the same fee-in-quote convention as buys.
6. Price impact is reported separately from the modeled pool fee. The output average price includes the fee.
7. The latest raw reserve deltas are applied to a shadow reserve state. This preserves the hypothetical order's reserve impact while replaying subsequent observed reserve changes.
8. A rejected order does not mutate shadow reserves.
9. Orders must be executed in non-decreasing timestamp order. An out-of-order `execute()` call is rejected with `invalid_order` / `orders_must_be_non_decreasing_in_time` instead of silently using a future pool state.

## Important assumptions and limitations

- This is a continuous floating-point approximation, not instruction-level reproduction. It does not reproduce u64/i128 arithmetic, lamport/token rounding, per-fee component rounding, creator/protocol/LP/buyback fee splits, transaction account constraints, priority fees, Jito tips, failed-transaction probability, or compute-budget constraints.
- `poolFeeRate` is treated as one aggregate fee rate. Pump's real fee schedule may contain multiple components and can change with market conditions. The simulator uses the latest fee-rate value observed before the order.
- For PumpSwap, the effective quote or token reserve incorporates the pool's virtual component when provided. The execution fee/reserve treatment remains an aggregate approximation because PumpAPI does not expose all on-chain fee components in the compact snapshot fields used here.
- The simulator assumes future observed external reserve changes are exogenous: it applies their recorded reserve deltas to a shadow state. It does not model how other traders would change their behavior in response to the hypothetical order.
- A pool snapshot is the post-event state reported by PumpAPI. Orders use only snapshots strictly earlier than their execution timestamp. Multiple events sharing the order's exact millisecond are conservatively excluded.
- A stale state can be rejected with `--max-state-age-ms`.
- Orders require an explicit `pool_id`. The simulator intentionally does not guess a pool or infer missing SOL reserves from a spot price.
- Historical OHLCV execution candles are not used as fills.
- Network/priority fees are not included in PnL yet. That is a separate cost-model step.

## Tests

```bash
uv run python -m unittest -v test_execution_simulator.py
```

The tests cover reserve-field selection for both PumpSwap virtual-reserve styles, ambiguous virtual reserves, unsupported markets, fee calculations, buy/sell calculations, strict timestamp cutoff, shadow reserve deltas, stale snapshots, price-impact rejection, liquidity limits, out-of-order source events, and out-of-order order rejection.
