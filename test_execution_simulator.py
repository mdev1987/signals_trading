from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from execution_simulator import ExecutionSimulator, WSOL_MINT, run_orders, snapshot_from_event


def event(
    timestamp: int,
    *,
    action: str = "buy",
    pool: str = "pump",
    pool_id: str = "pool-1",
    token_reserve: float = 1_000.0,
    quote_reserve: float = 100.0,
    fee_rate: float = 0.01,
    mint: str = "mint-1",
    quote_mint: str = WSOL_MINT,
) -> dict:
    result = {
        "timestamp": timestamp,
        "action": action,
        "mint": mint,
        "poolId": pool_id,
        "pool": pool,
        "quoteMint": quote_mint,
        "poolFeeRate": fee_rate,
        "signature": f"sig-{timestamp}",
    }
    if pool == "pump":
        result["vTokensInBondingCurve"] = token_reserve
        result["vQuoteInBondingCurve"] = quote_reserve
    else:
        result["tokensInPool"] = token_reserve
        result["quoteInPool"] = quote_reserve
        result["virtualQuoteInPool"] = 0.0
    return result


class SnapshotParsingTests(unittest.TestCase):
    def test_pump_uses_virtual_bonding_curve_reserves(self) -> None:
        snapshot = snapshot_from_event(event(100))
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.token_reserve, 1_000.0)
        self.assertEqual(snapshot.quote_reserve, 100.0)
        self.assertIsNone(snapshot.unsupported_reason)

    def test_pump_amm_uses_effective_quote_reserve(self) -> None:
        raw = event(100, pool="pump-amm")
        raw["quoteInPool"] = 100.0
        raw["virtualQuoteInPool"] = 5.0
        snapshot = snapshot_from_event(raw)
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.token_reserve, 1_000.0)
        self.assertEqual(snapshot.quote_reserve, 105.0)
        self.assertIsNone(snapshot.unsupported_reason)

    def test_pump_amm_allows_signed_virtual_quote_reserve(self) -> None:
        raw = event(100, pool="pump-amm")
        raw["quoteInPool"] = 100.0
        raw["virtualQuoteInPool"] = -5.0
        snapshot = snapshot_from_event(raw)
        self.assertEqual(snapshot.quote_reserve, 95.0)
        self.assertIsNone(snapshot.unsupported_reason)

    def test_pump_amm_requires_virtual_quote_field(self) -> None:
        raw = event(100, pool="pump-amm")
        raw.pop("virtualQuoteInPool")
        snapshot = snapshot_from_event(raw)
        self.assertEqual(snapshot.unsupported_reason, "missing_amm_reserves")

    def test_pump_amm_uses_virtual_tokens_reserve(self) -> None:
        raw = event(100, pool="pump-amm")
        raw["virtualQuoteInPool"] = None
        raw["virtualTokensInPool"] = 40.0
        snapshot = snapshot_from_event(raw)
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.token_reserve, 1_040.0)
        self.assertEqual(snapshot.quote_reserve, 100.0)
        self.assertEqual(snapshot.reserve_model, "amm_virtual_tokens")
        self.assertIsNone(snapshot.unsupported_reason)

    def test_pump_amm_virtual_quote_reserve_model(self) -> None:
        raw = event(100, pool="pump-amm")
        raw["virtualQuoteInPool"] = 5.0
        snapshot = snapshot_from_event(raw)
        self.assertEqual(snapshot.reserve_model, "amm_virtual_quote")

    def test_pump_amm_rejects_ambiguous_virtual_reserves(self) -> None:
        raw = event(100, pool="pump-amm")
        raw["virtualQuoteInPool"] = 5.0
        raw["virtualTokensInPool"] = 40.0
        snapshot = snapshot_from_event(raw)
        self.assertEqual(snapshot.unsupported_reason, "ambiguous_virtual_reserves")

    def test_other_pool_is_explicitly_unsupported(self) -> None:
        snapshot = snapshot_from_event(event(100, pool="raydium-cpmm"))
        self.assertEqual(snapshot.unsupported_reason, "unsupported_pool_model")

    def test_non_wsol_quote_is_unsupported(self) -> None:
        snapshot = snapshot_from_event(event(100, quote_mint="USDC"))
        self.assertEqual(snapshot.unsupported_reason, "non_wsol_quote")

    def test_missing_fee_is_not_assumed_zero(self) -> None:
        raw = event(100)
        raw.pop("poolFeeRate")
        snapshot = snapshot_from_event(raw)
        self.assertEqual(snapshot.unsupported_reason, "missing_or_invalid_pool_fee_rate")


