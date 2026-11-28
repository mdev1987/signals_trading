# Pump.fun → Jupiter compounding study

**Strategy ID:** `pumpfun_jupiter_compound_v1`
**Tool:** `src/pumpfun_compound_study.py` (report-only)
**Spec:** `docs/new_strategy/pumpfun_jupiter_compounding_strategy_test_spec.md`
**Mode:** paper / replay only. No signing, no broadcasting, no change to the
deployed lane, the frozen AVE rule, the scanner band, or any frozen threshold.

---

## 1. Question

Does the course strategy have an after-cost edge?

* Buy a Pump.fun token once its short-term activity is strong.
* Exit the whole position at **2.00×** or **0.82×** the entry reference price.
* Poll roughly every 8 seconds; retry entry every 5 seconds until the token is
  300 seconds old.
* Compounding lanes: **A** fixed $2, **B** 60/40 split + $1 play floor +
  2-loss/5-minute pause + 30-cycle cap, **C** lane B without the play floor.

The strategy is tested against two candidate sources:

| Lane | Source | Universe |
|---|---|---|
| `stream` | raw PumpAPI replay (`create` events) | every Pump.fun token creation |
| `channel` | the live scanner's `@AveSignalMonitor` `second_vibe_buy` cards | the cards the deployed lane actually consumes |

The `stream` lane is the faithful reproduction the spec asks for: it needs the
earliest creation timestamp, the creator buy, and point-in-time five-minute
activity. The `channel` lane is a **labelled adaptation**
(`pumpfun_amm_migration_adaptation_v1`): those cards fire after the AMM pool
exists, often after the 300-second creation window, so the token-age and
creator-buy filters cannot be applied faithfully.

---

## 2. Data

* **Replay:** `/mnt/HDD/PumpAPI_Replay_Zst/<YYYY-MM-DD>/<HH>.jsonl.zst`
  (tick-level `create` / `buy` / `sell` / `migrate` / `add` / `remove`, with
  reserves, `priceSol`, `priceUsd`, `poolFeeRate`, `quoteMint`).
* **Channel cards:** `output/backtest/signals/<YYYY-MM-DD>/strategy_signals.csv`
  (Telegram-captured, `second_vibe_buy`, one card per mint).
* Chain/event timestamps are used throughout; local receive time is never used
  as a substitute for launch time.

---

## 3. Point-in-time rules

* A creation candidate is first evaluated at **launch + 25 s**. If no trade has
  printed yet it retries every **5 s**; at **> 300 s** of age it expires.
* A channel candidate is evaluated at **card timestamp + 5 s**, matching the
  deployed lane's card-anchored eligibility.
* A candidate is screened only with prints at or before that instant. The
  five-minute window is truncated to whatever exists.
* One open position at a time. When several candidates qualify at the same
  decision timestamp, the highest `score_100` wins; ties break on earlier
  launch, then mint (spec §5.4). Candidates that arrive while a position is
  open are logged and dropped, never queued.
* Exit triggers are **marks**, not realisations. Realised proceeds come from
  the executable fill, never from the threshold.
* No forced time exit. Positions still open when the data ends are reported as
  `OPEN_AT_END` and valued at zero in a separate write-off sensitivity.

---

## 4. Execution model

All fill arithmetic runs in the pool's **own quote asset**, because Pump.fun
pools are not uniformly SOL-quoted (some quote against a memecoin such as
`Pren1F…`).

* `quote_usd = priceUsd / price` from the **same event**, so the conversion
  never mixes records.
* The tradeable reserve is `quote / price` from the same record. On the
  bonding curve this equals the virtual reserves; on an AMM it excludes the fee
  vault that inflates `tokensInPool` (see below).
* A buy/sell uses the constant-product formula on that reserve and the record's
  own `quoteInPool`/`vQuoteInBondingCurve`.
* Fee: **1.25 % per side** (`poolFeeRate` in the feed). Price impact is the
  constant-product impact, so it scales with position size.
