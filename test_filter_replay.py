from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

import zstandard as zstd

sys.path.insert(0, str(Path(__file__).parent / "tools" / "backtest"))

from filter_replay import (  # noqa: E402
    KEEP_ACTIONS,
    filter_archive,
    main,
    merge_parts,
)

WSOL = "So11111111111111111111111111111111111111112"


def write_archive(path: Path, events: list[dict]) -> None:
    compressor = zstd.ZstdCompressor(level=3)
    with path.open("wb") as target:
        with compressor.stream_writer(target, closefd=False) as writer:
            for event in events:
                writer.write(json.dumps(event).encode("utf-8"))
                writer.write(b"\n")


def read_archive(path: Path) -> list[dict]:
    with path.open("rb") as source:
        reader = zstd.ZstdDecompressor().stream_reader(
            source, read_across_frames=True
        )
        return [json.loads(line) for line in io.BufferedReader(reader) if line.strip()]


def trade(
    mint: str,
    action: str = "buy",
    timestamp: int = 1_000,
    *,
    breakdown: list[dict] | None = None,
    **extra,
) -> dict:
    event = {
        "signature": f"sig-{mint}-{action}-{timestamp}",
        "action": action,
        "poolId": "pool-1",
        "mint": mint,
        "quoteMint": WSOL,
        "pool": "pump-amm",
        "timestamp": timestamp,
        "tokenAmount": 10.0,
        "quoteAmount": 0.1,
        "priceSol": 0.01,
        "tokensInPool": 1000.0,
        "quoteInPool": 10.0,
        "virtualTokensInPool": -5.0,
        "virtualQuoteInPool": 0.0,
        "poolFeeRate": 0.003,
        "decimals": 6,
        "supply": 1_000_000.0,
        "preBalances": {"trader": {"sol": 1.0, WSOL: 0.0}},
        "postBalances": {"trader": {"sol": 0.9, WSOL: 0.0}},
    }
    if breakdown is not None:
        event["breakdown"] = breakdown
    event.update(extra)
    return event


