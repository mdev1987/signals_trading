from __future__ import annotations

import csv
import math
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
    if pool in ("pump", "raydium-launchpad"):
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
        snapshot = snapshot_from_event(event(100, pool="something-else"))
        self.assertEqual(snapshot.unsupported_reason, "unsupported_pool_model")

    def test_concentrated_liquidity_pools_are_unsupported(self) -> None:
        for pool in ("raydium-clmm", "meteora-dlmm", "orca-whirlpool"):
            with self.subTest(pool=pool):
                snapshot = snapshot_from_event(event(100, pool=pool))
                self.assertEqual(
                    snapshot.unsupported_reason, "unsupported_concentrated_liquidity"
                )

    def test_non_wsol_quote_without_quote_price_is_unsupported(self) -> None:
        snapshot = snapshot_from_event(event(100, quote_mint="USDC"))
        self.assertEqual(snapshot.unsupported_reason, "missing_quote_price")

    def test_non_wsol_quote_is_supported_with_quote_price(self) -> None:
        raw = event(100, quote_mint="USDC")
        raw["quotePriceSol"] = 0.01
        raw["solRouteFeeRate"] = 0.02245
        snapshot = snapshot_from_event(raw)
        self.assertIsNone(snapshot.unsupported_reason)
        self.assertEqual(snapshot.quote_price_sol, 0.01)
        self.assertEqual(snapshot.fee_rate, 0.02245)

    def test_raydium_launchpad_uses_bonding_curve_reserves(self) -> None:
        raw = event(100, pool="raydium-launchpad")
        snapshot = snapshot_from_event(raw)
        self.assertIsNone(snapshot.unsupported_reason)
        self.assertEqual(snapshot.token_reserve, 1_000.0)
        self.assertEqual(snapshot.quote_reserve, 100.0)
        self.assertEqual(snapshot.reserve_model, "bonding_curve")

    def test_vault_cp_pools_use_pool_reserves(self) -> None:
        for pool in ("raydium-cpmm", "meteora-damm-v2"):
            with self.subTest(pool=pool):
                snapshot = snapshot_from_event(event(100, pool=pool))
                self.assertIsNone(snapshot.unsupported_reason)
                self.assertEqual(snapshot.token_reserve, 1_000.0)
                self.assertEqual(snapshot.quote_reserve, 100.0)
                self.assertEqual(snapshot.reserve_model, "amm_vault")

    def test_meteora_launchpad_requires_a_reported_price(self) -> None:
        raw = event(100, pool="meteora-launchpad")
        raw["priceSol"] = 3.5e-8
        snapshot = snapshot_from_event(raw)
        self.assertIsNone(snapshot.unsupported_reason)
        self.assertEqual(snapshot.reserve_model, "launchpad_linear_curve")

        raw.pop("priceSol")
        self.assertEqual(
            snapshot_from_event(raw).unsupported_reason, "missing_price_sol"
        )

    def test_wsol_route_fee_falls_back_to_pool_fee(self) -> None:
        raw = event(100)
        raw.pop("solRouteFeeRate", None)
        snapshot = snapshot_from_event(raw)
        self.assertEqual(snapshot.fee_rate, 0.01)

    def test_drained_pool_is_not_a_valid_state(self) -> None:
        raw = event(100, token_reserve=1e-9, quote_reserve=1e-9)
        snapshot = snapshot_from_event(raw)
        self.assertEqual(snapshot.unsupported_reason, "pool_reserves_below_floor")

    def test_thin_but_tradeable_pool_is_still_valid(self) -> None:
        raw = event(100, token_reserve=50_000.0, quote_reserve=0.01)
        snapshot = snapshot_from_event(raw)
        self.assertIsNone(snapshot.unsupported_reason)

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

    def test_invalid_snapshot_after_valid_at_same_timestamp_blocks_order(self) -> None:
        invalid = event(100)
        invalid["action"] = "add"
        invalid.pop("vQuoteInBondingCurve")
        simulator, temp = self.make_simulator([event(100), invalid])
        self.addCleanup(temp.cleanup)
        result = simulator.execute(
            order_id="same-ts-valid-then-invalid", timestamp_ms=101,
            mint="mint-1", pool_id="pool-1", side="buy", amount=0.001,
        )
        self.assertEqual(result.status, "unsupported_pool")
        self.assertEqual(result.reason, "missing_bonding_curve_reserves")

    def test_valid_snapshot_after_invalid_at_same_timestamp_allows_order(self) -> None:
        invalid = event(100)
        invalid["action"] = "add"
        invalid.pop("vQuoteInBondingCurve")
        valid = event(100)
        valid["signature"] = "sig-valid-later"
        simulator, temp = self.make_simulator([invalid, valid])
        self.addCleanup(temp.cleanup)
        result = simulator.execute(
            order_id="same-ts-invalid-then-valid", timestamp_ms=101,
            mint="mint-1", pool_id="pool-1", side="buy", amount=0.001,
        )
        self.assertEqual(result.status, "filled")
        self.assertEqual(result.signature, "sig-valid-later")

    def test_shadow_reserve_becoming_non_positive_reports_reason(self) -> None:
        simulator, temp = self.make_simulator([
            event(100),
            event(200, token_reserve=500.0, quote_reserve=100.0),
        ])
        self.addCleanup(temp.cleanup)
        # Drain almost all shadow tokens, then apply an observed reserve drop
        # that would push the shadow token reserve below zero.
        filled = simulator.execute(
            order_id="drain", timestamp_ms=101, mint="mint-1", pool_id="pool-1",
            side="buy", amount=1_000_000.0,
        )
        self.assertEqual(filled.status, "filled")
        result = simulator.execute(
            order_id="after-shadow-break", timestamp_ms=201, mint="mint-1",
            pool_id="pool-1", side="buy", amount=0.001,
        )
        self.assertEqual(result.status, "unsupported_pool")
        self.assertEqual(result.reason, "shadow_reserve_became_non_positive")

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


