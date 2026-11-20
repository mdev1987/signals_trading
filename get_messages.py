"""Fetch Telegram channel messages and export PumpAPI signal CSVs.

Requires tgdata credentials in config.ini. Does not download replay archives.
"""
from __future__ import annotations
import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path
from tgdata import TgData

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_CHANNELS = ["@Riley_Signals", "@migratedpumpfun", "@AveSignalMonitor", "@WizzyTrades", "@crawlca"]
DEFAULT_START_DATE = "2026-11-08T04:00:00+00:00"
DEFAULT_END_DATE = "2026-11-09T12:00:00+00:00"
DEFAULT_LIMIT = 5000


def parse_datetime(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_optional_datetime(value: str | None) -> datetime | None:
    if value is None or value.strip().lower() in {"", "none", "null"}:
        return None
    return parse_datetime(value)


def load_channels(channels_file: Path) -> list[str]:
    if channels_file.exists():
        names = [line.strip() for line in channels_file.read_text(encoding="utf-8").splitlines()
                 if line.strip() and not line.lstrip().startswith("#")]
        # An existing but empty file means "fetch nothing", not "fetch everything".
        return list(dict.fromkeys(n if n.startswith("@") else "@" + n for n in names))
    return DEFAULT_CHANNELS.copy()


async def fetch_channel_messages(tg: TgData, channel_name: str, start_date: datetime,
                                 end_date: datetime | None, limit: int):
    return await tg.get_messages(group_id=channel_name, start_date=start_date,
                                 end_date=end_date, limit=limit,
                                 with_progress=sys.stdout.isatty())


async def run(channels: list[str], start_date: datetime, end_date: datetime | None,
              limit: int, out_dir: Path, config: Path) -> None:
    if not config.exists():
        raise FileNotFoundError(f"Telegram config not found: {config}")
    out_dir.mkdir(parents=True, exist_ok=True)
    tg = TgData(str(config))
    for channel_name in channels:
        messages = await fetch_channel_messages(tg, channel_name, start_date, end_date, limit)
        safe_name = channel_name.lstrip("@").lower().replace("/", "_")
        out_path = out_dir / f"@{safe_name}-messages.csv"
        tg.export_messages(messages, str(out_path))
        print(f"Exported {len(messages)} messages for {channel_name} -> {out_path}", flush=True)
        if len(messages) >= limit:
            print(f"WARNING: {channel_name} reached --limit {limit}; messages after the "
                  f"last exported one are missing. Raise --limit or narrow the date range.",
                  flush=True)
        if len(messages) and "Date" in messages:
            print(f"  date range: {messages['Date'].min()} -> {messages['Date'].max()}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch Telegram channel messages")
    parser.add_argument("--channels-file", default=str(PROJECT_ROOT / "channels.txt"))
    parser.add_argument("--channel", action="append", default=None)
    parser.add_argument("--start-date", default=DEFAULT_START_DATE,
                        help="ISO-8601 start timestamp (default UTC)")
    parser.add_argument("--end-date", default=DEFAULT_END_DATE,
                        help="ISO-8601 end timestamp, or 'none' for no upper bound "
                             f"(default {DEFAULT_END_DATE})")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                        help=f"Maximum messages per channel (default {DEFAULT_LIMIT})")
    parser.add_argument("--out-dir", default=str(PROJECT_ROOT / "messages"))
    parser.add_argument("--config", default=str(PROJECT_ROOT / "config.ini"))
    args = parser.parse_args()
    if args.limit <= 0:
        parser.error("--limit must be positive")
    start_date = parse_datetime(args.start_date)
    end_date = parse_optional_datetime(args.end_date)
    if end_date is not None and end_date <= start_date:
        parser.error("--end-date must be after --start-date")
    channels = args.channel or load_channels(Path(args.channels_file))
    if not channels:
        parser.error("No channels configured")
    channels = list(dict.fromkeys(c if c.startswith("@") else "@" + c for c in channels))
    asyncio.run(run(channels, start_date, end_date, args.limit,
                    Path(args.out_dir), Path(args.config)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