* The trigger and the fill use **one** mark: the reserve-backed price. Using
  `priceSol` for triggers and reserves for fills was rejected as internally
  inconsistent; on rare multi-pool records the two disagree by several times.
* There is **no** level check against the feed's `price`. A 2 % level gate was
  measured and dropped: it removed 84 % of August AMM cards but only 18 % of
  November cards, which would have made the days incomparable. The gap is
  structural (fee vault), not an anomaly, and the reserve correction now
  removes it. The raw gap is still recorded per candidate
  (`price_discrepancy`) as a diagnostic, never as a filter.

### One pool per token

A mint can be printed by more than one venue at once (Pump's own curve or AMM,
plus a Meteora or Raydium pool). Following whichever record printed last let
the mark jump between unrelated reserve sets: on 2026-08-09 one such switch
turned a 2× trade into a **59.9×** artefact when a `meteora-dlmm` liquidity
add was read as the token's price.

The simulator therefore pins each mint to its Pump pool lineage:

* only `pump` and `pump-amm` records are eligible;
* the one-way `pump → pump-amm` migration continues the same lineage;
* a change of `poolId` outside that transition, a return to the curve, or a
  change of quote asset is ignored;
* a token with no Pump pool is skipped (documented as a smaller universe, not
  a missing trade).

Migration continuity was measured on the 42 migrations in the 12:00 UTC hour of
2026-11-18. On the **feed price** the AMM/curve ratio has a median of **1.013**
(71 % within ±10 %, 81 % within 2×); on the **raw reserve price** it has a
median of **0.814**, a systematic 19 % step. The cause is that `tokensInPool`
counts the pool's fee vault, not only the constant-product reserve.

The fills therefore run on the tradeable reserve `quote / price` from the same
record, not on `tokensInPool`. On the bonding curve the two already agree; on
the AMM this removes the 19 % step and makes the mark equal to the feed price.
A migration is followed only if it does not move the price by more than 5×;
where it does (malformed records, a different pool) the position is not marked
or exited on that jump.

* Liquidity is the USD value of both sides of the pool (`2 × quote × quote_usd`).

### Archives without `priceUsd`

Replay archives before 2026-11-08 carry only SOL-denominated fields
(`priceSol`, `marketCapSol`, `marketCapQuote`). For those days the conversion
comes from `SOL_USD_BY_DAY` (Binance SOLUSDT hourly closes, daily mean), and
`--sol-usd` overrides it.

The choice barely matters for the PnL: the trade size is fixed in **USD**, so a
closed trade's USD PnL and return are invariant to the assumed SOL/USD rate.
It does matter for the liquidity and market-cap gates, which is why each day
uses its own rate (Aug 9 ≈ $76.6, Oct 3–5 ≈ $120–121, Nov 8 ≈ $88.9) rather
than a single constant. Non-WSOL-quoted pools cannot be converted at all on
those days and are skipped as `MISSING_POOL_PRICE`; on 2026-11-18/19/20 they
are included, so the older days are a WSOL-only subset of the same universe.

`pool_day_files` also filters `YYYYMMDD_HH` archives to the run's own date:
2026-11-08 spills 8 hours into 2026-11-09, and that day has only 20 hours of
data (04:00–23:00 UTC).

### Execution limits (`pool_cp_v1`)

The fill engine is a **pool-only constant-product model**, not a Jupiter
simulation: it never calls an aggregator and does not reproduce route
availability. The name `pool_cp_v1` is stored in every artifact so a future
execution-constrained model cannot be pooled with this one.

* **Entry:** the position-size impact of the constant-product fill is measured
  against the pool mark, fee excluded. Above **150 bps** (spec §6.3) the entry
  is rejected and logged as `ENTRY_SLIPPAGE_TOO_HIGH`. At the $2,500 liquidity
  floor a $2 stake is far below the cap, but a lane B/C stake compounded past
  roughly $70 is not, so this gate does bind in the compounding lanes.
