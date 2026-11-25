"""Tests for the out-of-sample exit-policy study.

These pin the simulator's contract, not a result: an exit with no fresh print is
a -100 % write-off, the return is capped at +999 %, and each declared policy
sells at the moment its name says. A change to any of those would silently
change the comparison table in ``docs/EXIT_POLICY_STUDY.md``.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parents[1]
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from exit_policy_study import (  # noqa: E402
    close_at,
    net,
    simulate,
    summarize,
)

ROW = {"entry_ts": 0, "entry_px": 1.0}


class PrimitiveTests(unittest.TestCase):
    def test_return_is_capped_at_999_pct(self) -> None:
        self.assertAlmostEqual(net(1.0, 1_000_000.0), 9.99)

    def test_return_is_net_of_both_fees(self) -> None:
        # 2.0 -> 1.0 round trip at 1.25 % per side.
        self.assertAlmostEqual(net(1.0, 1.0), (0.9875 / 1.0125) - 1.0)

    def test_close_at_requires_a_print_inside_the_window(self) -> None:
        times, prices = [100, 1_000], [1.0, 2.0]
        self.assertEqual(close_at(times, prices, 900, 200), 2.0)
        self.assertIsNone(close_at(times, prices, 900, 50))
        self.assertIsNone(close_at(times, prices, 2_000, 60_000))


class PolicyTests(unittest.TestCase):
    def test_horizon_without_fresh_print_is_written_off(self) -> None:
        series = [(0, 1.0), (100_000, 1.5)]
        self.assertEqual(simulate(ROW, series, "base5", 60_000), -1.0)

    def test_horizon_sells_the_first_print_after_five_minutes(self) -> None:
        series = [(0, 1.0), (310_000, 1.2)]
        self.assertAlmostEqual(simulate(ROW, series, "base5", 60_000), net(1.0, 1.2))

    def test_trailing_stop_fires_on_the_declared_drawdown(self) -> None:
        series = [(0, 1.0), (61_000, 2.0), (90_000, 2.1), (120_000, 1.3)]
        # 2.1 * 0.65 = 1.365, so 1.3 triggers the 35 % trail.
        self.assertAlmostEqual(
            simulate(ROW, series, "trail35", 60_000), net(1.0, 1.3)
        )

    def test_trailing_stop_ignores_a_smaller_pullback(self) -> None:
        series = [(0, 1.0), (61_000, 2.0), (90_000, 1.6), (300_000, 1.5)]
        self.assertAlmostEqual(
            simulate(ROW, series, "trail35_5", 60_000), net(1.0, 1.5)
        )

    def test_trailing_stop_deadline_differs_from_the_15m_cap(self) -> None:
        # Nothing triggers; the plain trail holds to 15 min, the _5 form stops
        # at the deployed horizon.
        series = [(0, 1.0), (61_000, 2.0), (300_000, 1.5), (900_000, 1.2)]
        self.assertAlmostEqual(
            simulate(ROW, series, "trail35", 60_000), net(1.0, 1.2)
        )
        self.assertAlmostEqual(
            simulate(ROW, series, "trail35_5", 60_000), net(1.0, 1.5)
        )

    def test_take_profit_sells_the_whole_position_at_target(self) -> None:
        series = [(0, 1.0), (80_000, 1.6), (300_000, 0.5)]
        self.assertAlmostEqual(
            simulate(ROW, series, "tp50all", 60_000), net(1.0, 1.6)
        )

    def test_partial_take_profit_leaves_half_to_the_horizon(self) -> None:
        series = [(0, 1.0), (80_000, 1.6), (300_000, 1.2)]
        expected = 0.5 * net(1.0, 1.6) + 0.5 * net(1.0, 1.2)
        self.assertAlmostEqual(simulate(ROW, series, "tp50h5", 60_000), expected)

    def test_dynamic_hold_exits_when_the_floor_breaks(self) -> None:
        series = [(0, 1.0), (300_000, 1.20), (330_000, 1.01)]
        self.assertAlmostEqual(simulate(ROW, series, "dyn5", 60_000), net(1.0, 1.01))

    def test_dynamic_hold_writes_off_a_pool_that_stops_printing(self) -> None:
        series = [(0, 1.0), (300_000, 1.20)]
        self.assertEqual(simulate(ROW, series, "dyn5", 60_000), -1.0)

    def test_absolute_stop_fires_on_the_floor(self) -> None:
        series = [(0, 1.0), (90_000, 0.49), (300_000, 0.8)]
        self.assertAlmostEqual(
            simulate(ROW, series, "stop50_5", 60_000), net(1.0, 0.49)
        )

    def test_absolute_stop_holds_above_the_floor(self) -> None:
        series = [(0, 1.0), (90_000, 0.6), (300_000, 0.8)]
        self.assertAlmostEqual(
            simulate(ROW, series, "stop50_5", 60_000), net(1.0, 0.8)
        )

    def test_ladder_averages_its_three_legs(self) -> None:
        series = [(0, 1.0), (120_000, 1.3), (300_000, 0.9), (600_000, 1.1)]
        expected = (
            net(1.0, 1.3) + net(1.0, 0.9) + net(1.0, 1.1)
        ) / 3
        self.assertAlmostEqual(simulate(ROW, series, "ladder", 60_000), expected)

    def test_oracle_uses_the_best_print_inside_its_window(self) -> None:
        series = [(0, 1.0), (200_000, 2.5), (400_000, 3.0)]
        self.assertAlmostEqual(simulate(ROW, series, "oracle5", 60_000), net(1.0, 2.5))
        self.assertAlmostEqual(
            simulate(ROW, series, "oracle15", 60_000), net(1.0, 3.0)
        )

    def test_unknown_policy_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            simulate(ROW, [(0, 1.0)], "moon", 60_000)


class SummaryTests(unittest.TestCase):
    def test_catastrophic_rate_counts_full_write_offs(self) -> None:
        summary = summarize([-1.0, 0.2, 0.1, -0.5], 0.1)
        self.assertEqual(summary["catastrophic"], 0.25)
        self.assertEqual(summary["n"], 4)
        self.assertAlmostEqual(summary["pnl_sol"], -0.12)


if __name__ == "__main__":
    unittest.main()
