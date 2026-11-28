# Strategy report — `scanner_pumpfun_amm`

Status: **paper only, not funded.** Written 2026-11-25 against the frozen rule in
[`SCANNER_EVIDENCE.md`](SCANNER_EVIDENCE.md). Nothing in this report changes a
threshold; the sections at the end are pre-registered variants, not deployed code.

## 1. The rule

| field | value |
| --- | --- |
| channel | `@AveSolanaTokenScanner`, first card per mint |
| filter | `dex == Pumpfunamm` and `20,000 <= liquidity_usd < 100,000` |
| entry | first BUY print at or after `card + 5 s`; give up at `card + 60 s` |
| exit | hard market sell at `entry + 5 min`; no stop, no trail |
| size | 0.100 SOL of a 10 SOL wallet |
| cost model | 1.25 % fee per side; exit preflight requires a quoted exit value ≥ 0.90× entry |
| concurrency | cap 10; measured peak 6–8, so it never binds on this lane |

Everything above is frozen. The forward gates (≥300 trades / ≥5 days, positive
realized PnL with bootstrap lower bound ≥ −5 % of capital, median ≥ +10 %,
catastrophic exits < 16 %, `no_route`/unsold write-offs < 16 %, fill rate
≥ 80 %, entry failures < 10 %) are the only acceptance criteria.

## 2. Where the edge is claimed to come from

The AMM pool that opens when a Pump.fun token migrates, in the 20–100k launch
liquidity band, tends to be repriced upward in the first minutes. The channel
posts the card at pool launch; the bot buys the first aggressive print and sells
the 5-minute mark. The edge is specific to `Pumpfunamm` (other DEXes are flat or
negative), and to the band: `<15k` is the rug zone, `>100k` wins but does not
move (median +0.9 %).

## 3. Marked replay evidence (updated with 2026-11-21 and 2026-11-22)

Days 18–20 are the recorded figures in the evidence doc; 2026-11-21 and
2026-11-22 were priced today with the same deployed convention (`--latency-ms 5000
--entry-fill-mode first-after --entry-side buy --max-entry-age-ms 2000
--max-entry-delay-ms 60000 --min-quote-sol 0.01`):

| day | n | win | median net |
| --- | --- | --- | --- |
| 2026-11-08 | 277 | 98.2 % | +25.2 % |
| 2026-11-18 | 502 | 96.8 % | +20.6 % |
| 2026-11-19 | 540 | 97.8 % | +20.3 % |
| 2026-11-20 | 499 | 96.4 % | +22.9 % |
| **2026-11-21 (new)** | **519** | **96.5 %** | **+23.9 %** |
| **2026-11-22 (new)** | **463** | **97.8 %** | **+22.5 %** |

The two new days reproduce the 4-day pattern (96.4–98.2 % win, +20.3 to
+25.2 % median), so the marked edge is stable across 6 days, not a property of
the original window.

### 3.1 The same cohort with unexecutable exits written off

A 5-minute "exit print" is only executable if the pool is still printing near
the horizon. In the 5-day cohort (18–22, n = 2,523) the age of the exit print:

| exit mark | n | win | mean return | median return | PF |
| --- | --- | --- | --- | --- | --- |
| marked, as priced | 2,523 | 97.07 % | +35.4 % | +22.1 % | 56.1 |
| exit print ≤ 5 s old only | 1,851 | 98.76 % | +23.9 % | +22.1 % | 94.8 |
| > 30 s old written off at −100 % | 2,523 | **79.07 %** | **−1.44 %** | +18.0 % | **0.93** |
| > 60 s old written off at −100 % | 2,523 | 80.62 % | +0.71 % | +18.3 % | 1.04 |

Stale share by day: 11.4 %, 14.8 %, 21.2 %, 24.9 %, 18.4 % (18th→22nd). At the
frozen 16 % tail bound the lane already failed on 11-20, 11-21 and 11-22.

Write-off PnL at 0.1 SOL: **+1.79 SOL over 2,523 trades (+0.7 %/trade)** — the
same "fragile in execution" result the evidence doc reports, now on five days.

## 4. Executed paper evidence (three forward samples)

Reconstructed from the Telegram exports in `messages/telegram_reports/`; entries
and exits are simulated fills from read-only Jupiter quotes, not transactions.

