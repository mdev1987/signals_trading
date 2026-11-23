"""Restart recovery (paper mode) backed by PickleDB.

These tests pin the safety properties that matter for a continuous forward
sample: a restart restores exactly the wallet/positions the process had, a
snapshot written for a different configuration is refused, and a corrupt or
non-finite snapshot never quietly turns into a fresh wallet.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parents[1]
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from paper_bot.broker import BrokerConfig, PaperBroker  # noqa: E402
from paper_bot.engine import EngineConfig, PaperEngine  # noqa: E402
from paper_bot.history import MarketHistory  # noqa: E402
from paper_bot.live import (  # noqa: E402
    LiveConfig,
    StrategyLane,
    _recovery_fingerprint,
    _state_saver,
)
from paper_bot.models import FillQuote, Position, Signal  # noqa: E402
from paper_bot.recovery import (  # noqa: E402
    RECOVERY_VERSION,
    RecoveryStore,
    default_state_db,
)
from paper_bot.signals import GateConfig  # noqa: E402
from paper_bot.store import PaperStore  # noqa: E402
from paper_bot.wallet import PaperWallet, WalletConfig  # noqa: E402

MINT = "2Z9UGHFZmKRwaYJ8r2ECn4uP6yey4H243Hszvt2Xpump"
ENTRY_TS = 1_800_000_000_000


def make_signal() -> Signal:
    return Signal(
        token_address=MINT,
        signal_ts_ms=ENTRY_TS - 5_000,
        received_ts_ms=ENTRY_TS - 5_000,
        channel="@test",
        message_id=7,
        network="solana",
        address_status="confirmed",
        address_type="mint",
        message_type="buy",
        is_buy_signal=True,
        is_confirmed_buy_signal=True,
        features={"strategy_tag": "A", "liquidity_usd": 50_000.0},
    )


def make_position(*, exit_in_flight: bool = False) -> Position:
    return Position(
        signal=make_signal(),
        mint=MINT,
        entry_ts_ms=ENTRY_TS,
        entry_price_sol=0.0000029,
        size=3_448_275.862,
        entry_value_sol=0.1,
        entry_fee_sol=0.00125,
        entry_impact=0.02,
        horizon_ts_ms=ENTRY_TS + 300_000,
        last_price_sol=0.0000031,
        last_ts_ms=ENTRY_TS + 60_000,
        entry_quote=FillQuote(
            price_sol=0.0000029,
            source="jupiter",
            in_amount=100_000_000,
            out_amount=3_448_275_862,
        ),
        reference_price_sol=0.0000029,
        reference_ts_ms=ENTRY_TS,
        reference_age_ms=0,
        reference_source="trade",
        sold_size=0.0,
        exit_in_flight=exit_in_flight,
    )


def make_engine(store: object | None = None, **overrides: object) -> PaperEngine:
    wallet = PaperWallet(WalletConfig(starting_sol=10.0))
    broker = PaperBroker(BrokerConfig(notional_sol=0.1), wallet=wallet)
    return PaperEngine(
        config=EngineConfig(
            entry_delay_ms=5_000,
            max_entry_delay_ms=60_000,
            live=True,
            **overrides,  # type: ignore[arg-type]
        ),
        broker=broker,
        quotes=object(),
        history=MarketHistory(),
        gate=GateConfig(rule="baseline"),
        store=store,
        clock=lambda: ENTRY_TS,
    )


def live_config(**overrides: object) -> LiveConfig:
    values: dict[str, object] = {
        "channels": ["@test"],
        "config_path": Path("config.ini"),
        "run_dir": Path("output/paper/run"),
    }
    values.update(overrides)
    return LiveConfig(**values)  # type: ignore[arg-type]


class RecoveryStoreTests(unittest.TestCase):
    def payload(self) -> dict[str, object]:
        return {
            "run_dir": "output/paper/live_20261123",
            "fingerprint": {"rule": "scanner_pumpfun_amm"},
            "wallet": {"sol_balance": 9.9},
            "token_balances": {MINT: 1234.0},
            "positions": [],
        }

    def test_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = RecoveryStore(Path(tmp) / "state.json")
            self.assertIsNone(store.load())
            store.save(self.payload())
            loaded = store.load()
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded["version"], RECOVERY_VERSION)
        self.assertEqual(loaded["wallet"], {"sol_balance": 9.9})
        self.assertEqual(loaded["token_balances"], {MINT: 1234.0})

    def test_save_replaces_previous_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = RecoveryStore(Path(tmp) / "nested" / "state.json")
            store.save(self.payload())
            second = self.payload()
            second["wallet"] = {"sol_balance": 8.5}
            store.save(second)
            loaded = store.load()
        assert loaded is not None
        self.assertEqual(loaded["wallet"], {"sol_balance": 8.5})

    def test_corrupt_json_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                RecoveryStore(path).load()

    def test_unsupported_version_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text(
                json.dumps({"state": {"version": 99, "run_dir": "x"}}),
                encoding="utf-8",
            )
            with self.assertRaises(RuntimeError):
                RecoveryStore(path).load()

    def test_non_object_payload_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text(json.dumps({"state": [1, 2, 3]}), encoding="utf-8")
            with self.assertRaises(RuntimeError):
                RecoveryStore(path).load()

    def test_missing_required_key_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            payload = self.payload()
            payload.pop("positions")
            path.write_text(
                json.dumps({"state": {"version": RECOVERY_VERSION, **payload}}),
                encoding="utf-8",
            )
            with self.assertRaises(RuntimeError):
                RecoveryStore(path).load()

    def test_default_state_db_is_lane_specific(self) -> None:
        first = default_state_db("scanner_pumpfun_amm", "trade")
        again = default_state_db("scanner_pumpfun_amm", "trade")
        other = default_state_db("scanner_pumpfun_amm", "timer")
        self.assertEqual(first, again)
        self.assertNotEqual(first, other)
        self.assertEqual(first.parent, Path("output/recovery"))
        self.assertEqual(first.name, "scanner_pumpfun_amm_trade.json")


class EngineStateTests(unittest.TestCase):
    def test_export_restore_round_trip(self) -> None:
        first = make_engine()
        first.broker.wallet.sol_balance = 9.9
        first.broker.wallet.token_balances = {MINT: 3_448_275.862}
        first.broker.wallet.entries = 1
        first.broker.wallet.exits = 0
        first.broker.wallet.fees_paid_sol = 0.00125
        first.positions[MINT] = make_position()

        payload = first.export_state()
        self.assertEqual(payload["wallet"]["sol_balance"], 9.9)
        self.assertEqual(len(payload["positions"]), 1)

        second = make_engine()
        second.restore_state(payload)

        self.assertAlmostEqual(second.broker.wallet.sol_balance, 9.9)
        self.assertAlmostEqual(
            second.broker.wallet.token_balances[MINT], 3_448_275.862
        )
        self.assertEqual(second.broker.wallet.entries, 1)
        self.assertAlmostEqual(second.broker.wallet.fees_paid_sol, 0.00125)
        restored = second.positions[MINT]
        self.assertEqual(restored.signal.token_address, MINT)
        self.assertEqual(restored.signal.features["strategy_tag"], "A")
        assert restored.entry_quote is not None
        self.assertEqual(restored.entry_quote.in_amount, 100_000_000)
        self.assertAlmostEqual(restored.entry_price_sol, 0.0000029)

    def test_restore_clears_stale_in_flight_flag(self) -> None:
        first = make_engine()
        first.positions[MINT] = make_position(exit_in_flight=True)
        payload = first.export_state()

        second = make_engine()
        second.restore_state(payload)
        self.assertFalse(second.positions[MINT].exit_in_flight)

    def test_restore_does_not_reclose_a_position_already_in_trades(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            store = PaperStore(run_dir)
            (run_dir / "trades.jsonl").write_text(
                json.dumps({"mint": MINT, "entry_ts_ms": ENTRY_TS}) + "\n",
                encoding="utf-8",
            )
            first = make_engine(store=store)
            first.positions[MINT] = make_position()
            payload = first.export_state()

            second = make_engine(store=PaperStore(run_dir))
            second.restore_state(payload)
        self.assertEqual(second.positions, {})

    def test_non_finite_position_value_fails_closed(self) -> None:
        first = make_engine()
        first.positions[MINT] = make_position()
        payload = first.export_state()
        payload["positions"][0]["size"] = float("nan")

        second = make_engine()
        with self.assertRaises(RuntimeError):
            second.restore_state(payload)

    def test_non_positive_entry_price_fails_closed(self) -> None:
        first = make_engine()
        first.positions[MINT] = make_position()
        payload = first.export_state()
        payload["positions"][0]["entry_price_sol"] = 0.0

        with self.assertRaises(RuntimeError):
            make_engine().restore_state(payload)

    def test_non_finite_wallet_balance_fails_closed(self) -> None:
        payload = {
            "wallet": {"sol_balance": float("inf")},
            "token_balances": {},
            "positions": [],
        }
        with self.assertRaises(RuntimeError):
            make_engine().restore_state(payload)

    def test_malformed_position_fails_closed(self) -> None:
        payload = {
            "wallet": {"sol_balance": 10.0},
            "token_balances": {},
            "positions": [{"mint": MINT}],
        }
        with self.assertRaises(RuntimeError):
            make_engine().restore_state(payload)

    def test_persist_state_is_noop_without_callback(self) -> None:
        engine = make_engine()
        engine.persist_state()  # must not raise

    def test_persist_state_invokes_callback(self) -> None:
        engine = make_engine()
        engine.positions[MINT] = make_position()
        seen: list[dict[str, object]] = []
        engine.on_state_change = seen.append
        engine.persist_state()
        self.assertEqual(len(seen), 1)
        self.assertEqual(len(seen[0]["positions"]), 1)


class StateSaverTests(unittest.TestCase):
    def test_saver_injects_run_dir_and_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = RecoveryStore(Path(tmp) / "state.json")
            config = live_config(state_db=store.path)
            lane = StrategyLane(
                name="A",
                rule="scanner_pumpfun_amm",
                entry_mode="trade",
                run_dir=Path("output/paper/live_20261123"),
                wallet=PaperWallet(),
                broker=object(),  # type: ignore[arg-type]
                store=object(),  # type: ignore[arg-type]
                engine=object(),  # type: ignore[arg-type]
                reporter=object(),  # type: ignore[arg-type]
                stats=object(),  # type: ignore[arg-type]
            )
            fingerprint = _recovery_fingerprint(
                config, "scanner_pumpfun_amm", "trade"
            )
            save = _state_saver(store, lane, fingerprint)
            save(
                {
                    "wallet": {"sol_balance": 9.5},
                    "token_balances": {},
                    "positions": [],
                }
            )
            payload = store.load()
        assert payload is not None
        self.assertEqual(payload["run_dir"], "output/paper/live_20261123")
        self.assertEqual(payload["fingerprint"], fingerprint)

    def test_fingerprint_tracks_execution_convention(self) -> None:
        base = _recovery_fingerprint(
            live_config(), "scanner_pumpfun_amm", "trade"
        )
        changed = _recovery_fingerprint(
            live_config(notional_sol=0.2), "scanner_pumpfun_amm", "trade"
        )
        other_mode = _recovery_fingerprint(
            live_config(), "scanner_pumpfun_amm", "timer"
        )
        self.assertNotEqual(base, changed)
        self.assertNotEqual(base, other_mode)
        self.assertEqual(
            base, _recovery_fingerprint(live_config(), "scanner_pumpfun_amm", "trade")
        )


class RecoveryConfigTests(unittest.TestCase):
    def test_live_mode_rejects_recovery(self) -> None:
        with self.assertRaises(ValueError):
            live_config(mode="live", state_db=Path("output/recovery/x.json"))

    def test_ab_run_rejects_recovery(self) -> None:
        with self.assertRaises(ValueError):
            live_config(
                ab_rule="scanner_pumpfun_amm",
                ab_entry_mode="timer",
                state_db=Path("output/recovery/x.json"),
            )

    def test_resume_requires_a_state_path(self) -> None:
        with self.assertRaises(ValueError):
            live_config(resume=True)

    def test_paper_single_lane_accepts_resume(self) -> None:
        config = live_config(
            resume=True, state_db=Path("output/recovery/x.json")
        )
        self.assertTrue(config.resume)
        self.assertEqual(config.state_db, Path("output/recovery/x.json"))


class ResumeStartupTests(unittest.IsolatedAsyncioTestCase):
    """The runner must refuse an unusable snapshot before touching the network."""

    async def _run(self, config: LiveConfig) -> None:
        import os

        from unittest import mock

        from paper_bot.live import run_live

        with mock.patch.dict(
            os.environ, {"JUPITER_API_KEY": "test-key"}, clear=False
        ):
            await run_live(config)

    async def test_resume_without_saved_state_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = live_config(
                resume=True,
                state_db=Path(tmp) / "missing.json",
                telegram=False,
                max_seconds=1,
            )
            with self.assertRaises(RuntimeError):
                await self._run(config)

    async def test_resume_with_a_different_fingerprint_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            config = live_config(
                resume=True,
                state_db=path,
                telegram=False,
                max_seconds=1,
            )
            RecoveryStore(path).save(
                {
                    "run_dir": str(Path(tmp) / "run"),
                    "fingerprint": {"rule": "something_else"},
                    "wallet": {"sol_balance": 10.0},
                    "token_balances": {},
                    "positions": [],
                }
            )
            with self.assertRaises(RuntimeError) as caught:
                await self._run(config)
            self.assertIn("different configuration", str(caught.exception))


class ResumedRunSummaryTests(unittest.TestCase):
    def test_finalize_includes_trades_from_the_previous_process(self) -> None:
        import polars as pl

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            store = PaperStore(run_dir)
            store.record_trade_payload(
                {
                    "mint": MINT,
                    "entry_ts_ms": ENTRY_TS,
                    "exit_ts_ms": ENTRY_TS + 300_000,
                    "pnl_sol": 0.5,
                    "return_pct": 0.05,
                }
            )
            # A new process opens the same run directory (resume) and closes
            # one more trade. Both must appear in the final parquet.
            resumed = PaperStore(run_dir)
            resumed.record_trade_payload(
                {
                    "mint": "other-mint",
                    "entry_ts_ms": ENTRY_TS + 1_000,
                    "exit_ts_ms": ENTRY_TS + 301_000,
                    "pnl_sol": -0.1,
                    "return_pct": -0.01,
                }
            )
            parquet = resumed.finalize()
            assert parquet is not None
            frame = pl.read_parquet(parquet)

        self.assertEqual(frame.height, 2)


if __name__ == "__main__":
    unittest.main()
