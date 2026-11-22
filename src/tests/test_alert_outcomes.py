"""Focused tests for the scanner-alert pricer.

The scanner evidence is only as good as its fill convention, and the previous
strategy died from a stale fill. These tests pin the two conventions, the
stale-entry filter, and the dust floor default.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parents[1]
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from alert_outcomes import build_frame, select_entry  # noqa: E402

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


class FrameFilterTests(unittest.TestCase):
    def test_stale_entry_is_dropped_and_its_return_is_nulled(self) -> None:
        item = target()
        # A print five seconds before the decision: exactly the hazard that
        # made the old hot-money edge disappear under event fills.
        item["entry_ts"] = item["entry_target"] - 5_000
        item["entry_px"] = 1.0
        item["exit_5_ts"] = item["exit_5_target"]
        item["exit_5_px"] = 1.5
        frame = build_frame(
            [item], [5], LATENCY, 0.0, max_entry_age_ms=2_000
        )
        self.assertIsNone(frame.loc[0, "entry_px"])
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
        frame = build_frame([item], [5], LATENCY, 0.0)
        self.assertEqual(frame.loc[0, "entry_delay_ms"], 800)
        self.assertEqual(frame.loc[0, "entry_price_age_ms"], -800)

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


if __name__ == "__main__":
    unittest.main()