| sample | window (UTC) | closes | win | median | net SOL | PF | catastrophic | fees | pre-fee |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| B — pre-guard | 11-22 13:17 → 11-23 07:58 | 335 | 79.70 % | +16.7 % | −0.0422 | 0.993 | 17.6 % | 0.848 | +0.805 |
| A — guard 0.90 | 11-24 07:42 → 17:20 | 68 | 77.94 % | +23.2 % | +0.1346 | 1.095 | 20.6 % | 0.174 | +0.308 |
| E — guard unconfirmed | 11-24 19:33 → 11-25 08:43 | 112 | 78.57 % | +18.9 % | −0.3563 | 0.853 | 21.4 % | 0.279 | −0.077 |

Do not pool these: B predates the 0.90 preflight guard, A has it, E's startup
message is not in the export. "Catastrophic" = realized return ≤ −90 %
(`near_zero_exit` in A/E; B predates the label but has the same −0.10 SOL exits).

Pre-fee = net + fees. In B and A the tape edge before fees was only +2.4 % and
+4.5 % of notional per trade; the 2.5 % round-trip fee then decided the result.
In E even the pre-fee number was negative. B's median is the ledger value
(recorded in the evidence doc); A and E are reconstructed from 4-decimal PnL in
the messages, so their medians carry about ±0.2 pp.

## 5. Why a 79 % win rate does not pay

| sample | avg win | avg loss | payoff | break-even win | actual win | gap | result |
| --- | --- | --- | --- | --- | --- | --- | --- |
| A | +29.3 % | −94.5 % | 1 : 3.2 | 76.3 % | 77.9 % | **+1.6 pp** | +0.135 SOL |
| B | +22.2 % | −87.8 % | 1 : 4.0 | 79.8 % | 79.7 % | **−0.1 pp** | −0.042 SOL |
| E | +23.6 % | −101.2 % | 1 : 4.3 | 81.1 % | 78.6 % | **−2.5 pp** | −0.356 SOL |

The sign of every sample is explained by the gap between actual and break-even
win rate. The structure that creates it:

1. **The winner is capped, the loser is not.** The 5-minute market sell closes
   every winner at the horizon mark — there is no runner — while a pool that
   stops printing is realized in full at −100 %. One rug cancels 3–4 winners.
2. **A ~20 % rug rate needs ~80 % wins.** With a 1:4 payoff, 79 % is not
   "high", it is break-even minus a rounding error. The marked 97 % number is
   not wrong arithmetic; ~1 in 5 of those exits had no executable print, and
   marking them at the last trade price is what turns 79 % into 97 %.
3. **Fees are the tiebreaker.** 2.5 % round trip costs 0.0025 SOL per trade,
   0.17–0.85 SOL per sample — larger than the net result in B and E.
4. **The horizon itself is right.** Extending to 15 min raises marked PnL to
   +80 SOL and drops the write-off case to −71 SOL (evidence doc). Do not
   lengthen the exit.

So: the entry timing has a real, repeatable marked edge; the exit is where it
dies, and the lane currently sits on the break-even line, with the tail gate
failing at 17.6–21.4 % against a 16 % bound.

## 6. Gate status

| gate | B | A | E |
| --- | --- | --- | --- |
| ≥300 trades / ≥5 days | 335 / 1 day — fail | 68 — fail | 112 — fail |
| positive PnL, bootstrap LB ≥ −0.5 SOL | −0.042; CI −1.81…+1.66 — fail | +0.135; not evaluated | −0.356; not evaluated |
| median ≥ +10 % | +16.7 % — pass | +23.2 % — pass | +18.9 % — pass |
| catastrophic < 16 % | 17.6 % — **fail** | 20.6 % — **fail** | 21.4 % — **fail** |
| write-offs, fill rate, entry failures | 0, 337/337 — pass | not exported | not exported |

No sample clears the set. The one gate that fails everywhere is the tail.

## 7. Variants: tested, and one now deployed as an experiment

All three were specified before the test and run on 2026-11-21/22 (982 trades,
entries unchanged) in [`EXIT_POLICY_STUDY.md`](EXIT_POLICY_STUDY.md):

- **V1 — pre-horizon liquidity exit: rejected.** A trailing stop is negative with
  the deployed 5-minute deadline (−3.7 to −4.0 SOL vs −5.1 baseline) and
  catastrophic with a 15-minute cap (−31 SOL). An absolute −50 % stop never
  fires usefully (−5.16 SOL, same as baseline): the pools that die stop
  printing before their collapse is observable.
- **V2 — winner extension / dynamic hold: rejected.** Holding to 15 minutes
  costs −37 SOL; extending in 30 s steps while price stays above entry costs
  −36 to −37 SOL. 60.7 % of pools have no print at 15 minutes.
