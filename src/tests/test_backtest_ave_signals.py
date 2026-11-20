from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

SRC_DIR = Path(__file__).resolve().parents[1]
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from backtest_ave_signals import (  # noqa: E402
    MintBars,
    _excursions,
    _last_at_or_before,
    backtest,
    load_flagged_mints,
    load_signals,
    summarise_exclusions,
    summarise_sensitivity,
)

DAY = "2026-11-08"
MINT = "mint-a"


def bars(closes: list[float], start_ms: int = 60_000, step_ms: int = 60_000) -> MintBars:
    close_ts = np.arange(len(closes), dtype="int64") * step_ms + start_ms
    close_px = np.asarray(closes, dtype="float64")
    return MintBars(
        close_ts=close_ts,
        close_px=close_px,
        high_px=close_px * 1.1,
        low_px=close_px * 0.9,
    )


def signals(timestamp_ms: int, *, signal_id: str = "telegram:1") -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "day": DAY,
                "signal_id": signal_id,
                "signal_timestamp_ms": timestamp_ms,
                "signal_timestamp_utc": "2026-11-08T00:00:00+00:00",
                "mint": MINT,
                "source_username": "@AveSignalMonitor",
                "signal_type": "second_vibe_buy",
            }
        ]
    )


class PricingTests(unittest.TestCase):
    def test_last_at_or_before_uses_latest_closed_bar(self) -> None:
        series = bars([1.0, 2.0, 3.0])
        self.assertEqual(_last_at_or_before(series, 120_000), (120_000, 2.0))
        self.assertEqual(_last_at_or_before(series, 119_999), (60_000, 1.0))

    def test_last_at_or_before_returns_none_before_first_close(self) -> None:
        self.assertIsNone(_last_at_or_before(bars([1.0]), 59_999))

    def test_excursions_start_after_entry_close(self) -> None:
        series = MintBars(
            close_ts=np.array([60_000, 120_000, 180_000], dtype="int64"),
            close_px=np.array([1.0, 2.0, 3.0]),
            high_px=np.array([1.0, 9.0, 4.0]),
            low_px=np.array([1.0, 0.5, 2.0]),
        )
        # The 120 s bar spans 60-120 s, before the entry at its close, so only
        # the 180 s bar (120-180 s) belongs to the holding window.
        high, low = _excursions(series, 120_000, 180_000)
        self.assertEqual(high, 4.0)
        self.assertEqual(low, 2.0)


class BacktestTests(unittest.TestCase):
    def run_one(self, series: MintBars, timestamp_ms: int, **kwargs):
        horizon = kwargs.pop("horizons", (1,))
        result = backtest(
            signals(timestamp_ms),
            {DAY: {MINT: series}},
            horizons=horizon,
            latency_ms=5_000,
            fee_rate=0.0125,
            stake_sol=0.1,
            max_price_age_ms=60_000,
            **kwargs,
        )
        return result

    def test_entry_is_latest_close_before_decision_not_signal_bar(self) -> None:
        series = bars([1.0, 2.0, 3.0])
        # Signal at 119 s: the 120 s close is 1 s away, so the decision at
        # 124 s can only use the 120 s close (2.0), never the 180 s close.
        result = self.run_one(series, 119_000).iloc[0]
        self.assertEqual(result["status"], "closed")
        self.assertEqual(result["entry_price_timestamp_ms"], 120_000)
        self.assertEqual(result["entry_price_sol"], 2.0)
        self.assertEqual(result["entry_price_age_ms"], 4_000)
        self.assertEqual(result["exit_price_timestamp_ms"], 180_000)
        self.assertEqual(result["exit_price_sol"], 3.0)
        expected_net = (3.0 * (1 - 0.0125)) / (2.0 * (1 + 0.0125)) - 1
        self.assertAlmostEqual(result["net_return"], expected_net)
        self.assertAlmostEqual(result["net_pnl_sol"], 0.1 * expected_net)

    def test_missing_horizon_price_is_stale_but_not_forced(self) -> None:
        series = bars([1.0, 2.0, 3.0])
        result = backtest(
            signals(119_000),
            {DAY: {MINT: series}},
            horizons=(60,),
            latency_ms=5_000,
            fee_rate=0.0125,
            stake_sol=0.1,
            max_price_age_ms=60_000,
        ).iloc[0]
        # The last real close (180 s) is used, and it is honestly stale.
        self.assertEqual(result["status"], "closed")
        self.assertEqual(result["exit_price_timestamp_ms"], 180_000)
        self.assertGreater(result["exit_price_age_ms"], 60_000)

    def test_no_movement_window_is_not_a_trade(self) -> None:
        series = bars([1.0])
        result = self.run_one(series, 120_000, horizons=(1,)).iloc[0]
        self.assertEqual(result["status"], "no_move_window")
        self.assertEqual(result["entry_price_timestamp_ms"], 60_000)

    def test_no_bars_is_reported_for_every_horizon(self) -> None:
        result = self.run_one(bars([]), 0, horizons=(1, 5))
        self.assertEqual(result["status"].tolist(), ["no_bars", "no_bars"])

    def test_flagged_mint_is_marked(self) -> None:
        result = self.run_one(
            bars([1.0, 2.0, 3.0]),
            119_000,
            flagged_mints_by_day={DAY: {MINT}},
        )
        self.assertTrue(bool(result.iloc[0]["flagged_mint"]))

    def test_net_return_includes_fees_on_both_sides(self) -> None:
        result = self.run_one(bars([1.0, 1.0]), 70_000).iloc[0]
        expected = (1 - 0.0125) / (1 + 0.0125) - 1
        self.assertAlmostEqual(result["gross_return"], 0.0)
        self.assertAlmostEqual(result["net_return"], expected)
        self.assertLess(result["net_return"], 0.0)


