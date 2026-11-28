# Pump.fun Compounding Study — Code Review

**Reviewed artifact:** `src.zip`  
**Primary implementation:** `src/pumpfun_compound_study.py`  
**Primary tests:** `src/tests/test_pumpfun_compound_study.py`  
**Study description:** `COMPOUND_STUDY.md`  
**Scope:** Static implementation review against the strategy test specification and the study's own methodology. This is not a validation of the underlying replay data or proof of strategy profitability.

## Executive verdict

The implementation is a useful first replay harness, and the pool-lineage work, point-in-time event handling, reserve-sized fills, and transparent stream/channel distinction are good foundations. However, **I would not use its current PnL as evidence of an after-cost edge yet**. Several implementation paths do not implement the documented strategy, and some execution assumptions systematically make exits easier than a real Jupiter route would be.

The highest-priority blockers are:

1. Creator-buy values are read as `quoteAmount` and labelled as SOL even when the pool quote asset is not WSOL; missing creator-buy data is also not rejected in the stream lane.
2. The channel lane removes `CREATOR_BUY_TOO_LARGE` unconditionally, including when a create event and creator-buy value are available.
3. Dead-pool recovery executes immediately, instead of entering a 600-second monitoring state. Since the constant-product sell function generally returns a positive amount whenever reserves exist, the retry path is rarely reached.
4. The simulator does not implement the 150 bps entry limit, the 200/500/1,000 bps sell tiers, Jupiter route availability, or route failure. These omissions matter especially when compounding increases the stake above $2.
5. The pooled bootstrap resamples realized dollar PnLs independently. That is not a valid simulation of the path-dependent compounding ledger in lanes B/C.

The channel lane is explicitly an adaptation and must not be described as a faithful course-strategy result. The stream lane is closer to the requested universe, but still needs the creator-buy and execution fixes above.

---

## Findings

### P1 — Creator-buy amount has the wrong unit for non-WSOL pools

**Location:** `src/pumpfun_compound_study.py:1049-1055`, `1170-1206`, `1257`, `253-257`.

The create event's raw `quoteAmount` is assigned directly to `creator_buy_sol`. The same value is later compared with `creator_buy_max_sol=20` and used by the fair-launch score. That is only dimensionally correct when the pool's quote asset is SOL/WSOL. For a non-WSOL quote pool, the amount is in that quote token, not SOL.

This conflicts with the implementation's stated support for non-SOL quote assets. A non-WSOL creator buy can therefore be incorrectly accepted or rejected and its fair-launch score can be meaningless.

**Fix:** Preserve the raw amount and its quote mint. Either:
- restrict the faithful creator-buy filter to WSOL-denominated creation events and mark other quote assets as unsupported/missing for this filter; or
- convert the creator buy using the same event's quote-asset USD value and a contemporaneous SOL/USD rate before naming it `creator_buy_sol`.

Do not compare an unconverted quote amount to a SOL threshold.

**Tests:** Add paired WSOL and non-WSOL create-event tests that represent the same USD/SOL creator investment and must produce the same `creator_buy_sol` result (or explicitly reject the non-WSOL case).

### P1 — Missing creator-buy data does not reject a stream candidate

**Location:** `src/pumpfun_compound_study.py:229-233`, `253-257`, `195-216`, `1049-1055`.

`rejection_reasons()` treats missing liquidity and market cap as required-data failures, but not a missing creator-buy value. `score_candidate()` returns `fair_launch=None` and `score_complete=False`, yet `screen_candidate()` still permits entry when the other filters pass.

The strategy spec says creator initial-buy data is required for the faithful reproduction and that a missing value should be `MISSING_REQUIRED_DATA`, unless a separately named variant intentionally omits this component. As written, incomplete scores can enter and can also affect candidate ranking.

**Fix:** For the `stream` strategy, reject missing creator-buy data as `MISSING_REQUIRED_DATA`. Keep an explicit alternate variant for any source that genuinely does not provide it. Never silently let an incomplete candidate enter the base strategy.

**Tests:** Missing creator-buy rejects the stream candidate; the explicitly named no-creator-data variant may proceed and must be reported separately.

### P1 — Channel lane disables the creator-buy filter even when the value exists

**Location:** `src/pumpfun_compound_study.py:1341-1347`; the adaptation report is emitted at `1447-1450`.

The channel path removes every reason whose name starts with `CREATOR_BUY`:

```python
[reason for reason in rejection_reasons(...)
 if not reason.startswith("CREATOR_BUY")]
```

This happens even when `series.creator_buy_sol` is known. The report text says the creator-buy filter is applied when a create event is present, but the code does not do that.

**Fix:** Only suppress the creator-buy rejection when the create event/value is genuinely absent. If a usable value is present, keep `CREATOR_BUY_TOO_LARGE`. Also make the adaptation report reflect the actual rule.

**Tests:** Channel candidate with `creator_buy_sol=21` is rejected; one with `creator_buy_sol=1` is not; one with no create event is labelled as unavailable and handled according to the explicitly documented channel adaptation.

### P1 — Dead-pool handling does not follow the 600-second retry rule

