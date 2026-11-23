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
from forward_gates import cluster_bootstrap, evaluate  # noqa: E402
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
        self, root: Path, *, trades: int, days: int, pnl: float
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


if __name__ == "__main__":
    unittest.main()
