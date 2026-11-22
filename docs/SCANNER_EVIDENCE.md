# Scanner evidence: `scanner_pumpfun_amm`

This is the reproducible record behind the deployed scanner lane. It replaces
the earlier `FINDINGS.md` / `FINAL_REPORT.md` pair, which lived under the
gitignored `output/` tree and could not be re-derived from a clean checkout.

The headline is deliberately split in two:

- **Marked, first BUY print after the card:** 97.2 % win, +22.0 % median over
  1,818 trades (+44.6 SOL at 0.1 SOL).
- **The same cohort with unexecutable exits written off:** +5.6 SOL. The edge
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
  `--entry-side buy` (the default) restricts the fill to a BUY print, which is
  what the engine waits for; the exit still prices any print.

`--min-quote-sol` defaults to 0.01 so a dust print cannot become a win. The
same ceiling applies to the entry delay (`--max-entry-delay-ms`, default
60,000) because the live bot gives up at 60 s.

Reproduce a day (≈4–5 min each, reads the replay archive):

```bash
env PYTHONPATH=src .venv/bin/python src/alert_outcomes.py \
  --day 2026-11-20 --latency-ms 5000 --entry-fill-mode first-after \
  --entry-side buy --max-entry-age-ms 2000 --max-entry-delay-ms 60000 \
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
| 2026-11-08 | 277 | 98.2 % | +25.2 % | +25.3 % | +7.01 SOL | 0.9 s / 3.0 s |
| 2026-11-18 | 502 | 96.8 % | +20.6 % | +41.2 % | +11.39 SOL | 1.7 s / 5.5 s |
| 2026-11-19 | 540 | 97.8 % | +20.3 % | +36.4 % | +12.28 SOL | 1.8 s / 5.3 s |
| 2026-11-20 | 499 | 96.4 % | +22.9 % | +30.5 % | +13.95 SOL | 1.4 s / 3.5 s |
| **all** | **1,818** | **97.2 %** | **+22.0 %** | **+34.4 %** | **+44.64 SOL** | 1.5 s / 4.5 s |

Median 1.5 s from card to fill, 90th percentile 4.5 s (p99 25 s). The 5 s
latency and the 60 s entry window cover the observed distribution with room
to spare. Every fill is a BUY print, matching what the engine waits for.

Telegram delivery is the one input the bot cannot control. The engine enters
relative to the message's own timestamp and gives up at 60 s, recording
`delivery_lag_ms` on every signal. A late delivery therefore shows up as a
longer entry lag, not as a backdated fill. An earlier falsification run priced
entries at 5 / 20 / 60 s and stayed positive at all three (+18.9 / +20.3 /
+16.1 % median), so a slow delivery does not by itself destroy the edge — but
a forward run where `delivery_lag_ms` is routinely tens of seconds is trading
a different fill than the table above.

### Why this replaces the old 93.4 % number

The original evidence was priced at `alert + 20 s` using `last-before`. That
is not a fill the bot takes, and 30.5 % of those entries used a print more
than 2 s old (median age 1.26 s, p90 5.9 s) — the same stale-mark hazard that
erased the AVE edge when it was re-priced with event fills. Re-pricing at the
deployed 5 s with `first-after` and `--entry-side buy` gives 97.2 % / +22.0 %,
which is *not* a downgrade; it is the first version of the number that matches
the bot.

| convention | n | win | median | capped PnL |
| --- | --- | --- | --- | --- |
| `last-before`, 20 s, no age filter (old) | 1,860 | 93.4 % | +19.6 % | +41.68 SOL |
| `last-before`, 20 s, age ≤ 2 s, BUY | 1,124 | 97.2 % | +20.8 % | +28.56 SOL |
| `first-after`, 5 s, BUY (deployed) | 1,818 | 97.2 % | +22.0 % | +44.64 SOL |

The first row is the retired number: reproduce it with `--latency-ms 20000
--entry-fill-mode last-before --entry-side any --max-entry-age-ms 0` (the
defaults now drop stale marks and require a BUY). The second row is the same
convention with the deployed 2,000 ms age filter and BUY-only entry.

Dropping the stale fills raises the win rate to 97.2 % but loses 736 of the
1,860 trades and 13 SOL of capped PnL — the old number was not inflated by
stale marks so much as diluted by them. The deployed `first-after` keeps 1,818
signals and prices each at the print the bot would actually buy, which is both
the honest and the larger sample.

The `last-before` rows are kept as a control, not as the deployment.

## The exit is the weak side

The 5-minute exit is the right horizon, because after it the tape thins out:

| exit mark | prints older than 60 s | median age | capped PnL |
| --- | --- | --- | --- |
| entry + 5 min | 15.9 % | 1.5 s | +44.64 SOL |
| entry + 15 min | 59.4 % | 463 s | +80.13 SOL marked |

The 15-minute number is not executable. If every exit print older than 60 s is
written off at −100 % (you cannot sell into a pool that stopped printing):

| exit | stale exits | marked PnL | stale-at-−100 % |
| --- | --- | --- | --- |
| 5 min | 289 | +44.64 SOL | **+5.64 SOL** |
| 15 min | 1,079 | +80.13 SOL | **−71.06 SOL** |

That is why the bot holds 5 minutes and why the funding gate below measures
*realized* exit quality, not the marked one.

## Fee / slippage stress

The tape return already includes 1.25 % per side. Additional per-side haircut
on the exit, applied to the deployed cohort:

| extra exit haircut | median | win | capped PnL |
| --- | --- | --- | --- |
| 0 % | +22.0 % | 97.2 % | +44.64 SOL |
| 10 % | +9.8 % | 77.4 % | +22.32 SOL |
| 15 % | +3.7 % | 63.8 % | +11.17 SOL |
| 20 % | −2.4 % | 40.6 % | +0.01 SOL |
| 30 % | −14.6 % | 7.8 % | −22.31 SOL |

The median stays positive through 15 % of extra exit slippage and crosses
zero between 15 % and 20 %. The 10 % hard ceiling in `--max-slippage-bps` is
therefore inside the safe zone, not at its edge; if realized exit slippage
approaches 15–20 %, the lane should be turned off rather than widened.

## What was rejected

Measured on the same four days and the same fill convention, over the whole
scanner feed (20 s, `last-before`, `--entry-side any`, as a channel-level
control — reproduce with `--latency-ms 20000 --entry-fill-mode last-before
--entry-side any --max-entry-age-ms 0`):

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

`>100k` wins often but does not move (median +0.9 %). The sub-15k tail is
where the rugs and unexecutable fills live (67.3 % / +7.4 %). 15–20k is
positive on this sample but is the thinnest bucket and sits next to that
failure zone, so the 20k floor is a conservative choice rather than a measured
optimum. 20–100k is the only band that pays after fees with room to spare.

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

1. **≥300 trades over at least 5 days** (the feed produces ~450 in-band
   alerts/day, so this is a time-coverage gate as much as a sample-size one).
2. **Realized win rate ≥80 %.** On actual fills, not marks. The backtest is
   97.2 % marked and 82.7 % with stale exits written off; below 80 % the edge
   is gone.
3. **Realized median net return ≥+10 %.** Backtest +22.0 % marked, +19.1 %
   with stale exits written off.
4. **Under 20 % of exits written off.** Read `exit_writeoffs.jsonl` and the
   `exit_dust` / `exit_quote_failed` reasons; the backtest write-off rate is
   15.9 %.
5. **No systematic entry failure.** `entry_delay_exceeded` /
   `entry_quote_failed` above ~10 % of in-band alerts means the bot is not
   taking the trades the backtest measured, regardless of the realized win
   rate.

If 2 or 3 fails while 5 passes, the edge did not survive the move from replay
to live and the lane should be retired, not retuned. Do not re-fit the rule on
the forward sample: that turns the gate into a second in-sample fit.

## Known limitations

- Four replay days is a small sample and they are consecutive-ish; the day
  spread (96.4–98.2 % win) is consistent but not independent.
- The marked PnL assumes the 5-minute exit print is executable. That is why
  the write-off column is shown next to every headline: it is the pessimistic
  bound, not a footnote.
- The scanner channel re-posts each pool ~3×. Only the first card is traded
  (`scanner_cards` / `scanner_duplicates` in `summary.json`), which is what
  the backtest assumes.
- Replay archives stop at 2026-11-20, so there is no forward out-of-sample
  day in the archive. The forward paper run is the only OOS test.
- Live mode has not been exercised against a funded wallet; the Jupiter
  read-only quote path has been smoke-tested only.
