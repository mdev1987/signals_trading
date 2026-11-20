#!/usr/bin/env python3
"""Turn a Telegram channel export into a strategy signal manifest.

Reads one exported messages CSV (see fetch_signals.py) and emits, for a single
replay window:

    <out-dir>/strategy_signals.csv
    <out-dir>/target_mints.txt
    <out-dir>/signal_ingestion_report.json

Only ``2nd Vibe Buy Signal`` messages tagged ``solana`` are kept, and each must
carry exactly one strict ``CA: <address>`` line. Anything ambiguous is reported
in ``invalid_signals`` instead of being guessed.

Examples:
    python tools/backtest/prepare_signal_inputs.py \\
        --signals-csv messages/2026-11-08/@avesignalmonitor-messages.csv \\
        --start 2026-11-08T04:00:00+00:00 --end 2026-11-09T12:00:00+00:00 \\
        --out-dir output/backtest/signals

SIGNALS_CSV can still be supplied through the environment when --signals-csv is
omitted, so existing invocations keep working.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

SOLANA_RE = re.compile(r"(?im)^\s*🔗\s*solana\s*$")
BSC_RE = re.compile(r"(?im)^\s*🔗\s*bsc\s*$")
ROBINHOOD_RE = re.compile(r"(?im)^\s*🔗\s*robinhood\s*$")

CA_RE = re.compile(
    r"(?im)^CA:\s*([1-9A-HJ-NP-Za-km-z]{32,44})\s*$"
)

SIGNAL_TEXT = "2nd Vibe Buy Signal"


def parse_utc(value: str) -> datetime:
    dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))

    if dt.tzinfo is None:
        raise ValueError(f"Timestamp has no timezone: {value}")

    return dt.astimezone(timezone.utc)


def build_rows(source_rows: list[dict]) -> tuple[list[dict], list[dict], Counter, int]:
    network_counts: Counter = Counter()
    signals: list[dict] = []
    invalid: list[dict] = []
    signal_message_count = 0

    for row in source_rows:
        message = row.get("Message") or ""

        if SIGNAL_TEXT not in message:
            continue

        signal_message_count += 1

        if SOLANA_RE.search(message):
            chain = "solana"
        elif BSC_RE.search(message):
            chain = "bsc"
        elif ROBINHOOD_RE.search(message):
            chain = "robinhood"
        else:
            chain = "unknown"

        network_counts[chain] += 1

        if chain != "solana":
            continue

        message_id = str(row.get("MessageId") or "").strip()
        message_link = str(row.get("MessageLink") or "").strip()
        username = str(row.get("Username") or "").strip()

        try:
            signal_dt = parse_utc(str(row["Date"]))
            signal_ts_ms = int(signal_dt.timestamp() * 1000)
        except Exception as exc:
            invalid.append({
                "message_id": message_id,
                "reason": f"invalid_timestamp: {exc}",
            })
            continue

        matches = CA_RE.findall(message)

        if len(matches) != 1:
            invalid.append({
                "message_id": message_id,
                "reason": f"expected_one_CA_found_{len(matches)}",
            })
            continue

        mint = matches[0]

        signals.append({
            "signal_id": f"telegram:{message_id}",
            "signal_timestamp_ms": signal_ts_ms,
            "signal_timestamp_utc": signal_dt.isoformat(),
            "mint": mint,
            "chain": chain,
            "signal_type": "second_vibe_buy",
            "source_message_id": message_id,
            "source_username": username,
            "source_message_link": message_link,
        })

    return signals, invalid, network_counts, signal_message_count


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--signals-csv", default=os.environ.get("SIGNALS_CSV"),
                        help="Exported messages CSV (default: $SIGNALS_CSV).")
    parser.add_argument("--start", default="2026-11-08T04:00:00+00:00",
                        help="Replay window start, inclusive (default 2026-11-08T04:00:00Z).")
    parser.add_argument("--end", default="2026-11-09T12:00:00+00:00",
                        help="Replay window end, exclusive (default 2026-11-09T12:00:00Z).")
    parser.add_argument("--out-dir", default="output/backtest/signals")
    parser.add_argument("--expected-signals", type=int, default=None,
                        help="Fail unless exactly this many Solana signals are selected.")
    parser.add_argument("--keep-outside", action="store_true",
                        help="Keep signals outside the replay window instead of dropping them.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.signals_csv:
        raise SystemExit("--signals-csv is required (or set SIGNALS_CSV)")

    source = Path(args.signals_csv)
    out = Path(args.out_dir)
    replay_start = parse_utc(args.start)
    replay_end = parse_utc(args.end)

    with source.open("r", encoding="utf-8-sig", newline="") as f:
        source_rows = list(csv.DictReader(f))

    signals, invalid, network_counts, signal_message_count = build_rows(source_rows)

    signals.sort(
        key=lambda x: (
            x["signal_timestamp_ms"],
            x["source_message_id"],
        )
    )

    mint_counts = Counter(x["mint"] for x in signals)
    duplicate_mints = {
        mint: count
        for mint, count in mint_counts.items()
        if count > 1
    }

    start_ms = int(replay_start.timestamp() * 1000)
    end_ms = int(replay_end.timestamp() * 1000)

    outside = [
        x["signal_id"]
        for x in signals
        if not start_ms <= x["signal_timestamp_ms"] < end_ms
    ]

    # The fetch deliberately includes a lookback hour so signals just before the
    # window are visible. They cannot be traded (no replay data before the
    # window), so they are reported and dropped rather than failing ingestion.
    if not args.keep_outside:
        signals = [
            x for x in signals
            if start_ms <= x["signal_timestamp_ms"] < end_ms
        ]
        mint_counts = Counter(x["mint"] for x in signals)
        duplicate_mints = {
            mint: count
            for mint, count in mint_counts.items()
            if count > 1
        }

    out.mkdir(parents=True, exist_ok=True)

    csv_path = out / "strategy_signals.csv"
    fields = list(signals[0].keys()) if signals else []

    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(signals)

    mint_path = out / "target_mints.txt"
    mint_path.write_text(
        "\n".join(sorted(mint_counts)) + "\n",
        encoding="utf-8",
    )

    count_ok = args.expected_signals is None or (
        len(signals) == args.expected_signals
        and len(mint_counts) == args.expected_signals
    )

    report = {
        "source_file": str(source),
        "source_messages": len(source_rows),
        "second_vibe_buy_messages": signal_message_count,
        "network_counts": dict(network_counts),
        "selected_solana_signals": len(signals),
        "unique_solana_mints": len(mint_counts),
        "duplicate_mints": duplicate_mints,
        "invalid_signals": invalid,
        "signals_outside_replay_window": outside,
        "replay_window_utc": {
            "start": replay_start.isoformat(),
            "end_exclusive": replay_end.isoformat(),
        },
        "signal_time_range_utc": {
            "first": signals[0]["signal_timestamp_utc"] if signals else None,
            "last": signals[-1]["signal_timestamp_utc"] if signals else None,
        },
        "outputs": {
            "signals_csv": str(csv_path),
            "target_mints": str(mint_path),
        },
        "passed": (
            count_ok
            and not invalid
            and not duplicate_mints
            and bool(signals)
            and (not args.keep_outside or not outside)
        ),
    }

    report_path = out / "signal_ingestion_report.json"
    report_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print(json.dumps(report, indent=2, ensure_ascii=False))

    if not report["passed"]:
        raise SystemExit("Signal ingestion validation failed.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
