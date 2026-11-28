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
| creation transaction | the transaction the enhanced API tags **`CREATE_POOL`** |
| supply | the mint→vault transfer **inside that transaction** (the initial supply) |
| pool vault | the recipient of that transfer |
| sell | base-token transfer **into** the vault after the creation transaction |
| buy | base-token transfer **out of** the vault |
| pre-entry distribution | sells at `t <= entry`; kept in `sold_pre`, **never** in the feature |
| feature | `sold_share_15s = sold(0 < t <= 15 s) / supply` |

The creation transfer is the only safe denominator. It is also excluded from the
sell/buy accounting because on these AMM launches the launcher seeds the whole
supply into the pool; counting it labels every mint as a 100 % dump. Pre-entry
selling is real but no exit rule can act on it, so it is recorded in
`sold_pre` and never enters the feature.

**Denominator correction (2026-11-28, still before any holdout was read).** Two
earlier denominators were tried on partial data and rejected:

1. *largest transfer in the window* — correct when the creation is inside the
   window, but for an already-migrated token the largest transfer is a buy, and
   dividing by it overstates the share. It also hid the distinction: it cannot
   tell a launch with a 600 000-token supply from a migrated token with a
   60 M-token buy.
2. *`getTokenSupply`* — wrong in the other direction: it returns supply
   **today**, and launchers burn unsold tokens after a rug (`700 000 → 65.7`).
   One row showed 92 % sold that way.

Both partial caches were deleted before the definitive collection. The frozen
rows below did not change.

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

- **Coverage.** A mint whose `CREATE_POOL` transaction is not inside
  `[entry - 240 s, entry + 70 s]` is dropped and counted (`no_creation`), not
  divided by a guess. This is the main coverage loss and it is visible in the
  fetch line; a resumed run does not re-fetch those mints.
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

Definitive collection, 2026-11-28. 2220 Helius requests, 0 failures, cache in
`output/early_flow/`. **1484 of 1533 mints measured; 43 dropped and counted
(`no_creation`)** because `CREATE_POOL` is not inside the window, which is
consistent with the scanner entering only already-migrated tokens for those.
Cohort: primary `scanner_*` n=1402, quote-guard n=82 (reported separately).
7 UTC days; 1193 training + 209 holdout (split by day, a mint is in one side).

### Frozen output (verbatim)

```
cohort rows=1484 (primary=1402 guard=82) days=7 mints=1484
feature: sold share at 15s, threshold 3.0% of supply
train rows=1193 cat=20.2% AUC=0.504 permutation p=0.866
holdout rows=209 days=2026-11-27,2026-11-28
  all   n= 209 cat= 17.2% mean=  +4.0% median= +23.1% pnl=+0.844 SOL
  fired n=  52 cat= 11.5% mean= +14.9% median= +28.0% pnl=+0.784 SOL
  kept  n= 157 cat= 19.1% mean=  +0.4% median= +21.8% pnl=+0.060 SOL
guard rows=82
  all   n=   82 cat= 28.0% mean= -11.5% median= +16.2% pnl=-0.957 SOL
  fired n=   38 cat= 31.6% mean= -14.6% median= +16.2% pnl=-0.560 SOL
  kept  n=   44 cat= 25.0% mean=  -8.9% median= +16.8% pnl=-0.397 SOL
candidate: holdout lowers the catastrophic rate without lowering mean return
```

### Reading the verdict honestly

The automated `decision()` returns **candidate**, which is **not** a ship
decision. Four checks against the frozen output:

1. **No training separation.** Pooled training AUC **0.504**, permutation
   p = 0.866. Per-day training AUCs are 0.41–0.54. The 3 % threshold was not
   confirmed on the data before the holdout.
2. **Holdout not statistically distinguishable.** Fired 11.5 % vs kept 19.1 %
   catastrophic (−7.6 pp), Fisher two-sided **p = 0.29**. Wilson-95 on the
   fired rate is 5.4–23.0 %, on kept 13.7–26.0 %. The frozen rule requires
   only ≥ 3 pp plus no lower mean return, not significance; it is met, but the
   sample does not reject "no difference."
3. **The holdout is effectively one day.** Both days are holdout, but
   2026-11-27 has 201 rows and all 52 fired; 2026-11-28 has only 8 rows and no
   fired rows. Within 11-27 the direction holds (fired 11.5 % vs 18.8 %), but
   there is no cross-day replication of the fired effect.
4. **Prediction, not PnL.** This measures whether early distribution *predicts*
   the outcome, not whether exiting at +15 s makes money. That is the
   pre-registered next step, not yet run. Fired PnL (+0.784 SOL) is the
   outcome PnL of fired rows, not the counterfactual PnL of acting on the
   signal; the mean-return comparison uses full-horizon return.

### Verdict

- The pre-registered frozen rule outputs `candidate`.
- **Nothing is shipped.** No running lane, gate or cost model changed.
- Because training has no separation and holdout p = 0.29, this is a **weak,
  unreplicated candidate**, consistent with the earlier in-sample read of
  `sold_share_15s` AUC ~0.63 on 39 trades not surviving a powered test.
- The line is **not closed** (the frozen rule would close it on `close`, and
  it did not), but proceeding requires the pre-registered next step, not a
  threshold revision.

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