class FilterReplayTests(unittest.TestCase):
    def test_keeps_target_mints_and_reserve_actions_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "20261108_04.jsonl.zst"
            part = root / "part.jsonl.zst"
            write_archive(
                source,
                [
                    trade("mint-target", timestamp=1_000),
                    trade("mint-other", timestamp=1_001),
                    trade("mint-target", action="transfer", timestamp=1_002),
                    trade("mint-target", action="create", timestamp=1_003),
                    trade("mint-target", action="migrate", timestamp=1_004),
                    trade("mint-target", action="add", timestamp=1_005),
                    trade("mint-target", action="remove", timestamp=1_006),
                    trade("mint-target", action="distributeFees", timestamp=1_007),
                ],
            )

            stats = filter_archive((str(source), str(part), ["mint-target"]))
            kept = read_archive(part)

            self.assertEqual(stats["events_kept"], 5)
            self.assertEqual(stats["lines_scanned"], 8)
            self.assertEqual(
                {event["action"] for event in kept},
                {"buy", "create", "migrate", "add", "remove"},
            )
            self.assertTrue(
                {event["action"] for event in kept}.issubset(KEEP_ACTIONS)
            )
            self.assertEqual({event["mint"] for event in kept}, {"mint-target"})

    def test_preserves_both_reserve_styles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "20261108_04.jsonl.zst"
            part = root / "part.jsonl.zst"
            write_archive(
                source,
                [
                    trade("mint-a", pool="pump-amm"),
                    trade(
                        "mint-b",
                        pool="pump",
                        vTokensInBondingCurve=795_874_627.0,
                        vQuoteInBondingCurve=0.6117,
                    ),
                ],
            )

            filter_archive((str(source), str(part), ["mint-a", "mint-b"]))
            kept = {event["mint"]: event for event in read_archive(part)}

            self.assertEqual(kept["mint-a"]["virtualTokensInPool"], -5.0)
            self.assertEqual(kept["mint-b"]["vTokensInBondingCurve"], 795_874_627.0)
            self.assertEqual(kept["mint-b"]["vQuoteInBondingCurve"], 0.6117)

    def test_balance_maps_only_kept_when_audit_needs_them(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "20261108_04.jsonl.zst"
            part = root / "part.jsonl.zst"
            components = [
                {"action": "buy", "trader": "a", "tokenAmount": 5.0, "quoteAmount": 0.05},
                {"action": "sell", "trader": "b", "tokenAmount": 1.0, "quoteAmount": 0.01},
            ]
            write_archive(
                source,
                [
                    trade("mint-a", timestamp=1_000),
                    trade("mint-a", timestamp=1_001, breakdown=components),
                ],
            )

            filter_archive((str(source), str(part), ["mint-a"]))
            kept = sorted(read_archive(part), key=lambda event: event["timestamp"])

            self.assertNotIn("preBalances", kept[0])
            self.assertNotIn("postBalances", kept[0])
            self.assertIn("preBalances", kept[1])
            self.assertIn("postBalances", kept[1])
            self.assertEqual(len(kept[1]["breakdown"]), 2)

    def test_merge_orders_and_deduplicates_boundary_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shared = trade("mint-a", timestamp=2_000)
            part_one = root / "one.jsonl.zst"
            part_two = root / "two.jsonl.zst"
            write_archive(part_one, [trade("mint-a", timestamp=1_000), shared])
            write_archive(part_two, [shared, trade("mint-a", timestamp=3_000)])
            output = root / "merged.jsonl.zst"

            stats = merge_parts([str(part_one), str(part_two)], str(output))
            merged = read_archive(output)

            self.assertEqual(stats["events_read"], 4)
            self.assertEqual(stats["events_written"], 3)
            self.assertEqual(stats["duplicates_dropped"], 1)
            self.assertEqual(stats["ordering_violations"], 0)
            self.assertEqual(
                [event["timestamp"] for event in merged], [1_000, 2_000, 3_000]
            )

    def test_merge_keeps_distinct_events_with_same_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            part_one = root / "one.jsonl.zst"
            part_two = root / "two.jsonl.zst"
            write_archive(part_one, [trade("mint-a", timestamp=1_000)])
            write_archive(part_two, [trade("mint-b", timestamp=1_000)])
            output = root / "merged.jsonl.zst"

            stats = merge_parts([str(part_one), str(part_two)], str(output))

            self.assertEqual(stats["duplicates_dropped"], 0)
            self.assertEqual(stats["events_written"], 2)
            self.assertEqual(stats["ordering_violations"], 0)

    def test_main_writes_report_and_returns_zero(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            replay_dir = root / "replay"
            replay_dir.mkdir()
            write_archive(
                replay_dir / "20261108_04.jsonl.zst",
                [trade("mint-a", timestamp=1_000), trade("mint-other", timestamp=1_001)],
            )
            write_archive(
                replay_dir / "20261108_05.jsonl.zst",
                [trade("mint-b", timestamp=3_600_000)],
            )
            mints = root / "mints.txt"
            mints.write_text("mint-a\nmint-b\n")
            output_dir = root / "out"

            exit_code = main(
                [
                    "--replay-dir",
                    str(replay_dir),
                    "--mints",
                    str(mints),
                    "--output-dir",
                    str(output_dir),
                    "--workers",
                    "1",
                ]
            )

            self.assertEqual(exit_code, 0)
            report = json.loads(
                (output_dir / "replay_filter_report.json").read_text()
            )
            self.assertTrue(report["passed"])
            self.assertEqual(report["mints_with_events"], 2)
            self.assertEqual(report["mints_without_events"], 0)
            self.assertEqual(report["merge"]["events_written"], 2)
            self.assertTrue((output_dir / "target_mints_events.jsonl.zst").exists())

    def test_main_fails_when_a_target_mint_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            replay_dir = root / "replay"
            replay_dir.mkdir()
            write_archive(
                replay_dir / "20261108_04.jsonl.zst",
                [trade("mint-a", timestamp=1_000)],
            )
            mints = root / "mints.txt"
            mints.write_text("mint-a\nmint-absent\n")
            output_dir = root / "out"

            exit_code = main(
                [
                    "--replay-dir",
                    str(replay_dir),
                    "--mints",
                    str(mints),
                    "--output-dir",
                    str(output_dir),
                    "--workers",
                    "1",
                ]
            )

            self.assertEqual(exit_code, 1)
            report = json.loads(
                (output_dir / "replay_filter_report.json").read_text()
            )
            self.assertFalse(report["passed"])
            self.assertEqual(report["mints_missing"], ["mint-absent"])


if __name__ == "__main__":
    unittest.main()