- **V3 — stricter entry-side exit depth (0.95×/0.98×): rejected before a lane.**
  The feasibility screen in `docs/EXECUTION_RESEARCH.md` §3 shows `0.95` sits
  below the fee floor and rejects nothing, `0.98` rejects 94.8 % of entries
  while leaving a 33 % catastrophic rate among survivors, and the ratio itself
  is not predictive (AUC 0.458). No V3 lane will be built.

The only positive policy in the study was selling everything at +50 %
(+1.76 SOL). It is three trades: the best 1 % of trades supply 144 % of its
PnL, removing the best three leaves −0.35 SOL, and it turns negative under a
stricter fill assumption. It is **not** an established edge, but at the
operator's request it is now wired as a separate pre-registered lane,
`scanner_pumpfun_amm_tp50`, with its own recovery snapshot and its own
acceptance gate ([`TP50_VARIANT.md`](TP50_VARIANT.md)). Its results must not be
pooled with the frozen sample.

A variant earns a paper run only if it is declared with its threshold and its
acceptance gate before data collection. No variant should be judged on the
samples in section 4.

## 8. Recommendation

Keep both lanes paper-only. Do not tune the frozen parameters on B/A/E — at
68–335 trades, all three samples are inside the noise band of a break-even
process (B's bootstrap P(>0) = 0.49). Trailing stops, absolute stops, partial
take-profits and dynamic holds were all tested and lost money (section 7), so
the only exit variant running is the +50 % take-profit, and only as a declared
experiment with a fixed acceptance gate.

Two levers remain, in order of expected value:

1. **Execution cost.** At 982 trades, one percentage point on each side (two
   points of round-trip cost) is 1.96 SOL, and one point of total round-trip
   cost is about 0.98 SOL — either is larger than every exit policy tested.
   Measure the real cost (priority fee, ATA rent, route fee, slippage) of the
   VPS paper fills before changing any rule, and do not lower the 1.25 %/side
   assumption to flatter a result. Quotes measure modeled cost, not fees
   actually paid. `src/execution_cost_report.py` produces the quote-derived
   report and a PnL grid over cost assumptions
   ([`EXECUTION_COST.md`](EXECUTION_COST.md)).
2. **Executable exitability, not more price stops.** The loss tail comes from
   pools that stop printing while the position is open, which a print-based
   stop cannot see. The separate `scanner_pumpfun_amm_quote50` lane asks what a
   full-size sale would return every 15 s and sells below `0.50×` entry cost
   ([`QUOTE_GUARD_VARIANT.md`](QUOTE_GUARD_VARIANT.md)). The combined
   `scanner_pumpfun_amm_tp50_quote_guard` variant keeps the +50 % exit but
   confirms the mark with a full-size quote and adds fraction-based salvage
   when the full-size route disappears. Both are paper-only with their own
   samples; neither is the deployed lane.
3. **More days on a fixed lane.** The frozen lane is at break-even with a ~20 %
   tail; the tp50 lane is a fragile positive in the study. Only a longer forward
   sample can tell whether either survives, and the gates already encode what
   "survives" means.

## Reproduction

```bash
# two new replay days (≈3.5 min each)
env PYTHONPATH=src .venv/bin/python src/alert_outcomes.py \
  --day 2026-11-21 --latency-ms 5000 --entry-fill-mode first-after \
  --entry-side buy --max-entry-age-ms 2000 --max-entry-delay-ms 60000 \
  --min-quote-sol 0.01 --horizons 5 --tag canon
env PYTHONPATH=src .venv/bin/python src/alert_outcomes.py \
  --day 2026-11-22 --latency-ms 5000 --entry-fill-mode first-after \
  --entry-side buy --max-entry-age-ms 2000 --max-entry-delay-ms 60000 \
  --min-quote-sol 0.01 --horizons 5 --tag canon

# gates on a real run directory
env PYTHONPATH=src .venv/bin/python src/forward_gates.py \
  --run-dir output/paper/live_YYYYMMDD_HHMMSS --starting-sol 10

# quote-derived execution cost + PnL under cost assumptions
env PYTHONPATH=src .venv/bin/python src/execution_cost_report.py \
  --run-dir output/paper/live_YYYYMMDD_HHMMSS
```

Paper ledger figures for B and A/E come from the Telegram exports; the
authoritative `trades.jsonl` for those runs lives on the VPS.
