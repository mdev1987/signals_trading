from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import pandas as pd

from candidate_execution_audit import AUDIT_COLUMNS, build_audit, main
from execution_simulator import WSOL_MINT


def snapshot(
    timestamp: int,
    *,
    mint: str,
    pool_id: str,
    pool: str,
    tokens: float,
    quote: float,
    virtual_tokens: float | None = None,
) -> dict:
    event = {
        "timestamp": timestamp,
        "action": "create",
        "mint": mint,
        "poolId": pool_id,
        "pool": pool,
        "quoteMint": WSOL_MINT,
        "poolFeeRate": 0.01,
        "signature": f"sig-{pool_id}",
    }
    if pool == "pump":
        event["vTokensInBondingCurve"] = tokens
        event["vQuoteInBondingCurve"] = quote
    else:
        event["tokensInPool"] = tokens
        event["quoteInPool"] = quote
        if virtual_tokens is not None:
            event["virtualTokensInPool"] = virtual_tokens
    return event


def candidate_row(
    mint: str,
    *,
    pool_id: str,
    pool: str,
    buy: int,
    sell: int,
) -> dict:
    return {
        "mint": mint,
        "bar_start_timestamp_ms": 0,
        "bar_end_timestamp_ms": 2_000,
        "order_timestamp_ms": 2_000,
        "pool_id": pool_id,
        "pool": pool,
        "quote_mint": WSOL_MINT,
        "buy_candidate": buy,
        "sell_candidate": sell,
    }


def timeline_row(
    mint: str,
    *,
    pool_id: str,
    pool: str,
    reserve_model: str,
    execution_supported: int,
) -> dict:
    return {
        "mint": mint,
        "timestamp_ms": 0,
        "bar_end_timestamp_ms": 2_000,
        "available_at_timestamp_ms": 2_000,
        "pool_id": pool_id,
        "pool": pool,
        "quote_mint": WSOL_MINT,
        "reserve_model": reserve_model,
        "execution_supported": execution_supported,
        "pool_selection_status": "ready",
    }


class CandidateExecutionAuditTests(unittest.TestCase):
    def make_fixture(self, root: Path) -> tuple[Path, Path, Path]:
        events_path = root / "events.jsonl"
        events = [
            snapshot(1_000, mint="mint-a", pool_id="pool-a", pool="pump",
                     tokens=1_000.0, quote=100.0),
            snapshot(1_000, mint="mint-b", pool_id="pool-b", pool="pump-amm",
                     tokens=10.0, quote=10.0),
            snapshot(1_000, mint="mint-c", pool_id="pool-c", pool="pump-amm",
                     tokens=60.0, quote=10.0, virtual_tokens=-10.0),
        ]
        events_path.write_text(
            "\n".join(json.dumps(event) for event in events) + "\n",
            encoding="utf-8",
        )

        candidates_path = root / "candidates.csv"
        pd.DataFrame(
            [
                candidate_row("mint-a", pool_id="pool-a", pool="pump",
                              buy=1, sell=1),
                candidate_row("mint-b", pool_id="pool-b", pool="pump-amm",
                              buy=1, sell=1),
                candidate_row("mint-c", pool_id="pool-c", pool="pump-amm",
                              buy=0, sell=1),
            ]
        ).to_csv(candidates_path, index=False)

        timeline_path = root / "timeline.csv"
        pd.DataFrame(
            [
                timeline_row("mint-a", pool_id="pool-a", pool="pump",
                             reserve_model="bonding_curve",
                             execution_supported=1),
                timeline_row("mint-b", pool_id="pool-b", pool="pump-amm",
                             reserve_model="unknown", execution_supported=0),
                timeline_row("mint-c", pool_id="pool-c", pool="pump-amm",
                             reserve_model="amm_virtual_tokens",
                             execution_supported=1),
            ]
        ).to_csv(timeline_path, index=False)
        return events_path, candidates_path, timeline_path

    def test_audit_reconciles_fills_and_all_reject_reasons(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            events_path, candidates_path, timeline_path = self.make_fixture(root)
            summary = build_audit(
                candidates_path=candidates_path,
                timeline_path=timeline_path,
                events_path=events_path,
                output_dir=root / "audit",
                buy_amount_sol=0.0001,
                sell_amount_tokens=100.0,
            )

            buy = summary["buy"]
            self.assertEqual(buy["candidates"], 2)
            self.assertEqual(buy["filled"], 1)
            self.assertEqual(buy["execution_unsupported"], 1)
            self.assertEqual(buy["execution_supported_not_filled"], 0)
            self.assertTrue(buy["reconciliation"]["reconciled"])
            self.assertTrue(buy["routing_invariant"])

            sell = summary["sell"]
            self.assertEqual(sell["candidates"], 3)
            self.assertEqual(sell["filled"], 1)
            self.assertEqual(sell["execution_unsupported"], 1)
            self.assertEqual(sell["execution_supported_not_filled"], 1)
            self.assertEqual(
                sell["status_reason_counts"][
                    "insufficient_liquidity|"
                    "sell_size_is_not_smaller_than_token_reserve"
                ],
                1,
            )
            self.assertTrue(sell["reconciliation"]["reconciled"])
            self.assertTrue(sell["routing_invariant"])
            self.assertEqual(sell["reconciliation"]["accounted_total"], 3)

            audit = pd.read_csv(summary["audit_csv"])
            self.assertEqual(len(audit), 5)
            self.assertEqual(list(audit.columns), AUDIT_COLUMNS)

            short = pd.read_csv(summary["sell_insufficient_liquidity_csv"])
            self.assertEqual(len(short), 1)
            self.assertEqual(short.iloc[0]["mint"], "mint-c")

    def test_main_returns_zero_when_checks_pass(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            events_path, candidates_path, timeline_path = self.make_fixture(root)
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "--candidates",
                        str(candidates_path),
                        "--timeline",
                        str(timeline_path),
                        "--events",
                        str(events_path),
                        "--output-dir",
                        str(root / "audit"),
                    ]
                )

            self.assertEqual(exit_code, 0)
            summary = json.loads(stdout.getvalue())
            self.assertTrue(summary["checks_passed"])

    def test_main_returns_nonzero_when_routing_invariant_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            events_path, candidates_path, timeline_path = self.make_fixture(root)
            timeline = pd.read_csv(timeline_path)
            timeline.loc[
                timeline["mint"] == "mint-b", "execution_supported"
            ] = 1
            timeline.to_csv(timeline_path, index=False)

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "--candidates",
                        str(candidates_path),
                        "--timeline",
                        str(timeline_path),
                        "--events",
                        str(events_path),
                        "--output-dir",
                        str(root / "audit"),
                    ]
                )

            self.assertEqual(exit_code, 1)
            summary = json.loads(stdout.getvalue())
            self.assertTrue(summary["buy"]["reconciliation"]["reconciled"])
            self.assertFalse(summary["buy"]["routing_invariant"])
            self.assertFalse(summary["checks_passed"])

    def test_candidate_timeline_metadata_mismatch_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            events_path, candidates_path, timeline_path = self.make_fixture(root)
            timeline = pd.read_csv(timeline_path)
            timeline.loc[0, "quote_mint"] = "USDC"
            timeline.to_csv(timeline_path, index=False)

            with self.assertRaisesRegex(ValueError, "quote_mint"):
                build_audit(
                    candidates_path=candidates_path,
                    timeline_path=timeline_path,
                    events_path=events_path,
                    output_dir=root / "audit",
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