* **Exit:** the same impact is measured on the full position. The spec's
  200/500/1,000 bps sell tiers collapse to a single check against the **1,000
  bps** maximum under a deterministic constant-product engine: if the fill
  impact exceeds 1,000 bps there is no tier that could execute it. Such an
  exit is recorded as an unresolved position (`failed_exit_attempts`) and
  retried on the next monitoring interval, never booked as a fill.
* **Dead pool:** a pool below **$25** liquidity enters `DEAD_POOL_MONITORING`.
  The first observation only starts the clock; the first sell attempt is 600 s
  later, and a failed attempt schedules another 600 s. A positive
  constant-product output is not treated as proof of an executable route.
* **Route failure:** modelled only as the impact gates above. A real
  aggregator outage or a missing route has no replay analogue and is not
  represented; this remains an optimistic assumption on *availability*, though
  no longer on *impact*.

### Assumptions on record

1. Fill fee 1.25 %/side, no separate network cost or priority fee.
2. Liquidity = both sides of the pool.
3. Non-WSOL quote assets are supported through their own `quote_usd`. The
   creator's initial buy is converted to SOL as
   `quoteAmount × quote_usd / SOL_USD` from the same create record; a missing
   input stays missing rather than becoming zero.
4. No 2 % feed/reserve level gate. That gap is structural (the AMM feed price
   is FDV-based and `tokensInPool` includes the fee vault), so the discrepancy
   is a per-candidate diagnostic only, never a filter.
5. One artifact is **one independent daily compounding run**: the ledger starts
   at the configured stake each day, so the 30-cycle cap, saved balance, pause
   state and open position do not cross day boundaries. `aggregate` reports
   this `run_boundary` explicitly; it does not model a continuous multi-day
   account.
6. One position at a time, as specified.

---

## 5. Gates

Applied exactly as specified: token age ≤ 300 s; liquidity $5,000–$2,000,000;
five-minute volume ≥ $2,000; ≥ 12 transactions; ≥ 8 buys; buy/sell ratio ≥ 1.5;
market cap ≤ $10 M; wash volume / liquidity < 50; creator buy ≤ 20 SOL;
honeypot activity; name keyword screen. Score is diagnostic only (**no**
minimum-score gate).

In the `stream` lane a missing creator buy is `MISSING_REQUIRED_DATA`, as the
spec requires, not a pass. In the `channel` lane the age filter is not applied
(the card fires after the 300 s window) and the creator-buy cap is applied
whenever the series carries a create value; only a genuinely absent value is
exempt, and that exemption is reported as part of the adaptation.

One variant of this table was registered and run: a lower ratio floor (1.0)
plus a rejection of the 1.5–2.0 band. It is recorded in §6.4 and was rejected;
the code carries only the gate set above.

---

## 6. Results

Run set: 2026-08-09, 10-03, 10-04, 10-05, 11-08, 11-18, 11-19, 11-20
(plus 11-21 for the stream lane), `pool_cp_v1`, config hash
`47a97d23c9ec0b6f`, one independent run per day.
All figures are after the 1.25 %/side fee and the constant-product impact.
Exits are realised fills; TP/SL trigger marks are not counted as fills.

### 6.1 Channel lane (the deployed scanner's cards)

| Lane | Closed | Open | Win % (95 % CI) | Break-even | PnL | 95 % CI | P(PnL>0) | Write-off | Return on notional |
|---|---|---|---|---|---|---|---|---|---|
| a (fixed $2) | 337 | 2 | 18.99 (15.16–23.52) | 20.54 | −$14.36 | −31.38 … +3.94 | 0.064 | −$18.36 | −2.13 % |
| b (60/40, floor, cap) | 162 | 1 | 16.05 (11.19–22.48) | 20.37 | −$16.83 | −32.26 … −1.07 | 0.018 | −$19.18 | −6.20 % |
| c (60/40, no floor) | 162 | 1 | 16.05 (11.19–22.48) | 20.34 | −$10.86 | −13.32 … −7.55 | 0.000 | −$10.86 | −16.57 % |

