from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd
import zstandard as zstd

SRC_DIR = Path(__file__).resolve().parents[1]
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from build_backtest_dataset import classify_wsol_routability  # noqa: E402

WSOL = "So11111111111111111111111111111111111111112"


def write_events(path: Path, events: list[dict]) -> None:
    with path.open("wb") as target:
        with zstd.ZstdCompressor(level=1).stream_writer(target, closefd=False) as writer:
            for event in events:
                writer.write(json.dumps(event).encode("utf-8"))
                writer.write(b"\n")


def event(mint: str, quote_mint: str, action: str = "buy") -> dict:
    return {
        "action": action,
        "mint": mint,
        "quoteMint": quote_mint,
        "timestamp": 1_000,
    }


def signals(mints: list[str]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "signal_id": [f"telegram:{index}" for index in range(len(mints))],
            "signal_timestamp_ms": [1_000 for _ in mints],
            "signal_timestamp_utc": ["2026-10-03 04:00:00+00:00" for _ in mints],
            "mint": mints,
        }
    )


class ClassifyWsolRoutabilityTests(unittest.TestCase):
    def test_marks_mints_with_wsol_trades_and_explains_others(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl.zst"
            write_events(
                path,
                [
                    event("mint-wsol", WSOL),
                    event("mint-other", "Xsc9qvGR1efVDFGLrVsmkzv3qi45LTBjeUKSPmx9qEh"),
                ],
            )

            routing = classify_wsol_routability(path, signals(["mint-wsol", "mint-other"]))
            by_mint = routing.set_index("mint")

            self.assertEqual(by_mint.loc["mint-wsol", "dataset_routable"], 1)
            self.assertEqual(by_mint.loc["mint-wsol", "reason"], "")
            self.assertEqual(by_mint.loc["mint-other", "dataset_routable"], 0)
            self.assertEqual(
                by_mint.loc["mint-other", "reason"], "no_wsol_quoted_trades"
            )

    def test_lifecycle_actions_do_not_count_as_routable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl.zst"
            write_events(
                path,
                [
                    event("mint-a", WSOL, action="create"),
                    event("mint-a", WSOL, action="migrate"),
                ],
            )

            routing = classify_wsol_routability(path, signals(["mint-a"]))

            self.assertEqual(int(routing["dataset_routable"].sum()), 0)

    def test_missing_mint_is_not_routable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl.zst"
            write_events(path, [event("mint-a", WSOL)])

            routing = classify_wsol_routability(path, signals(["mint-absent"]))

            self.assertEqual(int(routing["dataset_routable"].sum()), 0)
            self.assertEqual(routing.loc[0, "reason"], "no_wsol_quoted_trades")

    def test_mints_absent_from_replay_report_a_distinct_reason(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl.zst"
            write_events(path, [event("mint-a", WSOL)])

            routing = classify_wsol_routability(
                path, signals(["mint-a", "mint-hookedpad"]),
                missing_mints={"mint-hookedpad"},
            )
            by_mint = routing.set_index("mint")

            self.assertEqual(by_mint.loc["mint-a", "dataset_routable"], 1)
            self.assertEqual(by_mint.loc["mint-hookedpad", "reason"], "no_replay_events")

    def test_signal_after_the_last_wsol_trade_is_not_routable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl.zst"
            write_events(path, [event("mint-a", WSOL)])

            later = signals(["mint-a"]).assign(signal_timestamp_ms=5_000)
            routing = classify_wsol_routability(path, later)

            self.assertEqual(int(routing["dataset_routable"].sum()), 0)
            self.assertEqual(
                routing.loc[0, "reason"], "no_wsol_trades_after_signal"
            )

    def test_wsol_trade_after_the_signal_stays_routable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl.zst"
            write_events(path, [event("mint-a", WSOL)])

            earlier = signals(["mint-a"]).assign(signal_timestamp_ms=500)
            routing = classify_wsol_routability(path, earlier)

            self.assertEqual(routing.loc[0, "reason"], "")
            self.assertEqual(int(routing["dataset_routable"].sum()), 1)


if __name__ == "__main__":
    unittest.main()
