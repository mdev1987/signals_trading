# Mint-balanced filtered Replay sharding

This tool repartitions the already-filtered hourly `.jsonl.zst` files into balanced batch archives. Each Mint belongs to exactly one batch, and raw JSONL lines are retained byte-for-byte and in source order.

## Inputs

- `output/backtest/filtered_replay_32h/` — 32 filtered hourly archives
- `output/backtest/filtered_replay_32h/filtered_replay_report.json` — extraction report
- `output/backtest/filter_replay_diagnostics.json` — diagnostic report confirming exact top-level Mint matches and explaining the legacy malformed counter
- `output/backtest/signals/target_mints.txt` — 246 target Mints

The original extraction report has `passed=false` because its broad regex counted 14,869 valid records without a top-level `mint` as malformed. The diagnostic report shows those records have no usable top-level Mint; the sample records are transfer events with nested mint data. It also reconciles all 1,789,305 exact top-level matches and all four hour-boundary records. The sharder checks those diagnostic conditions instead of blindly accepting the old `passed` flag.

## Install

Copy `shard_filtered_replay.py` into:

`tools/backtest/shard_filtered_replay.py`

## Run

```bash
uv run python -m py_compile tools/backtest/shard_filtered_replay.py

uv run python tools/backtest/shard_filtered_replay.py \
  --input-dir output/backtest/filtered_replay_32h \
  --targets output/backtest/signals/target_mints.txt \
  --report output/backtest/filtered_replay_32h/filtered_replay_report.json \
  --diagnostics output/backtest/filter_replay_diagnostics.json \
  --output-dir output/backtest/mint_shards \
  --max-events-per-batch 100000
```

Use `--overwrite` only when intentionally regenerating the shard directory.

## Guarantees checked

- Every target Mint is assigned to exactly one batch.
- Per-file/per-Mint counts match the original filtered Replay report.
- No unexpected top-level Mint is accepted.
- Source file order and line order are preserved in each batch.
- JSON lines are valid objects with top-level Mint and timestamp.
- Each output batch is decompressed and verified after writing.
- SHA-256 of the raw concatenated lines matches before and after compression.
- Timestamp order is checked globally and inside each batch.

The default 100,000-event target is a soft cap: a single Mint is never split. A Mint with more events than the cap is kept in its own batch and is reported explicitly.

The tool does not modify the original filtered archives, source files, Git baseline, or Dataset Builder.
