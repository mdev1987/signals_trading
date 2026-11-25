"""The execution-cost report must reprice the run without inventing data."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parents[1]
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from execution_cost_report import (  # noqa: E402
    main,
    summarize,
)


def _trade(
    *,
    mint: str,
    exit_price: float,
    pnl: float,
    entry_fee: float = 0.00125,
    exit_fee: float = 0.0,
    unsold: float = 0.0,
    status: str = "horizon_complete",
) -> dict:
    return {
        "mint": mint,
        "rule": "scanner_pumpfun_amm",
        "size": 100.0,
        "sold_size": 100.0 - unsold,
        "unsold_size": unsold,
        "entry_ts_ms": 1_000,
        "exit_ts_ms": 301_000,
        "entry_price_sol": 0.001,
        "exit_price_sol": exit_price,
        "entry_value_sol": 0.1,
        "entry_fee_sol": entry_fee,
        "exit_value_sol": (100.0 - unsold) * exit_price,
        "exit_fee_sol": exit_fee,
        "pnl_sol": pnl,
        "return_pct": pnl / 0.10125,
        "entry_impact": 0.0,
        "exit_status": status,
        "exit_reason": "horizon",
        "entry_quote_price_impact_pct": -0.2,
        "exit_quote_price_impact_pct": -0.5,
        "entry_quote_slippage_bps": 50,
        "exit_quote_slippage_bps": 100,
        "exit_full_size": unsold == 0.0 and status != "no_route",
        "exit_quote_fractions": (
            []
            if status == "no_route"
            else ([1.0] if unsold == 0.0 else [0.8])
        ),
        "entry_quote_raw": json.dumps({"_quoted_at_ms": 900}),
        "exit_fill_evidence": json.dumps(
            [{"quote_ts_ms": 300_000, "fill_ts_ms": 300_500}]
        ),
        "quote_guard_checks": 3,
        "quote_guard_unavailable": 1,
        "quote_guard_min_ratio": 0.42,
    }


ROWS = [
    _trade(mint="win", exit_price=0.0015, pnl=0.046875, exit_fee=0.001875),
    _trade(mint="loss", exit_price=0.0004, pnl=-0.06175, exit_fee=0.0005),
    _trade(
        mint="writeoff",
        exit_price=0.0,
        pnl=-0.10125,
        unsold=100.0,
        status="no_route",
    ),
    _trade(mint="partial", exit_price=0.0012, pnl=-0.00537, exit_fee=0.00012, unsold=20.0),
]


class SummaryTests(unittest.TestCase):
    def test_totals_costs_and_quote_quality(self) -> None:
        result = summarize(ROWS)
        self.assertEqual(result["trades"], 4)
        self.assertAlmostEqual(
            result["total_pnl_sol"], -0.121495, places=6
        )
        self.assertAlmostEqual(result["entry_notional_sol"], 0.4, places=6)
        self.assertAlmostEqual(
            result["modeled_cost"]["fee_sol"], 0.004875, places=6
        )
        self.assertEqual(result["modeled_cost"]["trades"], 2)
        self.assertAlmostEqual(
            result["modeled_cost"]["entry_slippage_sol"], 0.0, places=9
        )
        self.assertAlmostEqual(
            result["modeled_cost"]["share_of_entry_notional"],
            0.024375,
            places=6,
        )
        self.assertEqual(result["entry_price_impact_pct"]["n"], 4)
        self.assertAlmostEqual(
            result["entry_price_impact_pct"]["median"], -0.2
        )
        self.assertAlmostEqual(result["entry_quote_age_ms"]["median"], 100.0)
        self.assertAlmostEqual(result["exit_quote_latency_ms"]["median"], 500.0)
        self.assertAlmostEqual(result["exit_full_size_share"], 0.5)
        self.assertAlmostEqual(result["exit_fraction_ladder_share"], 0.25)
        self.assertAlmostEqual(result["write_off_share"], 0.5)
        self.assertEqual(result["quote_guard"]["checks"], 12)
        self.assertEqual(result["quote_guard"]["unavailable"], 4)
        self.assertAlmostEqual(result["quote_guard"]["min_ratio"]["median"], 0.42)

    def test_sensitivity_reproduces_the_recorded_pnl_for_full_sells(self) -> None:
        result = summarize(ROWS, fee_grid=(0.0125,), slippage_grid=(0.0,))
        row = result["cost_sensitivity"][0]
        self.assertEqual(row["trades"], 2)
        self.assertAlmostEqual(row["pnl_sol"], 0.046875 - 0.06175, places=6)

    def test_zero_cost_is_the_raw_quote_move(self) -> None:
        result = summarize(ROWS, fee_grid=(0.0,), slippage_grid=(0.0,))
        row = result["cost_sensitivity"][0]
        # Gross quote value: +0.05 and -0.06 on the two full sells.
        self.assertAlmostEqual(row["pnl_sol"], -0.01, places=6)

    def test_partial_and_write_off_are_excluded_from_repricing(self) -> None:
        result = summarize(ROWS, fee_grid=(0.0125,), slippage_grid=(0.0,))
        self.assertEqual(result["cost_sensitivity"][0]["trades"], 2)
        self.assertEqual(result["modeled_cost"]["trades"], 2)

    def test_unpriced_rows_are_counted_not_dropped_silently(self) -> None:
        rows = ROWS + [{"mint": "broken", "size": "not-a-number"}]
        result = summarize(rows, corrupt_rows=2)
        self.assertEqual(result["trades"], 4)
        self.assertEqual(result["unusable_rows"], 1)
        self.assertEqual(result["corrupt_rows"], 2)

    def test_empty_run_reports_without_dividing_by_zero(self) -> None:
        result = summarize([])
        self.assertEqual(result["trades"], 0)
        self.assertIsNone(result["modeled_cost"]["share_of_entry_notional"])
        self.assertIsNone(result["exit_full_size_share"])
        self.assertEqual(result["cost_sensitivity"][0]["trades"], 0)


class CliTests(unittest.TestCase):
    def _run_dir(self, tmp: str) -> Path:
        run_dir = Path(tmp)
        lines = [json.dumps(row) for row in ROWS]
        lines.append("{not json")
        (run_dir / "trades.jsonl").write_text("\n".join(lines))
        return run_dir

    def test_json_output_and_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._run_dir(tmp)
            artifact = Path(tmp) / "out" / "cost.json"
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(
                    [
                        "--run-dir",
                        str(run_dir),
                        "--json",
                        "--artifact",
                        str(artifact),
                    ]
                )
            self.assertEqual(code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["corrupt_rows"], 1)
            self.assertEqual(payload["trades"], 4)
            saved = json.loads(artifact.read_text())
            self.assertEqual(saved["total_pnl_sol"], payload["total_pnl_sol"])

    def test_markdown_output_names_the_real_cost_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._run_dir(tmp)
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(["--run-dir", str(run_dir)])
            self.assertEqual(code, 0)
            text = stdout.getvalue()
            self.assertIn("Execution cost report", text)
            self.assertIn("not a paid fee", text)
            self.assertIn("PnL under cost assumptions", text)
            self.assertIn("Executable-exit guard", text)

    def test_missing_trades_file_fails_loudly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                main(["--run-dir", tmp])


if __name__ == "__main__":
    unittest.main()
