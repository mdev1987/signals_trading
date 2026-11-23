"""Focused tests for the scanner-alert pricer.

The scanner evidence is only as good as its fill convention, and the previous
strategy died from a stale fill. These tests pin the two conventions, the
stale-entry filter, and the dust floor default.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parents[1]
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from alert_outcomes import build_frame, main, select_entry, stream_day  # noqa: E402

ALERT_TS = 1_800_000_000_000
LATENCY = 5_000


def target(*, latency: int = LATENCY) -> dict:
    entry_target = ALERT_TS + latency
    return {
        "row": {"mint": "mint", "alert_ts_ms": ALERT_TS},
        "alert_ts": ALERT_TS,
        "entry_target": entry_target,
        "entry_ts": None,
        "entry_px": None,
        "first_trade_ts": None,
        "entry_checked": False,
        "exit_5_target": entry_target + 300_000,
        "exit_5_ts": None,
        "exit_5_px": None,
    }


class EntrySelectionTests(unittest.TestCase):
    def test_last_before_uses_the_previous_print(self) -> None:
        item = target()
        select_entry(item, item["entry_target"] + 1, 2.0, (item["entry_target"], 1.0), "last-before")
        self.assertEqual(item["entry_ts"], item["entry_target"])
        self.assertEqual(item["entry_px"], 1.0)

    def test_last_before_accepts_a_print_with_its_side(self) -> None:
        item = target()
        select_entry(
            item,
            item["entry_target"] + 1,
            2.0,
            (item["entry_target"], 1.0, "buy"),
            "last-before",
        )
        self.assertEqual(item["entry_ts"], item["entry_target"])
        self.assertEqual(item["entry_px"], 1.0)
        self.assertEqual(item["entry_side"], "buy")

    def test_last_before_never_fills_from_a_print_after_the_decision(self) -> None:
        item = target()
        select_entry(item, item["entry_target"] + 1, 2.0, None, "last-before")
        self.assertTrue(item["entry_checked"])
        self.assertIsNone(item["entry_px"])

    def test_first_after_uses_the_first_print_at_or_after(self) -> None:
        item = target()
        select_entry(item, item["entry_target"] - 500, 0.5, (0, 0.5), "first-after")
        self.assertFalse(item["entry_checked"])
        select_entry(item, item["entry_target"], 1.0, None, "first-after")
        self.assertEqual(item["entry_ts"], item["entry_target"])
        self.assertEqual(item["entry_px"], 1.0)

    def test_first_after_keeps_the_earliest_qualifying_print(self) -> None:
        item = target()
        select_entry(item, item["entry_target"] + 800, 1.1, None, "first-after")
        select_entry(item, item["entry_target"] + 1_500, 1.5, None, "first-after")
        self.assertEqual(item["entry_ts"], item["entry_target"] + 800)
        self.assertEqual(item["entry_px"], 1.1)

    def test_ineligible_print_does_not_fill_or_check_the_entry(self) -> None:
        item = target()
        # A SELL print while the bot is waiting for a BUY: not a fill.
        select_entry(
            item,
            item["entry_target"] + 200,
            1.0,
            None,
            "first-after",
            eligible=False,
        )
        self.assertFalse(item["entry_checked"])
        self.assertIsNone(item["entry_px"])
        select_entry(item, item["entry_target"] + 900, 1.2, None, "first-after")
        self.assertEqual(item["entry_ts"], item["entry_target"] + 900)
        self.assertEqual(item["entry_px"], 1.2)


class FrameFilterTests(unittest.TestCase):
    def test_stale_entry_is_dropped_and_its_return_is_nulled(self) -> None:
        item = target()
        # A print five seconds before the decision: exactly the hazard that
        # made the old hot-money edge disappear under event fills.
        item["entry_ts"] = item["entry_target"] - 5_000
        item["entry_px"] = 1.0
        item["entry_side"] = "buy"
        item["exit_5_ts"] = item["exit_5_target"]
        item["exit_5_px"] = 1.5
        frame = build_frame(
            [item], [5], LATENCY, 0.0, max_entry_age_ms=2_000
        )
        self.assertIsNone(frame.loc[0, "entry_px"])
        self.assertIsNone(frame.loc[0, "entry_side"])
        self.assertIsNone(frame.loc[0, "net_5m_return"])

    def test_fresh_entry_is_kept(self) -> None:
        item = target()
        item["entry_ts"] = item["entry_target"] - 1_000
        item["entry_px"] = 1.0
        item["exit_5_ts"] = item["exit_5_target"]
        item["exit_5_px"] = 1.5
        frame = build_frame(
            [item], [5], LATENCY, 0.0, max_entry_age_ms=2_000
        )
        self.assertAlmostEqual(frame.loc[0, "net_5m_return"], 0.5)

    def test_late_fill_has_positive_delay(self) -> None:
        item = target()
        item["entry_ts"] = item["entry_target"] + 800
        item["entry_px"] = 1.0
        item["exit_5_ts"] = item["exit_5_target"]
        item["exit_5_px"] = 1.5
        frame = build_frame([item], [5], LATENCY, 0.0, entry_fill_mode="first-after")
        self.assertEqual(frame.loc[0, "entry_delay_ms"], 800)
        self.assertEqual(frame.loc[0, "entry_price_age_ms"], -800)
        self.assertEqual(frame.loc[0, "entry_fill_mode"], "first-after")

    def test_fill_after_the_entry_window_is_dropped(self) -> None:
        item = target()
        # Nineteen minutes late: the deployed bot would have given up at 60 s.
        item["entry_ts"] = item["entry_target"] + 1_140_000
        item["entry_px"] = 1.0
        item["exit_5_ts"] = item["exit_5_target"]
        item["exit_5_px"] = 1.5
        frame = build_frame(
            [item], [5], LATENCY, 0.0, max_entry_delay_ms=60_000
        )
        self.assertIsNone(frame.loc[0, "entry_px"])
        self.assertIsNone(frame.loc[0, "net_5m_return"])


class EndOfDayFallbackTests(unittest.TestCase):
    """The day-end fallback must respect ``--entry-side``.

    The old fallback priced a last-before entry from the final print even when
    that print was a SELL, which is a fill the deployed buy-side engine could
    never take.
    """

    MINT = "MintTest1111111111111111111111111111"

    def _archive(self, root: Path, lines: list[dict]) -> None:
        import zstandard

        day_dir = root / "2026-11-20"
        day_dir.mkdir(parents=True, exist_ok=True)
        payload = (
            "\n".join(
                json.dumps(line, separators=(",", ":")) for line in lines
            )
            + "\n"
        ).encode()
        (day_dir / "0001.jsonl.zst").write_bytes(
            zstandard.ZstdCompressor().compress(payload)
        )

    def _target(self) -> dict:
        item = target()
        item["row"]["mint"] = self.MINT
        # The decision is after the only print, so only the fallback can fill.
        item["entry_target"] = ALERT_TS + 5_000
        item["exit_5_target"] = item["entry_target"] + 300_000
        return item

    def test_fallback_never_fills_a_buy_from_a_sell(self) -> None:
        # A SELL after the alert but before the decision; the last print of the
        # day is also a SELL. With --entry-side buy there is no eligible fill.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._archive(
                root,
                [
                    {
                        "mint": self.MINT,
                        "priceSol": 2.0,
                        "timestamp": ALERT_TS + 1_000,
                        "quoteAmount": 1.0,
                        "action": "sell",
                    },
                ],
            )
            item = self._target()
            stream_day(
                "2026-11-20",
                [item],
                [5],
                replay_root=root,
                entry_fill_mode="last-before",
                entry_side="buy",
            )
            self.assertIsNone(item["entry_px"])
            self.assertIsNone(item.get("entry_side"))

    def test_fallback_uses_the_last_eligible_buy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._archive(
                root,
                [
                    {
                        "mint": self.MINT,
                        "priceSol": 1.0,
                        "timestamp": ALERT_TS + 1_000,
                        "quoteAmount": 1.0,
                        "tokenAmount": 1.0,
                        "action": "buy",
                    },
                    {
                        "mint": self.MINT,
                        "priceSol": 2.0,
                        "timestamp": ALERT_TS + 2_000,
                        "quoteAmount": 1.0,
                        "tokenAmount": 0.5,
                        "action": "sell",
                    },
                ],
            )
            item = self._target()
            stream_day(
                "2026-11-20",
                [item],
                [5],
                replay_root=root,
                entry_fill_mode="last-before",
                entry_side="buy",
            )
            self.assertEqual(item["entry_px"], 1.0)
            self.assertEqual(item["entry_side"], "buy")


class ArgumentValidationTests(unittest.TestCase):
    def test_disjoint_action_and_entry_side_is_rejected(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit):
            main(
                [
                    "--day",
                    "2026-11-20",
                    "--action",
                    "sell",
                    "--entry-side",
                    "buy",
                ]
            )
        self.assertIn("disjoint prints", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
