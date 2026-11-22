# Scanner evidence: `scanner_pumpfun_amm`

This is the reproducible record behind the deployed scanner lane. It replaces
the earlier `FINDINGS.md` / `FINAL_REPORT.md` pair, which lived under the
gitignored `output/` tree and could not be re-derived from a clean checkout.

The headline is deliberately split in two:

- **Marked, first print after the card:** 95.9 % win, +22.1 % median over 1,849
  trades (+45.1 SOL at 0.1 SOL).
- **The same cohort with unexecutable exits written off:** +3.0 SOL. The edge
  is real in the tape and fragile in the execution.

## Deployed rule

| field | value |
| --- | --- |
| channel | `@AveSolanaTokenScanner`, first card per mint |
| filter | `dex == Pumpfunamm` and `20,000 <= liquidity_usd < 100,000` |
| entry | first BUY print at or after `card + 5 s`; give up at 60 s |
| exit | hard market sell at `entry + 5 min`; no stop, no trail |
| size | 0.1 SOL, at most 10 concurrent |

The other channels (`@AveSignalMonitor`, `@DRBTSolanaPF`, `@SOLTRENDING`) and
every other DEX are retired as trading lanes. They are kept only as research
controls.

## Data and method

The only days with scanner-channel history in the replay archive are
**2026-11-08, 2026-11-18, 2026-11-19 and 2026-11-20**. Raw archives live in
`/mnt/HDD/PumpAPI_Replay_Zst/<day>/*.jsonl.zst` and are streamed, never loaded
whole.

Fills are **real trade prints**, not 1-minute closes: entry and exit are
priced at an observed trade, fees are 1.25 % per side, and the PnL column caps
each trade at +900 % so one 1,000× print cannot carry the table. Nothing is
backfilled beyond the horizon and no position is force-closed early.

Two entry conventions are supported by `src/alert_outcomes.py`:

- `last-before` (default, conservative): the most recent print at or before
  the decision. On a fast mover this print can be seconds old, so
  `--max-entry-age-ms` (default 2,000) drops stale entries.
- `first-after`: the first print at or after the decision. This is what the
  deployed trade-mode bot actually does, and it is the convention below.

`--min-quote-sol` defaults to 0.01 so a dust print cannot become a win. The
same ceiling applies to the entry delay (`--max-entry-delay-ms`, default
60,000) because the live bot gives up at 60 s.

Reproduce a day (≈4–5 min each, reads the replay archive):

```bash
env PYTHONPATH=src .venv/bin/python src/alert_outcomes.py \
  --day 2026-11-20 --latency-ms 5000 --entry-fill-mode first-after \
  --max-entry-age-ms 2000 --max-entry-delay-ms 60000 \
  --min-quote-sol 0.01 --horizons 5 15 --tag fa5

env PYTHONPATH=src .venv/bin/python src/compare_channels.py \
  --tag fa5 --latency-ms 5000 --fill-mode first-after --horizon 5
```

Output lands in `output/backtest/signals_channels/<day>/alerts_priced_fa5.csv`
and `output/backtest/channel_comparison_fa5/report.md` (both gitignored; the
commands above are the record). The aggregate below is committed as
[`docs/scanner_evidence_summary.csv`](scanner_evidence_summary.csv) so the
headline numbers can be checked without the replay archive.

## Result: first print after the card, 5-minute exit

| day | n | win | median | mean | capped PnL @ 0.1 SOL | entry lag p50 / p90 |
| --- | --- | --- | --- | --- | --- | --- |
| 2026-11-08 | 277 | 98.2 % | +25.5 % | +25.5 % | +7.06 SOL | 0.7 s / 2.9 s |
| 2026-11-18 | 508 | 96.5 % | +20.8 % | +41.0 % | +11.54 SOL | 1.0 s / 4.9 s |
| 2026-11-19 | 551 | 96.0 % | +20.2 % | +36.0 % | +12.44 SOL | 1.1 s / 4.4 s |
| 2026-11-20 | 513 | 94.2 % | +22.7 % | +29.9 % | +14.10 SOL | 0.9 s / 2.6 s |
| **all** | **1,849** | **95.9 %** | **+22.1 %** | **+34.1 %** | **+45.14 SOL** | 0.9 s / 3.7 s |

Median 0.9 s from card to fill, 90th percentile 3.7 s. The 5 s latency and the
60 s entry window cover the observed distribution with room to spare.

### Why this replaces the old 93.4 % number

The original evidence was priced at `alert + 20 s` using `last-before`. That
is not a fill the bot takes, and 30.5 % of those entries used a print more
than 2 s old (median age 1.26 s, p90 5.9 s) — the same stale-mark hazard that
erased the AVE edge when it was re-priced with event fills. Re-pricing at the
deployed 5 s with `first-after` gives 95.9 % / +22.1 %, which is *not* a
downgrade; it is the first version of the number that matches the bot.

| convention | n | win | median | capped PnL |
| --- | --- | --- | --- | --- |
| `last-before`, 20 s, no age filter (old) | 1,860 | 93.4 % | +19.6 % | +41.68 SOL |
| `last-before`, 20 s, age ≤ 2 s | 1,293 | 97.3 % | +21.0 % | +32.57 SOL |
| `first-after`, 5 s (deployed) | 1,849 | 95.9 % | +22.1 % | +45.14 SOL |

