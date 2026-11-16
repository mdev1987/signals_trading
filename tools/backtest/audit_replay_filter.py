from __future__ import annotations

import csv
import json
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import orjson
import zstandard as zstd

ROOT = Path.cwd()
INPUT = ROOT / "2026-11-08-replay"
TARGETS = ROOT / "output/backtest/signals/target_mints.txt"
SIGNALS = ROOT / "output/backtest/signals/strategy_signals.csv"
OLD_REPORT = ROOT / "output/backtest/filtered_replay_32h/filtered_replay_report.json"
OUT = ROOT / "output/backtest/filter_replay_diagnostics.json"

START = datetime(2026, 11, 8, 4, tzinfo=timezone.utc)
HOURS = 32

MINT_RE = re.compile(rb'"mint"\s*:\s*"([^"\\]+)"')


def read_lines(path: Path):
    with path.open("rb") as source:
        reader = zstd.ZstdDecompressor().stream_reader(
            source,
            read_across_frames=True,
        )
        with reader:
            import io
            buffered = io.BufferedReader(reader, buffer_size=1 << 22)
            try:
                yield from buffered
            finally:
                buffered.close()


def get_timestamp(event: dict) -> int | None:
    value = event.get("timestamp", event.get("timestamp_ms"))
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


targets = {
    line.strip().encode("ascii")
    for line in TARGETS.read_text(encoding="utf-8").splitlines()
    if line.strip()
}

with SIGNALS.open("r", encoding="utf-8-sig", newline="") as f:
    signal_mints = {
        (row.get("mint") or "").strip().encode("ascii")
        for row in csv.DictReader(f)
        if (row.get("mint") or "").strip()
    }

if targets != signal_mints:
    raise SystemExit("Target manifest does not match signal CSV.")

previous = json.loads(OLD_REPORT.read_text(encoding="utf-8"))

counts = Counter()
per_file = []
samples = Counter()
sample_data = {}
out_of_hour_examples = []
inversion_examples = []

previous_global_ts = None
previous_global_location = None

expected = [
    (START + timedelta(hours=i)).strftime("%Y%m%d_%H.jsonl.zst")
    for i in range(HOURS)
]