Lane a, per day:

| Day | Closed | Open | Win % | PnL | Exits |
|---|---|---|---|---|---|
| 2026-08-09 | 15 | 0 | 13.33 | −5.09 | 13 SL / 2 TP |
| 2026-10-03 | 11 | 0 | 0.00 | −5.51 | 11 SL / 0 TP |
| 2026-10-04 | 11 | 0 | 27.27 | +2.43 | 8 SL / 3 TP |
| 2026-10-05 | 9 | 0 | 11.11 | −1.79 | 8 SL / 1 TP |
| 2026-11-08 | 76 | 0 | 19.74 | −1.06 | 61 SL / 15 TP |
| 2026-11-18 | 112 | 0 | 18.75 | −5.62 | 91 SL / 21 TP |
| 2026-11-19 | 74 | 1 | 20.27 | −1.15 | 59 SL / 15 TP |
| 2026-11-20 | 29 | 1 | 24.14 | +3.43 | 22 SL / 7 TP |

Lanes B/C reach the 30-cycle cap on three of the eight days (11-08, 11-18,
11-19) and stop earlier on the rest when the day's candidates run out. Lane B
also takes $2.0–$10.8 per day in external top-ups to hold the $1 play floor,
so its dollar PnL is not comparable with lane A. Per dollar of traded notional
the picture is worse, not better: −6.2 % (B) and −16.6 % (C) against −2.1 %
(A). In lane C the stake decays to $0.001–$0.006 by the end of 11-19: below
break-even, the 60/40 rule is a stake-destruction mechanism, not a bankroll.

### 6.2 Stream lane (token creations, the faithful universe)

| Day | Closed | Open | Win % | PnL | Exits |
|---|---|---|---|---|---|
| 2026-08-09 | 9 | 1 | 22.22 | −2.62 | 7 SL / 2 TP |
| 2026-10-03 | 20 | 1 | 10.00 | −9.12 | 18 SL / 2 TP |
| 2026-10-04 | 1 | 1 | 0.00 | −0.49 | 1 SL / 0 TP |
| 2026-10-05 | 6 | 1 | 0.00 | −3.72 | 6 SL / 0 TP |
| 2026-11-08 | 1 | 1 | 100.00 | +2.40 | 0 SL / 1 TP |
| 2026-11-18 | 12 | 1 | 33.33 | +1.59 | 8 SL / 4 TP |
| 2026-11-19 | 6 | 1 | 0.00 | −5.14 | 6 SL / 0 TP |
| 2026-11-20 | 4 | 1 | 25.00 | +0.26 | 3 SL / 1 TP |
| 2026-11-21 | 0 | 1 | — | 0.00 | 0 SL / 0 TP |
| **pooled** | **59** | **9** | **16.95 (9.48–28.46)** | **−$16.83** | 49 SL / 10 TP |

Break-even 26.91 %; 95 % CI on the pooled PnL −$38.05 … +$2.20;
P(PnL>0) = 0.044; write-off −$34.83; return on traded notional −14.26 %.

Stream lane B/C exists only for 2026-11-20: 7 entries each, 6 closed, one open.
Lane B returned +$0.17 (6 trades, 16.67 % win, write-off −$2.36, +1.67 % on
notional); lane C returned −$1.00 (6 trades, 16.67 % win, write-off −$1.60,
−15.61 % on notional). The sample is far too small to compare them.

### 6.3 What the run measured

* **The faithful lane still cannot resolve the question.** With one position at
  a time and no forced exit, 9 days produce 59 closed trades and one
  `OPEN_AT_END` every day. Some of those simply stopped printing; others kept
  printing but never left the 0.82×–2.0× band (2026-10-03: 91 marks, 0.94–1.61×).
  Either way the lane is blocked for the rest of the day, and the 59-trade
  interval still spans zero. The point estimate is negative (−14.3 % of
  notional), so this is not evidence of an edge either.
