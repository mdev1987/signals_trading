from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent / "tools" / "backtest"))

from verify_backtest_dataset import (  # noqa: E402
    _is_scope_dependent,
    compare_with_reference,
    timeframe_ms_from_label,
    verify_dataset,
)

BAR_MS = 60_000
START = pd.Timestamp("2026-11-08 05:00", tz="UTC")


def bar_frame(
    mints: list[str],
    bars: int,
    *,
    close_value: float = 1.0,
) -> pd.DataFrame:
    rows = []
    for mint in mints:
        for index in range(bars):
            timestamp = int((START + pd.Timedelta(minutes=index)).timestamp() * 1000)
            rows.append(
                {
                    "mint": mint,
                    "timestamp_ms": timestamp,
                    "datetime_utc": (
                        START + pd.Timedelta(minutes=index)
                    ).strftime("%Y-%m-%d %H:%M:%S"),
                    "open": close_value,
                    "high": close_value,
                    "low": close_value,
                    "close": close_value,
                    "is_filled": 0,
                    "pool": "pump-amm",
                    "age_bars": index,
                    "routed_trades": 0,
                    "prev_close": close_value,
                    "price_gap_elapsed_ms": BAR_MS,
                    "price_discontinuity_ratio": 1.0,
                    "open_event_index": index + 1,
                    "close_event_index": index + 1,
                }
            )
    return pd.DataFrame(rows)


def signal_frame(mints: list[str]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "signal_id": [f"telegram:{index}" for index in range(len(mints))],
            "mint": mints,
            "signal_timestamp_utc": [
                (START + pd.Timedelta(minutes=1)).isoformat() for _ in mints
            ],
        }
    )


