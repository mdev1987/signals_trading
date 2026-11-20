from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from tools.backtest.backtest_strategy import run_backtest

WSOL = "So11111111111111111111111111111111111111112"


def pump_event(
    timestamp: int,
    *,
    pool: str = "pump",
    pool_id: str = "pool-1",
    mint: str = "mint-1",
    token_reserve: float = 1_000.0,
    quote_reserve: float = 100.0,
    fee_rate: float = 0.01,
) -> dict:
    result = {
        "timestamp": timestamp,
        "action": "buy",
        "mint": mint,
        "poolId": pool_id,
        "pool": pool,
        "quoteMint": WSOL,
        "poolFeeRate": fee_rate,
        "signature": f"sig-{timestamp}",
    }
    if pool in ("pump", "raydium-launchpad"):
        result["vTokensInBondingCurve"] = token_reserve
        result["vQuoteInBondingCurve"] = quote_reserve
    else:
        result["tokensInPool"] = token_reserve
        result["quoteInPool"] = quote_reserve
        result["virtualQuoteInPool"] = 0.0
    return result


class BacktestStrategyTests(unittest.TestCase):
    def make_inputs(
        self,
        events: list[dict],
        signals: list[dict],
        *,
        routing: list[dict] | None = None,
    ) -> tuple[Path, Path, Path | None, tempfile.TemporaryDirectory]:
        temp = tempfile.TemporaryDirectory()
        root = Path(temp.name)
        events_path = root / "events.jsonl"
        events_path.write_text(
            "\n".join(json.dumps(item) for item in events) + "\n", encoding="utf-8"
        )
        signals_path = root / "signals.csv"
        with signals_path.open("w", newline="", encoding="utf-8") as target:
            writer = csv.DictWriter(
                target, fieldnames=["signal_id", "signal_timestamp_ms", "mint"]
            )
            writer.writeheader()
            writer.writerows(signals)
        routing_path = None
        if routing is not None:
            routing_path = root / "routing.csv"
            with routing_path.open("w", newline="", encoding="utf-8") as target:
                writer = csv.DictWriter(
                    target,
                    fieldnames=[
                        "signal_id",
                        "signal_timestamp_utc",
                        "mint",
                        "dataset_routable",
                        "reason",
                    ],
                )
                writer.writeheader()
                writer.writerows(routing)
        return events_path, signals_path, routing_path, temp

    def read_csv(self, path: Path) -> list[dict]:
        with path.open(newline="", encoding="utf-8") as source:
            return list(csv.DictReader(source))

    def test_round_trip_position_is_closed_and_reports_pnl(self) -> None:
        events_path, signals_path, _, temp = self.make_inputs(
            [pump_event(0)],
            [{"signal_id": "s1", "signal_timestamp_ms": 1_000, "mint": "mint-1"}],
        )
        self.addCleanup(temp.cleanup)

        report = run_backtest(
            events_path,
            signals_path,
            Path(temp.name) / "run",
            latency_ms=0,
            hold_ms=30_000,
        )

        self.assertEqual(report["positions"]["closed"], 1)
        self.assertEqual(report["positions"]["open_at_end"], 0)
        trades = self.read_csv(Path(report["outputs"]["trades"]))
        self.assertEqual(trades[0]["status"], "closed")
        self.assertEqual(trades[0]["entry_pool"], "pump")
        self.assertLess(float(trades[0]["realized_pnl_sol"]), 0.0)
        self.assertAlmostEqual(
            report["pnl"]["realized_pnl_sol"],
            float(trades[0]["realized_pnl_sol"]),
        )
        self.assertAlmostEqual(
            report["pnl"]["cash_end_sol"],
            report["pnl"]["cash_start_sol"] + report["pnl"]["realized_pnl_sol"],
        )

    def test_open_position_is_marked_to_market_not_force_closed(self) -> None:
        events_path, signals_path, _, temp = self.make_inputs(
            [pump_event(0), pump_event(50_000, quote_reserve=200.0)],
            [{"signal_id": "s1", "signal_timestamp_ms": 10_000, "mint": "mint-1"}],
        )
        self.addCleanup(temp.cleanup)

        report = run_backtest(
            events_path,
            signals_path,
            Path(temp.name) / "run",
            latency_ms=0,
            hold_ms=30 * 60 * 1000,
        )

        self.assertEqual(report["positions"]["closed"], 0)
        self.assertEqual(report["positions"]["open_at_end"], 1)
        self.assertEqual(report["fills_by_status"], {"filled": 1})
        trades = self.read_csv(Path(report["outputs"]["trades"]))
        self.assertEqual(trades[0]["status"], "open_at_end")
        self.assertGreater(float(trades[0]["mark_price_sol"]), 0.0)
        self.assertGreater(float(trades[0]["mark_value_sol"]), 0.0)
        self.assertAlmostEqual(
            report["pnl"]["cash_end_sol"], report["pnl"]["cash_start_sol"] - 0.1
        )
        self.assertAlmostEqual(
            report["pnl"]["equity_end_sol"],
            report["pnl"]["cash_end_sol"] + report["pnl"]["open_value_sol"],
        )

    def test_drained_pool_does_not_inflate_the_final_mark(self) -> None:
        drained = pump_event(5_000, token_reserve=1e-9, quote_reserve=1e-9)
        drained["action"] = "remove"
        events_path, signals_path, _, temp = self.make_inputs(
            [pump_event(0), drained],
            [{"signal_id": "s1", "signal_timestamp_ms": 1_000, "mint": "mint-1"}],
        )
        self.addCleanup(temp.cleanup)

        report = run_backtest(
            events_path,
            signals_path,
            Path(temp.name) / "run",
            latency_ms=0,
            hold_ms=30_000,
        )

        trades = self.read_csv(Path(report["outputs"]["trades"]))
        self.assertEqual(trades[0]["status"], "open_at_end")
        self.assertAlmostEqual(float(trades[0]["mark_price_sol"]), 0.1, places=2)
        self.assertLess(float(trades[0]["mark_value_sol"]), 0.2)
        self.assertLess(report["pnl"]["equity_end_sol"], 10.2)

    def test_exit_re_resolves_to_the_migrated_pool(self) -> None:
        events = [
            pump_event(0, pool="pump", pool_id="curve", quote_reserve=50.0),
            pump_event(20_000, pool="pump-amm", pool_id="amm", quote_reserve=500.0),
        ]
        events_path, signals_path, _, temp = self.make_inputs(
            events,
            [{"signal_id": "s1", "signal_timestamp_ms": 10_000, "mint": "mint-1"}],
        )
        self.addCleanup(temp.cleanup)

        report = run_backtest(
            events_path,
            signals_path,
            Path(temp.name) / "run",
            latency_ms=0,
            hold_ms=30_000,
        )

        trades = self.read_csv(Path(report["outputs"]["trades"]))
        self.assertEqual(trades[0]["entry_pool_id"], "curve")
        self.assertEqual(trades[0]["exit_pool_id"], "amm")
        self.assertEqual(report["pools"]["exits_by_pool"], {"pump-amm": 1})

    def test_concurrency_cap_skips_signals(self) -> None:
        events_path, signals_path, _, temp = self.make_inputs(
            [pump_event(0)],
            [
                {"signal_id": "s1", "signal_timestamp_ms": 1_000, "mint": "mint-1"},
                {"signal_id": "s2", "signal_timestamp_ms": 2_000, "mint": "mint-1"},
            ],
        )
        self.addCleanup(temp.cleanup)

        report = run_backtest(
            events_path,
            signals_path,
            Path(temp.name) / "run",
            latency_ms=0,
            hold_ms=30 * 60 * 1000,
            max_concurrent=1,
        )

        self.assertEqual(report["positions"]["open_at_end"], 1)
        self.assertEqual(
            report["skipped_by_reason"], {"max_concurrent_positions": 1}
        )

    def test_cash_cap_skips_signals(self) -> None:
        events_path, signals_path, _, temp = self.make_inputs(
            [pump_event(0)],
            [
                {"signal_id": "s1", "signal_timestamp_ms": 1_000, "mint": "mint-1"},
                {"signal_id": "s2", "signal_timestamp_ms": 2_000, "mint": "mint-1"},
            ],
        )
        self.addCleanup(temp.cleanup)

        report = run_backtest(
            events_path,
            signals_path,
            Path(temp.name) / "run",
            cash_sol=0.15,
            stake_sol=0.1,
            latency_ms=0,
            hold_ms=30 * 60 * 1000,
        )

        self.assertEqual(report["skipped_by_reason"], {"insufficient_cash": 1})

    def test_routing_excludes_unroutable_signals_with_the_reason(self) -> None:
        events_path, signals_path, routing_path, temp = self.make_inputs(
            [pump_event(0)],
            [
                {"signal_id": "s1", "signal_timestamp_ms": 1_000, "mint": "mint-1"},
                {"signal_id": "s2", "signal_timestamp_ms": 2_000, "mint": "mint-2"},
            ],
            routing=[
                {
                    "signal_id": "s2",
                    "signal_timestamp_utc": "",
                    "mint": "mint-2",
                    "dataset_routable": 0,
                    "reason": "no_replay_events",
                }
            ],
        )
        self.addCleanup(temp.cleanup)

        report = run_backtest(
            events_path,
            signals_path,
            Path(temp.name) / "run",
            routing_path=routing_path,
            latency_ms=0,
            hold_ms=30 * 60 * 1000,
        )

        self.assertEqual(report["signals"]["routable"], 1)
        self.assertEqual(report["skipped_by_reason"], {"dataset_not_routable": 1})
        skipped = self.read_csv(Path(report["outputs"]["skipped"]))
        self.assertEqual(skipped[0]["detail"], "no_replay_events")

    def test_signal_without_a_fresh_pool_is_skipped(self) -> None:
        events_path, signals_path, _, temp = self.make_inputs(
            [pump_event(0)],
            [{"signal_id": "s1", "signal_timestamp_ms": 500_000, "mint": "mint-1"}],
        )
        self.addCleanup(temp.cleanup)

        report = run_backtest(
            events_path,
            signals_path,
            Path(temp.name) / "run",
            latency_ms=0,
            hold_ms=30_000,
        )

        self.assertEqual(report["skipped_by_reason"], {"no_fresh_pool": 1})
        self.assertEqual(report["orders"]["entries_filled"], 0)

    def test_report_and_outputs_are_written(self) -> None:
        events_path, signals_path, _, temp = self.make_inputs(
            [pump_event(0)],
            [{"signal_id": "s1", "signal_timestamp_ms": 1_000, "mint": "mint-1"}],
        )
        self.addCleanup(temp.cleanup)
        out_dir = Path(temp.name) / "run"

        report = run_backtest(
            events_path,
            signals_path,
            out_dir,
            latency_ms=0,
            hold_ms=30_000,
        )

        for name in ("trades.csv", "skipped.csv", "equity_curve.csv", "fills.csv"):
            self.assertTrue((out_dir / name).exists(), name)
        written = json.loads((out_dir / "run_report.json").read_text())
        self.assertEqual(written["stage"], "strategy_backtest")
        self.assertEqual(written["config"]["hold_minutes"], 0.5)
        self.assertEqual(report["config"]["stake_sol"], 0.1)
        self.assertGreaterEqual(len(self.read_csv(out_dir / "equity_curve.csv")), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
