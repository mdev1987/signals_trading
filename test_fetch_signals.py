from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "tools" / "backtest"))

import fetch_signals  # noqa: E402

WSOL_MINT = "BKc7Rx2yxzT7eFJq9gKxFuC6JuGmKAhoaoRMNgcgpump"


def write_archive(path: Path, lines: int = 1) -> None:
    import zstandard as zstd

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as target:
        with zstd.ZstdCompressor(level=1).stream_writer(target, closefd=False) as writer:
            for index in range(lines):
                writer.write(b'{"action":"buy","mint":"mint-a"}\n')


def write_messages(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def row(message: str, date: str = "2026-10-03 04:00:00+00:00") -> dict:
    return {"MessageId": "1", "Username": "@AveSignalMonitor", "Message": message, "Date": date}


class FetchSignalsTests(unittest.TestCase):
    def test_replay_window_from_hour_only_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            day = Path(directory) / "2026-10-03"
            write_archive(day / "00.jsonl.zst")
            write_archive(day / "23.jsonl.zst")

            start, end = fetch_signals.replay_window(day)

            self.assertEqual(start, datetime(2026, 10, 3, 0, tzinfo=timezone.utc))
            self.assertEqual(end, datetime(2026, 10, 4, 0, tzinfo=timezone.utc))

    def test_replay_window_from_dated_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            day = Path(directory) / "2026-11-08"
            write_archive(day / "20261108_04.jsonl.zst")
            write_archive(day / "20261109_11.jsonl.zst")

            start, end = fetch_signals.replay_window(day)

            self.assertEqual(start, datetime(2026, 11, 8, 4, tzinfo=timezone.utc))
            self.assertEqual(end, datetime(2026, 11, 9, 12, tzinfo=timezone.utc))

    def test_replay_window_rejects_empty_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                fetch_signals.replay_window(Path(directory))

    def test_load_channels_ignores_comments_and_blanks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "channels.txt"
            path.write_text(
                "# comment\n\n@AveSignalMonitor\nRiley_Signals\n@AveSignalMonitor\n"
            )

            self.assertEqual(
                fetch_signals.load_channels(path),
                ["@AveSignalMonitor", "@Riley_Signals"],
            )

    def test_discover_days_skips_empty_and_respects_filter(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_archive(root / "2026-10-03" / "00.jsonl.zst")
            write_archive(root / "2026-10-04" / "00.jsonl.zst")
            (root / "2026-10-05").mkdir()

            self.assertEqual(
                [name for name, _ in fetch_signals.discover_days(root, None)],
                ["2026-10-03", "2026-10-04"],
            )
            self.assertEqual(
                [name for name, _ in fetch_signals.discover_days(root, ["2026-10-04"])],
                ["2026-10-04"],
            )

    def test_summarise_messages_counts_chains_and_cas(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "messages.csv"
            write_messages(
                path,
                [
                    row(
                        f"🔗 solana\nCA: {WSOL_MINT}\n🔢 2nd Vibe Buy Signal",
                        "2026-10-03 04:00:00+00:00",
                    ),
                    row(
                        "🔗 solana\nCA: not-an-address\n🔢 2nd Vibe Buy Signal",
                        "2026-10-03 04:05:00+00:00",
                    ),
                    row(
                        "🔗 bsc\nCA: 0xabc\n🔢 2nd Vibe Buy Signal",
                        "2026-10-03 04:10:00+00:00",
                    ),
                    row("chatter", "2026-10-03 04:15:00+00:00"),
                ],
            )

            summary = fetch_signals.summarise_messages(path)

            self.assertEqual(summary["messages"], 4)
            self.assertEqual(summary["signal_messages"], 3)
            self.assertEqual(summary["chains"], {"solana": 2, "bsc": 1})
            self.assertEqual(summary["solana_ca_rows"], 1)
            self.assertEqual(summary["invalid_solana_ca"], {"expected_one_CA_found_0": 1})
            self.assertEqual(
                summary["date_range"],
                ["2026-10-03 04:00:00+00:00", "2026-10-03 04:15:00+00:00"],
            )

    def test_dry_run_prints_windows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_archive(root / "replay" / "2026-10-03" / "12.jsonl.zst")
            channels = root / "channels.txt"
            channels.write_text("@AveSignalMonitor\n")

            exit_code = fetch_signals.main(
                [
                    "--replay-root",
                    str(root / "replay"),
                    "--channels-file",
                    str(channels),
                    "--dry-run",
                ]
            )

            self.assertEqual(exit_code, 0)


if __name__ == "__main__":
    unittest.main()
