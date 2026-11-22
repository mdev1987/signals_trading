#!/usr/bin/env python3
"""Check that the configured Telegram channels are resolvable and readable.

``fetch_signals.py`` reports how many messages a day-window yielded, but zero
is ambiguous: the handle may be wrong, the account may not be a member, the
channel may hide its history, or the window may genuinely be quiet. This tool
separates those cases without downloading a day of history:

- resolves each handle and prints its type, id, username and subscriber count;
- fetches the newest few posts and prints their dates;
- checks whether the channel is in the account's dialog list (that is the
  membership signal ``iter_messages`` needs for channels that hide history).

Examples:
    python src/probe_channels.py
    python src/probe_channels.py @SOLTRENDING --limit 10
    python src/probe_channels.py --dialogs sol            # find the real handle
"""

from __future__ import annotations

import argparse
import asyncio
import warnings
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_channels(path: Path) -> list[str]:
    names = [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    return list(dict.fromkeys(n if n.startswith("@") else "@" + n for n in names))


async def _dialogs_frame(tg, channels: list[str]) -> pd.DataFrame:
    """Return the dialog rows for the probed channels, matched case-insensitively."""
    frame = await tg.list_groups()
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        return frame if isinstance(frame, pd.DataFrame) else pd.DataFrame()
    wanted = {name.lstrip("@").lower() for name in channels}
    if "Username" not in frame.columns:
        return frame.iloc[0:0]
    usernames = frame["Username"].fillna("").astype(str).str.lstrip("@").str.lower()
    return frame[usernames.isin(wanted)]


def _describe(messages: pd.DataFrame) -> tuple[str, str | None, str | None]:
    """Return ``(summary, newest, oldest)`` for a newest-first message frame."""
    if messages is None or len(messages) == 0:
        return "none", None, None
    dates = pd.to_datetime(messages["Date"], utc=True, errors="coerce").dropna()
    if dates.empty:
        return f"{len(messages)} rows (no usable dates)", None, None
    return f"{len(messages)} rows", dates.max().isoformat(), dates.min().isoformat()


async def run(args: argparse.Namespace) -> int:
    config = Path(args.config)
    if not config.exists():
        raise SystemExit(f"Telegram config not found: {config}")
    handles = list(args.handles) or load_channels(Path(args.channels_file))
    if not handles:
        raise SystemExit("No channels given and channels.txt is empty")
    handles = [h if h.startswith("@") else "@" + h for h in handles]

    from tgdata import TgData

    # tgdata is noisy about its own bookkeeping; keep the probe readable.
    warnings.filterwarnings("ignore")
    tg = TgData(str(config))
    try:
        ok = await tg.validate_connection()
        print(f"connection: {'ok' if ok else 'FAILED'}")
        if not ok:
            return 1

        if args.dialogs is not None:
            frame = await tg.list_groups()
            if not isinstance(frame, pd.DataFrame) or frame.empty:
                print("dialogs: none visible to this account")
            else:
                needle = args.dialogs.lstrip("@").lower()
                match = frame[
                    frame.astype(str).apply(
                        lambda col: col.str.lower().str.contains(needle, regex=False)
                    ).any(axis=1)
                ]
                print(f"dialogs matching {args.dialogs!r}: {len(match)}")
                for _, row in match.iterrows():
                    print(
                        f"  {row.get('Identifier')}  {row.get('Title')!r}  "
                        f"channel={row.get('IsChannel')} participants={row.get('ParticipantsCount')}"
                    )

        dialogs = await _dialogs_frame(tg, handles)
        in_dialogs = (
            set(dialogs["Username"].fillna("").astype(str).str.lstrip("@").str.lower())
            if not dialogs.empty
            else set()
        )

        failures = 0
        for handle in handles:
            print(f"\n== {handle}")
            try:
                messages = await tg.get_messages(group_id=handle, limit=args.limit)
            except Exception as exc:  # noqa: BLE001 - one bad handle is not fatal
                print(f"  read failed: {type(exc).__name__}: {exc}")
                failures += 1
                continue
            summary, newest, oldest = _describe(messages)
            member = handle.lstrip("@").lower() in in_dialogs
            print(f"  newest posts: {summary}")
            if newest:
                print(f"  latest: {newest}")
            print(f"  in dialogs (member): {'yes' if member else 'no'}")
            if summary == "none":
                if not member:
                    print(
                        "  verdict: resolved but no visible history — the account is "
                        "not subscribed. Join the channel in Telegram, then re-run."
                    )
                else:
                    print(
                        "  verdict: subscribed but no readable posts — the channel may "
                        "hide history from this account or post nothing but media."
                    )
                failures += 1
            else:
                print("  verdict: readable")
        return 1 if failures else 0
    finally:
        close = getattr(tg, "close", None)
        if close is not None:
            result = close()
            if asyncio.iscoroutine(result):
                await result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("handles", nargs="*", help="Handles to probe (default: channels.txt).")
    parser.add_argument("--channels-file", default=str(PROJECT_ROOT / "channels.txt"))
    parser.add_argument("--config", default=str(PROJECT_ROOT / "config.ini"))
    parser.add_argument("--limit", type=int, default=5, help="Newest posts to read (default 5).")
    parser.add_argument(
        "--dialogs",
        nargs="?",
        const="",
        default=None,
        help="List the account's dialogs matching this substring (no argument lists all).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.limit <= 0:
        raise SystemExit("--limit must be positive")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