* **The channel lane is the only high-volume source and it is negative.** 337
  lane-A trades over 8 days, win rate 18.99 % against a 20.54 % break-even.
  Both profitable days together (+$5.86) are outweighed by the other six
  (−$20.22), so the deficit is not one bad day.
* **Compounding does not rescue it.** B and C reduce the loss in dollars only
  by shrinking the stake: −6.2 % and −16.6 % of traded notional against
  −2.1 % for lane A. In lane C the stake decays to $0.001–$0.006 by 11-19.
* **The new execution gates are inert here.** Max entry impact 6.1 bps, max
  exit impact 8.0 bps, zero dead-pool events, zero failed exits. They exist
  for the compounding lanes and for larger stakes, not for this run.
* **The creator-buy gate changed the universe as intended.** It rejected 18
  channel cards (`CREATOR_BUY_TOO_LARGE`) that the old path admitted, and it
  removes candidates with a missing create value from the stream lane.
* **The ratio floor does all the filtering.** 1,392 of 2,099 candidate
  evaluations carry `BUY_SELL_RATIO_BELOW_MIN`; the volume, transaction and
  buy-count floors remove only 15–57 each. This is why the §6.4 re-cut moved
  the result so much: it changed the one gate that binds.

---

### 6.4 Rejected gate variant (1.5–2.0 ratio band)

A prior sample put 75.2 % of traded mints in the 1.5–2.0 buy/sell-ratio band
at −3.94 % average return. That motivated a registered variant: lower the
floor to 1.0 and reject only the 1.5–2.0 band. It ran on the same days, lanes
and thresholds as §6.1, so the only difference is the ratio rule.

| Lane | Trades | Win % | Break-even | PnL | 95 % CI | P(PnL>0) | Write-off |
|---|---|---|---|---|---|---|---|
| a (fixed $2) | 473 | 17.97 | 21.00 | −$39.45 | −71.16 … −13.84 | 0.000 | −$47.45 |
| b (60/40, floor, cap) | 228 | 15.35 | 20.58 | −$26.60 | −39.02 … −13.64 | 0.000 | −$30.14 |
| c (60/40, no floor) | 228 | 15.35 | 20.55 | −$4.60 | −12.28 … +4.98 | 0.160 | −$4.61 |

The variant is worse in every lane, and it is not a small difference in lane a
(473 trades at −4.17 % of notional against the pre-registered run in §6.1).
Admitting the 1.0–1.5 band added 136 trades, and they were worse than the
band the variant rejected. The variant was therefore reverted: the code keeps
the single pre-registered floor at 1.5, the 1.5–2.0 rejection is gone, and the
variant's artifacts are not pooled with anything.

That is the useful part of the result: the "loser band" was a property of that
earlier sample, not of the strategy. Re-cutting the same days with a different
ratio rule moved the loss around but never produced an edge.

### 6.5 Live cross-check: the deployed TP50 ledger

The forward lane `scanner_pumpfun_amm_tp50` (lane A, 0.1 SOL, channel
`@AveSolanaTokenScanner`) has its own fill ledger. Two views were used: the
run directory `output/paper/live_20261125_121607` and the Telegram export
`ChatExport_2026-11-27`, which covers 2026-11-26 07:54 – 11-27 18:30 UTC after
a `clear_history`.

The export is the lane's own ledger (316 BUY and 317 SELL fills, one lane, one
rule), not a signal feed. It cannot validate this study's sample: none of its
319 mints appear among the 1,719 cards in `strategy_signals.csv`, and no
replay archive exists for 11-26/27. It does supply the one input the replay
lacks: the realised rate of unrecoverable exits.