The first row is the retired number: reproduce it with `--latency-ms 20000
--entry-fill-mode last-before --max-entry-age-ms 0` (the filter default now
drops it). The second row uses the default 2,000 ms age filter.

Dropping the stale third of the old cohort raises the win rate to 97.3 % and
loses 567 trades (and 9 SOL of capped PnL) — the old number was not inflated
by stale marks so much as diluted by them. The deployed `first-after` keeps
every signal and prices it at the print the bot would actually buy, which is
both the honest and the larger sample.

The `last-before` rows are kept as a control, not as the deployment.

## The exit is the weak side

The 5-minute exit is the right horizon, because after it the tape thins out:

| exit mark | prints older than 60 s | median age | capped PnL |
| --- | --- | --- | --- |
| entry + 5 min | 17.3 % | 1.6 s | +45.14 SOL |
| entry + 15 min | 60.0 % | 466 s | +80.75 SOL marked |

The 15-minute number is not executable. If every exit print older than 60 s is
written off at −100 % (you cannot sell into a pool that stopped printing):

| exit | stale exits | marked PnL | stale-at-−100 % |
| --- | --- | --- | --- |
| 5 min | 320 | +45.14 SOL | **+3.03 SOL** |
| 15 min | 1,110 | +80.75 SOL | **−73.75 SOL** |

That is why the bot holds 5 minutes and why the funding gate below measures
*realized* exit quality, not the marked one.

## Fee / slippage stress

The tape return already includes 1.25 % per side. Additional per-side haircut
on the exit, applied to the deployed cohort:

| extra exit haircut | median | win | capped PnL |
| --- | --- | --- | --- |
| 0 % | +22.1 % | 95.9 % | +45.14 SOL |
| 10 % | +9.9 % | 76.8 % | +22.46 SOL |
| 15 % | +3.8 % | 63.8 % | +11.13 SOL |
| 20 % | −2.3 % | 40.9 % | −0.21 SOL |
| 30 % | −14.5 % | 8.1 % | −22.88 SOL |

The strategy tolerates roughly 15 % of exit slippage before it stops being
worth funding. That matches the 10 % hard ceiling in
`--max-slippage-bps`: at the ceiling it is marginal, past it the lane should
be turned off rather than widened.

## What was rejected

Measured on the same four days and the same fill convention, over the whole
scanner feed (20 s, `last-before`, as a channel-level control):

| slice | n | win | median |
| --- | --- | --- | --- |
| Pumpfunamm | 4,470 | 82.2 % | +16.9 % |
| Meteoradammv2 | 3,281 | 39.0 % | −2.5 % |
| Pump (bonding curve) | 1,360 | 10.5 % | −3.1 % |
| Meteoradbcpswap | 110 | 33.6 % | −2.5 % |
| Orca | 146 | 7.5 % | −2.5 % |

The edge is specific to the AMM pool that opens on migration, not to the
channel and not to the bonding curve. Liquidity is a band, not a floor:

| launch liquidity | n | win | median |
| --- | --- | --- | --- |
| <15k | 1,870 | 67.3 % | +7.4 % |
| 15–20k | 435 | 89.4 % | +26.6 % |
| 20–50k | 1,201 | 94.3 % | +18.9 % |
| 50–100k | 659 | 91.8 % | +20.2 % |
| >100k | 305 | 94.8 % | +0.9 % |

`>100k` wins often but does not move (median +0.9 %), and `<20k` is where the
rugs live. The 20–100k band is the only one that pays after fees.

Also rejected:

- **AVE hot-money rule** — collapsed from 52.5 % win / +48.2 % to
  36.4 % / +2.0 % once re-priced with event fills (`2ba9c04`); holdout
  logistic AUC 0.504.
- **DRBT** — −513 SOL over 54,507 alerts, 11.7 % win.
- **`@AveSignalMonitor`** — −2.0 SOL, 34.0 % win.
- **`@SOLTRENDING`** — cannot be backtested (history is deleted after ~1 day);
  live capture only. Not deployed.

## Forward paper gates

Do not fund this lane until a forward paper run shows **all** of:

1. **≥300 trades** (about 5–6 days at the observed ~60/day in band).
2. **Win rate ≥80 %** (the backtest is 95.9 %; below 80 % the edge is gone).
3. **Median net return ≥+10 %** (backtest +22.1 %).
4. **Realized exit no more than 15 % worse than the marked exit** — read
   `exit_writeoffs.jsonl` and the `exit_dust` / `exit_quote_failed` skip
   reasons, not the marked PnL.
5. **No systematic entry failure.** `entry_delay_exceeded` /
   `entry_quote_failed` above ~10 % of in-band alerts means the bot is not
   taking the trades the backtest measured, regardless of the realized win
   rate.

If 2 or 3 fails while 5 passes, the edge did not survive the move from replay
to live and the lane should be retired, not retuned. Do not re-fit the rule on
the forward sample: that turns the gate into a second in-sample fit.

## Known limitations

- Four replay days is a small sample and they are consecutive-ish; the day
  spread (94.2–98.2 % win) is consistent but not independent.
- The scanner channel re-posts each pool ~3×. Only the first card is traded
  (`scanner_cards` / `scanner_duplicates` in `summary.json`), which is what
  the backtest assumes.
- Replay archives stop at 2026-11-20, so there is no forward out-of-sample
  day in the archive. The forward paper run is the only OOS test.
- Live mode has not been exercised against a funded wallet; the Jupiter
  read-only quote path has been smoke-tested only.