class VerifyBacktestDatasetTests(unittest.TestCase):
    def test_timeframe_label_parsing(self) -> None:
        self.assertEqual(timeframe_ms_from_label("1m"), 60_000)
        self.assertEqual(timeframe_ms_from_label("5m"), 300_000)
        self.assertEqual(timeframe_ms_from_label("15s"), 15_000)
        self.assertEqual(timeframe_ms_from_label("1h"), 3_600_000)

    def test_scope_classification(self) -> None:
        self.assertTrue(_is_scope_dependent("prev_close"))
        self.assertTrue(_is_scope_dependent("prev_close_tokens_in_pool"))
        self.assertTrue(_is_scope_dependent("open_event_index"))
        self.assertTrue(_is_scope_dependent("age_bars"))
        self.assertTrue(_is_scope_dependent("routed_trades"))
        self.assertTrue(_is_scope_dependent("price_gap_elapsed_ms"))
        self.assertTrue(_is_scope_dependent("price_discontinuity_ratio"))
        self.assertFalse(_is_scope_dependent("close"))
        self.assertFalse(_is_scope_dependent("open_price_sol"))
        self.assertFalse(_is_scope_dependent("vwap_gross_execution_sol"))

    def test_dataset_passes_with_full_coverage(self) -> None:
        mints = ["mint-a", "mint-b"]
        report = verify_dataset(bar_frame(mints, 5), signal_frame(mints), BAR_MS)

        self.assertTrue(report["passed"], report["checks"])
        self.assertEqual(report["mints"], 2)
        self.assertEqual(report["rows"], 10)
        self.assertEqual(report["missing_signaled_mints"], [])
        self.assertEqual(report["signals_without_bars"], 0)

    def test_missing_signaled_mint_fails(self) -> None:
        report = verify_dataset(
            bar_frame(["mint-a"], 5), signal_frame(["mint-a", "mint-b"]), BAR_MS
        )

        self.assertFalse(report["passed"])
        self.assertEqual(report["missing_signaled_mints"], ["mint-b"])
        self.assertFalse(report["checks"]["all_signaled_mints_present"])

    def test_excluded_mints_are_reported_but_not_required(self) -> None:
        report = verify_dataset(
            bar_frame(["mint-a"], 5),
            signal_frame(["mint-a", "mint-b"]),
            BAR_MS,
            exclude_mints={"mint-b"},
        )

        self.assertTrue(report["passed"], report["checks"])
        self.assertEqual(report["missing_signaled_mints"], [])
        self.assertEqual(report["excluded_mints"], ["mint-b"])
        self.assertEqual(report["signaled_mints"], 1)

    def test_excluded_mint_still_fails_when_bars_are_missing_for_others(self) -> None:
        report = verify_dataset(
            bar_frame(["mint-a"], 5),
            signal_frame(["mint-a", "mint-b", "mint-c"]),
            BAR_MS,
            exclude_mints={"mint-b"},
        )

        self.assertFalse(report["passed"])
        self.assertEqual(report["missing_signaled_mints"], ["mint-c"])

    def test_duplicate_and_misaligned_bars_fail(self) -> None:
        frame = bar_frame(["mint-a"], 3)
        duplicated = pd.concat([frame, frame.iloc[[0]]], ignore_index=True)
        # Shift a different row so it is misaligned without duplicating a key.
        duplicated.loc[1, "timestamp_ms"] = duplicated.loc[1, "timestamp_ms"] + 1

        report = verify_dataset(duplicated, signal_frame(["mint-a"]), BAR_MS)

        self.assertFalse(report["passed"])
        self.assertEqual(report["duplicate_mint_timestamp_keys"], 1)
        self.assertEqual(report["misaligned_timestamps"], 1)

    def test_signal_after_last_bar_fails(self) -> None:
        signals = signal_frame(["mint-a"])
        signals.loc[0, "signal_timestamp_utc"] = (
            START + pd.Timedelta(hours=2)
        ).isoformat()

        report = verify_dataset(bar_frame(["mint-a"], 5), signals, BAR_MS)

        self.assertFalse(report["passed"])
        self.assertEqual(report["signals_without_bars"], 1)

    def test_reference_comparison_explains_scope_columns(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            current = bar_frame(["mint-a"], 4)
            reference = current.copy()
            # Scope-dependent columns are expected to differ.
            reference["open_event_index"] = reference["open_event_index"] + 100
            reference["age_bars"] = reference["age_bars"] + 10
            reference["routed_trades"] = 7
            reference["prev_close"] = 0.5
            reference["price_gap_elapsed_ms"] = 1
            reference["price_discontinuity_ratio"] = 2.0
            path = root / "reference.csv"
            reference.to_csv(path, index=False)

            result = compare_with_reference(
                current,
                path,
                (START, START + pd.Timedelta(hours=1)),
                {"mint-a"},
            )

        self.assertTrue(result["passed"], result["mismatched_columns"])
        self.assertEqual(result["mismatched_columns"], {})
        self.assertEqual(
            set(result["scope_dependent_columns"]),
            {
                "open_event_index",
                "age_bars",
                "routed_trades",
                "prev_close",
                "price_gap_elapsed_ms",
                "price_discontinuity_ratio",
            },
        )

    def test_reference_comparison_fails_on_price_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            current = bar_frame(["mint-a"], 4)
            reference = current.copy()
            reference["close"] = reference["close"] + 1.0
            path = root / "reference.csv"
            reference.to_csv(path, index=False)

            result = compare_with_reference(
                current,
                path,
                (START, START + pd.Timedelta(hours=1)),
                {"mint-a"},
            )

        self.assertFalse(result["passed"])
        self.assertIn("close", result["mismatched_columns"])

    def test_reference_comparison_fails_on_key_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            current = bar_frame(["mint-a"], 4)
            reference = bar_frame(["mint-a"], 3)
            path = root / "reference.csv"
            reference.to_csv(path, index=False)

            result = compare_with_reference(
                current,
                path,
                (START, START + pd.Timedelta(hours=1)),
                {"mint-a"},
            )

        self.assertFalse(result["passed"])
        self.assertFalse(result["same_keys"])


if __name__ == "__main__":
    unittest.main()
