#!/usr/bin/env python3
"""Stream-filter PumpAPI replay archives down to a target mint universe.

The full replay is far too large to load into memory. This tool makes a single
pass over every ``*.jsonl.zst`` archive, keeps only events whose ``mint`` is in
the requested universe, and writes one timestamp-ordered zstd JSONL file.

Why the filtered file is reusable:

* It preserves every field ``raw_to_clean_ohlcv.py`` reads, so OHLCV generation
  can run unmodified against the reduced file.
* It preserves both reserve styles used by the feed:
  ``vTokensInBondingCurve`` / ``vQuoteInBondingCurve`` for ``pump`` bonding
  curves and ``tokensInPool`` / ``quoteInPool`` / ``virtualTokensInPool`` /
  ``virtualQuoteInPool`` for AMM pools. The OHLCV reader only stores the AMM
  style, so the bonding-curve fields must survive the filter for the execution
  simulator.
* It preserves pool lifecycle actions (``create``, ``createPool``, ``migrate``,
  ``add``, ``remove``) so pool reserves can be reconstructed from first
  appearance, not only from the first trade.

Memory stays bounded: each worker streams one archive and writes its own part
file; the merge step performs a k-way merge with a small dedupe window.

Examples:
    python tools/backtest/filter_replay.py \\
        --replay-dir 2026-11-08-replay \\
        --mints output/backtest/signals/target_mints.txt \\
        --output-dir output/backtest/replay_filtered
"""

from __future__ import annotations

import argparse
import heapq
import io
import json
import os
import sys
import time
from collections import Counter, deque
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

# Running as ``python tools/backtest/filter_replay.py`` puts the script directory
# on sys.path, so the project root must be added before importing repo modules.
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    import orjson

    def _loads(payload: bytes):
        return orjson.loads(payload)

    def _dumps(value) -> bytes:
        return orjson.dumps(value)

except ImportError:  # pragma: no cover - orjson is a declared dependency

    def _loads(payload: bytes):
        return json.loads(payload)

    def _dumps(value) -> bytes:
        return json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


# Pool lifecycle + trade actions carry reserve snapshots. Everything else in the
# feed (transfer, leaders, distributeFees, createConfig) has no pool reserves.
KEEP_ACTIONS = frozenset(
    {"buy", "sell", "create", "createPool", "migrate", "add", "remove"}
)

WSOL_MINT = "So11111111111111111111111111111111111111112"


def normalize_legacy_event(event: dict) -> bool:
    """Backfill SOL-denominated fields missing from pre-2026-11 archives.

    Archives from 2026-08/10 carry ``price`` and ``marketCapQuote`` in the
    pool's quote asset but omit ``priceSol``/``marketCapSol``. For WSOL-quoted
    pools those values are identical, so copying them is exact. Non-WSOL events
    are left alone: without ``quotePriceSol`` the SOL conversion is unknowable,
    and inventing one would silently misprice the pool.

    Returns True when a field was added.
    """
    if event.get("quoteMint") != WSOL_MINT:
        return False
    changed = False
    if event.get("priceSol") is None and event.get("price") is not None:
        event["priceSol"] = event["price"]
        changed = True
    if event.get("marketCapSol") is None and event.get("marketCapQuote") is not None:
        event["marketCapSol"] = event["marketCapQuote"]
        changed = True
    return changed

# Fields kept verbatim. This is a superset of what raw_to_clean_ohlcv.read_file
# pulls, plus the bonding-curve reserves and simulator metadata.
KEEP_FIELDS = (
    "signature",
    "action",
    "poolId",
    "mint",
    "quoteMint",
    "txSigner",
    "tokenAmount",
    "quoteAmount",
    "initialBuy",
    "tokensInPool",
    "quoteInPool",
    "virtualTokensInPool",
    "virtualQuoteInPool",
    "vTokensInBondingCurve",
    "vQuoteInBondingCurve",
    "price",
    "priceSol",
    "priceUsd",
    "quotePriceSol",
    "marketCapSol",
    "marketCapUsd",
    "marketCapQuote",
    "solRouteFeeRate",
    "poolFeeRate",
    "pool",
    "poolCreatedBy",
    "burnedLiquidity",
    "creatorFeeAddress",
    "mayhemMode",
    "cashbackEnabled",
    "holderRewardsRate",
    "minPrice",
    "maxPrice",
    "tickSpacing",
    "launchpadConfig",
    "migrationThresholds",
    "supply",
    "decimals",
    "tokenProgram",
    "block",
    "timestamp",
    "localTimestamp",
    "priorityFee",
    "breakdown",
    "name",
    "symbol",
)

