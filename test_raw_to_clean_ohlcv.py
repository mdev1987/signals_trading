"""Unit tests for pool-isolated OHLCV aggregation and quote-volume validation."""

import argparse
import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
import warnings
from unittest import mock

import numpy as np
import pandas as pd

import raw_to_clean_ohlcv as pipeline


BASE_ARGS = argparse.Namespace(
    wsol_only=False,
    min_trades=1,
    jump_ratio=5.0,
    bundle_ms=3,
    drop_jumps=False,
)


def make_rows(rows):
    records = []
    for i, row in enumerate(rows, start=1):
        records.append(
            {
                "event_index": i,
                "signature": row.get("signature", f"sig-{i}"),
                "action": row.get("action", "buy"),
                "mint": row.get("mint", "MINT-A"),
                "pool_id": row.get("pool_id", "POOL-A"),
                "pool": row.get("pool", "pump"),
                "quote_mint": row.get("quote_mint", pipeline.WSOL),
                "timestamp_ms": row["timestamp_ms"],
                "token_amount": row.get("token_amount", 100.0),
                "quote_amount": row.get("quote_amount", 100.0),
                "price_sol": row.get("price_sol", 1.0),
                "quote_price_sol": row.get("quote_price_sol", 1.0),
                "n_breakdown": row.get("n_breakdown", 1),
                "block": row.get("block", 1),
                "breakdown_json": row.get("breakdown_json", ""),
                "tokens_in_pool": row.get("tokens_in_pool", np.nan),
                "quote_in_pool": row.get("quote_in_pool", np.nan),
                "virtual_tokens_in_pool": row.get("virtual_tokens_in_pool", np.nan),
                "burned_liquidity": row.get("burned_liquidity", ""),
                "pool_created_by": row.get("pool_created_by", ""),
                "decimals": row.get("decimals", np.nan),
                "supply": row.get("supply", np.nan),
                "participant_quote_balance_delta_sol": row.get("participant_quote_balance_delta_sol", np.nan),
                "participant_balance_available": row.get("participant_balance_available", 0),
                "participant_balance_checked": row.get("participant_balance_checked", row.get("participant_balance_available", 0)),
            }
        )
    return pd.DataFrame(records, columns=pipeline.ROW_COLS)


