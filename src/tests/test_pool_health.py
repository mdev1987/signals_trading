"""Tests for the observational pool-health layer."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parents[1]
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from execution_simulator import ExecutionSimulator  # noqa: E402
from paper_bot.feeds import parse_pump_event  # noqa: E402
from paper_bot.models import WSOL_MINT  # noqa: E402
from paper_bot.pool_health import (  # noqa: E402
    DERIVED_COLUMNS,
    EXIT_LIQUIDITY_COLLAPSE,
    EXIT_NEAR_ZERO,
    EXIT_NORMAL,
    EXIT_ROUTE_FAILURE,
    LABEL_COLUMNS,
    OBSERVED_COLUMNS,
    classify_exit,
    reserve_state_from_trade,
    sell_quote_sol,
    snapshot_row,
)

MINT = "HealthMint"
POOL_ID = "pool-1"
EVENT_TS_MS = 1_000


def pump_amm_event(**overrides) -> dict:
    event = {
        "action": "sell",
        "mint": MINT,
        "poolId": POOL_ID,
        "pool": "pump-amm",
        "quoteMint": WSOL_MINT,
        "timestamp": EVENT_TS_MS,
        "priceSol": 130.0 / 1_000_000_000.0,
        "quotePriceSol": 1.0,
        "tokensInPool": 1_000_000_000.0,
        "quoteInPool": 100.0,
        "virtualTokensInPool": 0.0,
        "virtualQuoteInPool": 30.0,
        "poolFeeRate": 0.0025,
        "solRouteFeeRate": 0.0025,
    }
    event.update(overrides)
    return event


def trade_from(**overrides):
    event = pump_amm_event(**overrides)
    trade = parse_pump_event(json.dumps(event), 0)
    assert trade is not None
    return trade


class ParseReservesTests(unittest.TestCase):
    def test_parser_captures_reserves_without_changing_price(self) -> None:
        trade = trade_from()
        self.assertEqual(trade.tokens_in_pool, 1_000_000_000.0)
        self.assertEqual(trade.quote_in_pool, 100.0)
        self.assertEqual(trade.virtual_quote_in_pool, 30.0)
        self.assertEqual(trade.sol_route_fee_rate, 0.0025)
        self.assertAlmostEqual(trade.price_sol or 0.0, 1.3e-7)


class ReserveStateTests(unittest.TestCase):
    def test_virtual_quote_is_added_to_the_quote_side(self) -> None:
        state = reserve_state_from_trade(trade_from())
        self.assertTrue(state.usable)
        self.assertEqual(state.reserve_model, "amm_virtual_quote")
        self.assertEqual(state.token_reserve, 1_000_000_000.0)
        self.assertEqual(state.quote_reserve, 130.0)
        self.assertAlmostEqual(state.spot_price_sol or 0.0, 1.3e-7)

    def test_virtual_tokens_are_added_to_the_token_side(self) -> None:
        state = reserve_state_from_trade(
            trade_from(virtualQuoteInPool=0.0, virtualTokensInPool=250_000_000.0)
        )
        self.assertEqual(state.reserve_model, "amm_virtual_tokens")
        self.assertEqual(state.token_reserve, 1_250_000_000.0)
        self.assertEqual(state.quote_reserve, 100.0)

    def test_both_virtual_sides_is_refused(self) -> None:
        state = reserve_state_from_trade(
            trade_from(virtualTokensInPool=1.0, virtualQuoteInPool=1.0)
        )
        self.assertFalse(state.usable)
        self.assertEqual(state.unsupported_reason, "ambiguous_virtual_reserves")

    def test_no_virtual_side_is_refused_not_guessed(self) -> None:
        state = reserve_state_from_trade(
            trade_from(virtualQuoteInPool=0.0, virtualTokensInPool=0.0)
        )
        self.assertFalse(state.usable)
        self.assertEqual(state.unsupported_reason, "missing_amm_reserves")

    def test_dust_reserves_are_refused(self) -> None:
        state = reserve_state_from_trade(
            trade_from(tokensInPool=1e-9, quoteInPool=1.0)
        )
        self.assertFalse(state.usable)
        self.assertEqual(state.unsupported_reason, "pool_reserves_below_floor")

    def test_concentrated_liquidity_is_refused(self) -> None:
        state = reserve_state_from_trade(trade_from(pool="raydium-clmm"))
        self.assertFalse(state.usable)
        self.assertEqual(
            state.unsupported_reason, "unsupported_concentrated_liquidity"
        )

    def test_missing_fee_is_refused(self) -> None:
        state = reserve_state_from_trade(
            trade_from(poolFeeRate=None, solRouteFeeRate=None)
        )
        self.assertFalse(state.usable)
        self.assertEqual(
            state.unsupported_reason, "missing_or_invalid_pool_fee_rate"
        )


class SellQuoteTests(unittest.TestCase):
    def test_matches_the_research_simulator(self) -> None:
        state = reserve_state_from_trade(trade_from())
        tokens_in = 1_000_000.0
        ours = sell_quote_sol(state, tokens_in)
        self.assertIsNotNone(ours)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            path.write_text(json.dumps(pump_amm_event()) + "\n")
            simulator = ExecutionSimulator(path, max_state_age_ms=None)
            fill = simulator.execute(
                order_id="t1",
                timestamp_ms=EVENT_TS_MS + 1,
                mint=MINT,
                pool_id=POOL_ID,
                side="sell",
                amount=tokens_in,
            )
        self.assertEqual(fill.status, "filled", fill.reason)
        self.assertAlmostEqual(ours or 0.0, fill.quote_net or 0.0, places=12)

    def test_selling_more_than_the_pool_holds_is_refused(self) -> None:
        state = reserve_state_from_trade(trade_from())
        self.assertIsNone(sell_quote_sol(state, 1_000_000_000.0))

    def test_unusable_state_has_no_quote(self) -> None:
        state = reserve_state_from_trade(trade_from(pool="orca-whirlpool"))
        self.assertIsNone(sell_quote_sol(state, 1.0))


class SnapshotRowTests(unittest.TestCase):
    def build_row(self, **overrides):
        state = reserve_state_from_trade(trade_from())
        values = {
            "run_id": "live_test",
            "state": state,
            "entry_cost_sol": 0.10125,
            "size_tokens": 1_000_000.0,
            "pool_age_ms": 2_200,
            "reserve_exit_value_sol": 0.0995,
            "peak_reserve_exit_value_sol": 0.11,
            "jupiter_exit_quote_sol": 0.0975,
            "jupiter_quote_status": "available",
            "quote_timestamp_ms": EVENT_TS_MS,
            "net_flow_sol": -0.25,
            "trade_count": 12,
            "time_since_last_print_ms": 800,
        }
        values.update(overrides)
        return snapshot_row(**values)

    def test_columns_are_prefixed_and_disjoint(self) -> None:
        row = self.build_row()
        self.assertEqual(set(row), set(OBSERVED_COLUMNS) | set(DERIVED_COLUMNS))
        self.assertFalse(set(OBSERVED_COLUMNS) & set(DERIVED_COLUMNS))
        # A predictor row must not contain a label: leakage would be invisible.
        self.assertFalse(set(row) & set(LABEL_COLUMNS))

    def test_observed_values_are_copied_verbatim(self) -> None:
        row = self.build_row()
        self.assertEqual(row["obs_mint"], MINT)
        self.assertEqual(row["obs_timestamp_ms"], EVENT_TS_MS)
        self.assertEqual(row["obs_jupiter_quote_status"], "available")
        self.assertIsNone(row["obs_unsupported_reason"])

    def test_derived_values_use_only_supplied_history(self) -> None:
        row = self.build_row()
        self.assertAlmostEqual(row["der_exit_value_ratio"], 0.0995 / 0.10125)
        self.assertAlmostEqual(row["der_drawdown_from_entry"], 0.0995 / 0.10125 - 1.0)
        self.assertAlmostEqual(row["der_drawdown_from_peak"], 0.0995 / 0.11 - 1.0)
        self.assertAlmostEqual(row["der_reserve_model_error"], 0.0975 / 0.0995 - 1.0)

    def test_missing_quote_does_not_break_the_row(self) -> None:
        row = self.build_row(
            jupiter_exit_quote_sol=None,
            jupiter_quote_status="no_route",
            peak_reserve_exit_value_sol=None,
        )
        self.assertIsNone(row["der_reserve_model_error"])
        self.assertIsNone(row["der_drawdown_from_peak"])
        self.assertEqual(row["obs_jupiter_quote_status"], "no_route")

    def test_unusable_reserve_state_still_writes_a_row(self) -> None:
        state = reserve_state_from_trade(trade_from(pool="raydium-clmm"))
        row = self.build_row(state=state, reserve_exit_value_sol=None)
        self.assertEqual(
            row["obs_unsupported_reason"], "unsupported_concentrated_liquidity"
        )
        self.assertIsNone(row["der_exit_value_ratio"])


class LabelTests(unittest.TestCase):
    def test_route_failure_wins(self) -> None:
        self.assertEqual(
            classify_exit(exit_value_sol=None, entry_cost_sol=0.1, route_failed=True),
            EXIT_ROUTE_FAILURE,
        )

    def test_near_zero_uses_the_engine_ratio(self) -> None:
        self.assertEqual(
            classify_exit(exit_value_sol=0.0005, entry_cost_sol=0.1),
            EXIT_NEAR_ZERO,
        )

    def test_healthy_horizon_exit_is_normal(self) -> None:
        self.assertEqual(
            classify_exit(exit_value_sol=0.12, entry_cost_sol=0.1),
            EXIT_NORMAL,
        )

    def test_observed_collapse_is_its_own_label(self) -> None:
        self.assertEqual(
            classify_exit(
                exit_value_sol=0.12,
                entry_cost_sol=0.1,
                min_reserve_ratio=0.002,
            ),
            EXIT_LIQUIDITY_COLLAPSE,
        )


if __name__ == "__main__":
    unittest.main()
