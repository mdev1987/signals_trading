"""Tests for the reproducible gate and channel-comparison tooling.

These cover the definitions the evidence document depends on: the canonical
quote-notional filter (shared by replay and live) and the frozen gate
evaluation, including the seeded cluster bootstrap.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd

SRC_DIR = Path(__file__).resolve().parents[1]
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from compare_channels import deployed_scanner_rule  # noqa: E402
from forward_gates import (  # noqa: E402
    cluster_bootstrap,
    concentration_summary,
    evaluate,
    payoff_summary,
    tail_sensitivity,
    wilson_interval,
)
from paper_bot.models import canonical_quote_notional_sol  # noqa: E402

WSOL = "So11111111111111111111111111111111111111112"


class CanonicalNotionalTests(unittest.TestCase):
    def test_sol_quote_is_used_directly(self) -> None:
        value = canonical_quote_notional_sol(
            quote_amount=0.42,
            quote_mint=WSOL,
            quote_price_sol=None,
            token_amount=100.0,
            price_sol=0.0042,
        )
        self.assertAlmostEqual(value, 0.42)

    def test_usdc_quote_is_converted_to_sol(self) -> None:
        # 5 USDC at 0.0137 SOL/USDC is 0.0685 SOL, not 5 SOL.
        value = canonical_quote_notional_sol(
            quote_amount=5.0,
            quote_mint="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            quote_price_sol=0.0137,
            token_amount=1000.0,
            price_sol=0.0000685,
        )
        self.assertAlmostEqual(value, 0.0685, places=6)

    def test_missing_quote_metadata_falls_back_to_the_sol_mark(self) -> None:
        value = canonical_quote_notional_sol(
            quote_amount=5.0,
            quote_mint=None,
            quote_price_sol=None,
            token_amount=1000.0,
            price_sol=0.0002,
        )
        self.assertAlmostEqual(value, 0.2)

    def test_unknown_notional_is_none(self) -> None:
        self.assertIsNone(
            canonical_quote_notional_sol(
                quote_amount=None,
                quote_mint=None,
                quote_price_sol=None,
            )
        )


class ScannerRuleTests(unittest.TestCase):
    def frame(self) -> pd.DataFrame:
        return pd.DataFrame(
            [
                {
                    "channel": "@AveSolanaTokenScanner",
                    "mint": "mint-a",
                    "alert_ts_ms": 1,
                    "dex": "Raydium",
                    "liquidity_usd": 50_000,
                },
                {
                    "channel": "@AveSolanaTokenScanner",
                    "mint": "mint-a",
                    "alert_ts_ms": 2,
                    "dex": "Pumpfunamm",
                    "liquidity_usd": 50_000,
                },
                {
                    "channel": "@AveSolanaTokenScanner",
                    "mint": "mint-b",
                    "alert_ts_ms": 3,
                    "dex": "Pumpfunamm",
                    "liquidity_usd": 50_000,
                },
                {
                    "channel": "@AveSolanaTokenScanner",
                    "mint": "mint-c",
                    "alert_ts_ms": 4,
                    "dex": "Pumpfunamm",
                    "liquidity_usd": 10_000,
                },
                {
                    "channel": "@AveSolanaTokenScanner",
                    "mint": "mint-d",
                    "alert_ts_ms": 5,
                    "dex": "Pumpfunamm",
                    "liquidity_usd": 100_000,
                },
                {
                    "channel": "@DRBTSolanaPF",
                    "mint": "mint-e",
                    "alert_ts_ms": 6,
                    "dex": "Pumpfunamm",
                    "liquidity_usd": 50_000,
                },
            ]
        )

    def test_first_card_is_kept_and_later_cards_are_not(self) -> None:
        result = deployed_scanner_rule(self.frame())
        self.assertEqual(set(result["mint"]), {"mint-b"})
        # mint-a's first card was Raydium, so its later Pumpfunamm card is not
        # a second chance at the same pool.
        self.assertEqual(set(result["dex"]), {"Pumpfunamm"})

    def test_band_is_half_open_and_channel_is_fixed(self) -> None:
        result = deployed_scanner_rule(self.frame())
        self.assertTrue((result["liquidity_usd"] >= 20_000).all())
        self.assertTrue((result["liquidity_usd"] < 100_000).all())


class PayoffSummaryTests(unittest.TestCase):
    def test_break_even_win_rate_tracks_the_payoff(self) -> None:
        summary = payoff_summary(
            [0.02, 0.02, 0.02, 0.02, -0.1],
            [0.2, 0.2, 0.2, 0.2, -1.0],
        )
        assert summary is not None
        self.assertAlmostEqual(summary["win_rate"], 0.8)
        self.assertAlmostEqual(summary["avg_win_return"], 0.2)
        self.assertAlmostEqual(summary["avg_loss_return"], -1.0)
        # 1.0 / (0.2 + 1.0): an 80 % win rate is below break-even here.
        self.assertAlmostEqual(summary["break_even_win_rate"], 1.0 / 1.2)
        self.assertAlmostEqual(summary["win_rate_margin"], 0.8 - 1.0 / 1.2)
        self.assertTrue(summary["win_rate_margin"] < 0)

    def test_high_win_rate_can_still_have_negative_pnl(self) -> None:
        # 4 small wins, 1 total loss: 80 % wins, negative total.
        summary = payoff_summary(
            [0.02, 0.02, 0.02, 0.02, -0.1],
            [0.2, 0.2, 0.2, 0.2, -1.0],
        )
        assert summary is not None
        self.assertAlmostEqual(summary["pnl_sol"], -0.02)
        self.assertLess(summary["pnl_sol"], 0)

    def test_concentration_reports_pnl_without_the_best_trades(self) -> None:
        summary = payoff_summary(
            [0.5, 0.02, 0.02, -0.1],
            [5.0, 0.2, 0.2, -1.0],
        )
        assert summary is not None
        self.assertAlmostEqual(summary["pnl_sol"], 0.44)
        self.assertAlmostEqual(summary["pnl_without_best_1_sol"], -0.06)
        self.assertAlmostEqual(summary["pnl_without_best_3_sol"], -0.1)

    def test_no_losses_has_zero_break_even(self) -> None:
        summary = payoff_summary([0.01, 0.01], [0.1, 0.1])
        assert summary is not None
        self.assertEqual(summary["break_even_win_rate"], 0.0)
        self.assertAlmostEqual(summary["win_rate_margin"], 1.0)

    def test_empty_returns_is_none(self) -> None:
        self.assertIsNone(payoff_summary([], []))


class WilsonIntervalTests(unittest.TestCase):
    def test_matches_the_reference_interval(self) -> None:
        ci = wilson_interval(162, 870)
        assert ci is not None
        self.assertAlmostEqual(ci["p"], 162 / 870)
        self.assertAlmostEqual(ci["low"], 0.1617, places=4)
        self.assertAlmostEqual(ci["high"], 0.2134, places=4)

    def test_zero_successes_starts_at_zero(self) -> None:
        ci = wilson_interval(0, 40)
        assert ci is not None
        self.assertEqual(ci["low"], 0.0)
        self.assertGreater(ci["high"], 0.0)

    def test_no_trades_is_none(self) -> None:
        self.assertIsNone(wilson_interval(0, 0))


class TailSensitivityTests(unittest.TestCase):
    def test_break_even_row_is_where_expectancy_hits_zero(self) -> None:
        # Non-tail trades return +25 %, tails lose everything.
        returns = [0.25] * 75 + [-1.0] * 25
        pnls = [0.025] * 100
        sensitivity = tail_sensitivity(returns, pnls, steps=(0.0,))
        assert sensitivity is not None
        self.assertAlmostEqual(sensitivity["break_even_tail_rate"], 0.2)
        break_even_rows = [
            row for row in sensitivity["rows"] if abs(row["mean_return"]) < 1e-12
        ]
        self.assertEqual(len(break_even_rows), 1)
        self.assertAlmostEqual(break_even_rows[0]["expected_pnl_sol"], 0.0)

    def test_higher_tail_rate_reduces_expectancy(self) -> None:
        returns = [0.25] * 78 + [-1.0] * 22
        pnls = [0.025] * 100
        sensitivity = tail_sensitivity(returns, pnls)
        assert sensitivity is not None
        # Steps first (descending expectancy), then the break-even row at 0.
        steps = [row["mean_return"] for row in sensitivity["rows"][:-1]]
        self.assertEqual(steps, sorted(steps, reverse=True))
        self.assertLess(steps[-1], steps[0])
        self.assertAlmostEqual(sensitivity["rows"][-1]["mean_return"], 0.0)

    def test_empty_returns_is_none(self) -> None:
        self.assertIsNone(tail_sensitivity([], []))


class ConcentrationTests(unittest.TestCase):
    def _trade(self, day: int, mint: str, pnl: float) -> dict[str, object]:
        stamp = datetime(2026, 11, 20 + day, 12, tzinfo=UTC)
        return {
            "mint": mint,
            "pnl_sol": pnl,
            "return_pct": 0.2 if pnl > 0 else -1.0,
            "exit_ts_ms": int(stamp.timestamp() * 1000),
        }

    def test_day_flips_when_its_best_trade_is_removed(self) -> None:
        trades = [
            self._trade(1, "a", 0.5),
            self._trade(1, "b", -0.4),
            self._trade(2, "c", 0.1),
            self._trade(2, "d", 0.2),
        ]
        summary = concentration_summary(trades)
        assert summary is not None
        self.assertEqual(summary["total_days"], 2)
        self.assertEqual(summary["profitable_days"], 2)
        self.assertEqual(summary["days_flipped_by_best_1"], 1)
        flipped = [day for day in summary["days"] if day["flipped_by_best_1"]]
        self.assertEqual(len(flipped), 1)
        self.assertAlmostEqual(flipped[0]["pnl_without_best_1_sol"], -0.4)

    def test_best_mint_is_excluded_from_the_remainder(self) -> None:
        trades = [
            self._trade(1, "big", 0.9),
            self._trade(1, "small", 0.1),
            self._trade(2, "small", 0.05),
        ]
        summary = concentration_summary(trades)
        assert summary is not None
        self.assertEqual(summary["best_mint"], "big")
        self.assertAlmostEqual(summary["best_mint_pnl_sol"], 0.9)
        self.assertAlmostEqual(summary["pnl_without_best_mint_sol"], 0.15)
        self.assertEqual(summary["unique_mints"], 2)

    def test_malformed_rows_are_skipped(self) -> None:
        summary = concentration_summary([{"mint": "x", "pnl_sol": "bad"}])
        self.assertIsNone(summary)


class ForwardGateTests(unittest.TestCase):
    def test_bootstrap_does_not_split_trades_from_one_mint(self) -> None:
        # One cluster: every resample must contain both legs, so the total is
        # always zero. Treating trades as independent would produce spread.
        result = cluster_bootstrap([1.0, -1.0], ["same", "same"], resamples=200)
        assert result is not None
        self.assertEqual(result["clusters"], 1.0)
        self.assertAlmostEqual(result["lower_sol"], 0.0)
        self.assertAlmostEqual(result["upper_sol"], 0.0)

    def test_bootstrap_is_seeded(self) -> None:
        first = cluster_bootstrap([0.1, -0.2, 0.3], ["a", "b", "c"], resamples=50)
        second = cluster_bootstrap([0.1, -0.2, 0.3], ["a", "b", "c"], resamples=50)
        self.assertEqual(first, second)

    def _write_run(
        self,
        root: Path,
        *,
        trades: int,
        days: int,
        pnl: float,
        summary_integrity: str | None = "VALID",
        persistence_degraded: bool | None = False,
        meta_integrity: str | None = "VALID",
    ) -> None:
        start = datetime(2026, 11, 18, 12, tzinfo=UTC)
        with (root / "trades.jsonl").open("w", encoding="utf-8") as handle:
            for index in range(trades):
                when = start + timedelta(days=index % days, minutes=index)
                handle.write(
                    json.dumps(
                        {
                            "mint": f"mint-{index % 40}",
                            "exit_ts_ms": int(when.timestamp() * 1000),
                            "pnl_sol": pnl,
                            "return_pct": 0.2 if pnl > 0 else -0.2,
                            "exit_status": "horizon_complete",
                            "unsold_size": 0.0,
                        }
                    )
                    + "\n"
                )
        with (root / "signals.jsonl").open("w", encoding="utf-8") as handle:
            for _ in range(trades):
                handle.write(
                    json.dumps({"decision": {"allowed": True}}) + "\n"
                )
        with (root / "entries.jsonl").open("w", encoding="utf-8") as handle:
            for _ in range(trades):
                handle.write("{}\n")
        # A clean run records its persistence verdict in both files. Tests
        # that exercise the integrity gate pass ``None`` or a non-VALID value.
        if summary_integrity is not None or persistence_degraded is not None:
            (root / "summary.json").write_text(
                json.dumps(
                    {
                        "integrity": summary_integrity,
                        "persistence_degraded": persistence_degraded,
                    }
                ),
                encoding="utf-8",
            )
        if meta_integrity is not None:
            (root / "meta.json").write_text(
                json.dumps({"integrity_at_start": meta_integrity}),
                encoding="utf-8",
            )

    def test_coverage_failure_returns_not_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_run(root, trades=10, days=1, pnl=0.1)
            result = evaluate(root)
        self.assertFalse(result["coverage_met"])
        self.assertFalse(result["gates"]["coverage"]["passed"])

    def test_full_positive_run_passes_every_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_run(root, trades=300, days=5, pnl=0.01)
            result = evaluate(root)
        self.assertTrue(result["coverage_met"])
        self.assertTrue(result["passed"])
        self.assertGreater(result["gates"]["realized_pnl"]["pnl_sol"], 0)
        self.assertEqual(result["gates"]["catastrophic_exits"]["count"], 0)
        # Report-only: the payoff block is present and agrees with the ledger.
        self.assertAlmostEqual(result["payoff"]["win_rate"], 1.0)
        self.assertAlmostEqual(result["payoff"]["pnl_sol"], 3.0)

    def test_non_valid_integrity_fails_the_run(self) -> None:
        cases = {
            "summary INVALID": {"summary_integrity": "INVALID"},
            "summary DEGRADED": {"summary_integrity": "DEGRADED"},
            "persistence degraded": {"persistence_degraded": True},
            "meta INVALID": {"meta_integrity": "INVALID"},
            "meta DEGRADED": {"meta_integrity": "DEGRADED"},
            "meta missing": {"meta_integrity": None},
            "summary missing": {
                "summary_integrity": None,
                "persistence_degraded": None,
            },
            "summary null integrity": {
                "summary_integrity": None,
                "persistence_degraded": False,
            },
        }
        for label, overrides in cases.items():
            with self.subTest(label):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    self._write_run(
                        root, trades=300, days=5, pnl=0.01, **overrides
                    )
                    result = evaluate(root)
                gate = result["gates"]["run_integrity"]
                self.assertFalse(gate["passed"], gate["detail"])
                self.assertFalse(result["passed"])

    def test_valid_integrity_is_required_by_the_same_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_run(root, trades=300, days=5, pnl=0.01)
            result = evaluate(root)
        gate = result["gates"]["run_integrity"]
        self.assertTrue(gate["passed"], gate["detail"])
        self.assertEqual(gate["summary_integrity"], "VALID")
        self.assertEqual(gate["meta_integrity_at_start"], "VALID")

    def test_catastrophic_boundary_is_strictly_below_16_percent(self) -> None:
        for count, expected in ((47, True), (48, False), (49, False)):
            with self.subTest(count=count):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    self._write_run(root, trades=300, days=5, pnl=0.01)
                    rows = [
                        json.loads(line)
                        for line in (root / "trades.jsonl")
                        .read_text()
                        .splitlines()
                    ]
                    for row in rows[:count]:
                        row["return_pct"] = -0.95
                    (root / "trades.jsonl").write_text(
                        "\n".join(json.dumps(row) for row in rows) + "\n"
                    )
                    result = evaluate(root)
                gate = result["gates"]["catastrophic_exits"]
                self.assertEqual(gate["passed"], expected, gate["detail"])

    def test_write_off_boundary_is_strictly_below_16_percent(self) -> None:
        for count, expected in ((47, True), (48, False), (49, False)):
            with self.subTest(count=count):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    self._write_run(root, trades=300, days=5, pnl=0.01)
                    rows = [
                        json.loads(line)
                        for line in (root / "trades.jsonl")
                        .read_text()
                        .splitlines()
                    ]
                    for row in rows[:count]:
                        row["exit_status"] = "no_route"
                    (root / "trades.jsonl").write_text(
                        "\n".join(json.dumps(row) for row in rows) + "\n"
                    )
                    result = evaluate(root)
                execution = result["gates"]["execution"]
                self.assertEqual(
                    execution["passed"], expected, execution["detail"]
                )

    def test_preflight_failures_count_as_entry_failures(self) -> None:
        # 150 of 1000 allowed signals fail the exit-route preflight and are
        # skipped. The fill rate stays at 85 % (above 80 %), so only a correct
        # entry-failure numerator can reject this run.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_run(root, trades=300, days=5, pnl=0.01)
            (root / "signals.jsonl").write_text(
                "".join(
                    json.dumps({"decision": {"allowed": True}}) + "\n"
                    for _ in range(1000)
                ),
                encoding="utf-8",
            )
            (root / "entries.jsonl").write_text(
                "{}\n" * 850, encoding="utf-8"
            )
            (root / "skips.jsonl").write_text(
                "".join(
                    json.dumps({"reason": "entry_no_exit_economics"}) + "\n"
                    for _ in range(150)
                ),
                encoding="utf-8",
            )
            result = evaluate(root)
        execution = result["gates"]["execution"]
        self.assertAlmostEqual(execution["entry_fill_rate"], 0.85)
        self.assertAlmostEqual(execution["entry_failure_rate"], 0.15)
        self.assertEqual(
            execution["entry_failure_reason_counts"],
            {"entry_no_exit_economics": 150},
        )
        self.assertFalse(execution["passed"])
        self.assertFalse(result["passed"])

    def test_skip_for_a_rejected_signal_is_not_an_entry_failure(self) -> None:
        # Intake rejections already leave both fill-rate sides; a skip row that
        # shares only the reason string with an entry failure must not be
        # counted for a different, disallowed signal.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_run(root, trades=300, days=5, pnl=0.01)
            signals = []
            for index in range(1000):
                signals.append(
                    json.dumps(
                        {
                            "signal": {
                                "message_id": index,
                                "channel": "@c",
                                "token_address": f"mint-{index}",
                                "signal_ts_ms": 1000 + index,
                            },
                            "decision": {"allowed": True},
                        }
                    )
                )
            for index in range(1000, 1150):
                signals.append(
                    json.dumps(
                        {
                            "signal": {
                                "message_id": index,
                                "channel": "@c",
                                "token_address": f"mint-{index}",
                                "signal_ts_ms": 1000 + index,
                            },
                            "decision": {
                                "allowed": False,
                                "reasons": ["entry_window_passed"],
                            },
                        }
                    )
                )
            (root / "signals.jsonl").write_text(
                "\n".join(signals) + "\n", encoding="utf-8"
            )
            (root / "entries.jsonl").write_text(
                "{}\n" * 1000, encoding="utf-8"
            )
            (root / "skips.jsonl").write_text(
                "".join(
                    json.dumps(
                        {
                            "reason": "entry_no_exit_economics",
                            "message_id": index,
                            "channel": "@c",
                            "mint": f"mint-{index}",
                            "signal_ts_ms": 1000 + index,
                        }
                    )
                    + "\n"
                    for index in range(1000, 1150)
                ),
                encoding="utf-8",
            )
            result = evaluate(root)
        execution = result["gates"]["execution"]
        self.assertEqual(execution["entry_failures"], 0)
        self.assertEqual(execution["entry_failure_rate"], 0.0)
        self.assertTrue(execution["passed"])

    def test_catastrophic_tail_fails_even_when_total_is_positive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_run(root, trades=300, days=5, pnl=0.01)
            # Flip 60 of the 300 returns (20 %) below the -90 % tail.
            rows = [
                json.loads(line)
                for line in (root / "trades.jsonl").read_text().splitlines()
            ]
            for row in rows[:60]:
                row["return_pct"] = -0.95
            (root / "trades.jsonl").write_text(
                "\n".join(json.dumps(row) for row in rows) + "\n"
            )
            result = evaluate(root)
        self.assertFalse(result["gates"]["catastrophic_exits"]["passed"])
        self.assertFalse(result["passed"])


    def _write_boundary_run(self, root: Path, failures: int) -> None:
        # 300 trades satisfy coverage; 1000 allowed signals and entries make
        # the fill rate 100 % while the failure ratio is exactly failures/1000.
        self._write_run(root, trades=300, days=5, pnl=0.01)
        (root / "signals.jsonl").write_text(
            "\n".join(
                json.dumps({"decision": {"allowed": True}})
                for _ in range(1000)
            )
            + "\n",
            encoding="utf-8",
        )
        (root / "entries.jsonl").write_text("{}\n" * 1000, encoding="utf-8")
        (root / "skips.jsonl").write_text(
            "".join(
                json.dumps({"reason": "entry_delay_exceeded"}) + "\n"
                for _ in range(failures)
            ),
            encoding="utf-8",
        )

    def test_entry_failure_boundary_is_strictly_below_ten_percent(self) -> None:
        # The frozen criterion says "below 10 %": 9.9 % passes, 10.0 % and
        # 10.1 % fail even though the fill rate is 100 % in every case.
        for failures, expected in ((99, True), (100, False), (101, False)):
            with self.subTest(failures=failures):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    self._write_boundary_run(root, failures)
                    result = evaluate(root)
                execution = result["gates"]["execution"]
                self.assertEqual(execution["entry_fill_rate"], 1.0)
                self.assertEqual(
                    execution["passed"], expected, execution["detail"]
                )

    def test_entry_failures_fail_execution_even_when_fill_rate_passes(self) -> None:
        # Fill rate counts entries / allowed signals; a run can fill 80 %+ of
        # allowed cards yet lose another 10 % to delayed or unfilled quotes.
        # The documented gate caps those failures at 10 %, so this must fail.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_run(root, trades=300, days=5, pnl=0.01)
            with (root / "skips.jsonl").open("w", encoding="utf-8") as handle:
                for _ in range(36):
                    handle.write(
                        json.dumps({"reason": "entry_delay_exceeded"}) + "\n"
                    )
                for _ in range(4):
                    handle.write(
                        json.dumps({"reason": "entry_quote_failed"}) + "\n"
                    )
            result = evaluate(root)
        execution = result["gates"]["execution"]
        self.assertGreaterEqual(execution["entry_fill_rate"], 0.80)
        self.assertGreaterEqual(execution["entry_failure_rate"], 0.10)
        self.assertFalse(execution["passed"])
        self.assertFalse(result["passed"])

    def test_entry_failures_under_the_cap_still_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_run(root, trades=300, days=5, pnl=0.01)
            with (root / "skips.jsonl").open("w", encoding="utf-8") as handle:
                for _ in range(29):
                    handle.write(
                        json.dumps({"reason": "entry_quote_failed"}) + "\n"
                    )
            result = evaluate(root)
        execution = result["gates"]["execution"]
        self.assertLess(execution["entry_failure_rate"], 0.10)
        self.assertTrue(execution["passed"])

    def _write_signal(
        self,
        handle: object,
        *,
        allowed: bool,
        message_type: str = "scanner_pool_launch",
        reasons: list[str] | None = None,
    ) -> None:
        handle.write(
            json.dumps(
                {
                    "signal": {"message_type": message_type},
                    "decision": {
                        "allowed": allowed,
                        "reasons": reasons or [],
                    },
                }
            )
            + "\n"
        )

    def test_censor_share_exposes_rejected_scanner_cards(self) -> None:
        # A 100 % fill rate only measures cards that reached the entry gate.
        # Cards rejected earlier (late delivery, missed print, timer lateness,
        # concurrency) leave both numerator and denominator, so the gate alone
        # cannot tell a selective bot from a lucky one. The execution report
        # must show the traded share of all scanner cards and the reasons.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_run(root, trades=300, days=5, pnl=0.01)
            with (root / "signals.jsonl").open("w", encoding="utf-8") as handle:
                for _ in range(300):
                    self._write_signal(handle, allowed=True)
                for _ in range(50):
                    self._write_signal(
                        handle,
                        allowed=False,
                        reasons=["delivery_lag_exceeded"],
                    )
                for _ in range(20):
                    self._write_signal(
                        handle,
                        allowed=False,
                        reasons=["missed_eligible_print"],
                    )
                # Non-scanner rows are excluded from the scanner population.
                for _ in range(5):
                    self._write_signal(
                        handle,
                        allowed=False,
                        message_type="other_channel",
                    )
            result = evaluate(root)
        execution = result["gates"]["execution"]
        self.assertEqual(execution["scanner_cards"], 370)
        self.assertEqual(execution["scanner_cards_allowed"], 300)
        self.assertEqual(execution["signals_allowed"], 300)
        self.assertAlmostEqual(execution["censor_share"], 70 / 370)
        self.assertEqual(
            execution["rejection_reason_counts"],
            {"delivery_lag_exceeded": 50, "missed_eligible_print": 20},
        )
        # Visibility only: the frozen gate verdict is unchanged.
        self.assertTrue(execution["passed"])

    def test_unparsable_signal_row_fails_input_integrity(self) -> None:
        # A torn line after a hard kill must not vanish: dropping a rejection
        # row would flatter the fill rate and hide the censor share.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_run(root, trades=300, days=5, pnl=0.01)
            with (root / "signals.jsonl").open("a", encoding="utf-8") as handle:
                handle.write('{"decision": {"allowed": tr\n')
            result = evaluate(root)
        integrity = result["gates"]["input_integrity"]
        self.assertFalse(integrity["passed"])
        self.assertEqual(integrity["corrupt_rows"]["signals"], 1)
        self.assertEqual(result["corrupt_rows"]["signals"], 1)
        self.assertFalse(result["passed"])

    def test_trade_missing_pnl_fails_instead_of_counting_as_zero(self) -> None:
        # ``row.get("pnl_sol") or 0.0`` used to turn a damaged row into a flat
        # trade, which is indistinguishable from a real break-even exit.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_run(root, trades=300, days=5, pnl=0.01)
            rows = [
                json.loads(line)
                for line in (root / "trades.jsonl").read_text().splitlines()
            ]
            del rows[0]["pnl_sol"]
            (root / "trades.jsonl").write_text(
                "\n".join(json.dumps(row) for row in rows) + "\n",
                encoding="utf-8",
            )
            result = evaluate(root)
        integrity = result["gates"]["input_integrity"]
        self.assertFalse(integrity["passed"])
        self.assertEqual(integrity["malformed_trades"], 1)
        self.assertEqual(result["malformed_trades"], 1)
        self.assertFalse(result["passed"])


if __name__ == "__main__":
    unittest.main()
