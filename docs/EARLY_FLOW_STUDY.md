# Early on-chain distribution study (post-entry, pre-collapse)

Status: **pre-registered 2026-11-28, before the holdout was read.** Paper only,
measurement only. This study does not touch the strategy, the running lane, any
gate or the cost model.

## Question

The entry scans are flat: no card field, no preflight depth ratio (AUC 0.458)
and no walk-forward screen separates the wipes from the winners. Is there
instead a **post-entry, pre-collapse** signal — how much of the supply is sold
into the pool in the first seconds — that is visible while a full-size exit
still quotes near par?

## What was already inspected (disclosed)

The first 39 trades of the `scanner_pumpfun_amm_tp50_quote_guard` lane were
inspected while this study was being designed. Their result is therefore **not
a holdout**. They are excluded from the primary cohort and reported only as a
separate replication block. Every parameter below is frozen before the primary
holdout is read.

## Data and measurement

Source: Helius enhanced transactions,
`GET /v0/addresses/{mint}/transactions`, filtered to
`[entry - 240 s, entry + 70 s]`, `sort-order=asc`,
`token-accounts=all`, `limit=100`, at most three pages. Responses are cached in
`output/early_flow/flows.jsonl`, one JSON line per mint, so the collection is
resumable and the analysis is reproducible offline.

Per mint:

| step | rule |
| --- | --- |
| pool vault | recipient of the **largest single base-token transfer** in the window |
| supply proxy | that transfer's amount (Pump AMM seeds 1 000 000 000) |
| creation transaction | the transaction carrying that transfer; **all its transfers are excluded** |
| sell | base-token transfer **into** the vault after the creation transaction |
| buy | base-token transfer **out of** the vault |
| pre-entry distribution | sells at `t <= entry`; kept in `sold_pre`, **never** in the feature |
| feature | `sold_share_15s = sold(0 < t <= 15 s) / supply_proxy` |

The creation transfer must be excluded because on these AMM launches the
launcher seeds the whole supply into the pool; counting it labels every mint as
a 100 % dump. The same reasoning applies to pre-entry selling: it is real, but
no exit rule can act on it, so it is recorded separately.

## Frozen parameters

Declared before the holdout. Changing any row is a new pre-registration.

| item | value |
| --- | --- |
| cohort | one row per mint: the earliest closed trade in a `scanner_*` lane |
| excluded | `hot_money_continuation`; the quote-guard lane (replication only) |
| feature | `sold_share_15s` |
| threshold | **3.0 % of supply** |
| split | last **two UTC days** = holdout, earlier days = training; a mint appears in exactly one |
| primary statistic | AUC on training, plus a two-sided permutation p (5 000 resamples, seed 20261128) |
| holdout test | the frozen 3.0 % threshold applied unchanged: catastrophic rate and return of fired vs kept vs all |
| catastrophic | `return_pct <= -90 %` |
| decision | worth a live exit trigger only if the fired group's holdout catastrophic rate is **at least 3 pp below** the no-rule baseline **and** the fired group's mean return is not below it; otherwise close the line |

The threshold is not tuned in this study. A different value is a new
hypothesis on a new sample.

## Limitations, stated up front

- **Supply proxy.** A mint whose creation deposit is outside the window, or is
  split across transactions, gets a wrong denominator. Rows with
  `supply_proxy < 1e8` are dropped; coverage is reported. This biases the
  cohort toward launches whose seed transfer is visible in the window.
- **Three-page cap.** Very busy mints are truncated at 300 transactions; the
  flag is stored per row.
- **No counterfactual price yet.** This study measures *prediction* (does early
  distribution separate the outcomes), not the PnL of exiting at the signal.
  The counterfactual exit price needs the tape and is a separate step.
- **Reverse causality.** Selling and price are the same event: a dump can both
  cause the collapse and be caused by it. The 15 s window is chosen because it
  is the first moment a real exit rule can act on the current probe cadence.
- **One lane, one venue.** The cohort is Pump AMM cards from one Telegram
  channel; nothing here generalises to other pools.
- **Multiple comparisons.** One feature and one threshold are pre-registered
  precisely so the result is not the maximum of a scan.

## Results

Pending the collection run; this section is filled from
`src/early_flow_study.py` output without changing any frozen row above.

## Next step if the holdout is a candidate

1. Reconstruct the exit price at `entry + 15 s..30 s` from the PumpAPI tape
   (stream one hour, filter to the cohort, delete), and compute the
   counterfactual PnL with the deployed cost model.
2. Only then pre-register an exit trigger on a *new* lane, with its own
   recovery snapshot — never as a retune of the running guard sample.
3. Separately measure whether an earlier first probe (entry + 2 s instead of
   +15 s) would have caught the group that was already unsellable at the first
   probe. That is a probe-timing question, not a filter question.

## Reproduction

```bash
# collect and analyse (uses HELIUS_API_KEY from .env, <= 2 req/s)
env PYTHONPATH=src .venv/bin/python src/early_flow_study.py

# re-analyse a warm cache without any network call
env PYTHONPATH=src .venv/bin/python src/early_flow_study.py --no-fetch
env PYTHONPATH=src .venv/bin/python src/early_flow_study.py --no-fetch --json
```