**Location:** `src/pumpfun_compound_study.py:979-985` and `1400-1407`.

When liquidity falls below `$25`, both stream and channel paths immediately call `sell_sol()` and close the position if it returns any positive amount. They only schedule the 600-second retry if the sell function returns `None` or zero.

With positive quote reserves and a valid token reserve, the constant-product formula normally returns a positive quote amount. Thus the implementation is effectively an immediate forced recovery at the remaining pool reserves, not the specified `DEAD_POOL_MONITORING` state followed by a recheck after 600 seconds. The `dead_pool_retry_ms` setting is not implementing the specified state machine.

**Fix:** On first observing liquidity below `$25`, enter a distinct dead-pool state and schedule the next attempt for `now + 600s`. At that time, obtain a fresh executable quote/fill. Keep the position open if no executable route exists. Do not treat a merely positive mathematical constant-product output as proof that a route is executable.

**Tests:** No sell attempt occurs at the initial low-liquidity observation; the first attempt is exactly 600 seconds later; a failed attempt remains open and schedules another 600-second check; a successful attempt closes at its actual fill.

### P1 — Slippage limits, sell tiers, and route failures are not implemented

**Location:** `src/pumpfun_compound_study.py:437-474`, `967-1005`, `1388-1422`; the simplification is described in `COMPOUND_STUDY.md` lines 144-148 and 206-207.

`buy_tokens()` and `sell_sol()` always calculate a mathematical constant-product fill when the reserves are present. The code does not test the 150 bps entry limit, does not apply sell tiers of 200/500/1,000 bps, and does not represent Jupiter route absence or quote failure. A TP/SL exit therefore normally succeeds even where a real aggregator route might fail.

The study describes this as a simplification in the direction of more fills. That is not conservative for an edge estimate: additional successful exits can reduce the measured catastrophic tail and improve reported PnL.

The entry omission becomes more important in lanes B/C. The `$2` starting stake and `$2,500` liquidity floor may imply low entry impact for lane A, but the compounded stake can grow substantially after consecutive wins. A fixed liquidity floor does not guarantee a 150 bps fill limit for a much larger stake.

**Fix:** Keep the current constant-product engine as a named *pool-only optimistic/diagnostic fill model*. Add a separate execution-constrained lane that:
- computes position-size impact at entry and rejects above 150 bps;
- attempts exits at the configured sell tiers in order;
- records quote/route failure as an unresolved position, not a fill;
- uses full-position sell sizing;
- includes any network/priority/other fixed costs that the study intends to claim are included.

Do not call the current model a Jupiter simulation. It does not call Jupiter or reproduce Jupiter route availability.

**Tests:** Test stake sizes that grow under lane B/C at the same minimum pool liquidity; verify entry can fail the 150 bps guard. Test each sell tier, all-tier failure, and a later successful retry.

### P1 — `P(PnL > 0)` bootstrap does not replay the compounding strategy

**Location:** `src/pumpfun_compound_study.py:1609-1647`.

`pooled_summary()` samples already-realized `pnl_usd` values independently and sums them. That can be a rough trade-PnL resampling diagnostic for a fixed-size lane, subject to the acknowledged dependence problem. It is not a bootstrap of the B/C compounding process: trade sizes, future PnLs, savings, floor top-ups, pauses, and the 30-cycle cap depend on the order of wins and losses. Resampling dollar PnLs from the observed path preserves the historical sizing and does not simulate alternate paths.

**Fix:** For lanes B/C, bootstrap ordered daily/event blocks and replay each sampled path through the ledger and entry sizing rules, including the pause, floor, and cap. Alternatively remove `p_pnl_positive` and its confidence interval from B/C until such a simulator exists. Keep the present metric labelled as an iid resampling diagnostic, not the probability that the compounding strategy ends profitable.

**Tests:** Construct two sequences with the same set of trade returns but different ordering; verify compounding outcomes differ where path-dependent rules apply. Bootstrap must rerun the ledger rather than sum stored dollar PnLs.

### P2 — Multi-day aggregation does not create one continuous compounding run

**Location:** `src/pumpfun_compound_study.py:779`, `1281`, `1847-1855`.

Each day-level `stream` or `channel` invocation creates a fresh ledger with `$2`, zero savings, zero loss streak, and zero completed cycles. `aggregate` then pools the separate day artifacts. This is fine only if the intended experiment is **one independent compounding run per day**. It does not represent one continuous 30-cycle run across multiple days.

**Fix:** State the experiment definition explicitly. If the intended unit is one daily run, keep this behavior but report it as such. If the intended unit is a continuous multi-day account, replay the days chronologically with one shared ledger and carry the open-position/pause/cycle state across day boundaries.

**Tests:** Verify the selected run boundary and that the 30-cycle cap, saved balance, trade size, and pause state behave accordingly across files/days.

### P2 — Aggregation does not validate configuration identity

**Location:** `src/pumpfun_compound_study.py:1835-1855`; config hash is generated at `1513-1516`.

