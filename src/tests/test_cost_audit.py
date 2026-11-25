"""Tests for the paper-execution cost decomposition."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parents[1]
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from cost_audit import (  # noqa: E402
    decompose_trade,
    fee_sensitivity,
    parse_quote_costs,
    summarize,
)


def quote(**overrides) -> str:
    body = {
        "feeBps": 10,
        "platformFee": {"feeBps": 10, "feeMint": "So11111111111111111111111111111111111111112"},
        "priceImpact": -2.0,
        "slippageBps": 50,
        "outAmount": "1000000000",
        "otherAmountThreshold": "995000000",
        "prioritizationFeeLamports": 0,
        "rentFeeLamports": 0,
        "signatureFeeLamports": 0,
    }
    body.update(overrides)
    return json.dumps(body)


class ParseQuoteTests(unittest.TestCase):
    def test_platform_fee_is_counted_once(self) -> None:
        # feeBps and platformFee.feeBps are the same charge.
        costs = parse_quote_costs(quote())
        self.assertEqual(costs.platform_fee_bps, 10.0)

    def test_price_impact_is_a_positive_cost(self) -> None:
        costs = parse_quote_costs(quote(priceImpact=-2.5))
        self.assertAlmostEqual(costs.price_impact_bps, 250.0)

    def test_threshold_slippage_comes_from_the_amounts(self) -> None:
        costs = parse_quote_costs(
            quote(outAmount="1000", otherAmountThreshold="995", slippageBps=50)
        )
        self.assertAlmostEqual(costs.threshold_slippage_bps, 50.0)

    def test_missing_quote_is_zero(self) -> None:
        costs = parse_quote_costs(None)
        self.assertEqual(costs.platform_fee_bps, 0.0)
        self.assertEqual(costs.price_impact_bps, 0.0)


class DecomposeTests(unittest.TestCase):
    def build_trade(self, **overrides):
        body = {
            "mint": "m",
            "entry_ts_ms": 1,
            "entry_value_sol": 0.1,
            "entry_fee_sol": 0.00125,
            "exit_fee_sol": 0.00125,
            "pnl_sol": 0.02,
            "return_pct": 0.2,
            "entry_quote_raw": quote(),
            "exit_quote_raw": quote(),
        }
        body.update(overrides)
        return body

    def test_quote_costs_are_not_subtracted_again(self) -> None:
        row = decompose_trade(self.build_trade())
        # The recorded PnL is the baseline; only unapplied costs move it.
        self.assertAlmostEqual(row["pnl_paper_sol"], 0.02)
        self.assertAlmostEqual(row["quote_embedded_bps"], 2 * (10.0 + 200.0))
        self.assertAlmostEqual(row["broker_fee_bps"], 2 * 125.0)

    def test_threshold_fill_is_one_haircut_worse(self) -> None:
        row = decompose_trade(self.build_trade(), live_tx_lamports=0)
        # 50 bps per side on 0.1 SOL = 0.001 SOL.
        self.assertAlmostEqual(row["pnl_threshold_sol"], 0.02 - 0.001)

    def test_live_scenario_uses_configured_fee_when_quote_is_zero(self) -> None:
        row = decompose_trade(
            self.build_trade(), live_tx_lamports=12_000, ata_rent_sol=0.002
        )
        expected = 0.02 - 0.001 - 2 * 12_000 / 1_000_000_000
        self.assertAlmostEqual(row["pnl_live_sol"], expected)
        # ATA rent is locked capital, not a fee, so it stays out of PnL.
        self.assertAlmostEqual(row["ata_rent_locked_sol"], 0.002)

    def test_reference_deviation_is_reported_in_bps(self) -> None:
        row = decompose_trade(self.build_trade(), reference_deviation_pct=0.5)
        self.assertAlmostEqual(row["reference_deviation_bps"], 50.0)


class SummaryTests(unittest.TestCase):
    def test_one_point_cost_scales_with_notional(self) -> None:
        rows = [
            decompose_trade(
                {
                    "entry_value_sol": 0.1,
                    "entry_fee_sol": 0.00125,
                    "exit_fee_sol": 0.00125,
                    "pnl_sol": 0.0,
                    "entry_quote_raw": quote(),
                    "exit_quote_raw": quote(),
                    "mint": "a",
                }
            )
            for _ in range(10)
        ]
        summary = summarize(rows)
        self.assertAlmostEqual(summary["one_point_cost_sol"], 0.01)

    def test_missing_quotes_do_not_dilute_the_cost_means(self) -> None:
        quoted = decompose_trade(
            {
                "entry_value_sol": 0.1,
                "entry_fee_sol": 0.00125,
                "exit_fee_sol": 0.00125,
                "pnl_sol": 0.0,
                "entry_quote_raw": quote(priceImpact=-10.0),
                "exit_quote_raw": quote(priceImpact=-10.0),
                "mint": "a",
            }
        )
        bare = decompose_trade(
            {
                "entry_value_sol": 0.1,
                "entry_fee_sol": 0.00125,
                "exit_fee_sol": 0.00125,
                "pnl_sol": 0.0,
                "mint": "b",
            }
        )
        summary = summarize([quoted, bare])
        self.assertEqual(summary["quoted_n"], 1)
        self.assertAlmostEqual(summary["quote_coverage"], 0.5)
        # 10 % + 10 % impact + 10 + 10 bps platform fee, not halved by the bare row.
        self.assertAlmostEqual(summary["price_impact_bps_mean"], 2000.0)
        self.assertAlmostEqual(summary["platform_fee_bps_mean"], 20.0)

    def test_gross_edge_reconstructs_the_fee_overlay(self) -> None:
        row = decompose_trade(
            {
                "entry_value_sol": 0.1,
                "entry_fee_sol": 0.00125,
                "exit_fee_sol": 0.0012,
                "pnl_sol": 0.02,
                "entry_quote_raw": quote(),
                "exit_quote_raw": quote(),
                "mint": "a",
            }
        )
        self.assertAlmostEqual(row["gross_sol"], 0.02 + 0.00125 + 0.0012)
        summary = summarize([row])
        self.assertAlmostEqual(summary["broker_fee_sol_total"], 0.00245)

    def test_fee_sensitivity_is_linear_in_the_overlay(self) -> None:
        rows = [
            decompose_trade(
                {
                    "entry_value_sol": 0.1,
                    "exit_value_sol": 0.12,
                    "entry_fee_sol": 0.00125,
                    "exit_fee_sol": 0.0015,
                    "pnl_sol": 0.01825,
                    "mint": "a",
                }
            )
        ]
        bands = fee_sensitivity(rows, fees=(0.0125, 0.0))
        # At zero fee the gross edge is returned unchanged.
        self.assertAlmostEqual(bands[1]["pnl_sol"], 0.01825 + 0.00125 + 0.0015)

    def test_threshold_delta_is_like_for_like(self) -> None:
        quoted = decompose_trade(
            {
                "entry_value_sol": 0.1,
                "entry_fee_sol": 0.00125,
                "exit_fee_sol": 0.00125,
                "pnl_sol": 0.02,
                "entry_quote_raw": quote(),
                "exit_quote_raw": quote(),
                "mint": "a",
            }
        )
        bare = decompose_trade(
            {
                "entry_value_sol": 0.1,
                "entry_fee_sol": 0.00125,
                "exit_fee_sol": 0.00125,
                "pnl_sol": 0.05,
                "mint": "b",
            }
        )
        summary = summarize([quoted, bare])
        # The bare trade cannot be haircut, so it must not enter the delta.
        self.assertAlmostEqual(
            summary["quoted_pnl_paper_sol"], 0.02
        )
        self.assertAlmostEqual(summary["pnl_paper_sol"], 0.07)


if __name__ == "__main__":
    unittest.main()
