#!/usr/bin/env python3
"""Fetch Telegram signal-channel history for every replay day.

The replay corpus on disk has three clusters (2026-08-09, 2026-10-03..05 and
2026-11-08..09). Each cluster needs its own signal export, because
``prepare_signal_inputs.py`` only ever sees the messages for one window.

This tool derives the replay window from the archive filenames, fetches one
lookback hour of extra history (so signals just before the window are visible
and can be reported as out-of-window rather than silently missing), and writes
one CSV per channel per day:

    messages/<day>/@<channel>-messages.csv
    messages/<day>/fetch_report.json

The filename layout is idempotent: a day that already has a non-empty export is
skipped unless ``--force`` is passed.

Examples:
    python src/fetch_signals.py --dry-run
    python src/fetch_signals.py --days 2026-10-03
    python src/fetch_signals.py --force --days 2026-08-09 2026-10-03
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Mirrors the parsing rules in prepare_signal_inputs.py. Kept here so the fetch
# report can never disagree with what ingestion will later accept.
SIGNAL_TEXT = "2nd Vibe Buy Signal"
SOLANA_RE = re.compile(r"(?im)^\s*🔗\s*solana\s*$")
BSC_RE = re.compile(r"(?im)^\s*🔗\s*bsc\s*$")
ROBINHOOD_RE = re.compile(r"(?im)^\s*🔗\s*robinhood\s*$")
CA_RE = re.compile(r"(?im)^CA:\s*([1-9A-HJ-NP-Za-km-z]{32,44})\s*$")

ARCHIVE_RE = re.compile(r"^(?:(\d{8})_)?(\d{2})\.jsonl\.zst$")


def load_channels(channels_file: Path) -> list[str]:
    if not channels_file.exists():
        raise FileNotFoundError(f"Channels file not found: {channels_file}")
    names = [
        line.strip()
        for line in channels_file.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not names:
        raise ValueError(f"No channels configured in {channels_file}")
    return list(dict.fromkeys(n if n.startswith("@") else "@" + n for n in names))


def replay_window(day_dir: Path) -> tuple[datetime, datetime]:
    """Return [first archive hour, last archive hour + 1h) for one replay day."""
    hours = []
    for path in day_dir.iterdir():
        match = ARCHIVE_RE.match(path.name)
        if not match:
            continue
        date_text, hour_text = match.groups()
        date = date_text or day_dir.name.replace("-", "")
        try:
            start = datetime.strptime(date + hour_text, "%Y%m%d%H").replace(tzinfo=timezone.utc)
        except ValueError as exc:
            raise ValueError(f"Cannot parse archive hour from {path.name}") from exc
        hours.append(start)
    if not hours:
        raise ValueError(f"No *.jsonl.zst archives found in {day_dir}")
    return min(hours), max(hours) + timedelta(hours=1)


def discover_days(replay_root: Path, only: list[str] | None) -> list[tuple[str, Path]]:
    days = []
    for child in sorted(replay_root.iterdir()):
        if not child.is_dir():
            continue
        if only and child.name not in only:
            continue
        if not any(child.glob("*.jsonl.zst")):
            continue
        days.append((child.name, child))
    if not days:
        raise ValueError(f"No replay days found under {replay_root}")
    return days


def summarise_messages(path: Path) -> dict:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    chains: Counter[str] = Counter()
    signals = 0
    valid_ca = 0
    invalid: Counter[str] = Counter()
    for row in rows:
        message = row.get("Message") or ""
        if SIGNAL_TEXT not in message:
            continue
        signals += 1
        if SOLANA_RE.search(message):
            chain = "solana"
        elif BSC_RE.search(message):
            chain = "bsc"
        elif ROBINHOOD_RE.search(message):
            chain = "robinhood"
        else:
            chain = "unknown"
        chains[chain] += 1
        if chain != "solana":
            continue
        matches = CA_RE.findall(message)
        if len(matches) == 1:
            valid_ca += 1
        else:
            invalid[f"expected_one_CA_found_{len(matches)}"] += 1
    dates = [row.get("Date") for row in rows if row.get("Date")]
    return {
        "messages": len(rows),
        "signal_messages": signals,
        "chains": dict(chains),
        "solana_ca_rows": valid_ca,
        "invalid_solana_ca": dict(invalid),
        "date_range": [min(dates), max(dates)] if dates else None,
    }


async def fetch_day(
    channels: list[str],
    day: str,
    day_dir: Path,
    out_dir: Path,
    config: Path,
    lookback: timedelta,
    limit: int,
    force: bool,
) -> dict:
    window_start, window_end = replay_window(day_dir)
    fetch_start = window_start - lookback
    out_dir.mkdir(parents=True, exist_ok=True)

    report: dict = {
        "day": day,
        "replay_window_utc": {
            "start": window_start.isoformat(),
            "end_exclusive": window_end.isoformat(),
        },
        "fetch_window_utc": {
            "start": fetch_start.isoformat(),
            "end_exclusive": window_end.isoformat(),
        },
        "channels": {},
    }

    from tgdata import TgData

    tg = TgData(str(config))
    try:
        for channel in channels:
            safe_name = channel.lstrip("@").lower().replace("/", "_")
            out_path = out_dir / f"@{safe_name}-messages.csv"
            existing = out_path.exists() and out_path.stat().st_size > 0
            entry: dict = {"path": str(out_path), "fetched": False}
            if existing and not force:
                entry["skipped"] = "existing_export"
            else:
                messages = await tg.get_messages(
                    group_id=channel,
                    start_date=fetch_start,
                    end_date=window_end,
                    limit=limit,
                    with_progress=sys.stdout.isatty(),
                )
                count = 0 if messages is None else len(messages)
                if count >= limit:
                    raise RuntimeError(
                        f"{channel} returned {count} messages, hitting --limit {limit} "
                        f"for {day}; narrow the window or raise the limit before trusting "
                        "this export."
                    )
                tg.export_messages(messages, str(out_path))
                entry["fetched"] = True
                entry["messages"] = count
            entry["summary"] = summarise_messages(out_path)
            report["channels"][channel] = entry
    finally:
        close = getattr(tg, "close", None)
        if close is not None:
            result = close()
            if asyncio.iscoroutine(result):
                await result

    total = sum(c["summary"]["solana_ca_rows"] for c in report["channels"].values())
    report["solana_ca_rows"] = total
    report["passed"] = total > 0
    (out_dir / "fetch_report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    if not report["passed"]:
        raise RuntimeError(f"No valid Solana signal rows fetched for {day}")
    return report


async def run(args: argparse.Namespace) -> int:
    replay_root = Path(args.replay_root)
    days = discover_days(replay_root, args.days or None)
    channels = load_channels(Path(args.channels_file))
    config = Path(args.config)

    print(f"replay root : {replay_root}")
    print(f"days        : {', '.join(name for name, _ in days)}")
    print(f"channels    : {', '.join(channels)}")
    if args.dry_run:
        for name, day_dir in days:
            start, end = replay_window(day_dir)
            print(f"  {name}: {start.isoformat()} -> {end.isoformat()}")
        return 0

    if not config.exists():
        raise FileNotFoundError(f"Telegram config not found: {config}")

    for name, day_dir in days:
        report = await fetch_day(
            channels=channels,
            day=name,
            day_dir=day_dir,
            out_dir=Path(args.out_root) / name,
            config=config,
            lookback=timedelta(minutes=args.lookback_minutes),
            limit=args.limit,
            force=args.force,
        )
        summary = report["channels"][channels[0]]["summary"]
        print(
            f"{name}: {summary['messages']} messages, "
            f"{summary['signal_messages']} signal messages, "
            f"{summary['solana_ca_rows']} solana CAs, "
            f"{summary['date_range']}"
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--replay-root", default=str(PROJECT_ROOT / "replay"))
    parser.add_argument("--days", nargs="*", default=None, help="Limit to these day directory names.")
    parser.add_argument("--channels-file", default=str(PROJECT_ROOT / "channels.txt"))
    parser.add_argument("--out-root", default=str(PROJECT_ROOT / "messages"))
    parser.add_argument("--config", default=str(PROJECT_ROOT / "config.ini"))
    parser.add_argument("--lookback-minutes", type=int, default=60,
                        help="Extra history fetched before the replay window (default 60).")
    parser.add_argument("--limit", type=int, default=20000,
                        help="Maximum messages per channel per day; exceeding it is an error.")
    parser.add_argument("--force", action="store_true", help="Re-fetch days that already have exports.")
    parser.add_argument("--dry-run", action="store_true", help="Print the derived windows and exit.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.lookback_minutes < 0:
        raise SystemExit("--lookback-minutes must be non-negative")
    if args.limit <= 0:
        raise SystemExit("--limit must be positive")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