class PipelineTests(unittest.TestCase):
    def test_pool_volume_share_clips_only_floating_point_overshoot(self):
        raw = pd.Series([1.0 + 2 * np.finfo(float).eps, 1.0 + 1e-6, 0.75, np.nan])
        normalized, clipped = pipeline._normalize_pool_volume_share(raw)

        self.assertEqual(normalized.iloc[0], 1.0)
        self.assertEqual(int(clipped.iloc[0]), 1)
        self.assertGreater(normalized.iloc[1], 1.0)
        self.assertEqual(int(clipped.iloc[1]), 0)
        self.assertEqual(normalized.iloc[2], 0.75)
        self.assertTrue(np.isnan(normalized.iloc[3]))
        self.assertEqual(int(clipped.iloc[3]), 0)

        # A material share violation must remain visible to validation.
        rows = [
            {"timestamp_ms": 1_000, "pool_id": "POOL-A", "price_sol": 1.0, "quote_amount": 100.0},
        ]
        cleaned, _ = self.clean(rows)
        bars = pipeline.build_bars(cleaned, 60_000, False)
        bars.loc[0, "selected_pool_volume_share"] = 1.0 + 1e-6
        result = pipeline.validate(bars)
        self.assertFalse(result["passed"])
        self.assertEqual(result["invalid_pool_share"], 1)

    def test_build_and_fill_bars_do_not_emit_dataframe_fragmentation_warnings(self):
        rows = [
            {"timestamp_ms": 1_000, "pool_id": "POOL-A", "price_sol": 1.0, "quote_amount": 100.0},
            {"timestamp_ms": 3_000, "pool_id": "POOL-A", "price_sol": 1.1, "quote_amount": 110.0},
        ]
        cleaned, _ = self.clean(rows)
        with warnings.catch_warnings():
            warnings.simplefilter("error", pd.errors.PerformanceWarning)
            bars = pipeline.build_bars(cleaned, 1_000, False)
            filled = pipeline.fill_gaps(bars, 1_000)
            previous_close_bars = pipeline.build_bars(cleaned, 1_000, True)
            previous_close_filled = pipeline.fill_gaps(previous_close_bars, 1_000)

        self.assertEqual(int(filled["is_filled"].sum()), 1)
        self.assertIn("pool_volume_share_roundoff_clipped", filled.columns)
        self.assertEqual(int(filled["pool_volume_share_roundoff_clipped"].sum()), 0)
        self.assertEqual(int(previous_close_filled["is_filled"].sum()), 1)


    def test_directional_execution_ohlc_uses_component_prices_not_pool_spot(self):
        rows = [
            {"timestamp_ms": 1_000, "action": "buy", "price_sol": 1.2,
             "token_amount": 100.0, "quote_amount": 100.0},
            {"timestamp_ms": 2_000, "action": "sell", "price_sol": 1.1,
             "token_amount": 50.0, "quote_amount": 55.0},
            {
                "timestamp_ms": 3_000, "action": "buy", "price_sol": 1.3,
                "token_amount": 100.0, "quote_amount": 300.0, "n_breakdown": 2,
                "breakdown_json": json.dumps([
                    {"action": "buy", "tokenAmount": 200.0, "quoteAmount": 400.0},
                    {"action": "sell", "tokenAmount": 100.0, "quoteAmount": 100.0},
                ]),
            },
        ]
        cleaned, _ = self.clean(rows)
        bars = pipeline.build_bars(cleaned, 60_000, False)
        bar = bars.iloc[0]

        self.assertAlmostEqual(bar["spot_open"], 1.2)
        self.assertAlmostEqual(bar["spot_high"], 1.3)
        self.assertAlmostEqual(bar["buy_execution_open"], 1.0)
        self.assertAlmostEqual(bar["buy_execution_high"], 2.0)
        self.assertAlmostEqual(bar["buy_execution_low"], 1.0)
        self.assertAlmostEqual(bar["buy_execution_close"], 2.0)
        self.assertAlmostEqual(bar["sell_execution_open"], 1.1)
        self.assertAlmostEqual(bar["sell_execution_high"], 1.1)
        self.assertAlmostEqual(bar["sell_execution_low"], 1.0)
        self.assertAlmostEqual(bar["sell_execution_close"], 1.0)
        self.assertEqual(bar["execution_valid_events"], 3)
        self.assertEqual(bar["execution_invalid_events"], 0)
        self.assertEqual(bar["execution_ohlc_complete"], 1)
        self.assertEqual(bar["buy_execution_components"], 2)
        self.assertEqual(bar["sell_execution_components"], 2)
        self.assertAlmostEqual(bar["execution_coverage"], 1.0)
        self.assertTrue(pipeline.validate(bars)["passed"])

    def test_stale_bars_age_bars_and_execution_prices_are_not_filled(self):
        rows = [
            {"timestamp_ms": 1_000, "price_sol": 1.0, "quote_amount": 100.0, "token_amount": 100.0},
            {"timestamp_ms": 121_000, "price_sol": 2.0, "quote_amount": 200.0, "token_amount": 100.0},
        ]
        cleaned, _ = self.clean(rows)
        bars = pipeline.build_bars(cleaned, 60_000, False)
        filled = pipeline.fill_gaps(bars, 60_000)

        self.assertEqual(filled["stale_bars"].tolist(), [0, 1, 0])
        self.assertEqual(filled["age_bars"].tolist(), [0, 1, 2])
        self.assertEqual(filled["is_filled"].tolist(), [0, 1, 0])
        self.assertTrue(filled.loc[filled["is_filled"].eq(1), "spot_close"].notna().all())
        self.assertTrue(filled.loc[filled["is_filled"].eq(1), "buy_execution_open"].isna().all())
        self.assertTrue(filled.loc[filled["is_filled"].eq(1), "sell_execution_open"].isna().all())
        self.assertEqual(int(filled.loc[filled["is_filled"].eq(1), "execution_ohlc_complete"].iloc[0]), 0)
        self.assertTrue(pipeline.validate(filled)["passed"])

    def test_candle_range_anomaly_is_audit_only_and_uses_spot_range(self):
        rows = [
            {"timestamp_ms": 1_000, "price_sol": 1.0, "quote_amount": 100.0, "token_amount": 100.0},
            {"timestamp_ms": 2_000, "price_sol": 12.0, "quote_amount": 120.0, "token_amount": 10.0},
        ]
        cleaned, _ = self.clean(rows)
        bars = pipeline.build_bars(cleaned, 60_000, False, candle_range_ratio=10.0)
        bar = bars.iloc[0]

        self.assertAlmostEqual(bar["spot_high"], 12.0)
        self.assertAlmostEqual(bar["spot_low"], 1.0)
        self.assertAlmostEqual(bar["candle_range_ratio"], 12.0)
        self.assertEqual(int(bar["candle_range_anomaly_flag"]), 1)
        self.assertEqual(bar["trades"], 2)
        self.assertTrue(pipeline.validate(bars, candle_range_ratio=10.0)["passed"])

        stricter = pipeline.build_bars(cleaned, 60_000, False, candle_range_ratio=20.0)
        self.assertEqual(int(stricter.iloc[0]["candle_range_anomaly_flag"]), 0)
        self.assertEqual(stricter.iloc[0]["high"], bars.iloc[0]["high"])

    def test_open_prev_close_keeps_observed_spot_open_separate(self):
        rows = [
            {"timestamp_ms": 1_000, "price_sol": 1.0, "quote_amount": 100.0},
            {"timestamp_ms": 61_000, "price_sol": 10.0, "quote_amount": 100.0},
        ]
        cleaned, _ = self.clean(rows)
        bars = pipeline.build_bars(cleaned, 60_000, True)
        second = bars.iloc[1]

        self.assertAlmostEqual(second["open"], 1.0)
        self.assertAlmostEqual(second["spot_open"], 10.0)
        self.assertAlmostEqual(second["spot_high"], 10.0)
        self.assertAlmostEqual(second["spot_low"], 10.0)
        self.assertTrue(pipeline.validate(bars)["passed"])

    def test_invalid_execution_event_marks_bar_incomplete(self):
        rows = [
            {"timestamp_ms": 1_000, "price_sol": 1.0, "quote_amount": 100.0, "token_amount": 100.0},
            {"timestamp_ms": 2_000, "price_sol": 1.1, "quote_amount": 0.0, "token_amount": 100.0},
        ]
        cleaned, _ = self.clean(rows)
        bars = pipeline.build_bars(cleaned, 60_000, False)
        bar = bars.iloc[0]

        self.assertEqual(bar["execution_valid_events"], 1)
        self.assertEqual(bar["execution_invalid_events"], 1)
        self.assertAlmostEqual(bar["execution_coverage"], 0.5)
        self.assertEqual(bar["execution_ohlc_complete"], 0)
        self.assertTrue(pipeline.validate(bars)["passed"])

    def clean(self, rows):
        stats = {}
        cleaned, dropped = pipeline.clean_trades(make_rows(rows), BASE_ARGS, stats)
        self.assertTrue(dropped.empty)
        return cleaned, stats

    def test_selected_pool_metadata_is_preserved_for_downstream_routing(self):
        rows = [
            {
                "timestamp_ms": 1_000, "pool_id": "POOL-A", "pool": "pump",
                "quote_mint": pipeline.WSOL, "price_sol": 1.0,
                "token_amount": 100.0, "quote_amount": 100.0,
            },
            {
                "timestamp_ms": 2_000, "pool_id": "POOL-B", "pool": "pump-amm",
                "quote_mint": "QUOTE-B", "quote_price_sol": 1.0,
                "price_sol": 2.0, "token_amount": 100.0, "quote_amount": 200.0,
            },
            {
                "timestamp_ms": 3_000, "pool_id": "POOL-B", "pool": "pump-amm",
                "quote_mint": "QUOTE-B", "quote_price_sol": 1.0,
                "price_sol": 2.1, "token_amount": 100.0, "quote_amount": 210.0,
            },
        ]
        cleaned, _ = self.clean(rows)
        bars = pipeline.build_bars(cleaned, 60_000, False)

        # POOL-B wins on valid SOL quote volume. The metadata must come from
        # the selected pool, not from the first event or a discarded pool.
        self.assertEqual(bars.loc[0, "pool_id"], "POOL-B")
        self.assertEqual(bars.loc[0, "pool"], "pump-amm")
        self.assertEqual(bars.loc[0, "quote_mint"], "QUOTE-B")

        filled = pipeline.fill_gaps(
            pipeline.build_bars(cleaned, 1_000, False), 1_000
        )
        self.assertIn("pool", filled.columns)
        self.assertIn("quote_mint", filled.columns)

    def test_pool_prices_are_not_mixed_inside_a_candle(self):
        rows = [
            {"timestamp_ms": 1_000, "pool_id": "POOL-A", "price_sol": 1.0, "quote_amount": 100.0},
            {"timestamp_ms": 2_000, "pool_id": "POOL-B", "price_sol": 10.0, "quote_amount": 200.0},
            {"timestamp_ms": 3_000, "pool_id": "POOL-B", "price_sol": 11.0, "quote_amount": 100.0},
            {"timestamp_ms": 61_000, "pool_id": "POOL-A", "price_sol": 2.0, "quote_amount": 100.0},
        ]
        cleaned, _ = self.clean(rows)
        bars = pipeline.build_bars(cleaned, 60_000, False)

        first = bars.iloc[0]
        self.assertEqual(first["pool_id"], "POOL-B")
        self.assertEqual(first["open"], 10.0)
        self.assertEqual(first["high"], 11.0)
        self.assertEqual(first["low"], 10.0)
        self.assertEqual(first["close"], 11.0)
        self.assertEqual(first["trades"], 2)
        self.assertEqual(first["all_pool_trades"], 3)
        self.assertEqual(first["pool_excluded_trades"], 1)
        self.assertAlmostEqual(first["selected_pool_trade_share"], 2 / 3)
        self.assertAlmostEqual(first["selected_pool_volume_share"], 0.75)
        self.assertEqual(first["pool_count"], 2)

        second = bars.iloc[1]
        self.assertEqual(second["pool_id"], "POOL-A")
        self.assertEqual(second["pool_changed"], 1)
        self.assertTrue(pipeline.validate(bars)["passed"])

        with_previous_close = pipeline.build_bars(cleaned, 60_000, True)
        # A previous close from another pool must not become the new pool's synthetic open.
        self.assertEqual(with_previous_close.iloc[1]["open"], 2.0)

    def test_missing_pool_ids_are_separated_by_pool_type(self):
        rows = [
            {"timestamp_ms": 1_000, "pool_id": "", "pool": "pump", "price_sol": 1.0, "quote_amount": 100.0},
            {"timestamp_ms": 2_000, "pool_id": "", "pool": "raydium", "price_sol": 10.0, "quote_amount": 200.0},
        ]
        cleaned, stats = self.clean(rows)
        bars = pipeline.build_bars(cleaned, 60_000, False)
        bar = bars.iloc[0]

        self.assertEqual(stats["missing_pool_id_rows"], 2)
        self.assertEqual(bar["pool_count"], 2)
        self.assertEqual(bar["pool_id"], "")
        self.assertTrue(str(bar["pool_key"]).startswith("__missing_pool_id__:"))
        self.assertEqual(bar["pool_excluded_trades"], 1)
        self.assertTrue(pipeline.validate(bars)["passed"])

    def test_cross_pool_price_discontinuity_is_a_pool_change_not_an_intra_pool_jump(self):
        rows = [
            {"timestamp_ms": 1_000, "pool_id": "POOL-A", "price_sol": 1.0, "quote_amount": 100.0},
            {"timestamp_ms": 61_000, "pool_id": "POOL-B", "price_sol": 100.0, "quote_amount": 100.0},
        ]
        cleaned, _ = self.clean(rows)
        bars = pipeline.build_bars(cleaned, 60_000, False)

        self.assertEqual(int(bars.iloc[1]["pool_changed"]), 1)
        self.assertEqual(int(bars.iloc[1]["jump_flag"]), 0)
        self.assertEqual(int(bars.iloc[1]["price_discontinuity_flag"]), 1)
        self.assertEqual(int(bars.iloc[1]["pool_switch_discontinuity_flag"]), 1)
        self.assertAlmostEqual(bars.iloc[1]["price_discontinuity_ratio"], 100.0)
        self.assertEqual(bars.iloc[1]["prev_close"], 1.0)
        self.assertEqual(bars.iloc[1]["observed_open"], 100.0)
        self.assertEqual(bars.iloc[1]["price_gap_elapsed_ms"], 60_000)

        same_pool_rows = [
            {"timestamp_ms": 1_000, "pool_id": "POOL-A", "price_sol": 1.0, "quote_amount": 100.0},
            {"timestamp_ms": 61_000, "pool_id": "POOL-A", "price_sol": 100.0, "quote_amount": 100.0},
        ]
        same_pool_cleaned, _ = self.clean(same_pool_rows)
        same_pool_bars = pipeline.build_bars(same_pool_cleaned, 60_000, False)
        self.assertEqual(int(same_pool_bars.iloc[1]["pool_changed"]), 0)
        self.assertEqual(int(same_pool_bars.iloc[1]["jump_flag"]), 1)
        self.assertEqual(int(same_pool_bars.iloc[1]["price_discontinuity_flag"]), 1)
        self.assertEqual(int(same_pool_bars.iloc[1]["pool_switch_discontinuity_flag"]), 0)

    def test_price_discontinuity_uses_observed_open_and_is_audit_only(self):
        rows = [
            {"timestamp_ms": 1_000, "price_sol": 1.0, "quote_amount": 100.0},
            {"timestamp_ms": 61_000, "price_sol": 10.0, "quote_amount": 100.0},
        ]
        cleaned, _ = self.clean(rows)
        bars = pipeline.build_bars(cleaned, 60_000, True, discontinuity_ratio=5.0)

        second = bars.iloc[1]
        # --open-prev-close changes the execution-oriented open, not the audit metric.
        self.assertEqual(second["open"], 1.0)
        self.assertEqual(second["observed_open"], 10.0)
        self.assertEqual(second["prev_close"], 1.0)
        self.assertEqual(second["price_discontinuity_ratio"], 10.0)
        self.assertEqual(int(second["price_discontinuity_flag"]), 1)
        self.assertTrue(pipeline.validate(bars, discontinuity_ratio=5.0)["passed"])

        # A threshold above the measured ratio suppresses the flag without changing OHLC.
        higher_threshold = pipeline.build_bars(cleaned, 60_000, False, discontinuity_ratio=20.0)
        self.assertEqual(int(higher_threshold.iloc[1]["price_discontinuity_flag"]), 0)
        self.assertEqual(higher_threshold.iloc[1]["price_discontinuity_ratio"], 10.0)

    def test_zero_quote_does_not_inflate_volume_or_vwap(self):
        rows = [
            {"timestamp_ms": 1_000, "price_sol": 1.0, "quote_amount": 100.0, "token_amount": 100.0},
            {"timestamp_ms": 2_000, "price_sol": 2.0, "quote_amount": 200.0, "token_amount": 100.0},
            {"timestamp_ms": 3_000, "price_sol": 1.5, "quote_amount": 0.0, "token_amount": 999.0},
        ]
        cleaned, stats = self.clean(rows)
        bars = pipeline.build_bars(cleaned, 60_000, False)
        bar = bars.iloc[0]

        self.assertEqual(stats["flagged_zero_quote"], 1)
        self.assertEqual(bar["trades"], 3)
        self.assertEqual(bar["valid_quote_trades"], 2)
        self.assertEqual(bar["invalid_quote_trades"], 1)
        self.assertEqual(bar["zero_quote_trades"], 1)
        self.assertEqual(bar["volume_token"], 200.0)
        self.assertEqual(bar["volume_sol"], 300.0)
        self.assertAlmostEqual(bar["vwap_sol"], 1.5)
        self.assertEqual(bar["vwap_trades"], 2)
        self.assertAlmostEqual(bar["vwap_single_breakdown_sol"], 1.5)
        self.assertAlmostEqual(bar["vwap_trade_coverage"], 1.0)
        self.assertAlmostEqual(bar["vwap_sol_volume"], 300.0)
        self.assertAlmostEqual(bar["vwap_token_volume"], 200.0)
        self.assertTrue(pipeline.validate(bars)["passed"])

    def test_vwap_uses_gross_multi_breakdown_components(self):
        rows = [
            {"timestamp_ms": 1_000, "price_sol": 1.0, "quote_amount": 100.0, "token_amount": 100.0, "n_breakdown": 1},
            {"timestamp_ms": 2_000, "price_sol": 2.0, "quote_amount": 200.0, "token_amount": 100.0, "n_breakdown": 1},
            {
                "timestamp_ms": 3_000, "price_sol": 3.0,
                "quote_amount": 300.0, "token_amount": 100.0, "n_breakdown": 2,
                "breakdown_json": json.dumps([
                    {"action": "buy", "tokenAmount": 200.0, "quoteAmount": 400.0},
                    {"action": "sell", "tokenAmount": 100.0, "quoteAmount": 100.0},
                ]),
            },
        ]
        cleaned, _ = self.clean(rows)
        bars = pipeline.build_bars(cleaned, 60_000, False)
        bar = bars.iloc[0]

        self.assertEqual(bar["valid_quote_trades"], 3)
        self.assertEqual(bar["vwap_trades"], 3)
        self.assertAlmostEqual(bar["vwap_trade_coverage"], 1.0)
        self.assertAlmostEqual(bar["volume_sol"], 800.0)
        self.assertAlmostEqual(bar["volume_token"], 500.0)
        self.assertAlmostEqual(bar["buy_volume_token"], 400.0)
        self.assertAlmostEqual(bar["sell_volume_token"], 100.0)
        self.assertAlmostEqual(bar["vwap_sol_volume"], 800.0)
        self.assertAlmostEqual(bar["vwap_token_volume"], 500.0)
        self.assertAlmostEqual(bar["vwap_single_breakdown_sol"], 1.6)
        self.assertAlmostEqual(bar["vwap_sol"], 1.6)
        self.assertEqual(bar["breakdown_netting_flag"], 1)
        self.assertEqual(bar["breakdown_net_reconciles_event"], 1)
        self.assertTrue(pipeline.validate(bars)["passed"])

    def test_quote_implied_price_and_symmetric_spot_execution_ratio(self):
        rows = [
            {
                "timestamp_ms": 1_000,
                "price_sol": 1.0,
                "quote_amount": 200.0,
                "token_amount": 100.0,
            },
            {
                "timestamp_ms": 2_000,
                "price_sol": 10.0,
                "quote_amount": 100.0,
                "token_amount": 100.0,
            },
        ]
        cleaned, stats = self.clean(rows)

        self.assertAlmostEqual(cleaned.iloc[0]["implied_price_sol"], 2.0)
        self.assertAlmostEqual(cleaned.iloc[0]["raw_to_implied_ratio"], 0.5)
        self.assertAlmostEqual(cleaned.iloc[0]["price_consistency_ratio"], 2.0)
        self.assertEqual(int(cleaned.iloc[0]["price_consistency_flag"]), 0)
        self.assertAlmostEqual(cleaned.iloc[1]["implied_price_sol"], 1.0)
        self.assertAlmostEqual(cleaned.iloc[1]["raw_to_implied_ratio"], 10.0)
        self.assertAlmostEqual(cleaned.iloc[1]["price_consistency_ratio"], 10.0)
        self.assertEqual(int(cleaned.iloc[1]["price_consistency_flag"]), 1)
        self.assertEqual(stats["price_consistency"]["comparison_events"], 2)
        self.assertEqual(stats["price_consistency"]["flagged_events"], 1)

        bars, selected = pipeline.build_bars(
            cleaned, 60_000, False, return_selected=True
        )
        bar = bars.iloc[0]
        self.assertEqual(len(selected), 2)
        self.assertEqual(bar["price_comparison_trades"], 2)
        self.assertEqual(bar["price_consistency_flagged_trades"], 1)
        self.assertAlmostEqual(bar["open_implied_price_sol"], 2.0)
        self.assertAlmostEqual(bar["close_implied_price_sol"], 1.0)
        self.assertAlmostEqual(bar["open_price_consistency_ratio"], 2.0)
        self.assertAlmostEqual(bar["close_price_consistency_ratio"], 10.0)
        self.assertEqual(int(bar["open_price_consistency_flag"]), 0)
        self.assertEqual(int(bar["close_price_consistency_flag"]), 1)
        self.assertTrue(pipeline.validate(bars)["passed"])

    def test_non_wsol_implied_price_uses_quote_price_conversion(self):
        rows = [
            {
                "timestamp_ms": 1_000,
                "quote_mint": "USDC-MINT",
                "quote_amount": 100.0,
                "quote_price_sol": 0.01,
                "token_amount": 100.0,
                "price_sol": 0.01,
            }
        ]
        cleaned, _ = self.clean(rows)
        event = cleaned.iloc[0]

        self.assertAlmostEqual(event["vol_sol"], 1.0)
        self.assertAlmostEqual(event["implied_price_sol"], 0.01)
        self.assertAlmostEqual(event["price_consistency_ratio"], 1.0)
        self.assertEqual(int(event["price_consistency_flag"]), 0)

    def test_invalid_quote_has_no_implied_price_or_consistency_flag(self):
        rows = [
            {
                "timestamp_ms": 1_000,
                "price_sol": 0.5,
                "quote_amount": 0.0,
                "token_amount": 100.0,
            }
        ]
        cleaned, _ = self.clean(rows)
        event = cleaned.iloc[0]

        self.assertTrue(np.isnan(event["implied_price_sol"]))
        self.assertTrue(np.isnan(event["raw_to_implied_ratio"]))
        self.assertTrue(np.isnan(event["price_consistency_ratio"]))
        self.assertEqual(int(event["price_consistency_flag"]), 0)

    def test_ohlcv_and_discontinuity_audit_retain_first_last_event_identity(self):
        rows = [
            {
                "timestamp_ms": 1_000,
                "signature": "open-signature",
                "price_sol": 1.0,
                "quote_amount": 100.0,
                "token_amount": 100.0,
            },
            {
                "timestamp_ms": 2_000,
                "signature": "close-signature",
                "price_sol": 2.0,
                "quote_amount": 200.0,
                "token_amount": 100.0,
            },
            {
                "timestamp_ms": 61_000,
                "signature": "next-open-signature",
                "price_sol": 20.0,
                "quote_amount": 100.0,
                "token_amount": 100.0,
            },
        ]
        cleaned, _ = self.clean(rows)
        bars = pipeline.build_bars(cleaned, 60_000, False)

        first, second = bars.iloc[0], bars.iloc[1]
        self.assertEqual(first["open_signature"], "open-signature")
        self.assertEqual(first["close_signature"], "close-signature")
        self.assertEqual(second["open_signature"], "next-open-signature")
        self.assertEqual(second["prev_close_signature"], "close-signature")
        self.assertEqual(second["prev_close_event_index"], first["close_event_index"])
        self.assertEqual(second["prev_close_price_sol"], first["close_price_sol"])

        export = bars.copy()
        export["datetime_utc"] = pd.to_datetime(
            export["timestamp_ms"], unit="ms"
        ).dt.strftime("%Y-%m-%d %H:%M:%S")
        export = export[pipeline.FINAL_COLS]
        for column in (
            "open_signature", "close_signature", "prev_close_signature",
            "open_implied_price_sol", "close_implied_price_sol",
            "prev_close_implied_price_sol", "price_consistency_flagged_trades",
        ):
            self.assertIn(column, export.columns)
        self.assertTrue(pipeline.validate(export)["passed"])

    def test_cli_writes_event_level_price_consistency_audit(self):
        raw_rows = [
            (
                1, "cli-open", "buy", "CLI-MINT", "POOL-A", "pump", pipeline.WSOL,
                1_000, 100.0, 100.0, 1.0, 1.0, 1, 100,
            ),
            (
                2, "cli-flagged", "sell", "CLI-MINT", "POOL-A", "pump", pipeline.WSOL,
                2_000, 100.0, 100.0, 10.0, 1.0, 1, 101,
            ),
            (
                3, "cli-invalid-quote", "buy", "CLI-MINT", "POOL-A", "pump", pipeline.WSOL,
                61_000, 100.0, 0.0, 10.0, 1.0, 1, 102,
            ),
        ]

        with tempfile.TemporaryDirectory() as tmp:
            input_path = os.path.join(tmp, "stub.jsonl.zst")
            outdir = os.path.join(tmp, "output")
            with open(input_path, "wb") as handle:
                handle.write(b"stub")

            def fake_read_file(path, start_idx, stats):
                stats["lines_scanned"] = len(raw_rows)
                return raw_rows, start_idx + len(raw_rows)

            argv = [
                "raw_to_clean_ohlcv.py", input_path, "-o", outdir,
                "-t", "1m", "--fill", "--price-consistency-ratio", "5.0",
                "--candle-range-ratio", "10.0",
            ]
            with mock.patch.object(pipeline, "read_file", side_effect=fake_read_file):
                with mock.patch.object(sys, "argv", argv):
                    with contextlib.redirect_stderr(io.StringIO()):
                        pipeline.main()

            report_path = os.path.join(outdir, "cleaning_report.json")
            with open(report_path, encoding="utf-8") as handle:
                report = json.load(handle)
            self.assertTrue(report["timeframes"]["1m"]["validation"]["passed"])
            self.assertEqual(
                report["timeframes"]["1m"]["price_consistency_flagged_events"], 1
            )

            bars = pd.read_csv(os.path.join(outdir, "ohlcv_1m.csv"))
            self.assertEqual(bars.iloc[0]["open_signature"], "cli-open")
            self.assertEqual(bars.iloc[0]["close_signature"], "cli-flagged")
            self.assertEqual(bars.iloc[1]["prev_close_signature"], "cli-flagged")
            self.assertEqual(int(bars.iloc[1]["close_quote_valid"]), 0)
            self.assertTrue(np.isnan(bars.iloc[1]["close_implied_price_sol"]))
            self.assertIn("spot_open", bars.columns)
            self.assertIn("spot_high", bars.columns)
            self.assertIn("buy_execution_open", bars.columns)
            self.assertIn("sell_execution_close", bars.columns)
            self.assertIn("execution_ohlc_complete", bars.columns)
            self.assertIn("stale_bars", bars.columns)
            self.assertIn("age_bars", bars.columns)
            self.assertIn("candle_range_anomaly_flag", bars.columns)
            self.assertIn("pool", bars.columns)
            self.assertIn("quote_mint", bars.columns)
            self.assertEqual(bars.iloc[0]["pool"], "pump")
            self.assertEqual(bars.iloc[0]["quote_mint"], pipeline.WSOL)

            vbt_path = os.path.join(os.path.dirname(__file__), "build_vbt_dataset.py")
            vbt_spec = importlib.util.spec_from_file_location("build_vbt_dataset", vbt_path)
            vbt_module = importlib.util.module_from_spec(vbt_spec)
            vbt_spec.loader.exec_module(vbt_module)
            vbt_input = vbt_module.load(os.path.join(outdir, "ohlcv_1m.csv"))
            self.assertEqual(len(vbt_input), len(bars))
            self.assertIn("vwap_sol", vbt_input.columns)
            self.assertTrue(vbt_input.attrs["has_directional_execution_ohlc"])

            range_path = os.path.join(outdir, "flagged_candle_range_anomaly_1m.csv")
            self.assertTrue(os.path.exists(range_path))

            consistency_path = os.path.join(
                outdir, "flagged_price_consistency_1m.csv"
            )
            consistency = pd.read_csv(consistency_path)
            self.assertEqual(consistency["signature"].tolist(), ["cli-flagged"])
            self.assertAlmostEqual(consistency.iloc[0]["implied_price_sol"], 1.0)
            self.assertAlmostEqual(consistency.iloc[0]["price_consistency_ratio"], 10.0)

            discontinuity = pd.read_csv(
                os.path.join(outdir, "flagged_price_discontinuity_1m.csv")
            )
            self.assertIn("open_signature", discontinuity.columns)
            self.assertIn("close_signature", discontinuity.columns)
            self.assertIn("prev_close_signature", discontinuity.columns)

    def test_non_wsol_quote_requires_valid_conversion_rate(self):
        rows = [
            {
                "timestamp_ms": 1_000,
                "quote_mint": "USDC-MINT",
                "quote_amount": 50.0,
                "quote_price_sol": 0.01,
                "token_amount": 100.0,
                "price_sol": 0.5,
            },
            {
                "timestamp_ms": 2_000,
                "quote_mint": "USDC-MINT",
                "quote_amount": 25.0,
                "quote_price_sol": np.nan,
                "token_amount": 100.0,
                "price_sol": 0.5,
            },
        ]
        cleaned, stats = self.clean(rows)
        bars = pipeline.build_bars(cleaned, 60_000, False)
        bar = bars.iloc[0]

        self.assertEqual(stats["valid_quote_rows"], 1)
        self.assertEqual(stats["invalid_quote_rows"], 1)
        self.assertAlmostEqual(bar["volume_sol"], 0.5)
        self.assertEqual(bar["volume_token"], 100.0)
        self.assertEqual(bar["valid_quote_trades"], 1)
        self.assertEqual(bar["invalid_quote_trades"], 1)
        self.assertEqual(int(bar["close_quote_valid"]), 0)
        self.assertTrue(np.isnan(bar["close_quote_volume_sol"]))
        self.assertTrue(np.isnan(bar["close_implied_price_sol"]))
        self.assertTrue(np.isnan(bar["close_price_consistency_ratio"]))
        self.assertTrue(pipeline.validate(bars)["passed"])

    def test_no_valid_quote_produces_nan_volume_not_zero_volume(self):
        rows = [
            {
                "timestamp_ms": 1_000,
                "quote_mint": "USDC-MINT",
                "quote_amount": 10.0,
                "quote_price_sol": np.nan,
                "token_amount": 100.0,
                "price_sol": 0.5,
            }
        ]
        cleaned, _ = self.clean(rows)
        bars = pipeline.build_bars(cleaned, 60_000, False)
        bar = bars.iloc[0]
        self.assertTrue(np.isnan(bar["volume_sol"]))
        self.assertTrue(np.isnan(bar["volume_token"]))
        self.assertTrue(np.isnan(bar["vwap_sol"]))
        self.assertTrue(np.isnan(bar["vwap_single_breakdown_sol"]))
        self.assertTrue(np.isnan(bar["vwap_trade_coverage"]))
        self.assertTrue(np.isnan(bar["vwap_sol_volume"]))
        self.assertTrue(np.isnan(bar["vwap_token_volume"]))
        self.assertTrue(pipeline.validate(bars)["passed"])

    def test_fill_gaps_keeps_filled_candles_distinct_from_real_volume(self):
        rows = [
            {"timestamp_ms": 1_000, "price_sol": 1.0, "quote_amount": 100.0},
            {
                "timestamp_ms": 121_000,
                "quote_mint": "USDC-MINT",
                "quote_amount": 10.0,
                "quote_price_sol": np.nan,
                "token_amount": 100.0,
                "price_sol": 2.0,
            },
        ]
        cleaned, _ = self.clean(rows)
        bars = pipeline.build_bars(cleaned, 60_000, False)
        filled = pipeline.fill_gaps(bars, 60_000)

        middle = filled.iloc[1]
        self.assertEqual(int(middle["is_filled"]), 1)
        self.assertEqual(middle["open"], middle["close"])
        self.assertEqual(middle["volume_sol"], 0.0)
        self.assertEqual(middle["all_pool_trades"], 0)

        # The final candle is real, but its only quote is invalid. Keep its volume
        # fields NaN rather than turning unknown volume into measured zero volume.
        real_without_quote = filled.iloc[2]
        self.assertEqual(int(real_without_quote["is_filled"]), 0)
        self.assertEqual(int(real_without_quote["valid_quote_trades"]), 0)
        for column in pipeline.VOL_COLS:
            self.assertTrue(np.isnan(real_without_quote[column]), column)
        self.assertTrue(pipeline.validate(filled)["passed"])

    def test_netted_breakdown_uses_gross_execution_not_top_level_net_ratio(self):
        rows = [{
            "timestamp_ms": 1_000, "action": "buy",
            "price_sol": 1.0193073588503638e-8,
            "token_amount": 8477.710578, "quote_amount": 5.351e-6,
            "n_breakdown": 2,
            "breakdown_json": json.dumps([
                {"action": "buy", "tokenAmount": 520840409.753313, "quoteAmount": 5.345762921},
                {"action": "sell", "tokenAmount": 520848887.463891, "quoteAmount": 5.345757570},
            ]),
            "tokens_in_pool": 75853853860.72787, "quote_in_pool": 773.183914374,
        }]
        cleaned, _ = self.clean(rows)
        event = cleaned.iloc[0]
        self.assertEqual(int(event["quote_valid"]), 1)
        self.assertEqual(int(event["breakdown_netting_flag"]), 1)
        self.assertEqual(int(event["breakdown_net_reconciles_event"]), 1)
        self.assertAlmostEqual(event["breakdown_net_token_amount"], -8477.710578, places=5)
        self.assertAlmostEqual(event["breakdown_net_quote_sol"], 5.351e-6, places=12)
        self.assertAlmostEqual(event["gross_token_amount"], 1041689297.217204, places=3)
        self.assertAlmostEqual(event["gross_quote_volume_sol"], 10.691520491, places=9)
        self.assertLess(event["pool_spot_vs_execution_ratio"], 5.0)
        self.assertEqual(int(event["price_consistency_flag"]), 0)
        self.assertEqual(int(event["netting_explains_price_gap_flag"]), 1)

    def test_zero_net_multi_breakdown_event_is_preserved_with_gross_flow(self):
        rows = [{
            "timestamp_ms": 1_000, "action": "buy", "price_sol": 0.2,
            "token_amount": 0.0, "quote_amount": 0.0, "n_breakdown": 2,
            "breakdown_json": json.dumps([
                {"action": "buy", "tokenAmount": 10.0, "quoteAmount": 2.0},
                {"action": "sell", "tokenAmount": 10.0, "quoteAmount": 2.0},
            ]),
        }]
        cleaned, stats = self.clean(rows)
        self.assertEqual(len(cleaned), 1)
        event = cleaned.iloc[0]
        self.assertEqual(int(event["quote_valid"]), 1)
        self.assertEqual(int(event["breakdown_net_zero_flag"]), 1)
        self.assertEqual(int(event["breakdown_net_reconciles_event"]), 1)
        self.assertEqual(event["gross_token_amount"], 20.0)
        self.assertEqual(event["gross_quote_volume_sol"], 4.0)
        self.assertEqual(stats["dropped_reasons"]["bad_token_amount"], 0)

    def test_incomplete_multi_breakdown_is_retained_but_excluded_from_volume(self):
        rows = [{
            "timestamp_ms": 1_000, "action": "buy", "price_sol": 1.0,
            "token_amount": 100.0, "quote_amount": 100.0, "n_breakdown": 2,
            "breakdown_json": json.dumps([
                {"action": "buy", "tokenAmount": 100.0, "quoteAmount": 100.0},
            ]),
        }]
        cleaned, _ = self.clean(rows)
        event = cleaned.iloc[0]
        self.assertEqual(int(event["quote_valid"]), 0)
        self.assertEqual(int(event["invalid_quote"]), 1)
        self.assertEqual(event["invalid_quote_reason"], "breakdown_missing_incomplete_or_invalid;")
        bars = pipeline.build_bars(cleaned, 60_000, False)
        self.assertEqual(int(bars.iloc[0]["valid_quote_trades"]), 0)
        self.assertTrue(np.isnan(bars.iloc[0]["volume_sol"]))

    def test_effective_token_reserve_includes_virtual_token_reserve(self):
        rows = [{
            "timestamp_ms": 1_000, "action": "buy", "price_sol": 30.002,
            "token_amount": 0.005103, "quote_amount": 0.00003,
            "tokens_in_pool": 216.174395, "virtual_tokens_in_pool": -216.174394,
            "quote_in_pool": 0.000030002,
        }]
        cleaned, _ = self.clean(rows)
        event = cleaned.iloc[0]
        self.assertAlmostEqual(event["effective_tokens_in_pool"], 0.000001, places=9)
        self.assertAlmostEqual(event["pool_side_reserve_ratio"], 0.000001 / 0.005103, places=12)
        self.assertEqual(int(event["pool_depletion_flag"]), 1)
        self.assertEqual(int(event["price_impact_candidate_flag"]), 1)
        self.assertEqual(int(event["data_anomaly_candidate_flag"]), 0)

    def test_balance_audit_availability_requires_a_check(self):
        rows = [{
            "timestamp_ms": 1_000, "action": "buy", "price_sol": 1.0,
            "token_amount": 100.0, "quote_amount": 100.0,
            "participant_quote_balance_delta_sol": 0.0,
            "participant_balance_available": 1,
            "participant_balance_checked": 0,
        }]
        cleaned, _ = self.clean(rows)
        bars = pipeline.build_bars(cleaned, 60_000, False)
        self.assertEqual(pipeline.validate(bars)["invalid_balance_audit_flags"], 1)

    def test_balance_audit_trigger_skips_ordinary_events(self):
        ordinary = {
            "tokenAmount": 100.0, "quoteAmount": 1.0,
            "priceSol": 0.01, "quoteMint": pipeline.WSOL,
        }
        flagged = {
            "tokenAmount": 100.0, "quoteAmount": 1.0,
            "priceSol": 10.0, "quoteMint": pipeline.WSOL,
        }
        self.assertFalse(pipeline._needs_participant_balance_audit(ordinary, 1))
        self.assertTrue(pipeline._needs_participant_balance_audit(flagged, 1))
        self.assertTrue(pipeline._needs_participant_balance_audit(ordinary, 2))

    def test_pool_depletion_classifies_large_spot_execution_gap(self):
        rows = [{
            "timestamp_ms": 1_000, "action": "buy", "price_sol": 30.002,
            "token_amount": 0.005103, "quote_amount": 0.00003,
            "tokens_in_pool": 0.000001, "quote_in_pool": 0.000030002,
            "burned_liquidity": "100%", "pool_created_by": "custom", "n_breakdown": 1,
        }]
        cleaned, _ = self.clean(rows)
        event = cleaned.iloc[0]
        self.assertGreater(event["pool_spot_vs_execution_ratio"], 5_000)
        self.assertLess(event["pool_side_reserve_ratio"], 0.001)
        self.assertEqual(int(event["pool_depletion_flag"]), 1)
        self.assertEqual(int(event["price_impact_candidate_flag"]), 1)
        self.assertEqual(int(event["data_anomaly_candidate_flag"]), 0)

    def test_large_spot_execution_gap_without_reserve_evidence_is_unclassified(self):
        rows = [{
            "timestamp_ms": 1_000, "action": "sell", "price_sol": 100.0,
            "token_amount": 100.0, "quote_amount": 1.0, "n_breakdown": 1,
        }]
        cleaned, _ = self.clean(rows)
        event = cleaned.iloc[0]
        self.assertEqual(int(event["pool_depletion_flag"]), 0)
        self.assertEqual(int(event["price_impact_candidate_flag"]), 0)
        self.assertEqual(int(event["data_anomaly_candidate_flag"]), 0)
        self.assertEqual(int(event["price_gap_unclassified_flag"]), 1)

    def test_large_spot_execution_gap_with_sufficient_reserve_is_anomaly_candidate(self):
        rows = [{
            "timestamp_ms": 1_000, "action": "sell", "price_sol": 100.0,
            "token_amount": 100.0, "quote_amount": 1.0,
            "tokens_in_pool": 1_000_000.0, "quote_in_pool": 10_000.0,
        }]
        cleaned, _ = self.clean(rows)
        event = cleaned.iloc[0]
        self.assertEqual(int(event["price_consistency_flag"]), 1)
        self.assertGreater(event["pool_side_reserve_ratio"], 0.05)
        self.assertEqual(int(event["pool_depletion_flag"]), 0)
        self.assertEqual(int(event["price_impact_candidate_flag"]), 0)
        self.assertEqual(int(event["data_anomaly_candidate_flag"]), 1)
        self.assertEqual(int(event["price_gap_unclassified_flag"]), 0)

    def test_participant_quote_balance_residual_is_audit_only(self):
        rows = [{
            "timestamp_ms": 1_000, "action": "sell",
            "price_sol": 2.2085354783995453e-9,
            "token_amount": 0.000099, "quote_amount": 0.000097961,
            "participant_quote_balance_delta_sol": 0.000077961,
            "participant_balance_available": 1,
        }]
        cleaned, _ = self.clean(rows)
        event = cleaned.iloc[0]
        self.assertAlmostEqual(event["participant_quote_balance_delta_residual_sol"], -0.00002, places=12)
        self.assertEqual(int(event["quote_valid"]), 1)



if __name__ == "__main__":
    unittest.main(verbosity=2)
