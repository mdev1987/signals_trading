#!/usr/bin/env python3
"""Export replay and OHLCV artifacts to Parquet for research workloads.

The canonical formats stay as they are: the execution simulator streams
``.jsonl.zst`` and the audit pipeline owns the 285-column OHLCV CSV. Parquet is
a derived, read-optimised copy for repeated analytical loads, where measured
gains are large (OHLCV: 3.8 s -> 0.29 s full read, 0.8 s -> 0.02 s for a narrow
column set).

Two exports are supported:

* ``--ohlcv`` converts a curated OHLCV CSV into a sibling ``.parquet`` file,
  preserving every column and dtype.
* ``--events`` converts a filtered event stream into one compact
  ``events.parquet`` sorted by timestamp. The nested ``breakdown`` payload is
  reduced to per-event leg counts, unique-trader counts and signed leg sizes;
  the raw payload remains available in the ``.jsonl.zst`` replay.

Examples:
    python src/export_parquet.py --ohlcv output/backtest/ohlcv/ohlcv_1m.csv
    python src/export_parquet.py \\
        --events output/backtest/replay_filtered/target_mints_events.jsonl.zst \\
        --out-dir output/backtest/parquet
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = Path(__file__).resolve().parent
for entry in (str(REPO_ROOT), str(SCRIPT_DIR)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

import pyarrow as pa  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402

from filter_replay import _loads  # noqa: E402

EVENT_SCHEMA = pa.schema(
    [
        ("timestamp_ms", pa.int64()),
        ("mint", pa.string()),
        ("pool", pa.string()),
        ("pool_id", pa.string()),
        ("action", pa.string()),
        ("quote_mint", pa.string()),
        ("signature", pa.string()),
        ("tx_signer", pa.string()),
        ("price_sol", pa.float64()),
        ("market_cap_sol", pa.float64()),
        ("market_cap_quote", pa.float64()),
        ("quote_amount", pa.float64()),
        ("token_amount", pa.float64()),
        ("priority_fee", pa.float64()),
        ("supply", pa.float64()),
        ("decimals", pa.int64()),
        ("tokens_in_pool", pa.float64()),
        ("quote_in_pool", pa.float64()),
        ("v_tokens_in_bonding_curve", pa.float64()),
        ("v_quote_in_bonding_curve", pa.float64()),
        ("virtual_tokens_in_pool", pa.float64()),
        ("virtual_quote_in_pool", pa.float64()),
        ("pool_fee_rate", pa.float64()),
        ("leg_count", pa.int64()),
        ("leg_traders", pa.int64()),
        ("leg_buy_quote", pa.float64()),
        ("leg_sell_quote", pa.float64()),
        ("leg_buy_token", pa.float64()),
        ("leg_sell_token", pa.float64()),
    ]
)

EVENT_FIELDS = (
    ("timestamp_ms", "timestamp", int),
    ("mint", "mint", str),
    ("pool", "pool", str),
    ("pool_id", "poolId", str),
    ("action", "action", str),
    ("quote_mint", "quoteMint", str),
    ("signature", "signature", str),
    ("tx_signer", "txSigner", str),
    ("price_sol", "priceSol", float),
    ("market_cap_sol", "marketCapSol", float),
    ("market_cap_quote", "marketCapQuote", float),
    ("quote_amount", "quoteAmount", float),
    ("token_amount", "tokenAmount", float),
    ("priority_fee", "priorityFee", float),
    ("supply", "supply", float),
    ("decimals", "decimals", int),
    ("tokens_in_pool", "tokensInPool", float),
    ("quote_in_pool", "quoteInPool", float),
    ("v_tokens_in_bonding_curve", "vTokensInBondingCurve", float),
    ("v_quote_in_bonding_curve", "vQuoteInBondingCurve", float),
    ("virtual_tokens_in_pool", "virtualTokensInPool", float),
    ("virtual_quote_in_pool", "virtualQuoteInPool", float),
    ("pool_fee_rate", "poolFeeRate", float),
)


def _number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _integer(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def event_row(event: dict) -> dict:
    row: dict = {}
    for target, source, caster in EVENT_FIELDS:
        value = event.get(source)
        if caster is float:
            row[target] = _number(value)
        elif caster is int:
            row[target] = _integer(value)
        else:
            row[target] = str(value) if value is not None else None

    legs = event.get("breakdown")
    legs = legs if isinstance(legs, list) else []
    traders = set()
    buy_quote = sell_quote = buy_token = sell_token = 0.0
    for leg in legs:
        if not isinstance(leg, dict):
            continue
        trader = leg.get("trader")
        if trader:
            traders.add(str(trader))
        quote_amount = _number(leg.get("quoteAmount")) or 0.0
        token_amount = _number(leg.get("tokenAmount")) or 0.0
        if str(leg.get("action") or "").lower() == "sell":
            sell_quote += quote_amount
            sell_token += token_amount
        else:
            buy_quote += quote_amount
            buy_token += token_amount
    row["leg_count"] = len(legs)
    row["leg_traders"] = len(traders) if legs else (1 if row.get("tx_signer") else 0)
    row["leg_buy_quote"] = buy_quote
    row["leg_sell_quote"] = sell_quote
    row["leg_buy_token"] = buy_token
    row["leg_sell_token"] = sell_token
    return row


def convert_ohlcv(path: Path, out_path: Path | None) -> dict:
    import pandas as pd

    started = time.time()
    frame = pd.read_csv(path, low_memory=False)
    read_seconds = time.time() - started
    target = out_path or path.with_suffix(".parquet")
    target.parent.mkdir(parents=True, exist_ok=True)
    started = time.time()
    frame.to_parquet(target, compression="zstd", index=False)
    write_seconds = time.time() - started

    started = time.time()
    reloaded = pd.read_parquet(target, columns=["timestamp_ms", "mint", "close"])
    narrow_seconds = time.time() - started
    del reloaded, frame

    return {
        "kind": "ohlcv",
        "source": str(path),
        "output": str(target),
        "rows": int(
            pq.ParquetFile(target).metadata.num_rows
        ),
        "columns": pq.ParquetFile(target).metadata.num_columns,
        "csv_bytes": path.stat().st_size,
        "parquet_bytes": target.stat().st_size,
        "read_seconds": round(read_seconds, 2),
        "write_seconds": round(write_seconds, 2),
        "narrow_read_seconds": round(narrow_seconds, 3),
    }


def convert_events(path: Path, out_path: Path, batch_size: int = 200_000) -> dict:
    import zstandard as zstd

    started = time.time()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    writer = pq.ParquetWriter(out_path, EVENT_SCHEMA, compression="zstd")
    rows = 0
    batch: list[dict] = []
    try:
        with path.open("rb") as source:
            reader = zstd.ZstdDecompressor().stream_reader(
                source, read_across_frames=True
            )
            for line in io.BufferedReader(reader, 1 << 20):
                if not line.strip():
                    continue
                event = _loads(line)
                if not isinstance(event, dict):
                    continue
                batch.append(event_row(event))
                if len(batch) >= batch_size:
                    writer.write_table(pa.Table.from_pylist(batch, schema=EVENT_SCHEMA))
                    rows += len(batch)
                    batch = []
        if batch:
            writer.write_table(pa.Table.from_pylist(batch, schema=EVENT_SCHEMA))
            rows += len(batch)
    finally:
        writer.close()

    return {
        "kind": "events",
        "source": str(path),
        "output": str(out_path),
        "rows": rows,
        "columns": len(EVENT_SCHEMA),
        "source_bytes": path.stat().st_size,
        "parquet_bytes": out_path.stat().st_size,
        "elapsed_seconds": round(time.time() - started, 1),
        "note": (
            "breakdown payload reduced to leg_count/leg_traders/signed leg sizes; "
            "raw breakdown remains in the .jsonl.zst replay"
        ),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--ohlcv", type=Path, action="append", default=[],
                        help="Curated OHLCV CSV to convert (repeatable).")
    parser.add_argument("--ohlcv-out", type=Path, default=None,
                        help="Output path for a single --ohlcv input.")
    parser.add_argument("--events", type=Path, default=None,
                        help="Filtered .jsonl.zst event stream to convert.")
    parser.add_argument("--out-dir", type=Path, default=Path("output/backtest/parquet"),
                        help="Directory for --events output.")
    parser.add_argument("--events-out", type=Path, default=None,
                        help="Explicit output path for --events.")
    parser.add_argument("--batch-size", type=int, default=200_000)
    parser.add_argument("--overwrite", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.ohlcv and args.events is None:
        raise SystemExit("Nothing to do: pass --ohlcv and/or --events")
    if args.ohlcv_out is not None and len(args.ohlcv) != 1:
        raise SystemExit("--ohlcv-out requires exactly one --ohlcv input")

    results = []
    for path in args.ohlcv:
        if not path.exists():
            raise SystemExit(f"Missing OHLCV file: {path}")
        target = (args.ohlcv_out or path.with_suffix(".parquet"))
        if target.exists() and not args.overwrite:
            raise SystemExit(f"Refusing to overwrite {target}; pass --overwrite")
        results.append(convert_ohlcv(path, target))

    if args.events is not None:
        if not args.events.exists():
            raise SystemExit(f"Missing event stream: {args.events}")
        target = args.events_out or (args.out_dir / "events.parquet")
        if target.exists() and not args.overwrite:
            raise SystemExit(f"Refusing to overwrite {target}; pass --overwrite")
        results.append(
            convert_events(args.events, target, batch_size=args.batch_size)
        )

    print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
