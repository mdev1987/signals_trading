"""Tests for the out-of-sample exit-policy study.

These pin the simulator's contract, not a result: an exit with no fresh print is
a -100 % write-off, the return is capped at +999 %, and each declared policy
sells at the moment its name says. A change to any of those would silently
change the comparison table in ``docs/EXIT_POLICY_STUDY.md``.
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

from exit_policy_study import (  # noqa: E402
    POLICIES,
    build_artifact,
    close_at,
    main,
    net,
    run_day_details,
    simulate,
    simulate_trade,
    summarize,
)

ROW = {"mint": "mint", "entry_ts": 0, "entry_px": 1.0}


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


class TradeEvidenceTests(unittest.TestCase):
    def test_take_profit_records_the_triggering_print(self) -> None:
        series = [(0, 1.0), (80_000, 1.6), (300_000, 0.5)]
        trade = simulate_trade(ROW, series, "tp50all", 60_000)
        self.assertEqual(trade["exit_reason"], "take_profit")
        self.assertEqual(trade["exit_ts_ms"], 80_000)
        self.assertAlmostEqual(trade["exit_price_sol"], 1.6)
        self.assertTrue(trade["fresh"])
        self.assertAlmostEqual(trade["net"], net(1.0, 1.6))

    def test_write_off_records_no_exit_price(self) -> None:
        series = [(0, 1.0), (100_000, 1.5)]
        trade = simulate_trade(ROW, series, "base5", 60_000)
        self.assertEqual(trade["exit_reason"], "horizon_no_fresh_print")
        self.assertIsNone(trade["exit_ts_ms"])
        self.assertIsNone(trade["exit_price_sol"])
        self.assertFalse(trade["fresh"])
        self.assertEqual(trade["net"], -1.0)

    def test_partial_policy_records_both_weighted_legs(self) -> None:
        series = [(0, 1.0), (80_000, 1.6), (300_000, 1.2)]
        trade = simulate_trade(ROW, series, "tp50h5", 60_000)
        self.assertEqual(
            [leg["reason"] for leg in trade["legs"]],
            ["take_profit", "horizon"],
        )
        self.assertEqual([leg["weight"] for leg in trade["legs"]], [0.5, 0.5])
        expected = 0.5 * net(1.0, 1.6) + 0.5 * net(1.0, 1.2)
        self.assertAlmostEqual(trade["net"], expected)


class ArtifactTests(unittest.TestCase):
    ROWS = [
        {"mint": "mint-a", "entry_ts": 0, "entry_px": 1.0},
        # The same launch alerted twice: one bootstrap cluster, two trades.
        {"mint": "mint-a", "entry_ts": 500_000, "entry_px": 1.0},
        {"mint": "mint-b", "entry_ts": 0, "entry_px": 1.0},
    ]
    SERIES = {
        "mint-a": [(0, 1.0), (80_000, 1.6), (300_000, 1.2), (900_000, 1.1)],
        "mint-b": [(0, 1.0), (100_000, 0.7)],
    }

    def details(self) -> dict[str, dict[str, list[dict]]]:
        from unittest.mock import patch

        with patch(
            "exit_policy_study.lane_rows", return_value=self.ROWS
        ), patch(
            "exit_policy_study.collect_prints", return_value=self.SERIES
        ):
            return {"d1": run_day_details("d1", 60_000, 0.0125)}

    def test_artifact_is_complete_and_reproducible(self) -> None:
        details = self.details()
        first = build_artifact(
            details,
            days=["d1"],
            fresh_ms=60_000,
            fee=0.0125,
            notional_sol=0.1,
            resamples=200,
            seed=7,
        )
        second = build_artifact(
            details,
            days=["d1"],
            fresh_ms=60_000,
            fee=0.0125,
            notional_sol=0.1,
            resamples=200,
            seed=7,
        )
        self.assertEqual(first, second)
        self.assertEqual(set(first["policies"]), set(POLICIES))
        tp50 = first["policies"]["tp50all"]
        self.assertEqual(tp50["summary"]["n"], len(self.ROWS))
        self.assertEqual(
            [row["mint"] for row in tp50["trades"]],
            ["mint-a", "mint-a", "mint-b"],
        )
        row = tp50["trades"][0]
        self.assertEqual(row["exit_ts_ms"], 80_000)
        self.assertAlmostEqual(row["exit_price_sol"], 1.6)
        self.assertTrue(row["fresh"])
        self.assertAlmostEqual(row["net_sol"], row["net"] * 0.1)
        # Mints are clusters, not trades.
        self.assertEqual(tp50["bootstrap"]["clusters"], 2.0)
        self.assertEqual(tp50["bootstrap"]["resamples"], 200.0)
        self.assertEqual(tp50["bootstrap"]["seed"], 7.0)
        self.assertEqual(tp50["bootstrap"]["method"], "percentile_over_mint_clusters")
        self.assertGreaterEqual(tp50["bootstrap"]["probability_positive"], 0.0)
        self.assertLessEqual(tp50["bootstrap"]["probability_positive"], 1.0)
        # The pooled bootstrap documentation matches the per-policy one.
        self.assertEqual(first["study"]["bootstrap"]["seed"], 7)

    def test_multi_leg_policy_keeps_its_legs_in_the_artifact(self) -> None:
        artifact = build_artifact(
            self.details(),
            days=["d1"],
            fresh_ms=60_000,
            fee=0.0125,
            notional_sol=0.1,
            resamples=50,
            seed=1,
        )
        trade = artifact["policies"]["tp50h5"]["trades"][0]
        self.assertEqual(
            [leg["weight"] for leg in trade["legs"]], [0.5, 0.5]
        )
        self.assertAlmostEqual(
            trade["net"], sum(
                leg["weight"] * leg["net"] for leg in trade["legs"]
            )
        )
        # A single-leg policy has no legs key: the top-level row is enough.
        self.assertNotIn("legs", artifact["policies"]["tp50all"]["trades"][0])

    def test_cli_writes_the_artifact_and_prints_the_bootstrap(self) -> None:
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "artifact.json"
            stdout = io.StringIO()
            with patch(
                "exit_policy_study.lane_rows", return_value=self.ROWS
            ), patch(
                "exit_policy_study.collect_prints", return_value=self.SERIES
            ), contextlib.redirect_stdout(stdout):
                code = main(
                    [
                        "--days",
                        "d1",
                        "--artifact",
                        str(out),
                        "--bootstrap-resamples",
                        "20",
                    ]
                )
            self.assertEqual(code, 0)
            payload = json.loads(out.read_text())
        self.assertEqual(payload["study"]["days"], ["d1"])
        self.assertEqual(payload["study"]["bootstrap"]["resamples"], 20)
        self.assertIn("tp50all mint-cluster bootstrap", stdout.getvalue())



if __name__ == "__main__":
    unittest.main()
