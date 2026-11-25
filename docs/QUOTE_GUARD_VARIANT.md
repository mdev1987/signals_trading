# `scanner_pumpfun_amm_quote50`: pre-registered executable-exit guard

Status: **declared 2026-11-25, paper only, experiment.** Not deployed as the
default lane. It exists to test one question: does selling when the position
*stops being sellable* reduce realized losses better than waiting for a low
trade print? It is a separate lane with its own recovery snapshot, and its
results must never be pooled with `scanner_pumpfun_amm` (frozen) or
`scanner_pumpfun_amm_tp50`.

## Why this experiment

The loss mechanism in the forward samples is not a slow decline that a
price-based stop can observe. It is a pool that **stops printing** while the
position is still open: the last trade mark stays high, then the five-minute
sale recovers almost nothing or has no route at all. A trailing stop or an
absolute stop needs a trade print to fire, so it misses exactly this case.

The guard asks a different question:

| question | mechanism |
| --- | --- |
| trailing / absolute stop | has the *last traded price* fallen far enough? |
| executable-exit guard | what would a **full-size sale return in SOL right now**? |

A quote is not a guaranteed fill. The guard is therefore evidence about
exitability, not a promise of recovery, and a failed route is never treated as
a successful sale.

## Pre-registered rule

Declared before any quote50 trade is collected. Do not change any row below on
the same sample; a different value is a new pre-registration.

| item | value |
| --- | --- |
| lane / rule name | `scanner_pumpfun_amm_quote50` |
| channel | `@AveSolanaTokenScanner`, first card per mint |
| entry filter | `dex == Pumpfunamm` and `20,000 <= liquidity_usd < 100,000` |
| entry | identical to `scanner_pumpfun_amm`: market buy on the first BUY print at/after card + 5 s; retry to card + 60 s |
| size | 0.1 SOL, 10 SOL paper wallet, at most 10 concurrent positions |
| guard | every **15 s** per open position, request a **full-size** (`100 %` of the remaining position) Jupiter sell quote |
| guard measure | net executable SOL output ÷ entry cash cost, using the same model as the preflight guard: `size × quote_price × (1 − slippage) × (1 − fee)` over `entry_value + entry_fee` |
| guard threshold | sell the whole position when the ratio is **strictly below 0.50** |
| guard request policy | one request per check at the configured base slippage; the normal exit slippage ladder may widen it, exactly as a real sale would |
| unavailable route | recorded as `unavailable`; **does not** trigger or count as a sale; retried at the next interval |
| retry policy | a failed check waits for the next 15 s interval; there is no request burst inside a check |
| fallback | the ordinary hard sale at `entry + 5 min` (last in-horizon reference, Jupiter quote) is unchanged |
| stops / trail / TP | none |
| preflight guard | unchanged `0.90×` exit-value ratio |
| fees / slippage | unchanged `1.25 %` per side model, 50 bps start, 1000 bps cap |
| recovery | `output/recovery/scanner_pumpfun_amm_quote50_trade.sqlite3` (separate file) |
| mode | paper only; live mode is refused by `run_bot.sh` and `run_live()` |

The first probe of a position is at `entry + 15 s`, not at entry: the entry
preflight already measured the sell route at `0.90×`, so probing again
immediately would duplicate it and burn a quote on the most congested moment.

## What is recorded per trade

Every probe appends a compact row to `quote_guard_evidence`:

| field | meaning |
| --- | --- |
| `ts_ms` | when the probe ran |
| `status` | `ok`, `unavailable` (no route / error) or `invalid` (quote could not be priced) |
| `ratio` | net exit value ÷ entry cost, or `null` |
| `price_sol`, `out_amount` | the quote's own conversion |
| `price_impact_pct`, `slippage_bps`, `quoted_at_ms` | route quality and quote age |

The trade also carries `quote_guard_ratio` (the threshold), `quote_guard_checks`,
`quote_guard_unavailable`, `quote_guard_min_ratio`, and the existing
`exit_trigger_source == "quote_guard"` / `exit_reference_kind ==
"trigger_observation"` when the guard fired. `quote_guard_unavailable` counts
only probes that could not route; a low but valid quote is counted in
`quote_guard_checks` and is the only thing that can trigger the sale.

## Acceptance gate

Same gate as the other lanes ([`SCANNER_EVIDENCE.md`](SCANNER_EVIDENCE.md)):
at least 300 closed trades over at least five UTC days, positive realized PnL,
median `return_pct >= +10 %`, catastrophic exits strictly below 16 %, write-offs
strictly below 16 %, fill rate at least 80 %, entry failures strictly below
10 %, and `run_integrity == VALID`. A guard run that merely reclassifies losses
without raising realized PnL does **not** pass.

The comparison for this experiment is the **frozen lane's same-period sample**,
not the replay: it must reduce the realized loss from dead pools without
turning winning positions into premature exits. Report at least:

- share of positions where the guard fired, and the realized PnL of that group;
- share of guard-fired positions that would have recovered above `0.50×` by
  the five-minute mark (the opportunity cost);
- `unavailable` probes per position, and how many positions had no route at
  the first probe;
- catastrophic and write-off shares versus the frozen lane.

## Deploy / switch back

```bash
# run the experiment
env PYTHONPATH=src RULE=scanner_pumpfun_amm_quote50 RESUME=0 ./run_bot.sh paper

# later restarts (paper only)
env PYTHONPATH=src RULE=scanner_pumpfun_amm_quote50 RESUME=1 ./run_bot.sh paper
```

`run_bot.sh` sets `--quote-guard-ratio 0.50 --quote-guard-interval-ms 15000` for
this rule and refuses `MODE=live`. Switching `RULE` back to
`scanner_pumpfun_amm_tp50` or `scanner_pumpfun_amm` resumes that lane's own
snapshot; the three samples are never merged.

## What this experiment does not do

- It does not change entry selection, the liquidity band, the five-minute
  horizon, sizing or concurrency.
- It does not claim a quote is a fill.
- It does not lower the fee assumption or reinterpret Jupiter price impact as
  a paid fee.
- It does not modify the frozen or tp50 lanes, and it is not deployed by
  default.