class ExecutionTests(unittest.TestCase):
    def make_simulator(self, events: list[dict], **kwargs) -> tuple[ExecutionSimulator, tempfile.TemporaryDirectory]:
        temp = tempfile.TemporaryDirectory()
        path = Path(temp.name) / "events.jsonl"
        path.write_text("\n".join(json.dumps(item) for item in events) + "\n", encoding="utf-8")
        return ExecutionSimulator(path, **kwargs), temp

    def test_buy_applies_fee_and_curve_impact(self) -> None:
        simulator, temp = self.make_simulator([event(100)])
        self.addCleanup(temp.cleanup)
        result = simulator.execute(
            order_id="buy-1", timestamp_ms=101, mint="mint-1", pool_id="pool-1",
            side="buy", amount=1.0,
        )
        self.assertEqual(result.status, "filled")
        self.assertEqual(result.reserve_model, "bonding_curve")
        expected_net = 1.0 * 0.99
        self.assertAlmostEqual(result.quote_fee, 1.0 - expected_net)
        self.assertAlmostEqual(result.quote_net, expected_net)
        self.assertAlmostEqual(result.token_amount, 1_000 * expected_net / (100.0 + expected_net))
        self.assertGreater(result.price_impact_bps, 0)

    def test_sell_returns_net_quote_after_fee(self) -> None:
        simulator, temp = self.make_simulator([event(100)])
        self.addCleanup(temp.cleanup)
        result = simulator.execute(
            order_id="sell-1", timestamp_ms=101, mint="mint-1", pool_id="pool-1",
            side="sell", amount=10.0,
        )
        self.assertEqual(result.status, "filled")
        gross = 100 * 10 / 1_010
        self.assertAlmostEqual(result.quote_gross, gross)
        self.assertAlmostEqual(result.quote_fee, gross * 0.01)
        self.assertAlmostEqual(result.quote_net, gross * 0.99)
        self.assertGreater(result.price_impact_bps, 0)

    def test_snapshot_at_execution_timestamp_is_excluded(self) -> None:
        simulator, temp = self.make_simulator([event(101, token_reserve=2_000, quote_reserve=100)])
        self.addCleanup(temp.cleanup)
        result = simulator.execute(
            order_id="same-ms", timestamp_ms=101, mint="mint-1", pool_id="pool-1",
            side="buy", amount=1.0,
        )
        self.assertEqual(result.status, "no_pool_state")

    def test_reserve_deltas_preserve_shadow_fill_effect(self) -> None:
        simulator, temp = self.make_simulator([
            event(100),
            event(200, token_reserve=990.0, quote_reserve=101.0),
        ])
        self.addCleanup(temp.cleanup)
        first = simulator.execute(
            order_id="buy-1", timestamp_ms=150, mint="mint-1", pool_id="pool-1",
            side="buy", amount=1.0,
        )
        self.assertEqual(first.status, "filled")
        shadow_after_buy_x = first.reserve_token_after
        shadow_after_buy_y = first.reserve_quote_after

        second = simulator.execute(
            order_id="buy-2", timestamp_ms=201, mint="mint-1", pool_id="pool-1",
            side="buy", amount=1.0,
        )
        self.assertEqual(second.status, "filled")
        self.assertAlmostEqual(second.reserve_token_before, shadow_after_buy_x - 10.0)
        self.assertAlmostEqual(second.reserve_quote_before, shadow_after_buy_y + 1.0)

    def test_new_invalid_snapshot_blocks_using_an_older_valid_state(self) -> None:
        invalid = event(200)
        invalid.pop("poolFeeRate")
        simulator, temp = self.make_simulator([event(100), invalid])
        self.addCleanup(temp.cleanup)
        result = simulator.execute(
            order_id="invalid-newer-state", timestamp_ms=201, mint="mint-1", pool_id="pool-1",
            side="buy", amount=1.0,
        )
        self.assertEqual(result.status, "unsupported_pool")
        self.assertEqual(result.reason, "missing_or_invalid_pool_fee_rate")

    def test_stale_pool_state_is_rejected(self) -> None:
        simulator, temp = self.make_simulator([event(100)], max_state_age_ms=10)
        self.addCleanup(temp.cleanup)
        result = simulator.execute(
            order_id="stale", timestamp_ms=111, mint="mint-1", pool_id="pool-1",
            side="buy", amount=1.0,
        )
        self.assertEqual(result.status, "stale_pool_state")

    def test_price_impact_guard_rejects_fill_without_mutating_state(self) -> None:
        simulator, temp = self.make_simulator([event(100)], max_price_impact_bps=1.0)
        self.addCleanup(temp.cleanup)
        result = simulator.execute(
            order_id="large", timestamp_ms=101, mint="mint-1", pool_id="pool-1",
            side="buy", amount=1.0,
        )
        self.assertEqual(result.status, "price_impact_exceeded")
        state = simulator._states[("mint-1", "pool-1")]
        self.assertEqual(state.shadow_token_reserve, 1_000.0)
        self.assertEqual(state.shadow_quote_reserve, 100.0)

    def test_sell_cannot_exceed_token_reserve(self) -> None:
        simulator, temp = self.make_simulator([event(100)])
        self.addCleanup(temp.cleanup)
        result = simulator.execute(
            order_id="too-large", timestamp_ms=101, mint="mint-1", pool_id="pool-1",
            side="sell", amount=1_000.0,
        )
        self.assertEqual(result.status, "insufficient_liquidity")


    def test_run_orders_writes_fill_csv(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            events_path = root / "events.jsonl"
            orders_path = root / "orders.csv"
            output_path = root / "fills.csv"

            events_path.write_text(
                json.dumps(event(100, action="create")) + "\n",
                encoding="utf-8",
            )
            orders_path.write_text(
                "timestamp_ms,mint,pool_id,side,amount,order_id\n"
                "101,mint-1,pool-1,buy,1.0,buy-1\n",
                encoding="utf-8",
            )

            summary = run_orders(
                events_path,
                orders_path,
                output_path,
                max_state_age_ms=60_000,
                max_price_impact_bps=None,
            )

            self.assertEqual(summary["filled"], 1)
            self.assertEqual(summary["orders_total"], 1)
            with output_path.open("r", newline="", encoding="utf-8") as source:
                rows = list(csv.DictReader(source))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["order_id"], "buy-1")
            self.assertEqual(rows[0]["status"], "filled")

    def test_out_of_order_source_events_fail_fast(self) -> None:
        simulator, temp = self.make_simulator([event(200), event(100)])
        self.addCleanup(temp.cleanup)
        with self.assertRaises(ValueError):
            simulator.advance_before(300)

    def test_out_of_order_orders_are_rejected_without_look_ahead(self) -> None:
        simulator, temp = self.make_simulator([event(100), event(200, token_reserve=2_000)])
        self.addCleanup(temp.cleanup)
        first = simulator.execute(
            order_id="late", timestamp_ms=201, mint="mint-1", pool_id="pool-1",
            side="buy", amount=0.001,
        )
        self.assertEqual(first.status, "filled")
        earlier = simulator.execute(
            order_id="early", timestamp_ms=150, mint="mint-1", pool_id="pool-1",
            side="buy", amount=0.001,
        )
        self.assertEqual(earlier.status, "invalid_order")
        self.assertEqual(earlier.reason, "orders_must_be_non_decreasing_in_time")


if __name__ == "__main__":
    unittest.main(verbosity=2)