class PoolResolutionTests(unittest.TestCase):
    """Routing a mint to one of its live pools at order time."""

    def make_simulator(self, events: list[dict], **kwargs) -> tuple[ExecutionSimulator, tempfile.TemporaryDirectory]:
        temp = tempfile.TemporaryDirectory()
        path = Path(temp.name) / "events.jsonl"
        path.write_text("\n".join(json.dumps(item) for item in events) + "\n", encoding="utf-8")
        return ExecutionSimulator(path, **kwargs), temp

    def test_resolves_the_deepest_fresh_pool(self) -> None:
        shallow = event(100, pool="pump", pool_id="pump-pool", quote_reserve=100.0)
        deep = event(110, pool="pump-amm", pool_id="amm-pool", quote_reserve=500.0)
        simulator, temp = self.make_simulator([shallow, deep])
        self.addCleanup(temp.cleanup)

        snapshot = simulator.resolve_pool("mint-1", 200)

        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.pool_id, "amm-pool")
        self.assertEqual(snapshot.quote_reserve, 500.0)

    def test_wsol_quote_wins_even_when_shallower(self) -> None:
        wsol = event(100, pool="pump-amm", pool_id="wsol-pool", quote_reserve=50.0)
        usdc = event(110, pool="raydium-cpmm", pool_id="usdc-pool", quote_reserve=10_000.0)
        usdc["quoteMint"] = "USDC"
        usdc["quotePriceSol"] = 0.01
        usdc["solRouteFeeRate"] = 0.02245
        usdc["poolFeeRate"] = 0.02
        simulator, temp = self.make_simulator([wsol, usdc])
        self.addCleanup(temp.cleanup)

        snapshot = simulator.resolve_pool("mint-1", 200)

        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.pool_id, "wsol-pool")

    def test_stale_pools_are_not_resolved(self) -> None:
        simulator, temp = self.make_simulator([event(100)])
        self.addCleanup(temp.cleanup)

        self.assertIsNone(simulator.resolve_pool("mint-1", 100_000))

    def test_unknown_mint_has_no_pool(self) -> None:
        simulator, temp = self.make_simulator([event(100)])
        self.addCleanup(temp.cleanup)

        self.assertIsNone(simulator.resolve_pool("mint-other", 200))

    def test_migration_moves_routing_to_the_new_pool(self) -> None:
        # Bonding curve stops trading at t=100; the migrated AMM pool is the
        # only fresh state two hours later.
        curve = event(100, pool="pump", pool_id="curve-pool", quote_reserve=500.0)
        amm = event(7_300_000, pool="pump-amm", pool_id="amm-pool", quote_reserve=120.0)
        simulator, temp = self.make_simulator([curve, amm])
        self.addCleanup(temp.cleanup)

        self.assertEqual(
            simulator.resolve_pool("mint-1", 200).pool_id, "curve-pool"
        )
        self.assertEqual(
            simulator.resolve_pool("mint-1", 7_300_100).pool_id, "amm-pool"
        )

    def test_spot_price_sol_returns_mark_and_pool(self) -> None:
        simulator, temp = self.make_simulator([event(100, quote_reserve=100.0)])
        self.addCleanup(temp.cleanup)

        mark = simulator.spot_price_sol("mint-1", 200)

        self.assertIsNotNone(mark)
        price, timestamp_ms, pool_id = mark
        self.assertAlmostEqual(price, 0.1)
        self.assertEqual(timestamp_ms, 100)
        self.assertEqual(pool_id, "pool-1")

    def test_mark_price_ignores_future_snapshots(self) -> None:
        simulator, temp = self.make_simulator(
            [event(100, quote_reserve=100.0), event(5_000, quote_reserve=500.0)]
        )
        self.addCleanup(temp.cleanup)

        early = simulator.mark_price_sol("mint-1", 1_000)
        late = simulator.mark_price_sol("mint-1", 6_000)

        self.assertAlmostEqual(early[0], 0.1)
        self.assertEqual(early[1], 100)
        self.assertAlmostEqual(late[0], 0.5)
        self.assertEqual(late[1], 5_000)

    def test_mark_price_falls_back_to_the_last_valid_state(self) -> None:
        drained = event(5_000, token_reserve=1e-9, quote_reserve=1e-9)
        simulator, temp = self.make_simulator([event(100), drained])
        self.addCleanup(temp.cleanup)

        mark = simulator.mark_price_sol("mint-1", 10_000)

        self.assertAlmostEqual(mark[0], 0.1)
        self.assertEqual(mark[1], 100)


