"""Tests for the preflight exit-depth threshold study."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parents[1]
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from preflight_study import (  # noqa: E402
    CATASTROPHIC_RETURN,
    JOIN_TOLERANCE_MS,
    JoinedTrade,
    PreflightQuote,
    depth_margin_bps,
    evaluate,
    fee_floor_ratio,
    format_table,
    join_quotes,
    load_funnel,
    parse_preflight_log,
    summarize_ratios,
)


def log_line(
    ts: str = "2026-11-24 05:02:14,090",
    mint: str = "MintA",
    ratio: str = "0.967206",
    minimum: str = "0.9000",
) -> str:
    return (
        f"{ts} INFO paper_bot.engine preflight lane mint={mint} "
        f"exit_price=0.000842659411 entry_cost=0.101250000 "
        f"ratio={ratio} min={minimum}\n"
    )


def trade(
    mint: str = "MintA",
    entry_ts_ms: int = 1_795_609_734_000,
    return_pct: float = 0.2,
    pnl_sol: float = 0.02,
    *,
    out: str | None = None,
    threshold: str | None = None,
) -> dict:
    row = {
        "mint": mint,
        "entry_ts_ms": entry_ts_ms,
        "return_pct": return_pct,
        "pnl_sol": pnl_sol,
        "entry_value_sol": 0.1,
        "entry_fee_sol": 0.00125,
        "exit_value_sol": 0.12,
        "fee": 0.0125,
    }
    if out is not None and threshold is not None:
        row["exit_quote_raw"] = json.dumps(
            {"outAmount": out, "otherAmountThreshold": threshold}
        )
    return row


class FeeFloorTests(unittest.TestCase):
    def test_floor_matches_the_deployed_cost_model(self) -> None:
        # 1.25 %/side plus 50 bps slippage: a zero-move trade still returns
        # about 0.97, so 0.95 can never reject anything.
        self.assertAlmostEqual(fee_floor_ratio(0.0125, 0.005), 0.970432, places=6)

    def test_zero_fee_zero_slippage_floor_is_one(self) -> None:
        self.assertAlmostEqual(fee_floor_ratio(0.0, 0.0), 1.0)

    def test_depth_margin_is_relative_to_the_floor(self) -> None:
        floor = 0.97
        self.assertAlmostEqual(depth_margin_bps(0.97, floor), 0.0)
        self.assertAlmostEqual(depth_margin_bps(0.97 * 1.005, floor), 50.0)


class ParseTests(unittest.TestCase):
    def test_parses_ratio_and_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bot.log"
            path.write_text(log_line() + "unrelated line\n")
            quotes = parse_preflight_log(path)
        self.assertEqual(len(quotes), 1)
        self.assertEqual(quotes[0].mint, "MintA")
        self.assertAlmostEqual(quotes[0].ratio, 0.967206)
        self.assertEqual(quotes[0].minimum, 0.9)
        self.assertEqual(quotes[0].timestamp_ms, 1_795_496_534_090)

    def test_invalid_ratio_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bot.log"
            path.write_text(log_line(ratio="invalid"))
            self.assertEqual(parse_preflight_log(path), [])


class JoinTests(unittest.TestCase):
    def test_nearest_entry_time_wins(self) -> None:
        quotes = [
            PreflightQuote(1_000_000, "MintA", 0.97, 0.9, 1.0, 0.1),
            PreflightQuote(9_000_000, "MintA", 0.98, 0.9, 1.0, 0.1),
        ]
        trades = [
            trade(entry_ts_ms=1_100_000),
            trade(entry_ts_ms=9_100_000),
        ]
        joined, unmatched_quotes, unmatched_trades = join_quotes(quotes, trades)
        self.assertEqual(unmatched_quotes, 0)
        self.assertEqual(unmatched_trades, 0)
        self.assertEqual([item.ratio for item in joined], [0.97, 0.98])

    def test_far_apart_pair_is_not_joined(self) -> None:
        quotes = [PreflightQuote(1_000_000, "MintA", 0.97, 0.9, 1.0, 0.1)]
        trades = [trade(entry_ts_ms=1_000_000 + JOIN_TOLERANCE_MS + 1)]
        joined, unmatched_quotes, unmatched_trades = join_quotes(quotes, trades)
        self.assertEqual(joined, [])
        self.assertEqual(unmatched_quotes, 1)
        self.assertEqual(unmatched_trades, 1)

    def test_each_quote_is_used_once(self) -> None:
        quotes = [PreflightQuote(1_000_000, "MintA", 0.97, 0.9, 1.0, 0.1)]
        trades = [trade(entry_ts_ms=1_050_000), trade(entry_ts_ms=1_060_000)]
        joined, unmatched_quotes, _ = join_quotes(quotes, trades)
        self.assertEqual(len(joined), 1)
        self.assertEqual(unmatched_quotes, 0)


class FunnelTests(unittest.TestCase):
    def test_counts_signals_accepted_and_skip_reasons(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            (run / "signals.jsonl").write_text(
                json.dumps({"decision": {"allowed": True}})
                + "\n"
                + json.dumps({"decision": {"allowed": False}})
                + "\n"
            )
            (run / "skips.jsonl").write_text(
                json.dumps({"reason": "entry_no_exit_economics"})
                + "\n"
                + json.dumps({"reason": "gate_rejected"})
                + "\n"
            )
            funnel = load_funnel([run])
        self.assertEqual(funnel["signals_received"], 2)
        self.assertEqual(funnel["signals_accepted"], 1)
        self.assertEqual(funnel["skip_reasons"]["gate_rejected"], 1)
        self.assertEqual(funnel["skip_reasons"]["entry_no_exit_economics"], 1)

    def test_missing_files_are_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            funnel = load_funnel([Path(tmp)])
        self.assertEqual(funnel["signals_received"], 0)
        self.assertEqual(funnel["skip_reasons"], {})


class EvaluateTests(unittest.TestCase):
    def build(self):
        values = [
            # (ratio, return_pct, pnl_sol)
            (0.9600, +0.30, +0.03),
            (0.9650, -0.95, -0.095),
            (0.9680, +0.20, +0.02),
            (0.9720, -0.92, -0.092),
            (0.9750, +0.40, +0.04),
        ]
        joined = []
        for index, (ratio, ret, pnl) in enumerate(values):
            joined.append(
                JoinedTrade(
                    ratio=ratio,
                    entry_ts_ms=index,
                    mint=f"M{index}",
                    return_pct=ret,
                    pnl_sol=pnl,
                    exit_value_sol=0.1,
                    pnl_at_threshold_sol=None,
                )
            )
        return joined

    def test_baseline_keeps_everything(self) -> None:
        rows = evaluate(self.build(), [0.90], baseline_ratio=0.90)
        row = rows[0]
        self.assertEqual(row["accepted"], 5)
        self.assertEqual(row["rejected"], 0)
        self.assertTrue(row["is_deployed_gate"])
        self.assertAlmostEqual(row["catastrophic_rate"], 2 / 5)

    def test_stricter_threshold_removes_the_tail_but_costs_winners(self) -> None:
        rows = evaluate(self.build(), [0.97], baseline_ratio=0.90)
        row = rows[0]
        self.assertEqual(row["accepted"], 2)
        self.assertEqual(row["rejected"], 3)
        # One catastrophic and two winners are rejected.
        self.assertEqual(row["rejected_catastrophic"], 1)
        self.assertEqual(row["rejected_profitable"], 2)
        self.assertAlmostEqual(row["rejected_pnl_sol"], 0.03 - 0.095 + 0.02)
        # 1 of the remaining 2 is still catastrophic.
        self.assertAlmostEqual(row["catastrophic_rate"], 1 / 2)

    def test_near_zero_loss_is_reported_positive(self) -> None:
        rows = evaluate(self.build(), [0.90], baseline_ratio=0.90)
        self.assertAlmostEqual(rows[0]["near_zero_loss_sol"], 0.095 + 0.092)

    def test_catastrophic_definition_matches_the_repo(self) -> None:
        self.assertEqual(CATASTROPHIC_RETURN, -0.9)

    def test_max_drawdown_is_peak_to_trough(self) -> None:
        rows = evaluate(self.build(), [0.90], baseline_ratio=0.90)
        # Equity: +0.03, -0.065, -0.045, -0.137, -0.097.
        # Peak 0.03 -> trough -0.137 is the worst drawdown.
        self.assertAlmostEqual(rows[0]["max_drawdown_sol"], -0.167)

    def test_threshold_fill_pnl_is_none_without_quotes(self) -> None:
        rows = evaluate(self.build(), [0.90], baseline_ratio=0.90)
        self.assertIsNone(rows[0]["pnl_at_threshold_fill_sol"])

    def test_threshold_fill_scales_the_exit_value(self) -> None:
        joined, _, _ = join_quotes(
            [PreflightQuote(1_000_000, "MintA", 0.97, 0.9, 1.0, 0.1)],
            [trade(entry_ts_ms=1_000_000, out="1000", threshold="995")],
        )
        self.assertIsNotNone(joined[0].pnl_at_threshold_sol)
        # A 0.5 % worse exit on 0.12 gross SOL, after 1.25 % fee.
        expected = 0.02 - 0.12 * 0.005 * (1 - 0.0125)
        self.assertAlmostEqual(joined[0].pnl_at_threshold_sol, expected)


class FormatTests(unittest.TestCase):
    def test_table_renders_the_depth_margin(self) -> None:
        joined, _, _ = join_quotes(
            [PreflightQuote(1_000_000, "MintA", 0.97, 0.9, 1.0, 0.1)],
            [trade(entry_ts_ms=1_000_000)],
        )
        floor = fee_floor_ratio(0.0125, 0.005)
        rows = evaluate(joined, [0.90, 0.98], baseline_ratio=0.90)
        text = format_table(rows, floor=floor, stats=summarize_ratios(joined, floor))
        self.assertIn("0.900*", text)
        self.assertIn("depth", text)
        self.assertIn("PnL@thr", text)


if __name__ == "__main__":
    unittest.main()