Run-directory snapshot at the crash (2026-11-27 10:09, 402 closed): 77.6 % win,
PnL −1.2360 SOL, fees 1.0021 SOL, equity 8.764, max drawdown −2.471, profit
factor 0.86, 21.6 % catastrophic exits, 5.5 % take-profit hits. The process was
killed by `memory_limit:550MB` after 29 h 47 m (peak RSS 550 MB), then resumed.

Full ledger at 15:10 UTC (447 closed):

| Measure | Value |
|---|---|
| Gross return per trade (before fees) | **+0.41 %** (95 % CI −4.55 … +5.25) |
| Round-trip fee | **2.51 %** |
| Net return per trade | **−2.10 %** (95 % CI −7.08 … +2.62) |
| Net PnL | −0.98 SOL on 44.7 SOL notional |
| P(net total > 0) | 0.20 |
| Catastrophic exits (≤ −0.05 SOL) | 94 (21.1 %), −9.51 SOL |
| Near-zero exits | 84, −8.50 SOL |
| Horizon-complete exits | 328, +7.66 SOL |
| Take-profit exits | 28, +0.27 SOL (21 positive, 7 negative) |

The mechanism is not a slow decline or a late stop. All 84 near-zero exits
were taken at the five-minute horizon with an exit price impact of −100 % and
an exit value of 0–1 % of entry; the median last observed mark was 2.9 % of
entry. In 21 of the 84 the mark was still above 0.9× entry when the exit began:
the pool stopped quoting, not printing. Seven of the 28 take-profit triggers
realised a loss, five of them about −0.10 SOL, so even a TP mark is not a fill.

No card feature separated that tail. Against a 22.0 % base rate in the export,
the catastrophic rate was 17.9 / 0 / 24.9 / 0 / 35.0 % across $20–30k / 30–40k
/ 40–50k / 50–70k / 70–100k launch liquidity, 11.8–27.8 % across 1 m flow
buckets, 11.1–33.3 % across 30 m volume, 14.0–33.3 % across entry impact, and
flat across entry delay and open positions. The one sizeable split — excluding
the $40–50k liquidity cohort, 71 % of all entries — gives +0.566 SOL over the
127 kept trades, bootstrap CI −0.49 … +1.56 (P(>0) = 0.87), permutation
p = 0.055 over fifteen screens tried. That is in-sample and not actionable.

Two gates are inert on this universe: `mcap` is 0 on 350/447 fills (79 %), and
`hist_sells_5m` is 0 on 400/447 (90 %), so the buy/sell ratio is infinite and a
≥1.5 floor keeps 442/447. The research gate and the live activity series are
not the same measurement.

A walk-forward check settles the filter question. Training on 11-26 and
testing on 11-27, the best of nine card features was prior 30 m volume
> 4.2 SOL: it kept 29 of the 133 next-day trades for +0.21 SOL, against
+1.24 SOL for taking all of them. The same happened with every screen — the
average trade improved, the total fell, because the screen removed winners
along with the wipes. A filter that cannot identify the wipe in advance only
shrinks the sample.

Carried back to this study: the replay's share of exits at or below −50 % is
about 3 %; the live lane's share at about −100 % is 21 %. "Zero dead-pool
events" is a property of the replay data, not of the strategy. Substituting a
21 % cohort of −100 % exits for the same slice of the −14.5 % average stop adds
about 18 percentage points of loss per trade, which would take the channel
lane from −4.2 % to roughly −22 % of notional. That is a stress test, not an
estimate, but it fixes the sign of the correction: the live tail makes the
channel lane worse, and the tail — not the TP level — is what a filter would
have to predict.

The lane was stopped at a clean shutdown on 2026-11-28 01:12 UTC with **525
closed trades** (integrity VALID): −1.334 SOL, −2.51 %/trade, median +20.87 %,
21.5 % catastrophic exits, exits 383 horizon / 102 near-zero / 34 tp_hit /
4 no_route / 2 forced shutdown. The 447-trade cut above is the snapshot the study was written
against; the final cut does not change its verdict. The archived copy is
`output/paper/tp50_baseline_20261128_0112/`.