class PoolCoverageTests(unittest.TestCase):
    """Fills for the newly supported pool models and quote assets."""

    def make_simulator(self, events: list[dict], **kwargs) -> tuple[ExecutionSimulator, tempfile.TemporaryDirectory]:
        temp = tempfile.TemporaryDirectory()
        path = Path(temp.name) / "events.jsonl"
        path.write_text("\n".join(json.dumps(item) for item in events) + "\n", encoding="utf-8")
        return ExecutionSimulator(path, **kwargs), temp

    def test_raydium_launchpad_bonding_curve_fill(self) -> None:
        simulator, temp = self.make_simulator([event(100, pool="raydium-launchpad")])
        self.addCleanup(temp.cleanup)
        result = simulator.execute(
            order_id="rl-buy", timestamp_ms=101, mint="mint-1", pool_id="pool-1",
            side="buy", amount=1.0,
        )
        self.assertEqual(result.status, "filled")
        self.assertEqual(result.reserve_model, "bonding_curve")
        expected_net = 1.0 * 0.99
        self.assertAlmostEqual(result.quote_net, expected_net)
        self.assertAlmostEqual(
            result.token_amount, 1_000 * expected_net / (100.0 + expected_net)
        )

    def test_vault_cp_fill_uses_pool_reserves(self) -> None:
        for pool in ("raydium-cpmm", "meteora-damm-v2"):
            with self.subTest(pool=pool):
                simulator, temp = self.make_simulator([event(100, pool=pool)])
                self.addCleanup(temp.cleanup)
                result = simulator.execute(
                    order_id="cp-buy", timestamp_ms=101, mint="mint-1",
                    pool_id="pool-1", side="buy", amount=1.0,
                )
                self.assertEqual(result.status, "filled")
                self.assertEqual(result.reserve_model, "amm_vault")
                expected_net = 0.99
                self.assertAlmostEqual(
                    result.token_amount, 1_000 * expected_net / (100.0 + expected_net)
                )

    def test_non_wsol_buy_converts_at_quote_price_and_route_fee(self) -> None:
        raw = event(100, pool="raydium-cpmm", quote_mint="USDC")
        raw["quoteInPool"] = 10_000.0
        raw["tokensInPool"] = 1_000_000_000.0
        raw["poolFeeRate"] = 0.02
        raw["solRouteFeeRate"] = 0.02245
        raw["quotePriceSol"] = 0.01
        simulator, temp = self.make_simulator([raw])
        self.addCleanup(temp.cleanup)

        result = simulator.execute(
            order_id="usdc-buy", timestamp_ms=101, mint="mint-1", pool_id="pool-1",
            side="buy", amount=0.1,
        )

        self.assertEqual(result.status, "filled")
        self.assertEqual(result.quote_price_sol, 0.01)
        net_sol = 0.1 * (1.0 - 0.02245)
        net_quote = net_sol / 0.01
        self.assertAlmostEqual(result.quote_net, net_sol)
        self.assertAlmostEqual(result.quote_fee, 0.1 - net_sol)
        self.assertAlmostEqual(result.reference_spot_price, 100.0 / 1_000_000_000.0)
        self.assertAlmostEqual(
            result.token_amount, 1_000_000_000.0 * net_quote / (10_000.0 + net_quote)
        )
        self.assertAlmostEqual(
            result.average_price_quote_per_token, 0.1 / result.token_amount
        )

    def test_non_wsol_sell_returns_sol_after_route_fee(self) -> None:
        raw = event(100, pool="meteora-damm-v2", quote_mint="USDC")
        raw["quoteInPool"] = 10_000.0
        raw["tokensInPool"] = 1_000_000_000.0
        raw["poolFeeRate"] = 0.02
        raw["solRouteFeeRate"] = 0.02245
        raw["quotePriceSol"] = 0.01
        simulator, temp = self.make_simulator([raw])
        self.addCleanup(temp.cleanup)

        token_in = 1_000_000.0
        result = simulator.execute(
            order_id="usdc-sell", timestamp_ms=101, mint="mint-1", pool_id="pool-1",
            side="sell", amount=token_in,
        )

        self.assertEqual(result.status, "filled")
        gross_quote = 10_000.0 * token_in / (1_000_000_000.0 + token_in)
        gross_sol = gross_quote * 0.01
        self.assertAlmostEqual(result.quote_gross, gross_sol)
        self.assertAlmostEqual(result.quote_net, gross_sol * (1.0 - 0.02245))
        # The shadow quote reserve moves by the gross curve output.
        self.assertAlmostEqual(
            result.reserve_quote_before - result.reserve_quote_after, gross_sol
        )

    def test_meteora_launchpad_needs_two_observations_before_filling(self) -> None:
        first = event(100, pool="meteora-launchpad")
        first["priceSol"] = 4.0e-8
        simulator, temp = self.make_simulator([first])
        self.addCleanup(temp.cleanup)

        result = simulator.execute(
            order_id="lp-no-slope", timestamp_ms=101, mint="mint-1",
            pool_id="pool-1", side="buy", amount=0.1,
        )

        self.assertEqual(result.status, "unsupported_pool")
        self.assertEqual(result.reason, "missing_curve_slope")

    def test_meteora_launchpad_buy_uses_observed_curve_slope(self) -> None:
        first = event(100, pool="meteora-launchpad")
        first["quoteInPool"] = 5.0
        first["priceSol"] = 4.0e-8
        second = event(200, pool="meteora-launchpad")
        first["tokensInPool"] = 1_000_000_000.0
        second["tokensInPool"] = 1_000_000_000.0
        second["quoteInPool"] = 6.0
        second["priceSol"] = 4.5e-8
        simulator, temp = self.make_simulator([first, second])
        self.addCleanup(temp.cleanup)

        result = simulator.execute(
            order_id="lp-buy", timestamp_ms=201, mint="mint-1", pool_id="pool-1",
            side="buy", amount=0.1,
        )

        slope = (4.5e-8 - 4.0e-8) / (6.0 - 5.0)
        net = 0.1 * 0.99
        expected_tokens = math.log1p(slope * net / 4.5e-8) / slope
        self.assertEqual(result.status, "filled")
        self.assertEqual(result.reserve_model, "launchpad_linear_curve")
        self.assertAlmostEqual(result.reference_spot_price, 4.5e-8)
        self.assertAlmostEqual(result.token_amount, expected_tokens)
        self.assertAlmostEqual(result.quote_net, net)
        self.assertGreater(result.price_impact_bps, 0.0)
        self.assertAlmostEqual(
            simulator._states[("mint-1", "pool-1")].shadow_price_sol,
            4.5e-8 + slope * net,
        )

    def test_meteora_launchpad_sell_is_the_inverse_curve(self) -> None:
        first = event(100, pool="meteora-launchpad")
        first["quoteInPool"] = 5.0
        first["priceSol"] = 4.0e-8
        second = event(200, pool="meteora-launchpad")
        first["tokensInPool"] = 1_000_000_000.0
        second["tokensInPool"] = 1_000_000_000.0
        second["quoteInPool"] = 6.0
        second["priceSol"] = 4.5e-8
        simulator, temp = self.make_simulator([first, second])
        self.addCleanup(temp.cleanup)

        token_in = 1_000_000.0
        result = simulator.execute(
            order_id="lp-sell", timestamp_ms=201, mint="mint-1", pool_id="pool-1",
            side="sell", amount=token_in,
        )

        slope = (4.5e-8 - 4.0e-8) / (6.0 - 5.0)
        expected_gross = (4.5e-8 * math.exp(slope * token_in) - 4.5e-8) / slope
        self.assertEqual(result.status, "filled")
        self.assertAlmostEqual(result.quote_gross, expected_gross)
        self.assertAlmostEqual(result.quote_net, expected_gross * 0.99)

    def test_concentrated_liquidity_pool_rejects_orders(self) -> None:
        for pool in ("raydium-clmm", "meteora-dlmm", "orca-whirlpool"):
            with self.subTest(pool=pool):
                simulator, temp = self.make_simulator([event(100, pool=pool)])
                self.addCleanup(temp.cleanup)
                result = simulator.execute(
                    order_id="clmm", timestamp_ms=101, mint="mint-1",
                    pool_id="pool-1", side="buy", amount=0.1,
                )
                self.assertEqual(result.status, "unsupported_pool")
                self.assertEqual(
                    result.reason, "unsupported_concentrated_liquidity"
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