for file_index, filename in enumerate(expected):
    path = INPUT / filename

    if not path.is_file():
        raise FileNotFoundError(path)

    hour = datetime.strptime(
        filename, "%Y%m%d_%H.jsonl.zst"
    ).replace(tzinfo=timezone.utc)

    hour_start_ms = int(hour.timestamp() * 1000)
    hour_end_ms = hour_start_ms + 3_600_000

    local = Counter()
    previous_local_ts = None
    previous_local_line = None
    line_count = 0

    for line_number, line in enumerate(read_lines(path), start=1):
        line_count += 1

        match = MINT_RE.search(line)
        if match is None or match.group(1) not in targets:
            continue

        local["regex_candidate_lines"] += 1
        counts["regex_candidate_lines"] += 1

        try:
            event = orjson.loads(line)
        except orjson.JSONDecodeError:
            local["json_decode_errors"] += 1
            counts["json_decode_errors"] += 1

            if samples["json_decode_errors"] < 5:
                sample_data.setdefault("json_decode_errors", []).append({
                    "file": filename,
                    "line": line_number,
                    "raw_prefix": line[:250].decode("utf-8", "replace"),
                })
                samples["json_decode_errors"] += 1
            continue

        if not isinstance(event, dict):
            local["valid_non_object_json"] += 1
            counts["valid_non_object_json"] += 1

            if samples["valid_non_object_json"] < 5:
                sample_data.setdefault("valid_non_object_json", []).append({
                    "file": filename,
                    "line": line_number,
                    "json_type": type(event).__name__,
                })
                samples["valid_non_object_json"] += 1
            continue

        root_mint = event.get("mint")

        if not isinstance(root_mint, str) or not root_mint:
            local["top_level_mint_missing_or_invalid"] += 1
            counts["top_level_mint_missing_or_invalid"] += 1

            category = "top_level_mint_missing_or_invalid"

        elif root_mint.encode("utf-8") not in targets:
            local["top_level_mint_not_target"] += 1
            counts["top_level_mint_not_target"] += 1

            category = "top_level_mint_not_target"

        else:
            local["exact_top_level_matches"] += 1
            counts["exact_top_level_matches"] += 1
            category = None

        if category and samples[category] < 5:
            sample_data.setdefault(category, []).append({
                "file": filename,
                "line": line_number,
                "regex_mint": match.group(1).decode("utf-8", "replace"),
                "top_level_mint": root_mint,
                "action": event.get("action"),
                "pool": event.get("pool"),
                "root_keys": list(event.keys())[:30],
            })
            samples[category] += 1

        if category:
            continue

        ts = get_timestamp(event)

        if ts is None:
            local["exact_matches_missing_timestamp"] += 1
            counts["exact_matches_missing_timestamp"] += 1
            continue

        if not hour_start_ms <= ts < hour_end_ms:
            local["exact_matches_outside_filename_hour"] += 1
            counts["exact_matches_outside_filename_hour"] += 1

            if len(out_of_hour_examples) < 20:
                out_of_hour_examples.append({
                    "file": filename,
                    "line": line_number,
                    "timestamp_ms": ts,
                    "timestamp_utc": datetime.fromtimestamp(
                        ts / 1000, timezone.utc
                    ).isoformat(),
                    "offset_from_file_hour_start_ms": ts - hour_start_ms,
                    "mint": root_mint,
                    "action": event.get("action"),
                    "pool": event.get("pool"),
                    "signature": event.get("signature"),
                })

        if previous_global_ts is not None and ts < previous_global_ts:
            counts["global_timestamp_inversions"] += 1

            if len(inversion_examples) < 20:
                inversion_examples.append({
                    "previous": previous_global_location,
                    "previous_timestamp_ms": previous_global_ts,
                    "current": {
                        "file": filename,
                        "line": line_number,
                    },
                    "current_timestamp_ms": ts,
                })

        previous_global_ts = ts
        previous_global_location = {
            "file": filename,
            "line": line_number,
        }

        if previous_local_ts is not None and ts < previous_local_ts:
            local["within_file_timestamp_inversions"] += 1
            counts["within_file_timestamp_inversions"] += 1

            if len(inversion_examples) < 20:
                inversion_examples.append({
                    "file": filename,
                    "previous_line": previous_local_line,
                    "previous_timestamp_ms": previous_local_ts,
                    "current_line": line_number,
                    "current_timestamp_ms": ts,
                })

        previous_local_ts = ts
        previous_local_line = line_number

    per_file.append({
        "file": filename,
        "source_lines": line_count,
        **dict(local),
    })

legacy_malformed_reconstructed = sum(
    counts[key]
    for key in (
        "json_decode_errors",
        "valid_non_object_json",
        "top_level_mint_missing_or_invalid",
        "top_level_mint_not_target",
    )
)

report = {
    "version": "1.0.0",
    "target_mints": len(targets),
    "files_processed": len(per_file),
    "source_lines_scanned": sum(x["source_lines"] for x in per_file),
    "classification": dict(counts),
    "legacy_malformed_selected_events": previous["malformed_selected_events"],
    "legacy_malformed_reconstructed": legacy_malformed_reconstructed,
    "legacy_malformed_count_matches": (
        legacy_malformed_reconstructed
        == previous["malformed_selected_events"]
    ),
    "exact_top_level_matches_expected": previous["matching_events_written"],
    "exact_top_level_matches_reconciled": (
        counts["exact_top_level_matches"]
        == previous["matching_events_written"]
    ),
    "out_of_hour_expected": previous["selected_events_outside_filename_hour"],
    "out_of_hour_reconciled": (
        counts["exact_matches_outside_filename_hour"]
        == previous["selected_events_outside_filename_hour"]
    ),
    "out_of_hour_examples": out_of_hour_examples,
    "timestamp_inversion_examples": inversion_examples,
    "sample_false_positives": sample_data,
    "per_file": per_file,
}

report["passed"] = (
    len(per_file) == HOURS
    and counts["json_decode_errors"] == 0
    and counts["exact_matches_missing_timestamp"] == 0
    and report["legacy_malformed_count_matches"]
    and report["exact_top_level_matches_reconciled"]
    and report["out_of_hour_reconciled"]
)

OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(
    json.dumps(report, ensure_ascii=False, indent=2),
    encoding="utf-8",
)

print(json.dumps({
    key: value
    for key, value in report.items()
    if key != "per_file" and key != "sample_false_positives"
}, ensure_ascii=False, indent=2))

print(f"\nSaved: {OUT}")

if not report["passed"]:
    raise SystemExit(1)