# Participant balance maps are the largest per-event payload. raw_to_clean_ohlcv
# only inspects them when it flags an event for the balance audit, so they are
# retained only for those events. Dropping them otherwise keeps the filtered
# file small without changing any audit result.
BALANCE_FIELDS = ("preBalances", "postBalances")

_NEEDS_BALANCE_AUDIT = None


def _needs_balance_audit(event: dict, breakdown_count: int) -> bool:
    """Mirror raw_to_clean_ohlcv._needs_participant_balance_audit."""
    global _NEEDS_BALANCE_AUDIT
    if _NEEDS_BALANCE_AUDIT is None:
        from raw_to_clean_ohlcv import (
            _needs_participant_balance_audit as check,
        )

        _NEEDS_BALANCE_AUDIT = check
    return bool(_NEEDS_BALANCE_AUDIT(event, breakdown_count))


def _utc(ms: int | None) -> str | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()


def filter_archive(
    job: tuple[str, str, list[str]], *, normalize_legacy: bool = False
) -> dict:
    """Filter one archive into one part file. Returns per-file statistics."""
    source_path, part_path, targets = job
    import zstandard as zstd

    target_set = set(targets)
    stats = {
        "source": source_path,
        "part": part_path,
        "lines_scanned": 0,
        "events_kept": 0,
        "legacy_events_normalized": 0,
        "bad_json": 0,
        "action_counts": Counter(),
        "first_timestamp_ms": None,
        "last_timestamp_ms": None,
        "ordering_violations": 0,
    }
    previous_ts = None
    started = time.time()

    compressor = zstd.ZstdCompressor(level=3)
    with open(source_path, "rb") as src, open(part_path, "wb") as dst:
        reader = zstd.ZstdDecompressor().stream_reader(src, read_across_frames=True)
        buffer = io.BufferedReader(reader, 1 << 22)
        with compressor.stream_writer(dst, closefd=False) as writer:
            for line in buffer:
                stats["lines_scanned"] += 1
                try:
                    event = _loads(line)
                except ValueError:
                    stats["bad_json"] += 1
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get("mint") not in target_set:
                    continue
                action = event.get("action")
                if action not in KEEP_ACTIONS:
                    continue

                compact = {}
                for field in KEEP_FIELDS:
                    value = event.get(field)
                    if value is not None:
                        compact[field] = value

                if normalize_legacy and normalize_legacy_event(compact):
                    stats["legacy_events_normalized"] += 1

                breakdown = event.get("breakdown")
                breakdown = breakdown if isinstance(breakdown, list) else []
                if _needs_balance_audit(event, len(breakdown)):
                    for field in BALANCE_FIELDS:
                        if field in event:
                            compact[field] = event[field]

                timestamp = event.get("timestamp")
                if isinstance(timestamp, (int, float)):
                    if previous_ts is not None and timestamp < previous_ts:
                        stats["ordering_violations"] += 1
                    previous_ts = timestamp
                    if stats["first_timestamp_ms"] is None:
                        stats["first_timestamp_ms"] = int(timestamp)
                    stats["last_timestamp_ms"] = int(timestamp)

                writer.write(_dumps(compact))
                writer.write(b"\n")
                stats["events_kept"] += 1
                stats["action_counts"][action] += 1

    stats["elapsed_seconds"] = round(time.time() - started, 2)
    stats["action_counts"] = dict(stats["action_counts"])
    return stats


def _event_key(event: dict) -> tuple:
    return (
        event.get("signature"),
        event.get("poolId"),
        event.get("mint"),
        event.get("action"),
        event.get("timestamp"),
    )


def _event_ts(event: dict) -> int:
    timestamp = event.get("timestamp")
    return int(timestamp) if isinstance(timestamp, (int, float)) else 0


def _iter_part(path: str):
    import zstandard as zstd

    with open(path, "rb") as fh:
        reader = zstd.ZstdDecompressor().stream_reader(fh, read_across_frames=True)
        for line in io.BufferedReader(reader, 1 << 20):
            if line.strip():
                yield line