class ReportingTests(unittest.TestCase):
    def make_results(self) -> pd.DataFrame:
        rows = []
        for index, (mint, flagged) in enumerate(
            [("mint-a", False), ("mint-b", True), ("mint-c", False)]
        ):
            for horizon in (1, 30):
                rows.append(
                    {
                        "day": DAY,
                        "signal_id": f"telegram:{index}",
                        "mint": mint,
                        "flagged_mint": flagged,
                        "horizon_minutes": horizon,
                        "status": "closed",
                        "net_return": 0.1 * (index + 1),
                        "net_pnl_sol": 0.01 * (index + 1),
                        "entry_price_age_ms": 0,
                        "exit_price_age_ms": 1_000,
                    }
                )
        return pd.DataFrame(rows)

    def test_sensitivity_excludes_flagged_mints(self) -> None:
        table = summarise_sensitivity(self.make_results(), 30).set_index("subset")
        self.assertEqual(int(table.loc["all", "trades"]), 3)
        self.assertEqual(int(table.loc["ex_flagged_mints", "trades"]), 2)
        self.assertAlmostEqual(
            table.loc["ex_flagged_mints", "mean_net_return"], 0.2
        )

    def test_coverage_counts_exclusions_per_day(self) -> None:
        results = self.make_results()
        results.loc[results["mint"] == "mint-c", "status"] = "no_bars"
        signals = pd.DataFrame(
            {
                "day": [DAY] * 4,
                "mint": ["mint-a", "mint-b", "mint-c", "mint-d"],
            }
        )
        coverage = summarise_exclusions(results, signals, 30).iloc[0]
        self.assertEqual(int(coverage["signals"]), 4)
        self.assertEqual(int(coverage["closed"]), 2)
        self.assertEqual(int(coverage["excluded"]), 2)


class SignalLoaderTests(unittest.TestCase):
    def test_channel_filter_is_exact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "output" / "backtest" / "signals" / DAY
            target.mkdir(parents=True)
            pd.DataFrame(
                [
                    {
                        "signal_id": "telegram:1",
                        "signal_timestamp_ms": 1_700_000_000_000,
                        "signal_timestamp_utc": "2026-11-08T00:00:00+00:00",
                        "mint": "mint-a",
                        "chain": "solana",
                        "signal_type": "second_vibe_buy",
                        "source_message_id": 1,
                        "source_username": "@AveSignalMonitor",
                        "source_message_link": "https://t.me/AveSignalMonitor/1",
                    },
                    {
                        "signal_id": "telegram:2",
                        "signal_timestamp_ms": 1_700_000_060_000,
                        "signal_timestamp_utc": "2026-11-08T00:01:00+00:00",
                        "mint": "mint-b",
                        "chain": "solana",
                        "signal_type": "second_vibe_buy",
                        "source_message_id": 2,
                        "source_username": "@AveSignalMonitorFan",
                        "source_message_link": "https://t.me/AveSignalMonitorFan/2",
                    },
                ]
            ).to_csv(target / "strategy_signals.csv", index=False)

            loaded = load_signals(root, [DAY], "AveSignalMonitor")
            self.assertEqual(loaded["mint"].tolist(), ["mint-a"])


class FlagLoaderTests(unittest.TestCase):
    def test_missing_flag_file_means_nothing_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(load_flagged_mints(Path(directory), DAY), set())

    def test_flag_file_is_read_from_ohlcv_folder(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "output" / "backtest" / "days" / DAY / "ohlcv"
            target.mkdir(parents=True)
            (target / "flagged_candle_range_anomaly_1m.csv").write_text(
                "mint,reason\nmint-a,range\nmint-b,range\n", encoding="utf-8"
            )
            self.assertEqual(
                load_flagged_mints(Path(directory), DAY), {"mint-a", "mint-b"}
            )


if __name__ == "__main__":
    unittest.main()