---

## 7. Verdict

**No after-cost edge was found, and the faithful lane is underpowered.**

* **Channel adaptation:** rejected. Every lane loses, every lane's win rate
  sits below its break-even, and the probability that the pooled result is
  positive is at most 6.4 % (lane a), 1.8 % (B) and <0.1 % (C). This is the
  only source that can be wired live, so there is nothing here to deploy.
* **Stream lane:** inconclusive by sample size, not by outcome. 59 closed
  trades over 9 days moved the point estimate to −14.3 % of notional
  (−$16.83, P 0.044) but the 95 % interval still spans zero, and the write-off
  case is −$34.83. The run does show a structural problem: one stalled token
  per day consumes the whole lane, so a one-week replay of this design can
  never produce enough trades to test it.
* **No tuning.** The failure is not a threshold miss to be fixed by moving
  the TP or the gates; the win rate is below the break-even implied by the
  strategy's own payoff in every lane. The one re-cut that was tried (§6.4)
  was worse, not better. Nothing here justifies changing the 2.0×/0.82× exits,
  the gates, or the deployed lane.
* **The live lane says the same thing from the other side.** 447 forward TP50
  exits returned +0.41 %/trade before fees and paid 2.51 %/trade in fees
  (net −2.10 %, §6.5). 21.1 % of those exits were full wipes and no card
  feature predicted them. An entry gate that cannot see the tail is not a fix,
  and the fee is larger than the gross point estimate.
* **Decision:** do not wire this strategy to the live channel and do not
  retune the entry gates. The pre-registered experiment aimed at the actual
  loss mechanism is the executable-exit guard: V1
  `scanner_pumpfun_amm_quote50` (never run) and its combined successor
  `scanner_pumpfun_amm_tp50_quote_guard`, deployed as the sole paper lane on
  2026-11-28. That — not another entry filter — is the next experiment, and it
  stays a separate paper lane with its own recovery snapshot.
* **TP50 keeps its completed sample** (525 closed trades at a clean shutdown,
  archived with its recovery DB) and its snapshot. The deployed paper lane is
  now the combined V2 guard; the two samples are never pooled.

---

## 8. Reproduce

```bash
# stream lane (token-creation universe), one day, one lane
env PYTHONPATH=src .venv/bin/python src/pumpfun_compound_study.py stream \
  --day 2026-11-20 --lane a --output output/compound_study/2026-11-20/stream_lanea.json

# channel lane (scanner cards)
env PYTHONPATH=src .venv/bin/python src/pumpfun_compound_study.py channel \
  --day 2026-11-20 --lane a --output output/compound_study/2026-11-20/channel_lanea.json

# pool several artifacts of the same lane, keeping the JSON for the record
env PYTHONPATH=src .venv/bin/python src/pumpfun_compound_study.py aggregate \
  --inputs output/compound_study/2026-11-1*/channel_lane?.json \
  --json > output/compound_study/aggregate_channel.json

# the stream lane is one lane per artifact; a full day takes ~10 minutes
env PYTHONPATH=src .venv/bin/python src/pumpfun_compound_study.py aggregate \
  --inputs output/compound_study/*/stream_lanea.json \
  --json > output/compound_study/aggregate_stream.json
```

`aggregate` refuses to mix artifacts with different `config_hash` values, so a
retuned run cannot be silently pooled with the pre-registered one.

`output/` is git-ignored; the artifacts are regenerable from the commands
above.

The pre-review run (execution model v1.0, no creator-buy conversion, immediate
dead-pool exit, iid bootstrap) is preserved under
`output/compound_study_prior_model_v1_0/`. It must not be pooled with the
current artifacts: `aggregate` refuses inputs whose `config_hash`,
`execution_model` or `run_boundary` differ.

---

## 9. Limitations

