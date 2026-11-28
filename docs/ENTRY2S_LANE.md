# Pre-registration: `entry + 2 s` first-probe lane (L1) + separate `sold_share_15s` (L2)

Status: **frozen 2026-11-28, before any holdout data for this lane is read.**
Paper only. Nothing is shipped; nothing pooled; nothing about
`scanner_pumpfun_amm`, the running `scanner_pumpfun_amm_tp50_quote_guard`
lane, any gate, the cost model, or any frozen verdict changes.

## Recap of evidence (why this, and only this)

`docs/EXECUTION_RESEARCH.md`, `docs/EARLY_FLOW_STUDY.md`, `docs/DEV_LABELS.md`,
`docs/QUOTE_GUARD_VARIANT.md`, `docs/TP50_VARIANT.md`, `docs/SCANNER_EVIDENCE.md`,
`docs/COMPOUND_STUDY.md`:

- **No robust entry/exit signal filter.** Entry features flat; `sold_share_15s
  ≥ 3 %` weak + unreplicated (train AUC 0.504; holdout Fisher p=0.29;
  candidate, not ship); `sold_pre` real but not pre-registered; **dev-labeling
  yields no dev filter** (`funded_sniper`=0; creator holds no bag); V3 no-op /
  over-rejects; no health-score auto-exit.
- **Bimodal tail, margin ~0:** catastrophic ~18–28 % vs break-even ~79–81 %;
  PnL ≈ `1 − cat_rate`.
- **Wipe mechanism is execution, not a feature.** Of 13 catastrophes:
  **~5 already unsellable at first probe** (87–97 % sold before; quote ≈ 0;
  `no_route`), ~6 gradual distribution, ~2 LP pull; **collapse is a step
  between 15 s probes.** **Blocked today:** "removing the 5 already-gone cases
  needs a probe at entry+2 s, which the current pre-registered guard
  deliberately omits (first probe at +15 s)."
- **Your 5-min rule can't work literally:** horizon = 300 s (median hold 299 s);
  5 min *is* the exit; **Top Traders is a holder snapshot, not sell history;
  Helius is ground truth.**

So the only remaining honest test is **execution timing, not a filter.**

## L1 — `entry + 2 s` first probe (execution-risk gate, not a signal filter)

- **Entry is unchanged.** `scanner_pumpfun_amm` intact; this is **only** a
  change to **first-probe time: +15 s → +2 s**; everything else identical to
  `scanner_pumpfun_amm_tp50_quote_guard`.
- **New lane, not a retune.** `scanner_pumpfun_amm_tp50_quote_guard_entry2s`.
  Own recovery snapshot. **Never pooled** with `scanner_pumpfun_amm`,
  `scanner_pumpfun_amm_tp50`, or `scanner_pumpfun_amm_tp50_quote_guard`.
  **Not deployed** by this document. The running guard lane is untouched.

### Cohort (frozen; split by day and mint; never by row)

- Source: closed trades in `scanner_pumpfun_amm_tp50_quote_guard`.
- **One row per mint**; `rule` preserved.
- **Split by UTC day AND by mint**; a mint is in exactly one side; a day is not
  split across sides.
- **Last two UTC days = holdout**, frozen before any row of it is read.
- **Same mints** in both arms (+15 s baseline vs +2 s), paired by mint.

### Deciding metric (pre-registered, **both** reported)

- **Selection cohort** AND the **baseline** are both reported:
  - selection = holdout rows in `scanner_pumpfun_amm_tp50_quote_guard_entry2s`;
  - baseline = **same mints** under `scanner_pumpfun_amm_tp50_quote_guard`
    (+15 s), and additionally the full `scanner_pumpfun_amm_tp50_quote_guard`
    no-rule baseline.
- **Primary:** **already-unsellable-at-first-probe rate** =
  `P(full-position sell quote ≈ 0 OR no_route at first probe)`.
  Paired comparison on the same mints: +2 s vs +15 s.
- **Secondary (guard, not replacement):** full-horizon **net-of-fee return**;
  **catastrophic rate** (`return_pct ≤ −90 %`); concentration (`forward_gates.
  concentration_summary`); Wilson intervals; day-split and mint-split.

### Acceptance (frozen; ship-candidate, not auto-ship)

Ship-**candidate** only if, on the **unpooled holdout**:

1. +2 s **lowers already-unsellable-at-first-probe by ≥ 3 pp** vs +15 s on the
   same mints; **and**
2. **does not lower** full-horizon **net-of-fee** mean return; **and**
3. **catastrophic rate does not rise**; **and**
4. result holds on **day-split** and **mint-split** (no single day/mint drives
   it); concentration reported; **and**
5. **real all-in fee already measured** (see order); nothing about any other
   parameter, threshold, filter, or cost changed; **no pooling**; **no
   post-hoc revision.** Otherwise **close**; `undecided` if no holdout.

A `candidate` is **not** a ship; a separate, fresh pre-registration + untouched
new-lane sample would still be required. **No auto-exit on a health score.**

## L2 — `sold_share_15s` (separate; NOT bundled with L1)

`docs/EARLY_FLOW_STUDY.md` is frozen and stands. **Not reopened, not revised,
not pooled with L1.** Proceeding requires the already-pre-registered
**counterfactual-PnL** step below; recall L2's predictive test was `candidate`,
**not** robust. Keep L1 and L2 unpooled.

## Counterfactual requirement (any PnL claim; prediction ≠ PnL)

No PnL claim without **realized counterfactual**, not just `prediction`:

- **Streamed replay only.** PumpAPI
  `https://replay.pumpapi.io/YYYY/MM/DD/HH.jsonl.zst`. **One hour at a time.**
  Stream-decompress; **never fully load**; filter to cohort; **delete each hour
  file before downloading the next; old files deleted before downloading new
  ones;** raw replay never persisted; **no `playwright`.**
- Reconstruct exit price at first probe (+2 s / +15 s) and horizon; sell
  **full position**; **full-position sell quote net of fees, not liquidity
  alone.** `execution_simulator` is the reference fill model.
- Apply **deployed cost model**; **recoverable ATA rent ≠ expense**;
  **real all-in fee measured first; do not tune 1.25 %/side to flatter PnL;**
  keep `execution_simulator` + `pool_health` math test-pinned.
- **Order (frozen):** freeze this doc → **measure real all-in fee** →
  **verify streamed replay schema on one hour** → run L1 on holdout **once** →
  stop. No pooling; no post-hoc revision; no shipping without OOS.
- **No `Jupiter` 15 s-per-position polling** without measuring request load;
  **no many filters at once.**

## Replay mechanics (built + verified; NOT a shipping test)

`src/counterfactual_replay.py` implements the hard replay rules; it computes
**no PnL**. `iter_hour` streams HTTP→zstd→line-buffered JSON, **one hour at a
time**, filters, never fully loads, `via_file` downloads to a scratch temp
under `/tmp/opencode`, **deletes old files before downloading new ones and
after filtering**, removes scratch, raw replay never persisted; no
`playwright`. `--schema-check` verifies schema. 7 tests. **Schema verified on
a streamed hour** (`https://replay.pumpapi.io/2026/11/27/10.jsonl.zst`, HTTP
200; 81-key union; events carry `mint`, `action` (`buy`/`sell`/`transfer`…),
`price`, `priceSol`, `quoteChange`, `breakdown`, `pre/postBalances`,
`localTimestamp`, `txSigner`, `pool`; **not only Pump.fun** — filter `pool`;
schema varies). **This does not make PnL;** real fee + verified fill are still
required, and the predictive basis for L1 is not robust. **L1 is NOT run;
nothing is shipped.**

## Ordering / blocks (honest; not waved away)

- **Real all-in fee currently unmeasured.** Requires a **funded round trip**
  (we hold no keys/funds; read-only research) or authoritative Pump AMM
  fee-schedule confirmation. **Step order is blocked here**; **1.25 %/side
  unchanged.** PnL-grade claims can't be made without it.
- **No local replay archive;** PumpAPI replay availability/schema must be
  verified on a streamed sample; **no offline guarantee.**
- **No proof L1's cause;** L1 tests whether +2 s removes the uninsurable slice,
  which is **not** itself proven; **`CREATE_POOL`-in-window coverage**;
  wallet ≠ identity; catastrophic ≠ intent; **candidate ≠ ship**; running
  quote-guard lane untouched + unpooled.

## Change control

Any change to lane, probe time, threshold, cohort, split, metric, fee, replay
rule, or acceptance after freezing is a **new** pre-registration on a **new**
sample — not a revision of L1/L2. **Nothing here is shipped.**
