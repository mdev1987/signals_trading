"""Tests for the final spot/execution and stale-price VectorBT dataset stage."""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

import numpy as np
import pandas as pd

import build_vbt_dataset as builder


class BuildVbtDatasetTests(unittest.TestCase):
    def make_real_bars(self):
        rows = [
            {
                "mint": "MINT-A", "timestamp_ms": 0,
                "open": 10.0, "high": 12.0, "low": 8.0, "close": 10.0,
                "spot_open": 10.0, "spot_high": 12.0, "spot_low": 8.0, "spot_close": 10.0,
                "trades": 1, "is_filled": 0, "vwap_sol": 9.9,
                "volume_token": 100.0, "volume_sol": 990.0, "jump_flag": 0,
                "buy_execution_open": 9.8, "buy_execution_high": 10.0,
                "buy_execution_low": 9.5, "buy_execution_close": 9.9,
                "sell_execution_open": np.nan, "sell_execution_high": np.nan,
                "sell_execution_low": np.nan, "sell_execution_close": np.nan,
                "buy_execution_components": 1, "sell_execution_components": 0,
                "execution_ohlc_complete": 1, "execution_coverage": 1.0,
                "candle_range_ratio": 1.5, "candle_range_anomaly_flag": 0,
                "pool_id": "POOL-A",
            },
            {
                "mint": "MINT-A", "timestamp_ms": 120_000,
                "open": 11.0, "high": 140.0, "low": 1.1, "close": 12.0,
                "spot_open": 11.0, "spot_high": 140.0, "spot_low": 1.1, "spot_close": 12.0,
                "trades": 2, "is_filled": 0, "vwap_sol": 11.5,
                "volume_token": 200.0, "volume_sol": 2300.0, "jump_flag": 1,
                "buy_execution_open": 10.8, "buy_execution_high": 11.0,
                "buy_execution_low": 10.5, "buy_execution_close": 10.9,
                "sell_execution_open": 12.0, "sell_execution_high": 13.0,
                "sell_execution_low": 11.0, "sell_execution_close": 12.5,
                "buy_execution_components": 1, "sell_execution_components": 1,
                "execution_ohlc_complete": 1, "execution_coverage": 1.0,
                "candle_range_ratio": 140.0 / 1.1, "candle_range_anomaly_flag": 1,
                "pool_id": "POOL-A",
            },
        ]
        return pd.DataFrame(rows)

    def build(self, max_stale_bars):
        real = self.make_real_bars()
        grid = np.array([0, 60_000, 120_000], dtype="int64")
        last_ts = pd.Series({"MINT-A": 120_000})
        frames, alive = builder.build_wide(
            real, grid, last_ts, True, True,
            max_stale_bars=max_stale_bars,
            candle_range_ratio=10.0,
        )
        return frames, alive

    def test_spot_and_directional_execution_frames_are_separate(self):
        frames, alive = self.build(max_stale_bars=0)
        self.assertEqual(float(frames["spot_open"].iloc[0, 0]), 10.0)
        self.assertEqual(float(frames["spot_high"].iloc[2, 0]), 140.0)
        self.assertEqual(float(frames["buy_execution_open"].iloc[2, 0]), 10.8)
        self.assertEqual(float(frames["sell_execution_high"].iloc[2, 0]), 13.0)
        self.assertEqual(int(frames["buy_execution_valid"].iloc[2, 0]), 1)
        self.assertEqual(int(frames["sell_execution_valid"].iloc[2, 0]), 1)
        self.assertTrue(builder.validate(frames, alive, 0, 10.0)["passed"])

    def test_filled_spot_price_is_invalid_by_default_and_execution_never_fills(self):
        frames, alive = self.build(max_stale_bars=0)
        self.assertEqual(frames["stale_bars"]["MINT-A"].tolist(), [0.0, 1.0, 0.0])
        self.assertEqual(frames["spot_mark_valid"]["MINT-A"].tolist(), [1, 0, 1])
        self.assertEqual(frames["real_bar"]["MINT-A"].tolist(), [1, 0, 1])
        self.assertTrue(np.isnan(frames["buy_execution_open"].iloc[1, 0]))
        self.assertTrue(np.isnan(frames["sell_execution_close"].iloc[1, 0]))
        self.assertEqual(int(frames["execution_valid"].iloc[1, 0]), 0)
        self.assertTrue(builder.validate(frames, alive, 0, 10.0)["passed"])

    def test_max_stale_bars_changes_mark_validity_not_execution_validity(self):
        frames, alive = self.build(max_stale_bars=1)
        self.assertEqual(frames["spot_mark_valid"]["MINT-A"].tolist(), [1, 1, 1])
        self.assertEqual(frames["execution_valid"]["MINT-A"].tolist(), [1, 0, 1])
        self.assertTrue(builder.validate(frames, alive, 1, 10.0)["passed"])

    def test_candle_range_anomaly_is_flagged_without_dropping_bar(self):
        frames, alive = self.build(max_stale_bars=0)
        self.assertEqual(int(frames["candle_range_anomaly_flag"].iloc[2, 0]), 1)
        self.assertEqual(int(frames["real_bar"].iloc[2, 0]), 1)
        self.assertEqual(float(frames["spot_high"].iloc[2, 0]), 140.0)
        self.assertTrue(builder.validate(frames, alive, 0, 10.0)["passed"])

    def test_cli_applies_stale_and_range_thresholds_to_build_and_validation(self):
        rows = []
        for timestamp_ms, spot_open, spot_high, spot_low, spot_close in (
            (0, 0.0010, 0.0011, 0.0009, 0.0010),
            (120_000, 0.0012, 0.0050, 0.0010, 0.0013),
            (240_000, 0.0013, 0.0015, 0.0011, 0.0014),
        ):
            rows.append({
                "mint": "MINT-CLI", "timestamp_ms": timestamp_ms,
                "open": spot_open, "high": spot_high, "low": spot_low, "close": spot_close,
                "spot_open": spot_open, "spot_high": spot_high,
                "spot_low": spot_low, "spot_close": spot_close,
                "trades": 10, "is_filled": 0, "vwap_sol": 0.001,
                "volume_token": 1000.0, "volume_sol": 1.0, "jump_flag": 0,
                "buy_execution_open": spot_open * 0.99,
                "buy_execution_high": spot_high * 0.99,
                "buy_execution_low": spot_low * 0.99,
                "buy_execution_close": spot_close * 0.99,
                "sell_execution_open": spot_open * 1.01,
                "sell_execution_high": spot_high * 1.01,
                "sell_execution_low": spot_low * 1.01,
                "sell_execution_close": spot_close * 1.01,
                "buy_execution_components": 1, "sell_execution_components": 1,
                "execution_ohlc_complete": 1, "execution_coverage": 1.0,
                "candle_range_ratio": spot_high / spot_low,
                "candle_range_anomaly_flag": int(spot_high / spot_low >= 2.0),
                "pool_id": "POOL-CLI",
            })

        with tempfile.TemporaryDirectory() as tmp:
            input_path = Path(tmp) / "ohlcv_1m.csv"
            outdir = Path(tmp) / "out"
            pd.DataFrame(rows).to_csv(input_path, index=False)
            argv = [
                "build_vbt_dataset.py", str(input_path), "-o", str(outdir),
                "--min-trades", "1", "--min-bars", "2", "--min-volume-sol", "0",
                "--max-stale-bars", "1", "--candle-range-ratio", "2.0",
                "--require-execution-ohlc",
            ]
            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                with mock.patch.object(sys, "argv", argv):
                    builder.main()

            report = json.loads((outdir / "dataset_report.json").read_text())
            self.assertTrue(report["validation"]["passed"])
            self.assertEqual(report["config"]["max_stale_bars"], 1)
            self.assertEqual(report["config"]["candle_range_ratio"], 2.0)
            self.assertEqual(report["spot_mark_valid_rows"], 5)
            self.assertEqual(report["candle_range_anomaly_rows"], 1)
            self.assertEqual(report["execution_valid_rows"], 3)
            self.assertGreater(report["universe_eligible_rows"], 0)
            self.assertEqual(report["config"]["universe_delay_bars"], 1)
            self.assertTrue((outdir / "wide" / "universe_eligible.csv").exists())
            self.assertTrue((outdir / "universe_timeline.csv").exists())
            self.assertTrue((outdir / "universe_transitions.csv").exists())
            universe_summary = pd.read_csv(outdir / "universe.csv")
            self.assertIn("ever_eligible", universe_summary.columns)
            self.assertIn("eligible_at_window_end", universe_summary.columns)
            self.assertNotIn("included", universe_summary.columns)
            long_output = pd.read_csv(outdir / "ohlcv_clean_1m.csv")
            self.assertIn("universe_eligible", long_output.columns)
            self.assertEqual(int(long_output["universe_eligible"].sum()), report["universe_eligible_rows"])

    def test_require_execution_ohlc_rejects_source_without_complete_execution_bars(self):
        rows = []
        for timestamp_ms, close in ((0, 0.001), (60_000, 0.0012), (120_000, 0.0014)):
            rows.append({
                "mint": "MINT-INCOMPLETE", "timestamp_ms": timestamp_ms,
                "open": close, "high": close * 1.1, "low": close * 0.9, "close": close,
                "spot_open": close, "spot_high": close * 1.1,
                "spot_low": close * 0.9, "spot_close": close,
                "trades": 10, "is_filled": 0, "vwap_sol": close,
                "volume_token": 1000.0, "volume_sol": 1.0, "jump_flag": 0,
                "buy_execution_open": np.nan, "buy_execution_high": np.nan,
                "buy_execution_low": np.nan, "buy_execution_close": np.nan,
                "sell_execution_open": np.nan, "sell_execution_high": np.nan,
                "sell_execution_low": np.nan, "sell_execution_close": np.nan,
                "buy_execution_components": 0, "sell_execution_components": 0,
                "execution_ohlc_complete": 0, "execution_coverage": 0.0,
                "candle_range_ratio": 1.1 / 0.9, "candle_range_anomaly_flag": 0,
                "pool_id": "POOL-INCOMPLETE",
            })

        with tempfile.TemporaryDirectory() as tmp:
            input_path = Path(tmp) / "ohlcv_1m.csv"
            pd.DataFrame(rows).to_csv(input_path, index=False)
            argv = [
                "build_vbt_dataset.py", str(input_path), "-o", str(Path(tmp) / "out"),
                "--min-trades", "1", "--min-bars", "3", "--min-volume-sol", "0",
                "--require-execution-ohlc",
            ]
            with mock.patch.object(sys, "argv", argv):
                with self.assertRaises(SystemExit) as raised:
                    builder.main()
            self.assertIn("No eligible BUY or SELL execution bars", str(raised.exception))

    def test_legacy_input_is_loaded_as_spot_only_and_execution_is_not_invented(self):
        legacy = pd.DataFrame({
            "mint": ["MINT-A"], "timestamp_ms": [0],
            "open": [1.0], "high": [1.1], "low": [0.9], "close": [1.0],
            "trades": [1], "is_filled": [0],
        })
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ohlcv_1m.csv"
            legacy.to_csv(path, index=False)
            loaded = builder.load(path)
        self.assertFalse(loaded.attrs["has_directional_execution_ohlc"])
        self.assertEqual(float(loaded.loc[0, "spot_open"]), 1.0)
        self.assertTrue(pd.isna(loaded.loc[0, "buy_execution_open"]))
        self.assertEqual(int(loaded.loc[0, "execution_ohlc_complete"]), 0)


    def test_point_in_time_universe_uses_only_prior_completed_bars(self):
        real = pd.DataFrame([
            {
                "mint": "MINT-PIT", "timestamp_ms": ts,
                "open": 0.001, "high": high, "low": low, "close": close,
                "spot_open": 0.001, "spot_high": high, "spot_low": low, "spot_close": close,
                "trades": trades, "is_filled": 0, "volume_sol": volume,
                "non_wsol_trades": 0, "zero_quote_trades": 0,
            }
            for ts, trades, volume, close, high, low in (
                (0, 10, 0.30, 0.0010, 0.0011, 0.0009),
                (60_000, 10, 0.30, 0.0012, 0.0013, 0.0010),
                (120_000, 50, 2.00, 0.0013, 0.0014, 0.0011),
                (180_000, 10, 0.30, 0.0014, 0.0015, 0.0012),
            )
        ])
        args = type("Args", (), {
            "min_trades": 20, "min_bars": 2, "min_volume_sol": 0.5,
            "min_price": 1e-5, "max_price": 0.01, "pegged_ratio": 1.02,
            "pegged_min_trades": 50, "max_non_wsol_frac": 0.5,
            "max_zero_quote_frac": 0.2, "keep_all": False,
        })()
        grid = np.array([0, 60_000, 120_000, 180_000, 240_000], dtype="int64")
        pit = builder.build_point_in_time_universe(real, grid, args, delay_bars=1)
        mask = pit["eligible"]["MINT-PIT"].tolist()

        self.assertEqual(mask[0], 0)
        self.assertEqual(mask[1], 0)
        self.assertEqual(mask[2], 1)
        self.assertEqual(mask[3], 1)
        self.assertEqual(mask[4], 1)
        self.assertEqual(float(pit["metrics"]["cumulative_trades"].iloc[2, 0]), 20.0)

    def test_point_in_time_universe_does_not_use_same_bar_volume_or_trade_count(self):
        real = pd.DataFrame([
            {
                "mint": "MINT-NO-LOOKAHEAD", "timestamp_ms": ts,
                "open": 0.001, "high": 0.0012, "low": 0.0009, "close": 0.001,
                "spot_open": 0.001, "spot_high": 0.0012,
                "spot_low": 0.0009, "spot_close": 0.001,
                "trades": trades, "is_filled": 0, "volume_sol": volume,
                "non_wsol_trades": 0, "zero_quote_trades": 0,
            }
            for ts, trades, volume in ((0, 1, 0.01), (60_000, 100, 10.0), (120_000, 1, 0.01))
        ])
        args = type("Args", (), {
            "min_trades": 50, "min_bars": 1, "min_volume_sol": 1.0,
            "min_price": 1e-5, "max_price": 0.01, "pegged_ratio": 1.01,
            "pegged_min_trades": 1000, "max_non_wsol_frac": 0.5,
            "max_zero_quote_frac": 0.2, "keep_all": False,
        })()
        grid = np.array([0, 60_000, 120_000, 180_000], dtype="int64")
        pit = builder.build_point_in_time_universe(real, grid, args, delay_bars=1)
        mask = pit["eligible"]["MINT-NO-LOOKAHEAD"].tolist()
        self.assertEqual(mask, [0, 0, 1, 1])

    def test_point_in_time_universe_can_exit_when_cumulative_quality_rule_changes(self):
        real = pd.DataFrame([
            {
                "mint": "MINT-EXIT", "timestamp_ms": ts,
                "open": 0.001, "high": 0.0012, "low": 0.0009, "close": 0.001,
                "spot_open": 0.001, "spot_high": 0.0012,
                "spot_low": 0.0009, "spot_close": 0.001,
                "trades": trades, "is_filled": 0, "volume_sol": volume,
                "non_wsol_trades": non_wsol, "zero_quote_trades": 0,
            }
            for ts, trades, volume, non_wsol in (
                (0, 30, 1.0, 0), (60_000, 30, 1.0, 0),
                (120_000, 100, 2.0, 100), (180_000, 1, 0.1, 1),
            )
        ])
        args = type("Args", (), {
            "min_trades": 20, "min_bars": 1, "min_volume_sol": 0.5,
            "min_price": 1e-5, "max_price": 0.01, "pegged_ratio": 1.001,
            "pegged_min_trades": 10000, "max_non_wsol_frac": 0.5,
            "max_zero_quote_frac": 0.2, "keep_all": False,
        })()
        grid = np.array([0, 60_000, 120_000, 180_000, 240_000], dtype="int64")
        pit = builder.build_point_in_time_universe(real, grid, args, delay_bars=1)
        mask = pit["eligible"]["MINT-EXIT"].tolist()
        self.assertEqual(mask[:3], [0, 1, 1])
        self.assertEqual(mask[3], 0)

    def test_universe_delay_must_be_at_least_one_bar(self):
        args = type("Args", (), {
            "min_trades": 1, "min_bars": 1, "min_volume_sol": 0,
            "min_price": 1e-5, "max_price": 0.01, "pegged_ratio": 1.01,
            "pegged_min_trades": 100, "max_non_wsol_frac": 0.5,
            "max_zero_quote_frac": 0.2, "keep_all": True,
        })()
        with self.assertRaises(ValueError):
            builder.build_point_in_time_universe(self.make_real_bars(), np.array([0, 60_000]), args, 0)

    def test_universe_summary_and_transitions_are_auditable(self):
        real = self.make_real_bars()
        real["trades"] = 20
        real["volume_sol"] = 1.0
        real["non_wsol_trades"] = 0
        real["zero_quote_trades"] = 0
        real["pool_changed"] = 0
        real["routed_trades"] = 0
        args = type("Args", (), {
            "min_trades": 1, "min_bars": 1, "min_volume_sol": 0,
            "min_price": 1e-5, "max_price": 100, "pegged_ratio": 1.001,
            "pegged_min_trades": 1000, "max_non_wsol_frac": 0.5,
            "max_zero_quote_frac": 0.2, "keep_all": False,
        })()
        grid = np.array([0, 60_000, 120_000], dtype="int64")
        pit = builder.build_point_in_time_universe(real, grid, args, 1)
        meta, _ = builder.mint_table(real, args)
        meta = builder.attach_point_in_time_summary(meta, pit, grid)
        self.assertTrue(bool(meta.loc["MINT-A", "ever_eligible"]))
        self.assertTrue(bool(meta.loc["MINT-A", "eligible_at_window_end"]))
        self.assertEqual(int(meta.loc["MINT-A", "eligible_bar_count"]), 2)



    def test_keep_all_still_waits_until_the_next_bar(self):
        real = self.make_real_bars()
        args = type("Args", (), {
            "min_trades": 999999, "min_bars": 999999, "min_volume_sol": 999999,
            "min_price": 100, "max_price": 101, "pegged_ratio": 100,
            "pegged_min_trades": 1, "max_non_wsol_frac": 0.0,
            "max_zero_quote_frac": 0.0, "keep_all": True,
        })()
        grid = np.array([0, 60_000, 120_000], dtype="int64")
        pit = builder.build_point_in_time_universe(real, grid, args, delay_bars=1)
        self.assertEqual(pit["eligible"]["MINT-A"].tolist(), [0, 1, 1])

    def test_universe_delay_of_two_bars_is_respected(self):
        real = self.make_real_bars()
        real["trades"] = 20
        real["volume_sol"] = 1.0
        real["non_wsol_trades"] = 0
        real["zero_quote_trades"] = 0
        args = type("Args", (), {
            "min_trades": 1, "min_bars": 1, "min_volume_sol": 0,
            "min_price": 1e-5, "max_price": 100, "pegged_ratio": 1.001,
            "pegged_min_trades": 1000, "max_non_wsol_frac": 0.5,
            "max_zero_quote_frac": 0.2, "keep_all": False,
        })()
        grid = np.array([0, 60_000, 120_000, 180_000], dtype="int64")
        pit = builder.build_point_in_time_universe(real, grid, args, delay_bars=2)
        self.assertEqual(pit["eligible"]["MINT-A"].tolist(), [0, 0, 1, 1])



if __name__ == "__main__":
    unittest.main()
