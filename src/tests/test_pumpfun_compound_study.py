"""Tests for the ``pumpfun_jupiter_compound_v1`` research simulator.

These pin the mechanics the study's conclusions rest on: the
quote-asset-aware pool mark and fills, the rejection gates for records whose
reserves disagree with their feed price, the point-in-time activity window,
and the compounding ledger rules from the specification.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parents[1]
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from pumpfun_compound_study import (  # noqa: E402
    CandidateStats,
    Ledger,
    PoolState,
    StrategyConfig,
    TokenTrack,
    _trailing_stats,
    bootstrap_day_runs,
    buy_tokens,
    channel_rejection_reasons,
    continues_pump_lineage,
    creator_buy_in_sol,
    dead_pool_step,
    entry_impact_bps,
    exit_impact_bps,
    main,
    pooled_summary,
    rejection_reasons,
    sell_sol,
    wilson_interval,
)

WSOL = "So11111111111111111111111111111111111111112"
PREN = "Pren1FvFX6J3E4kXhJuCiAD5aDmGEb7qJRncwA8Lkhw"


def make_state(**overrides) -> PoolState:
    defaults = dict(
        ts_ms=1_000,
        price_sol=1e-6,
        price_quote=1e-6,
        price_usd=7.5e-5,
        market_cap_usd=75_000.0,
        quote_usd=75.0,
        tokens=1_000_000_000.0,
        quote=100.0,
        pool="pump-amm",
        quote_mint=WSOL,
    )
    defaults.update(overrides)
    return PoolState(**defaults)


def qualifying_stats(**overrides) -> CandidateStats:
    defaults = dict(
        mint="mint",
        age_seconds=25.0,
        liquidity_usd=9_000.0,
        market_cap_usd=50_000.0,
        volume_5m_usd=2_000.0,
        transactions_5m=20,
        buys_5m=15,
        sells_5m=5,
        creator_buy_sol=1.0,
        price_sol=1e-6,
        quote_usd=75.0,
        name="token",
        quote_mint=WSOL,
        mark_price_usd=7.5e-5,
        price_discrepancy=0.0,
    )
    defaults.update(overrides)
    return CandidateStats(**defaults)


class PoolMarkTests(unittest.TestCase):
    def test_sol_pool_mark_is_the_feed_price(self) -> None:
        state = make_state()
        # The tradeable reserve is quote/price, so the mark is the feed price
        # and quote_usd is USD per SOL.
        self.assertAlmostEqual(state.reserve_price_quote(), 1e-6)
        self.assertAlmostEqual(state.effective_tokens(), 100.0 / 1e-6)
        self.assertAlmostEqual(state.mark_price_usd(), 1e-6 * 75.0)
        self.assertAlmostEqual(state.liquidity_usd(), 2 * 100.0 * 75.0)

    def test_non_sol_quote_converts_with_its_own_quote_usd(self) -> None:
        # A Pren1F-quoted pool: price is in quote units, quote_usd ~ $963.
        state = make_state(
            quote_mint=PREN,
            price_quote=8.578e-9,
            price_usd=8.26e-6,
            quote_usd=8.26e-6 / 8.578e-9,
            price_sol=1.099e-7,
            tokens=657_606_565.0,
            quote=5.641,
        )
        self.assertAlmostEqual(state.mark_price_usd(), 8.26e-6, places=9)
        self.assertAlmostEqual(state.liquidity_usd(), 2 * 5.641 * state.quote_usd)

    def test_feed_reserve_mismatch_is_reported(self) -> None:
        # 84CoroQB style record: feed price ~4.7x the price of its reserves.
        state = make_state(price_quote=2.7646e-8, price_sol=2.7646e-8)
        state.tokens = 806_095_589.0
        state.quote = 4.7168
        # The feed price is ~4.7x the reserve price, a relative gap near 0.79.
        self.assertGreater(state.price_discrepancy(), 0.7)

    def test_missing_price_fields_fall_back(self) -> None:
        state = make_state(price_quote=None, quote_usd=None, price_usd=None)
        # No conversion and no feed price: the reserve mark is unknowable.
        self.assertIsNone(state.mark_price_usd())
        self.assertIsNone(state.liquidity_usd())

    def test_carry_forward_fills_quote_usd(self) -> None:
        prev = make_state()
        add = make_state(price_quote=None, price_usd=None, quote_usd=None)
        add.with_carry(prev)
        self.assertEqual(add.quote_usd, prev.quote_usd)
        self.assertEqual(add.quote_mint, WSOL)


class PoolLineageTests(unittest.TestCase):
    def test_same_pool_is_the_same_lineage(self) -> None:
        prev = make_state(pool="pump-amm", pool_id="pool-a")
        new = make_state(pool="pump-amm", pool_id="pool-a")
        self.assertTrue(continues_pump_lineage(prev, new))

    def test_curve_to_amm_migration_is_allowed_once(self) -> None:
        prev = make_state(pool="pump", pool_id="curve")
        new = make_state(pool="pump-amm", pool_id="amm")
        self.assertTrue(continues_pump_lineage(prev, new))

    def test_amm_to_curve_is_not_a_continuation(self) -> None:
        prev = make_state(pool="pump-amm", pool_id="amm")
        new = make_state(pool="pump", pool_id="curve")
        self.assertFalse(continues_pump_lineage(prev, new))

    def test_third_party_venue_is_rejected(self) -> None:
        prev = make_state(pool="pump-amm", pool_id="amm")
        new = make_state(pool="meteora-dlmm", pool_id="dlmm")
        self.assertFalse(continues_pump_lineage(prev, new))

    def test_a_jumpy_migration_is_not_a_continuation(self) -> None:
        # The 2026-11-18 malformed records opened the AMM ~3800x above the
        # curve's last print; following them fabricated a 59.9x trade.
        prev = make_state(pool="pump", pool_id="curve", price_quote=1e-9)
        new = make_state(pool="pump-amm", pool_id="amm", price_quote=4e-6)
        self.assertFalse(continues_pump_lineage(prev, new))

    def test_quote_asset_change_is_rejected(self) -> None:
        prev = make_state(pool="pump", pool_id="curve", quote_mint=WSOL)
        new = make_state(pool="pump-amm", pool_id="amm", quote_mint=PREN)
        self.assertFalse(continues_pump_lineage(prev, new))

    def test_track_ignores_a_second_venue_state(self) -> None:
        track = TokenTrack(
            mint="mint", t0_ms=0, name="", symbol="", creator_buy_sol=None
        )
        self.assertTrue(track.accept_state(make_state(pool="pump-amm", pool_id="amm")))
        mark = track.state
        assert mark is not None
        self.assertFalse(
            track.accept_state(
                make_state(
                    pool="meteora-dlmm",
                    pool_id="dlmm",
                    ts_ms=2_000,
                    tokens=99_386.0,
                    quote=15.5,
                )
            )
        )
        self.assertIs(track.state, mark)


class FillTests(unittest.TestCase):
    def test_round_trip_loses_fees_and_impact(self) -> None:
        state = make_state(tokens=1_000_000_000.0, quote=100.0)
        tokens = buy_tokens(state, 0.01, 0.0125)
        self.assertIsNotNone(tokens)
        assert tokens is not None
        proceeds = sell_sol(state, tokens, 0.0125)
        self.assertIsNotNone(proceeds)
        assert proceeds is not None
        # Two 1.25% fees plus a little constant-product impact.
        self.assertLess(proceeds, 0.01)
        self.assertGreater(proceeds, 0.0096)
        self.assertAlmostEqual(proceeds / 0.01, 0.9751, places=3)

    def test_buy_quantity_matches_constant_product(self) -> None:
        state = make_state(tokens=1_000_000_000.0, quote=100.0)
        reserve = 100.0 / 1e-6
        tokens = buy_tokens(state, 0.01, 0.0)
        assert tokens is not None
        expected = reserve - (reserve * 100.0 / (100.0 + 0.01))
        self.assertAlmostEqual(tokens, expected, places=6)

    def test_price_fallback_when_reserves_missing(self) -> None:
        state = make_state(tokens=None, quote=None, price_quote=2e-6)
        tokens = buy_tokens(state, 0.01, 0.0)
        assert tokens is not None
        self.assertAlmostEqual(tokens, 0.01 / 2e-6)


class RejectionTests(unittest.TestCase):
    def test_clean_candidate_passes(self) -> None:
        self.assertEqual(rejection_reasons(qualifying_stats(), StrategyConfig()), [])

    def test_config_pins_the_preregistered_gates(self) -> None:
        # The study pre-registered stricter values than the course baseline.
        # Anything else is a new variant and must not be pooled with these runs.
        config = StrategyConfig()
        self.assertEqual(config.buy_sell_ratio_min, 1.5)
        self.assertEqual(config.liquidity_min_usd, 5_000.0)
        self.assertEqual(config.liquidity_max_usd, 2_000_000.0)
        self.assertEqual(config.volume_5m_min_usd, 2_000.0)
        self.assertEqual(config.transactions_5m_min, 12)
        self.assertEqual(config.buys_5m_min, 8)
        self.assertEqual(config.market_cap_max_usd, 10_000_000.0)
        self.assertEqual(config.wash_volume_to_liquidity_max, 50.0)
        self.assertEqual(config.creator_buy_max_sol, 20.0)

    def test_buy_sell_ratio_at_the_floor_passes(self) -> None:
        stats = qualifying_stats(buys_5m=15, sells_5m=10)
        self.assertEqual(rejection_reasons(stats, StrategyConfig()), [])

    def test_buy_sell_ratio_below_the_floor_is_rejected(self) -> None:
        stats = qualifying_stats(buys_5m=9, sells_5m=7)
        self.assertIn(
            "BUY_SELL_RATIO_BELOW_MIN",
            rejection_reasons(stats, StrategyConfig()),
        )

    def test_ratio_just_above_the_floor_is_not_a_separate_rejection(self) -> None:
        stats = qualifying_stats(buys_5m=9, sells_5m=5)
        self.assertEqual(rejection_reasons(stats, StrategyConfig()), [])

    def test_non_sol_quote_is_allowed(self) -> None:
        stats = qualifying_stats(quote_mint=PREN)
        self.assertEqual(rejection_reasons(stats, StrategyConfig()), [])

    def test_structural_amm_price_gap_is_not_rejected(self) -> None:
        # A Pump AMM feed price is FDV-based, so a ~20 % gap to the reserve
        # price is normal and must not remove the whole AMM universe.
        stats = qualifying_stats(price_discrepancy=0.25)
        self.assertEqual(rejection_reasons(stats, StrategyConfig()), [])

    def test_sol_usd_fallback_converts_legacy_archive_records(self) -> None:
        from pumpfun_compound_study import pool_state_from_event

        event = {
            "mint": "mint",
            "quoteMint": WSOL,
            "action": "buy",
            "timestamp": 1_000,
            "priceSol": 1e-7,
            "price": 1e-7,
            "marketCapSol": 500.0,
            "tokensInPool": 1_000_000_000.0,
            "quoteInPool": 100.0,
            "pool": "pump-amm",
        }
        state = pool_state_from_event(event, sol_usd=76.59)
        assert state is not None
        self.assertAlmostEqual(state.quote_usd, 76.59)
        self.assertAlmostEqual(state.mark_price_usd(), 1e-7 * 76.59)
        self.assertAlmostEqual(state.market_cap_usd, 500.0 * 76.59)
        self.assertAlmostEqual(state.liquidity_usd(), 2 * 100.0 * 76.59)

    def test_modern_record_ignores_the_fallback(self) -> None:
        from pumpfun_compound_study import pool_state_from_event

        event = {
            "mint": "mint",
            "quoteMint": WSOL,
            "action": "buy",
            "timestamp": 1_000,
            "priceSol": 1e-7,
            "price": 1e-7,
            "priceUsd": 2e-5,
            "marketCapUsd": 20_000.0,
            "tokensInPool": 1_000_000_000.0,
            "quoteInPool": 100.0,
            "pool": "pump-amm",
        }
        state = pool_state_from_event(event, sol_usd=999.0)
        assert state is not None
        self.assertAlmostEqual(state.quote_usd, 2e-5 / 1e-7)
        self.assertAlmostEqual(state.market_cap_usd, 20_000.0)

    def test_missing_mark_is_rejected(self) -> None:
        stats = qualifying_stats(mark_price_usd=None)
        self.assertIn("MISSING_POOL_PRICE", rejection_reasons(stats, StrategyConfig()))

    def test_token_age_applies_only_to_creation_lane(self) -> None:
        stats = qualifying_stats(age_seconds=900.0)
        self.assertIn("TOKEN_TOO_OLD", rejection_reasons(stats, StrategyConfig()))
        self.assertNotIn(
            "TOKEN_TOO_OLD",
            rejection_reasons(stats, StrategyConfig(), require_age=False),
        )


class ActivityWindowTests(unittest.TestCase):
    def test_prints_after_the_decision_instant_are_ignored(self) -> None:
        track = TokenTrack(
            mint="mint",
            t0_ms=0,
            name="token",
            symbol="TOK",
            creator_buy_sol=None,
        )
        track.trades.extend(
            [
                (1_000, 1.0, 2.0, True),
                (2_000, 1.0, 2.0, False),
                (9_000, 100.0, 100.0, True),
            ]
        )
        volume, transactions, buys, sells = _trailing_stats(track, 2_500)
        self.assertEqual((transactions, buys, sells), (2, 1, 1))
        self.assertAlmostEqual(volume, 4.0)

    def test_prints_outside_the_window_are_dropped(self) -> None:
        track = TokenTrack(
            mint="mint",
            t0_ms=0,
            name="token",
            symbol="TOK",
            creator_buy_sol=None,
        )
        track.trades.extend([(0, 1.0, 2.0, True), (400_000, 1.0, 2.0, True)])
        volume, transactions, _buys, _sells = _trailing_stats(track, 400_000)
        self.assertEqual(transactions, 1)
        self.assertAlmostEqual(volume, 2.0)


class LedgerTests(unittest.TestCase):
    def test_lane_a_keeps_a_fixed_size(self) -> None:
        config = StrategyConfig()
        ledger = Ledger(lane="a", config=config, trade_size_usd=2.0)
        ledger.on_close(2.0, 4.0, 1_000)
        self.assertEqual(ledger.trade_size_usd, 2.0)
        self.assertEqual(ledger.saved_usd, 0.0)
        self.assertEqual(ledger.cycles, 1)

    def test_lane_b_splits_a_win_sixty_forty(self) -> None:
        ledger = Ledger(lane="b", config=StrategyConfig(), trade_size_usd=2.0)
        ledger.on_close(2.0, 4.0, 1_000)
        self.assertAlmostEqual(ledger.saved_usd, 1.6)
        self.assertAlmostEqual(ledger.trade_size_usd, 2.4)

    def test_lane_b_pauses_after_two_losses(self) -> None:
        ledger = Ledger(lane="b", config=StrategyConfig(), trade_size_usd=2.0)
        ledger.on_close(2.0, 1.5, 1_000)
        self.assertEqual(ledger.pauses, 0)
        ledger.on_close(1.5, 1.2, 5_000)
        self.assertEqual(ledger.pauses, 1)
        self.assertEqual(ledger.pause_until_ms, 305_000)
        self.assertFalse(ledger.can_enter(304_999))
        self.assertTrue(ledger.can_enter(305_000))

    def test_lane_b_tops_up_below_the_play_floor(self) -> None:
        ledger = Ledger(lane="b", config=StrategyConfig(), trade_size_usd=2.0)
        ledger.on_close(2.0, 0.5, 1_000)
        self.assertEqual(ledger.floor_resets, 1)
        self.assertAlmostEqual(ledger.topups_usd, 1.5)
        self.assertEqual(ledger.trade_size_usd, 2.0)

    def test_lane_c_does_not_top_up(self) -> None:
        ledger = Ledger(lane="c", config=StrategyConfig(), trade_size_usd=2.0)
        ledger.on_close(2.0, 0.5, 1_000)
        self.assertEqual(ledger.floor_resets, 0)
        self.assertAlmostEqual(ledger.trade_size_usd, 0.5)

    def test_cycle_cap_stops_new_entries(self) -> None:
        config = StrategyConfig(max_cycles=2)
        ledger = Ledger(lane="b", config=config, trade_size_usd=2.0)
        ledger.on_close(2.0, 4.0, 1_000)
        self.assertTrue(ledger.can_enter(2_000))
        ledger.on_close(2.4, 4.8, 3_000)
        self.assertFalse(ledger.can_enter(4_000))

    def test_a_win_resets_the_loss_streak(self) -> None:
        ledger = Ledger(lane="b", config=StrategyConfig(), trade_size_usd=2.0)
        ledger.on_close(2.0, 1.5, 1_000)
        ledger.on_close(1.5, 3.0, 2_000)
        self.assertEqual(ledger.consecutive_losses, 0)
        ledger.on_close(1.8, 1.0, 3_000)
        self.assertEqual(ledger.pauses, 0)


def _trade(pnl: float, cost: float = 2.0, open_: bool = False) -> dict:
    return {
        "entry_cost_usd": cost,
        "pnl_usd": None if open_ else pnl,
        "return_pct": None if open_ else pnl / cost,
        "still_open": open_,
        "entry_ts_ms": 0,
        "exit_ts_ms": None if open_ else 1,
        "exit_reason": "OPEN_AT_END" if open_ else ("TAKE_PROFIT" if pnl > 0 else "STOP_LOSS"),
    }


def _result(pnls: list[float], opens: int = 0, day: str = "2026-11-18") -> dict:
    trades = [_trade(p) for p in pnls] + [_trade(0.0, open_=True) for _ in range(opens)]
    return {
        "strategy_id": "s",
        "source": "channel",
        "lane": "a",
        "day": day,
        "config": StrategyConfig().as_dict(),
        "config_hash": "abc",
        "run_boundary": "one_compounding_run_per_day",
        "trades": trades,
        "outcomes": {
            "exit_reasons": {
                reason: sum(1 for t in trades if t["exit_reason"] == reason)
                for reason in {t["exit_reason"] for t in trades}
            }
        },
    }


class PooledSummaryTests(unittest.TestCase):
    def test_wilson_interval_brackets_the_rate(self) -> None:
        low, high = wilson_interval(50, 100)
        self.assertLess(low, 0.5)
        self.assertGreater(high, 0.5)
        self.assertGreater(high - low, 0.15)
        self.assertEqual(wilson_interval(0, 0), (0.0, 1.0))

    def test_pooled_pnl_and_writeoff_include_open_positions(self) -> None:
        summary = pooled_summary([_result([1.0, -1.0], opens=1, day="2026-11-18")])
        self.assertAlmostEqual(summary["pnl_usd"], 0.0)
        self.assertAlmostEqual(summary["pnl_writeoff_usd"], -2.0)
        self.assertEqual(summary["trades"], 2)
        self.assertEqual(summary["open_at_end"], 1)
        self.assertAlmostEqual(summary["win_rate"], 0.5)

    def test_pooled_days_are_deduplicated_and_sorted(self) -> None:
        summary = pooled_summary(
            [_result([1.0], day="2026-11-19"), _result([-1.0], day="2026-11-18")]
        )
        self.assertEqual(summary["days"], ["2026-11-18", "2026-11-19"])
        self.assertAlmostEqual(summary["pnl_usd"], 0.0)

    def test_per_day_rows_are_reported_per_artifact(self) -> None:
        summary = pooled_summary(
            [_result([1.0, -1.0], day="2026-11-18"), _result([2.0], day="2026-11-19")]
        )
        rows = {row["day"]: row for row in summary["per_day"]}
        self.assertAlmostEqual(rows["2026-11-18"]["pnl_usd"], 0.0)
        self.assertAlmostEqual(rows["2026-11-19"]["pnl_usd"], 2.0)
        self.assertEqual(rows["2026-11-18"]["trades"], 2)
        self.assertAlmostEqual(rows["2026-11-19"]["win_rate"], 1.0)

    def test_break_even_ignores_a_uniform_fee_scale(self) -> None:
        base = pooled_summary([_result([1.0] * 3 + [-0.3] * 7)])
        scaled = pooled_summary(
            [_result([0.95] * 3 + [-0.285] * 7)]
        )
        self.assertAlmostEqual(
            base["break_even_win_rate"], scaled["break_even_win_rate"], places=6
        )


class CreatorBuyTests(unittest.TestCase):
    def test_non_wsol_buy_converts_to_the_same_sol_amount(self) -> None:
        # 2 SOL of creator buy, expressed either in WSOL (quote_usd == SOL/USD)
        # or in a memecoin quote worth 0.5 USD per unit.
        self.assertAlmostEqual(creator_buy_in_sol(2.0, 100.0, 100.0), 2.0)
        self.assertAlmostEqual(creator_buy_in_sol(400.0, 0.5, 100.0), 2.0)

    def test_missing_inputs_stay_missing_rather_than_zero(self) -> None:
        self.assertIsNone(creator_buy_in_sol(None, 100.0, 100.0))
        self.assertIsNone(creator_buy_in_sol(2.0, None, 100.0))
        self.assertIsNone(creator_buy_in_sol(2.0, 100.0, None))

    def test_missing_creator_buy_rejects_the_stream_candidate(self) -> None:
        stats = qualifying_stats(creator_buy_sol=None)
        self.assertIn(
            "MISSING_REQUIRED_DATA", rejection_reasons(stats, StrategyConfig())
        )
        self.assertNotIn(
            "MISSING_REQUIRED_DATA",
            rejection_reasons(stats, StrategyConfig(), require_creator_buy=False),
        )

    def test_channel_cap_applies_whenever_a_create_value_exists(self) -> None:
        config = StrategyConfig()
        too_large = qualifying_stats(creator_buy_sol=21.0)
        self.assertIn(
            "CREATOR_BUY_TOO_LARGE",
            channel_rejection_reasons(too_large, config, creator_buy_known=True),
        )
        small = qualifying_stats(creator_buy_sol=1.0)
        self.assertNotIn(
            "CREATOR_BUY_TOO_LARGE",
            channel_rejection_reasons(small, config, creator_buy_known=True),
        )

    def test_channel_absence_is_an_adaptation_not_a_silent_pass(self) -> None:
        stats = qualifying_stats(creator_buy_sol=None)
        reasons = channel_rejection_reasons(stats, StrategyConfig(), creator_buy_known=False)
        self.assertNotIn("MISSING_REQUIRED_DATA", reasons)
        self.assertNotIn("CREATOR_BUY_TOO_LARGE", reasons)


class DeadPoolTests(unittest.TestCase):
    def test_first_observation_only_starts_the_clock(self) -> None:
        attempt, since, next_check = dead_pool_step(None, 1_000, 600_000)
        self.assertFalse(attempt)
        self.assertEqual(since, 1_000)
        self.assertEqual(next_check, 601_000)

    def test_no_attempt_before_the_retry_interval(self) -> None:
        attempt, since, next_check = dead_pool_step(1_000, 1_000 + 599_999, 600_000)
        self.assertFalse(attempt)
        self.assertEqual(since, 1_000)
        self.assertEqual(next_check, 601_000)

    def test_attempt_exactly_at_the_retry_interval(self) -> None:
        attempt, since, _next = dead_pool_step(1_000, 1_000 + 600_000, 600_000)
        self.assertTrue(attempt)
        self.assertEqual(since, 1_000)

    def test_after_an_attempt_the_next_check_is_another_interval(self) -> None:
        # A failed sell leaves dead_since unchanged, so the next scheduled
        # check is 600 s after the attempt, not an immediate retry.
        attempt, since, next_check = dead_pool_step(1_000, 601_000, 600_000)
        self.assertTrue(attempt)
        self.assertEqual(next_check, 1_201_000)
        # The scheduled check is the earliest one that can attempt again.
        attempt_early, _, _ = dead_pool_step(since, 1_200_999, 600_000)
        self.assertTrue(attempt_early)
        attempt_at, _, _ = dead_pool_step(since, 1_201_000, 600_000)
        self.assertTrue(attempt_at)


class ExecutionLimitTests(unittest.TestCase):
    def _state(self) -> PoolState:
        # 9 000 USD liquidity: 60 quote units at 75 USD each, per side.
        return make_state(quote=60.0, tokens=6e7, price_quote=1e-6)

    def test_small_stake_passes_the_entry_impact_limit(self) -> None:
        state = self._state()
        tokens = buy_tokens(state, 2.0 / 75.0, 0.0125)
        assert tokens is not None
        impact = entry_impact_bps(state, tokens, 2.0, 0.0125)
        assert impact is not None
        self.assertLess(impact, 150.0)

    def test_compounded_stake_breaks_the_entry_impact_limit(self) -> None:
        state = self._state()
        tokens = buy_tokens(state, 200.0 / 75.0, 0.0125)
        assert tokens is not None
        impact = entry_impact_bps(state, tokens, 200.0, 0.0125)
        assert impact is not None
        self.assertGreater(impact, 150.0)

    def test_exit_impact_grows_with_the_position(self) -> None:
        state = self._state()
        small = buy_tokens(state, 2.0 / 75.0, 0.0125)
        large = buy_tokens(state, 800.0 / 75.0, 0.0125)
        assert small is not None and large is not None
        small_proceeds = sell_sol(state, small, 0.0125)
        large_proceeds = sell_sol(state, large, 0.0125)
        assert small_proceeds is not None and large_proceeds is not None
        small_impact = exit_impact_bps(state, small, small_proceeds, 75.0, 0.0125)
        large_impact = exit_impact_bps(state, large, large_proceeds, 75.0, 0.0125)
        assert small_impact is not None and large_impact is not None
        self.assertLess(small_impact, 1_000.0)
        self.assertGreater(large_impact, 1_000.0)


def _lane_result(
    returns: list[float], *, lane: str = "b", day: str = "2026-11-18"
) -> dict:
    """One day-run whose trades have the given fractional returns."""
    trades = []
    for index, value in enumerate(returns):
        cost = 2.0
        trades.append(
            {
                "entry_cost_usd": cost,
                "pnl_usd": cost * value,
                "return_pct": value,
                "still_open": False,
                "entry_ts_ms": index * 1_000,
                "exit_ts_ms": index * 1_000 + 500,
                "exit_reason": "TAKE_PROFIT" if value > 0 else "STOP_LOSS",
            }
        )
    return {
        "strategy_id": "s",
        "source": "channel",
        "lane": lane,
        "day": day,
        "config": StrategyConfig().as_dict(),
        "config_hash": "abc",
        "run_boundary": "one_compounding_run_per_day",
        "trades": trades,
        "outcomes": {"exit_reasons": {}},
    }


class BootstrapTests(unittest.TestCase):
    def test_bootstrap_resamples_whole_day_runs(self) -> None:
        # Two days, two draws: (win, win), (win, loss), (loss, win),
        # (loss, loss). The day totals are +6 and -5.4, so three of the four
        # paths end positive and the interval must span both extremes.
        wins = _lane_result([1.0, 1.0, 1.0], day="2026-11-18")
        losses = _lane_result([-0.9, -0.9, -0.9], day="2026-11-19")
        probability, interval = bootstrap_day_runs(
            [wins, losses], seed=7, resamples=4_000
        )
        assert probability is not None
        self.assertAlmostEqual(probability, 0.75, delta=0.03)
        self.assertAlmostEqual(interval[0], -10.8, delta=0.01)
        self.assertAlmostEqual(interval[1], 12.0, delta=0.01)

    def test_interval_is_centred_on_the_observed_pooled_pnl(self) -> None:
        # The point estimate sums independent daily runs, so the resampled
        # interval must contain it. A continuous-account replay would not.
        results = [
            _lane_result([1.0, -0.9], day="2026-11-18"),
            _lane_result([-0.9, -0.9], day="2026-11-19"),
            _lane_result([1.0, 1.0, -0.9], day="2026-11-20"),
        ]
        summary = pooled_summary(results, seed=5, resamples=4_000)
        low, high = summary["pnl_ci_usd"]
        self.assertLess(low, summary["pnl_usd"])
        self.assertGreater(high, summary["pnl_usd"])

    def test_bootstrap_is_deterministic_for_a_seed(self) -> None:
        results = [
            _lane_result([1.0, -0.9], day="2026-11-18"),
            _lane_result([-0.9, 1.0], day="2026-11-19"),
        ]
        first = bootstrap_day_runs(results, seed=11, resamples=500)
        second = bootstrap_day_runs(results, seed=11, resamples=500)
        self.assertEqual(first[0], second[0])
        self.assertEqual(first[1], second[1])

    def test_a_continuous_run_boundary_is_refused(self) -> None:
        results = [_lane_result([1.0], day="2026-11-18")]
        results[0]["run_boundary"] = "continuous_account"
        with self.assertRaises(ValueError):
            bootstrap_day_runs(results, resamples=10)


class AggregateIdentityTests(unittest.TestCase):
    def _write(self, directory: Path, name: str, payload: dict) -> Path:
        path = directory / name
        path.write_text(__import__("json").dumps(payload))
        return path

    def test_mixing_config_hashes_fails_loudly(self) -> None:
        import contextlib
        import io
        import tempfile

        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            first = _result([1.0], day="2026-11-18")
            second = _result([1.0], day="2026-11-19")
            second["config_hash"] = "different"
            paths = [
                self._write(directory, "a.json", first),
                self._write(directory, "b.json", second),
            ]
            with self.assertRaises(SystemExit):
                with contextlib.redirect_stdout(io.StringIO()):
                    main(["aggregate", "--inputs", *map(str, paths)])


if __name__ == "__main__":
    unittest.main()