* The replay has no Jupiter routing. A fill is the best constant-product fill
  in the token's own pool; a real aggregator route could be better or worse,
  and route *availability* is not modelled at all.
* Serial dependence: the lane holds one position at a time, so trades are not
  independent. The uncertainty interval is a **block bootstrap over whole
  day-runs**: the resampling unit is the day, whose realised total already
  contains that day's size path, 60/40 split, play floor, pause and cycle cap.
  Resampling days rather than trades keeps the within-day dependence intact
  and matches the experiment as defined (N independent daily runs). A
  continuous-account bootstrap would need the ledger replayed across days;
  that is a different experiment and `bootstrap_day_runs` refuses it.
* `aggregate` refuses to pool artifacts with different `config_hash`,
  `execution_model` or `run_boundary`.

---

## 10. Review reconciliation

`docs/new_strategy/PUMPFUN_COMPOUND_CODE_REVIEW.md` raised eleven findings
against the first harness. Every one was checked against the spec and the
code; the resolutions below are what the current artifacts implement.

| Finding | Resolution |
|---|---|
| P1 creator-buy unit (`quoteAmount` called SOL) | Converted with the create record's own `quote_usd` and the day's SOL/USD; missing inputs stay missing. Paired WSOL/non-WSOL test. |
| P1 missing creator buy passed the stream lane | `MISSING_REQUIRED_DATA` unless the caller explicitly opts out; the stream lane never opts out. |
| P1 channel suppressed the creator-buy cap unconditionally | Cap applied whenever a create value exists; suppressed only when the value is genuinely absent, and that is the labelled adaptation. |
| P1 dead-pool exit was immediate | `DEAD_POOL_MONITORING`: first observation starts the clock, first attempt at +600 s, a failed attempt schedules another 600 s. `dead_pool_step` is the shared state machine. |
| P1 no slippage limits / sell tiers / route failure | `pool_cp_v1`: entry rejected above 150 bps impact; exits rejected above the 1,000 bps top tier and retried as an unresolved position. The 200/500/1,000 tier sequence collapses to the top tier under a deterministic engine. Route *availability* is still not modelled and is stated as a limitation. |
| P1 iid bootstrap on dollar PnLs | Block bootstrap over whole day-runs. Each day's realised total already embeds its size path, 60/40 split, floor, pause and cap, so trades are never resampled independently; a continuous-account replay is explicitly refused. |
| P2 multi-day aggregation implied one continuous account | Artifacts carry `run_boundary: one_compounding_run_per_day`; the document states it. A continuous multi-day account is not modelled. |
| P2 aggregation ignored config identity | `aggregate` fails on differing `config_hash`, `execution_model`, `run_boundary` or strategy id. |
| P2 document claimed a 2 % multi-pool exclusion | Claim removed. `price_discrepancy` is diagnostic only, matching the spec. |
| P2 candidate audit fields incomplete | Candidates now export creator buy, quote mint/rate, transaction counts, mark price, `price_sol`, discrepancy and `score_complete`; trades carry entry/exit impact and failed-exit count. |
| P2 stream tracks retained for the whole day | Rejected, expired, ranked-out and busy-skipped tracks are removed from the dictionary once their candidate record is written. |

The review's order of work was followed: fixes first, then the same cohorts
rerun, with the prior model kept for comparison. No threshold was tuned from
either set of results.
* `OPEN_AT_END` positions are excluded from the headline PnL and shown
  separately as a write-off. In the stream lane this is not a rounding
  detail: one position per day never returns a mark, so the write-off case
  (−$34.83 vs −$16.83) is the realistic one.
* One position at a time plus no time exit means a single stalled token can
  block a whole day. That is faithful to the spec, but it caps the sample at
  a few trades per day in the stream lane and makes an eight-day test
  underpowered.
* The `channel` lane's universe is the scanner's card stream, not token
  creations, so it cannot be presented as the course strategy.