Artifacts are grouped only by `(strategy_id, source, lane)`. `config_hash` is not part of the grouping key and mismatches are not rejected. Artifacts produced with different configurations can therefore be silently pooled while the aggregate reports only the first artifact's hash.

**Fix:** Require identical `config_hash`, strategy version, and relevant execution-model version within an aggregate group; fail with a clear error when they differ. Include code/replay schema version in the artifact identity, not just `StrategyConfig` values.

**Tests:** Mixing two artifacts with different config hashes must fail or produce separate groups.

### P2 — Study document and implementation disagree on the 2% price/reserve discrepancy assumption

**Location:** `src/pumpfun_compound_study.py:348-358` and `220-240`; `COMPOUND_STUDY.md` lines 150-156.

The study document lists an assumption that multi-pool tokens are excluded when feed/reserve prices disagree by more than 2%. The implementation says `price_discrepancy` is diagnostic only and never uses it as a rejection filter. The broader strategy specification also describes this discrepancy as a diagnostic rather than a gate.

**Fix:** Choose one interpretation and make the document/code agree. Based on the current code comments and the strategy spec, the cleanest correction appears to be removing or rewriting the 2% exclusion claim in `COMPOUND_STUDY.md`; do not silently add a 2% gate, because that would change the candidate universe.

### P2 — Candidate audit records omit fields needed to reproduce the decision

**Location:** `src/pumpfun_compound_study.py:1568-1585` and `TradeRecord` around `535-575`.

The candidate export omits creator-buy amount, quote mint, transaction count, buy/sell ratio, mark/reference price, quote-USD conversion, and whether the score was complete. The trade export likewise does not retain the full candidate metrics or explicit execution-model/fill assumptions for that entry. This makes it difficult to explain why a candidate passed or reconstruct its rank after the run.

**Fix:** Store the full qualification snapshot (including raw units and quote mint) with every candidate and entered trade. Include entry/exit fill model, fee rate, price impact, and whether an execution tier/route constraint passed.

### P2 — Stream replay retains every token track until the run ends

**Location:** `src/pumpfun_compound_study.py:775`, `TokenTrack.retire()` around `680-700`, and candidate retirement paths around `942-965`.

`retire()` clears the trade deque, but the token remains in the `tracks` dictionary. Rejected, expired, ranked-out, and busy-skipped candidates therefore retain their `TokenTrack` and `PoolState` objects for the rest of the day. This can make memory use grow with every eligible creation in a long replay.

**Fix:** Remove finalized non-position tracks from the dictionary after their candidate record has been written. Retain only the currently open position's track and candidates still awaiting their qualification/retry time. Add a full-day memory regression test.

---

## Good parts worth preserving

- `stream` and `channel` have separate strategy identifiers; the channel lane is not passed off as the course creation-event universe.
- Stream qualification uses event timestamps and only accumulated prints up to the decision time; future prints are excluded by the point-in-time window.
- Candidate collisions at the same decision timestamp are resolved deterministically by score, launch time, and mint.
- Pool lineage is pinned to Pump/Pump AMM, and third-party pool records are not allowed to drive the mark.
- Trigger marks and fills use a consistent quote-asset unit model, with quote-to-USD conversion taken from the same event.
- PnL is based on modeled sale proceeds, not simply the 2.00x/0.82x trigger threshold.
- Open-at-end positions are separated from closed-trade PnL and shown in a write-off sensitivity.
- Lanes A/B/C are clearly separated, and the floor top-up is recorded as external capital rather than profit.

These are good foundations, but they do not cancel the execution and creator-buy issues above.

---

## Validation performed on the supplied archive

- `python -m py_compile src/pumpfun_compound_study.py`: **passed**.
- `ruff check`: **not run** — `ruff` is not installed in the review environment.
- Normal pytest collection: **blocked** because `zstandard` is not installed in the review environment.
- The focused strategy test file: **36 passed** after providing a minimal import-only `zstandard` stub. The tests do not read replay archives, so this verifies the current unit tests only; it does not verify replay parsing or end-to-end simulation.

The existing tests do not catch the creator-buy unit/gating problems, the channel filter suppression, or the dead-pool timing mismatch.

---

## Recommended implementation order before trusting PnL

1. Fix creator-buy unit handling and required-data behavior in the stream lane.
2. Fix the channel lane to apply the creator-buy gate only when a valid create value exists; retain the adaptation label.
3. Implement the dead-pool 600-second state machine.
4. Add an execution-constrained lane with entry impact cap, sell tiers, and explicit route failures. Keep the current CP-only version as a separate diagnostic lane so historical outputs remain reproducible.
5. Make B/C uncertainty analysis compounding-aware, or remove its current `P(PnL>0)` interpretation.
6. Enforce config/version identity in `aggregate`, decide daily-vs-continuous compounding run boundaries, and expand candidate audit records.
7. Run the strategy on at least one small replay fixture with known event chronology, plus the complete target-day test suite in the project's pinned Python environment.

**Do not tune the strategy thresholds from current results until these corrections are resolved.** First freeze a corrected execution model, rerun the same cohorts, and preserve the current output as the prior model for comparison.