def merge_parts(
    part_paths: list[str],
    output_path: str,
    *,
    dedupe_window_ms: int = 300_000,
    level: int = 3,
) -> dict:
    """K-way merge sorted parts into one ordered, deduplicated JSONL file.

    Parts are individually timestamp ordered. Because adjacent hourly archives
    overlap by a few milliseconds at the boundary, identical events can appear in
    two parts; those are dropped using a bounded time window.
    """
    import zstandard as zstd

    stats = {
        "parts": len(part_paths),
        "events_read": 0,
        "events_written": 0,
        "duplicates_dropped": 0,
        "ordering_violations": 0,
        "first_timestamp_ms": None,
        "last_timestamp_ms": None,
    }

    heap: list[tuple[int, int, bytes]] = []
    iterators = []
    for index, path in enumerate(part_paths):
        iterator = _iter_part(path)
        iterators.append(iterator)
        try:
            line = next(iterator)
        except StopIteration:
            continue
        heap.append((_event_ts(_loads(line)), index, line))
    heapq.heapify(heap)

    seen: dict[tuple, int] = {}
    eviction_queue: deque[tuple[int, tuple]] = deque()
    last_ts = None
    compressor = zstd.ZstdCompressor(level=level)
    with open(output_path, "wb") as dst:
        with compressor.stream_writer(dst, closefd=False) as writer:
            while heap:
                timestamp, index, line = heapq.heappop(heap)
                try:
                    nxt = next(iterators[index])
                    heapq.heappush(heap, (_event_ts(_loads(nxt)), index, nxt))
                except StopIteration:
                    pass

                stats["events_read"] += 1
                event = _loads(line)
                if last_ts is not None and timestamp < last_ts:
                    stats["ordering_violations"] += 1
                last_ts = timestamp

                cutoff = timestamp - dedupe_window_ms
                while eviction_queue and eviction_queue[0][0] < cutoff:
                    _, stale_key = eviction_queue.popleft()
                    seen.pop(stale_key, None)

                key = _event_key(event)
                if key in seen:
                    stats["duplicates_dropped"] += 1
                    continue
                seen[key] = timestamp
                eviction_queue.append((timestamp, key))

                writer.write(_dumps(event))
                writer.write(b"\n")
                stats["events_written"] += 1
                if stats["first_timestamp_ms"] is None:
                    stats["first_timestamp_ms"] = timestamp
                stats["last_timestamp_ms"] = timestamp

    return stats


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Stream-filter replay archives to a target mint universe."
    )
    parser.add_argument(
        "--replay-dir",
        type=Path,
        default=Path("2026-11-08-replay"),
        help="Directory containing hourly *.jsonl.zst archives.",
    )
    parser.add_argument(
        "--mints",
        type=Path,
        default=Path("output/backtest/signals/target_mints.txt"),
        help="Newline-separated mint list.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output/backtest/replay_filtered"),
        help="Directory for the filtered file, parts, and report.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=min(8, os.cpu_count() or 1),
        help="Parallel archive workers (default: min(8, cpu_count)).",
    )
    parser.add_argument(
        "--limit-files",
        type=int,
        default=0,
        help="Debug: only process the first N archives (0 = all).",
    )
    parser.add_argument(
        "--keep-parts",
        action="store_true",
        help="Keep per-archive part files after the merge.",
    )
    parser.add_argument(
        "--normalize-legacy",
        action="store_true",
        help=(
            "Backfill priceSol/marketCapSol from price/marketCapQuote for "
            "WSOL-quoted events. Needed for pre-2026-11 archives; a no-op on "
            "archives that already carry the SOL-denominated fields."
        ),
    )
    parser.add_argument(
        "--allow-missing-mints",
        type=int,
        default=0,
        help=(
            "Tolerate up to this many target mints with no replay events. "
            "Some venues (for example hookedpad.com) are not covered by the "
            "feed, so a signal mints file can contain addresses the replay "
            "never mentions. Missing mints are always listed in the report."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if not args.mints.exists():
        raise SystemExit(f"Mint list not found: {args.mints}")
    targets = [line.strip() for line in args.mints.read_text().splitlines() if line.strip()]
    if not targets:
        raise SystemExit(f"Mint list is empty: {args.mints}")
    if len(set(targets)) != len(targets):
        raise SystemExit(f"Mint list contains duplicates: {args.mints}")

    archives = sorted(args.replay_dir.glob("*.jsonl.zst"))
    if not archives:
        raise SystemExit(f"No *.jsonl.zst archives found in {args.replay_dir}")
    if args.limit_files:
        archives = archives[: args.limit_files]

    args.output_dir.mkdir(parents=True, exist_ok=True)
    parts_dir = args.output_dir / "parts"
    if parts_dir.exists():
        for stale in parts_dir.glob("*.part.jsonl.zst"):
            stale.unlink()
    parts_dir.mkdir(parents=True, exist_ok=True)

    jobs = [
        (str(path), str(parts_dir / f"{path.name}.part.jsonl.zst"), targets)
        for path in archives
    ]

    started = time.time()
    file_stats = []
    workers = max(1, min(args.workers, len(jobs)))
    print(
        f"[filter] {len(jobs)} archives, {len(targets)} target mints, "
        f"{workers} workers",
        file=sys.stderr,
    )
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(filter_archive, job, normalize_legacy=args.normalize_legacy): job
            for job in jobs
        }
        for future in as_completed(futures):
            stat = future.result()
            file_stats.append(stat)
            print(
                f"[filter] {Path(stat['source']).name}: "
                f"{stat['events_kept']:,} kept / {stat['lines_scanned']:,} scanned "
                f"({stat['elapsed_seconds']}s)",
                file=sys.stderr,
            )

    file_stats.sort(key=lambda item: item["source"])
    part_paths = [stat["part"] for stat in file_stats]

    output_path = args.output_dir / "target_mints_events.jsonl.zst"
    print(f"[merge] {len(part_paths)} parts -> {output_path}", file=sys.stderr)
    merge_stats = merge_parts(part_paths, str(output_path))

    if not args.keep_parts:
        for path in part_paths:
            Path(path).unlink(missing_ok=True)
        try:
            parts_dir.rmdir()
        except OSError:
            pass

    action_counts: Counter = Counter()
    for stat in file_stats:
        action_counts.update(stat["action_counts"])

    mint_counts: Counter = Counter()
    for line in _iter_part(str(output_path)):
        mint_counts[_loads(line).get("mint")] += 1

    missing = sorted(set(targets) - set(mint_counts))

    report = {
        "target_mints": len(targets),
        "replay_dir": str(args.replay_dir),
        "archives_processed": [Path(stat["source"]).name for stat in file_stats],
        "archive_count": len(file_stats),
        "total_lines_scanned": sum(stat["lines_scanned"] for stat in file_stats),
        "total_events_kept": sum(stat["events_kept"] for stat in file_stats),
        "bad_json": sum(stat["bad_json"] for stat in file_stats),
        "action_counts": dict(action_counts.most_common()),
        "mints_with_events": len(mint_counts),
        "mints_without_events": len(missing),
        "mints_missing": missing,
        "allowed_missing_mints": args.allow_missing_mints,
        "events_per_mint": {
            "min": min(mint_counts.values()) if mint_counts else 0,
            "max": max(mint_counts.values()) if mint_counts else 0,
            "mean": round(sum(mint_counts.values()) / len(mint_counts), 1)
            if mint_counts
            else 0,
        },
        "merge": merge_stats,
        "window_utc": {
            "first": _utc(merge_stats["first_timestamp_ms"]),
            "last": _utc(merge_stats["last_timestamp_ms"]),
        },
        "output": {
            "path": str(output_path),
            "bytes": output_path.stat().st_size,
            "compression": "zstd level 3",
        },
        "field_policy": {
            "actions_kept": sorted(KEEP_ACTIONS),
            "fields_kept": list(KEEP_FIELDS),
            "balance_maps": (
                "preBalances/postBalances retained only for events that trigger "
                "raw_to_clean_ohlcv participant balance audit"
            ),
            "normalize_legacy": bool(args.normalize_legacy),
            "legacy_events_normalized": sum(
                stat["legacy_events_normalized"] for stat in file_stats
            ),
        },
        "elapsed_seconds": round(time.time() - started, 1),
        "passed": (
            merge_stats["ordering_violations"] == 0
            and merge_stats["events_written"] > 0
            and (
                bool(args.limit_files)
                or len(missing) <= args.allow_missing_mints
            )
        ),
    }

    report_path = args.output_dir / "replay_filter_report.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(json.dumps(report, indent=2))
    if not report["passed"]:
        print("[filter] validation failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
