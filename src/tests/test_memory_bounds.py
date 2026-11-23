"""Bounded in-memory state: a long forward run must not grow with messages.

The JSONL files are the record of truth. These tests pin that the process only
keeps a bounded tail plus a cumulative counter, so RSS stops rising once the
cap is reached instead of climbing for the whole run.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

SRC_DIR = Path(__file__).resolve().parents[1]
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import paper_bot.engine as engine_module  # noqa: E402
from paper_bot.broker import BrokerConfig, PaperBroker  # noqa: E402
from paper_bot.engine import EngineConfig, PaperEngine  # noqa: E402
from paper_bot.history import MarketHistory  # noqa: E402
from paper_bot.models import Signal  # noqa: E402
from paper_bot.quotes import MAX_DECIMALS_CACHE, TokenMetadataResolver  # noqa: E402
from paper_bot.signals import GateConfig  # noqa: E402
from paper_bot.wallet import PaperWallet, WalletConfig  # noqa: E402

MINT = "So11111111111111111111111111111111111111112"
NOW_MS = 1_800_000_000_000


def make_engine() -> PaperEngine:
    wallet = PaperWallet(WalletConfig(starting_sol=10.0))
    broker = PaperBroker(BrokerConfig(notional_sol=0.1), wallet=wallet)
    return PaperEngine(
        config=EngineConfig(
            entry_delay_ms=5_000,
            max_entry_delay_ms=60_000,
            live=True,
        ),
        broker=broker,
        quotes=object(),
        history=MarketHistory(),
        gate=GateConfig(rule="baseline"),
        clock=lambda: NOW_MS,
    )


def future_signal() -> Signal:
    # Dated in the future, so intake rejects it without scheduling a timer but
    # still records the decision in signal_log.
    return Signal(
        token_address=MINT,
        signal_ts_ms=NOW_MS + 60_000,
        received_ts_ms=NOW_MS,
        channel="@test",
        message_id=1,
        network="solana",
        address_status="confirmed",
        address_type="mint",
        message_type="buy",
        is_buy_signal=True,
        is_confirmed_buy_signal=True,
        features={},
    )


class InMemoryTailBoundsTests(unittest.IsolatedAsyncioTestCase):
    def test_skip_tail_is_bounded_but_total_is_cumulative(self) -> None:
        with mock.patch.object(engine_module, "IN_MEMORY_SKIPS", 3):
            engine = make_engine()
        for index in range(10):
            engine._record_skip(
                mint=MINT,
                signal_ts_ms=0,
                ts_ms=index,
                reason="test",
            )
        self.assertEqual(len(engine.skips), 3)
        self.assertEqual(engine.skip_total, 10)
        # The retained tail holds the most recent records, which is what the
        # live debug paths (`skips[-1]`) read.
        self.assertEqual([skip.ts_ms for skip in engine.skips], [7, 8, 9])

    async def test_signal_tail_is_bounded_but_total_is_cumulative(self) -> None:
        with mock.patch.object(engine_module, "IN_MEMORY_SIGNALS", 4):
            engine = make_engine()
        for _ in range(10):
            decision = await engine.on_signal(future_signal())
            self.assertFalse(decision.allowed)
        self.assertEqual(len(engine.signal_log), 4)
        self.assertEqual(engine.signal_total, 10)
        self.assertEqual(
            engine.signal_log[-1][1].reasons, ["signal_timestamp_in_future"]
        )

    def test_closed_tail_has_a_finite_cap(self) -> None:
        with mock.patch.object(engine_module, "IN_MEMORY_CLOSED", 2):
            engine = make_engine()
        self.assertEqual(engine.closed.maxlen, 2)
        self.assertEqual(engine.closed_total, 0)


class DecimalsCacheBoundsTests(unittest.IsolatedAsyncioTestCase):
    async def test_decimal_cache_evicts_oldest_entries(self) -> None:
        with mock.patch("paper_bot.quotes.MAX_DECIMALS_CACHE", 2):
            resolver = TokenMetadataResolver(api_key=None)
            try:
                await resolver.decimals("mint-a", event_decimals=6)
                await resolver.decimals("mint-b", event_decimals=9)
                # Touch "a" so it becomes the most recently used entry.
                await resolver.decimals("mint-a")
                await resolver.decimals("mint-c", event_decimals=6)
            finally:
                await resolver.close()
        self.assertEqual(list(resolver._cache), ["mint-a", "mint-c"])
        self.assertLessEqual(len(resolver._cache), MAX_DECIMALS_CACHE)


if __name__ == "__main__":
    unittest.main()
