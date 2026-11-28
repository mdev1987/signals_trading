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

---

# V2: `scanner_pumpfun_amm_tp50_quote_guard` (combined TP + guard)

Status: **declared 2026-11-27, paper only, experiment. Not yet run.** This is a
new pre-registration; it is not a retune of V1 and its sample is never pooled
with `scanner_pumpfun_amm_tp50`, `scanner_pumpfun_amm_quote50` or the frozen
lane.

## Why combine them

The tp50 lane closes on the first mark at +50 %. That mark is a trade print,
not a sale: in the forward sample some positions printed +50 % and then sold
for almost nothing. The quote50 lane avoids that by measuring a real sale, but
it throws away the take-profit and can exit early on a temporary liquidity
dip. V2 keeps both mechanisms and makes each one answer the question it can
actually answer:

| decision | evidence |
| --- | --- |
| take profit | the mark reached +50 % **and** a full-size sale quote still recovers at least the entry cash cost |
| protective exit | a full-size sale quote returns less than `0.50×` the entry cost |
| partial salvage | the full-size route is gone but a smaller fraction still routes |

No exit rule can promise a sale after the liquidity is gone. A failed route is
recorded as evidence, never booked as a fill.

## Pre-registered configuration

| item | value |
| --- | --- |
| lane / rule name | `scanner_pumpfun_amm_tp50_quote_guard` |
| channel / entry | identical to `scanner_pumpfun_amm_tp50`: first card per mint, `dex == Pumpfunamm`, `20,000 <= liquidity_usd < 100,000`, first BUY print at/after card + 5 s, retry to +60 s |
| size / wallet / concurrency | 0.1 SOL, 10 SOL paper wallet, at most 10 open positions |
| take profit | mark reaches `+50 %` **and** a full-size sell quote returns `>= 1.00×` the entry cash cost |
| TP confirmation cadence | at most one confirmation probe per position per 15 s; a rejected mark is recorded and the position stays open |
| quote guard | every **15 s**, a **full-size** sell quote; sell when the net ratio is **strictly below 0.50** |
| guard measure | `size × quote_price × (1 − slippage) × (1 − fee)` over `entry_value + entry_fee` (the existing `preflight_exit_value_ratio`) |
| full-size route unavailable | record `unavailable`; only a **terminal** route/size failure (no route, zero output) probes the fractions (`0.5`, `0.25`, `0.1`, `0.01`) at most once in that check. A timeout/5xx is a venue problem and is retried at the next interval, never treated as a collapse |
| fraction route available | start the ordinary exit ladder (`quote_guard_partial`); the ladder sells what it can and the remainder is handled by the normal timeout/route policy |
| fraction measure | the fraction's net output over the **same fraction** of the entry cash cost; comparing a half-size sale with the full entry would be meaningless |
| unavailable route | never counted as a fill; no synthetic price is invented |
| fallback | unchanged hard sale at `entry + 5 min` |
| preflight guard | unchanged `0.90×` exit-value ratio |
| fees / slippage | unchanged `1.25 %` per side model, 50 bps start, 1000 bps cap |
| recovery | `output/recovery/scanner_pumpfun_amm_tp50_quote_guard_trade.sqlite3` (separate file) |
| mode | paper only; live is refused by `run_bot.sh` and `run_live()` |

The parameters are frozen in `src/paper_bot/signals.py`
(`SCANNER_TP50_QUOTE_GUARD_*`). `LiveConfig` fills them in from the rule name
and refuses a different value, so a typo cannot silently start a different
experiment under the same lane name.

## What is recorded

TP confirmation adds `tp_confirmation_checks`, `tp_unconfirmed`,
`tp_confirmation_ratio` and `tp_confirmation_evidence` to the trade row. Each
evidence row carries the mark (`mark_price_sol`, `mark_source`), the quote
price/impact/slippage, and a status of `confirmed`, `unconfirmed`, `invalid` or
`unavailable`. A confirmed TP keeps `exit_reason == "take_profit"` and
`exit_trigger_source == "tp_confirmation"`.

The guard rows keep the V1 fields and add:

| field | meaning |
| --- | --- |
| `status` | `ok`, `unavailable`, `invalid`, `partial_route`, `partial_unavailable` |
| `fraction` | the fraction probed in the recovery path |
| `ratio` | net output over the proportional entry cost |

The trade also carries `quote_guard_partial_probes`. `exit_quote_fractions`
shows what the ladder actually sold, so a partial liquidation is visible
without inferring it from the evidence list.

Outcome classification is also corrected for research reports: any
non-synthetic priced close whose realized exit value is below 1 % of the entry
cost is recorded as `near_zero_exit`, whatever triggered it (`take_profit`,
`quote_guard`, `horizon`). The PIT `exit_reason` is unchanged, so a catastrophic
TP is no longer counted as a clean `tp_hit`. This changes labels only; PnL is
untouched.

## Evaluation

Compare V2 with the **same-period tp50 sample**, not the replay, and report:

- net realized PnL and catastrophic (`return <= -90 %`) rate / total loss;
- guard exits and their realized PnL, split into full-size and partial;
- `tp_unconfirmed` marks and what the same positions did by the five-minute
  mark (the opportunity cost of not closing at the mark);
- TP mark vs realized return for confirmed TPs;
- `unavailable` probes and the partial-liquidation rate;
- premature exits: guard-fired positions that would have recovered above
  `0.50×` by the deadline.

Acceptance is the existing tp50 gate: at least 300 closed trades over five UTC
days, positive realized PnL, median `return_pct >= +10 %`, catastrophic exits
strictly below 16 %, write-offs strictly below 16 %, and
`run_integrity == VALID`. A drop in the win rate is not a failure if the
catastrophic tail falls by more; a higher win rate with the same tail is not a
pass.

The `0.70×` / 5 s candidate is a **separate** pre-registration. It must not be
run by changing this lane's constants mid-sample.

## Deploy / switch back

```bash
# first start of the experiment
env PYTHONPATH=src RULE=scanner_pumpfun_amm_tp50_quote_guard RESUME=0 ./run_bot.sh paper

# later restarts (paper only)
env PYTHONPATH=src RULE=scanner_pumpfun_amm_tp50_quote_guard RESUME=1 ./run_bot.sh paper
```

`run_bot.sh` sets the tp50 and guard arguments for this rule and refuses
`MODE=live`. Switching `RULE` back to `scanner_pumpfun_amm_tp50` resumes the
untouched baseline snapshot; the two samples are never merged.
