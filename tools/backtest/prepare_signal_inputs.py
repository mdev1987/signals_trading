from __future__ import annotations

import csv
import json
import re
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

SOURCE = Path(os.environ["SIGNALS_CSV"])
OUT = Path("output/backtest/signals")

REPLAY_START = datetime(2026, 11, 8, 4, tzinfo=timezone.utc)
REPLAY_END = datetime(2026, 11, 9, 12, tzinfo=timezone.utc)

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

with SOURCE.open(
    "r",
    encoding="utf-8-sig",
    newline="",
) as f:
    source_rows = list(csv.DictReader(f))

network_counts = Counter()
signals = []
invalid = []
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

start_ms = int(REPLAY_START.timestamp() * 1000)
end_ms = int(REPLAY_END.timestamp() * 1000)

outside = [
    x["signal_id"]
    for x in signals
    if not start_ms <= x["signal_timestamp_ms"] < end_ms
]

OUT.mkdir(parents=True, exist_ok=True)

csv_path = OUT / "strategy_signals.csv"
fields = list(signals[0].keys()) if signals else []

with csv_path.open("w", encoding="utf-8", newline="") as f:
    writer = csv.DictWriter(f, fieldnames=fields)
    writer.writeheader()
    writer.writerows(signals)

mint_path = OUT / "target_mints.txt"
mint_path.write_text(
    "\n".join(sorted(mint_counts)) + "\n",
    encoding="utf-8",
)

report = {
    "source_file": str(SOURCE),
    "source_messages": len(source_rows),
    "second_vibe_buy_messages": signal_message_count,
    "network_counts": dict(network_counts),
    "selected_solana_signals": len(signals),
    "unique_solana_mints": len(mint_counts),
    "duplicate_mints": duplicate_mints,
    "invalid_signals": invalid,
    "signals_outside_replay_window": outside,
    "replay_window_utc": {
        "start": REPLAY_START.isoformat(),
        "end_exclusive": REPLAY_END.isoformat(),
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
        len(signals) == 246
        and len(mint_counts) == 246
        and not invalid
        and not outside
        and not duplicate_mints
    ),
}

report_path = OUT / "signal_ingestion_report.json"
report_path.write_text(
    json.dumps(report, indent=2, ensure_ascii=False),
    encoding="utf-8",
)

print(json.dumps(report, indent=2, ensure_ascii=False))

if not report["passed"]:
    raise SystemExit("Signal ingestion validation failed.")
