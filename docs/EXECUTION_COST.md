# Execution-cost measurement

Status: **read-only measurement. No strategy change, no lane, no new gate.**
This is roadmap step 1: establish what the quotes actually cost before
retuning anything.

## Run it

After a paper run has written `trades.jsonl`:

```bash
env PYTHONPATH=src .venv/bin/python src/execution_cost_report.py \
  --run-dir output/paper/live_YYYYMMDD_HHMMSS
# machine-readable, and optionally saved for the record
env PYTHONPATH=src .venv/bin/python src/execution_cost_report.py \
  --run-dir output/paper/live_YYYYMMDD_HHMMSS \
  --json --artifact output/reports/execution_cost_YYYYMMDD.json
```

The script only reads the run directory. It never trades, posts, or edits a
run.

## What it reports

- **Modeled round-trip cost**, split into entry slippage, exit slippage and
  fee (both sides), in SOL and as a share of entry notional.
- **Quote quality**: entry price impact, exit price impact, entry quote age
  (fill time minus quote time) and exit quote latency (fill time minus quote
  timestamp), each with n/mean/median/p05/p95. These are Jupiter's own
  reported numbers and the timestamps recorded on the fill; they are not
  assumed.
- **Executability**: share of exits that priced one full-size quote, share
  that needed the fraction ladder, write-off / near-zero share, catastrophic
  share.
- **PnL under cost assumptions**: total PnL on fully sold trades for a grid of
  fee per side (`0`–`2 %`) and slippage per side (`0`–`1.25 %`), holding the
  observed quote prices fixed. This answers "how thin is the edge?" without
  changing the running strategy.
- **Quote-guard telemetry** when present: probes, unavailable routes and the
  median lowest exit-value ratio.

## What it cannot measure

Paper fills use read-only Jupiter quotes. The report can therefore show
**modeled** cost and the quote's own price impact, but not:

- priority fees,
- Jito tips,
- ATA rent,
- transaction fees,
- the difference between the quoted minimum output and the amount actually
  received on chain.

Those require executed transactions. Do not present a modeled cost as a paid
fee, and do not lower the `1.25 %`/side assumption on the strength of a
quote-only report.

## Reading the two cost units

At 982 trades × 0.1 SOL = 98.2 SOL of notional:

| change | value |
| --- | ---: |
| 1 percentage point on **total round-trip** cost | ≈ 0.982 SOL |
| 1 percentage point on **each side** | ≈ 1.964 SOL |

The exit-policy study's effects are smaller than either. That is why the
cheapest possible improvement is to measure and reduce real execution cost,
not to add another exit rule.
