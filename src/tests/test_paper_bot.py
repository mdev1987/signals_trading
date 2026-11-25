"""Focused tests for the Telegram live/paper bot.

The research-facing modules are covered elsewhere; these tests pin the pieces
that decide whether real SOL can move: the deployed rule, the causal history
features, the live-execution guards, and the paper/live report wording.
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
import tempfile
import unittest
from collections import OrderedDict
from pathlib import Path

import polars as pl

SRC_DIR = Path(__file__).resolve().parents[1]
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from paper_bot.broker import BrokerConfig, PaperBroker  # noqa: E402
from paper_bot.engine import (  # noqa: E402
    EngineConfig,
    PaperEngine,
    entry_backoff_active,
    preflight_exit_value_ratio,
)
from paper_bot.wallet import PaperWallet, WalletConfig  # noqa: E402
from paper_bot.execution import (  # noqa: E402
    LiveExecutionConfig,
    LiveExecutionError,
    LiveQuoteProvider,
    LiveSolanaExecutor,
    jupiter_error_reason,
    live_config_from_env,
)
from paper_bot.feeds import (  # noqa: E402
    PumpApiFeed,
    _best_sol_marks,
    parse_pump_event,
)
from paper_bot.history import MarketHistory  # noqa: E402
from paper_bot.models import (  # noqa: E402
    FillQuote,
    PendingEntry,
    Position,
    Signal,
    TradeEvent,
    canonical_quote_notional_sol,
)
from paper_bot.notifier import (  # noqa: E402
    ReporterConfig,
    ReporterStats,
    TelegramReporter,
    _signal_evidence,
    format_entry,
    format_startup,
    format_status,
)
from paper_bot.live import _claim_scanner_card  # noqa: E402
from paper_bot.recovery import RecoveryStore  # noqa: E402
from paper_bot.quotes import (  # noqa: E402
    QuoteError,
    JupiterQuoteProvider,
    TokenMetadataResolver,
    is_terminal_exit_error,
    is_transient_quote_error,
    normalize_price_impact_pct,
    slippage_ladder,
)
from paper_bot.signal_parser import (  # noqa: E402
    parse_scanner_card,
    parse_signal_message,
)
from paper_bot.store import PaperStore  # noqa: E402
from paper_bot.signals import (  # noqa: E402
    PAPER_ONLY_RULES,
    SCANNER_QUOTE_GUARD_RULE,
    SCANNER_RULE,
    SCANNER_TP50_RULE,
    GateConfig,
    evaluate_gate,
    is_scanner_rule,
    parse_signal_text,
)

MINT = "So11111111111111111111111111111111111111112"
SIGNAL_TS = 1_800_000_000_000

# Throwaway base58 keypair used only to exercise request building. The tests
# never broadcast a transaction.
TEST_PRIVATE_KEY = (
    "3ccLLgBmuNJBpjuVgCt9SNTPN3TTqh3EEPhbuqj3eEDg4NppbFKxZmqmbmGr9CZtsrs4"
    "ojpfKEPtYqrjBhQC9LuA"
)


def signal(features: dict[str, float | None]) -> Signal:
    return Signal(
        token_address=MINT,
        signal_ts_ms=SIGNAL_TS,
        received_ts_ms=SIGNAL_TS,
        network="solana",
        address_status="confirmed",
        message_type="buy",
        is_buy_signal=True,
        is_confirmed_buy_signal=True,
        features=dict(features),
    )


class AlertParsingTests(unittest.TestCase):
    def test_total_buy_is_not_double_counted_as_inflow(self) -> None:
        message = (
            "\U0001f4b9 Max Pump: 3.5x\n"
            "\U0001f4b0 4 KOL Wallet Buy\n"
            "\U0001f911 Current MC: 12.5K\n"
            "\U0001f4b8 Total Buy 9.5 SOL\n"
            "\U0001f6d2 Inflow\n"
            "Buy 4 SOL\n"
            "Buy 3 SOL\n"
            "Buy 2.5 SOL\n"
        )
        features = parse_signal_message(message)
        self.assertEqual(features["total_buy_sol"], 9.5)
        self.assertEqual(features["inflow_n"], 3)
        self.assertEqual(features["inflow_sum"], 9.5)


SCANNER_CARD = """\U0001f4a0 New Solana Pool Launched \U0001f4a0

Token: BERNIE
CA: 2Z9UGHFZmKRwaYJ8r2ECn4uP6yey4H243Hszvt2Xpump
LP: DbnZrPS8tD8jWgLCZWewBTXaujk3mKHGS86masa8cCS7

Init Price: $0.0{4}293
MCap: $29.03K
Pair: 210.20M BERNIE / 66.12 SOL
Dex: Pumpfunamm
Liquidity: $10.95K
Insiders: 2(Holdings 0%)
SNIPES: 15  RUSHERS: 0
Token Holders: 616
    |_4ALRKjQWdiXtfxjwTGKGXsmLCxf1Ro9eqESGrx3LM4Ah 0%
    |_6usR4bEbXTSwzjmPqrm41SAGicpGuJ8r2zAzmfrkAmB2 0.8406%
Security: Score: 0(\U0001f7e2Low Risk)
"""


class ScannerCardTests(unittest.TestCase):
    def test_card_fields_are_parsed(self) -> None:
        card = parse_scanner_card(SCANNER_CARD)
        assert card is not None
        self.assertEqual(
            card["token_address"], "2Z9UGHFZmKRwaYJ8r2ECn4uP6yey4H243Hszvt2Xpump"
        )
        self.assertEqual(card["dex"], "Pumpfunamm")
        self.assertEqual(card["liquidity_usd"], 10_950.0)
        self.assertEqual(card["mcap_usd"], 29_030.0)
        self.assertEqual(card["symbol"], "BERNIE")

    def test_non_scanner_message_is_not_a_card(self) -> None:
        self.assertIsNone(parse_scanner_card("BUY SIGNAL $WIF\nCA: " + MINT))

    def test_signal_uses_the_card_ca_not_a_security_row(self) -> None:
        result = parse_signal_text(
            SCANNER_CARD,
            signal_ts_ms=SIGNAL_TS,
            received_ts_ms=SIGNAL_TS + 2_000,
        )
        self.assertEqual(
            result.token_address, "2Z9UGHFZmKRwaYJ8r2ECn4uP6yey4H243Hszvt2Xpump"
        )
        self.assertTrue(result.is_buy_signal)
        self.assertEqual(result.address_status, "confirmed")
        self.assertEqual(result.message_type, "scanner_pool_launch")
        self.assertEqual(result.features["liquidity_usd"], 10_950.0)
        self.assertEqual(result.features["delivery_lag_ms"], 2_000)

    def test_scanner_rule_accepts_only_the_priced_band(self) -> None:
        def allowed(**features: object) -> bool:
            candidate = signal(features)
            candidate.message_type = "scanner_pool_launch"
            return evaluate_gate(candidate, GateConfig(rule=SCANNER_RULE)).allowed

        self.assertTrue(allowed(dex="Pumpfunamm", liquidity_usd=20_000.0))
        self.assertTrue(allowed(dex="Pumpfunamm", liquidity_usd=99_999.0))
        self.assertFalse(allowed(dex="Pumpfunamm", liquidity_usd=19_999.0))
        self.assertFalse(allowed(dex="Pumpfunamm", liquidity_usd=100_000.0))
        self.assertFalse(allowed(dex="Raydiumclmm", liquidity_usd=50_000.0))
        self.assertFalse(allowed(dex="Pumpfunamm", liquidity_usd=None))

    def test_tp50_variant_uses_the_identical_scanner_entry(self) -> None:
        def allowed(rule: str, **features: object) -> bool:
            candidate = signal(features)
            candidate.message_type = "scanner_pool_launch"
            return evaluate_gate(candidate, GateConfig(rule=rule)).allowed

        for features in (
            {"dex": "Pumpfunamm", "liquidity_usd": 20_000.0},
            {"dex": "Pumpfunamm", "liquidity_usd": 99_999.0},
            {"dex": "Pumpfunamm", "liquidity_usd": 19_999.0},
            {"dex": "Pumpfunamm", "liquidity_usd": 100_000.0},
            {"dex": "Raydiumclmm", "liquidity_usd": 50_000.0},
            {"dex": "Pumpfunamm", "liquidity_usd": None},
        ):
            self.assertEqual(
                allowed(SCANNER_RULE, **features),
                allowed(SCANNER_TP50_RULE, **features),
            )
            self.assertEqual(
                allowed(SCANNER_RULE, **features),
                allowed(SCANNER_QUOTE_GUARD_RULE, **features),
            )

    def test_all_scanner_lanes_share_first_card_only_handling(self) -> None:
        self.assertTrue(is_scanner_rule(SCANNER_RULE))
        self.assertTrue(is_scanner_rule(SCANNER_TP50_RULE))
        self.assertTrue(is_scanner_rule(SCANNER_QUOTE_GUARD_RULE))
        self.assertFalse(is_scanner_rule("baseline"))

    def test_dex_match_ignores_case_and_padding_in_both_paths(self) -> None:
        # The live card and the replay CSV go through the same normalizer, so
        # "  pumpfunamm " must not mean a different venue than "Pumpfunamm".
        from paper_bot.signals import normalize_dex

        self.assertEqual(normalize_dex("  pumpfunamm "), normalize_dex("Pumpfunamm"))

        def allowed(dex: object) -> bool:
            candidate = signal({"dex": dex, "liquidity_usd": 50_000.0})
            candidate.message_type = "scanner_pool_launch"
            return evaluate_gate(candidate, GateConfig(rule=SCANNER_RULE)).allowed

        self.assertTrue(allowed("Pumpfunamm"))
        self.assertTrue(allowed("  pumpfunamm "))
        self.assertFalse(allowed("pumpfun-amm"))

    def test_market_cap_and_symbol_are_mapped_for_the_gate(self) -> None:
        result = parse_signal_text(
            SCANNER_CARD,
            signal_ts_ms=SIGNAL_TS,
            received_ts_ms=SIGNAL_TS,
        )
        self.assertEqual(result.features["mc_usd"], 29_030.0)
        self.assertEqual(result.features["mcap_usd"], 29_030.0)
        self.assertEqual(result.features["symbol"], "BERNIE")
        # A market-cap bound now actually reads the card's cap instead of
        # rejecting every scanner signal for a missing ``mc_usd``.
        self.assertNotIn(
            "mc_below_min",
            evaluate_gate(
                result, GateConfig(rule=SCANNER_RULE, min_mc_usd=10_000.0)
            ).reasons,
        )
        self.assertIn(
            "mc_above_max",
            evaluate_gate(
                result, GateConfig(rule=SCANNER_RULE, max_mc_usd=10_000.0)
            ).reasons,
        )

    def test_card_liquidity_is_judged_against_the_rule(self) -> None:
        result = parse_signal_text(
            SCANNER_CARD,
            signal_ts_ms=SIGNAL_TS,
            received_ts_ms=SIGNAL_TS,
        )
        decision = evaluate_gate(result, GateConfig(rule=SCANNER_RULE))
        self.assertFalse(decision.allowed)
        self.assertIn("rule_liquidity_below_20000", decision.reasons)


class PumpApiFeedParsingTests(unittest.TestCase):
    """The live feed and the replay archive must agree on quote fields."""

    MINT = "FeedMint111111111111111111111111111111111111"
    WSOL = "So11111111111111111111111111111111111111112"
    USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

    def test_sol_quoted_event_is_preserved(self) -> None:
        event = parse_pump_event(
            json.dumps(
                {
                    "action": "buy",
                    "mint": self.MINT,
                    "timestamp": 1_800_000_000_000,
                    "priceSol": 0.02,
                    "tokenAmount": 100.0,
                    "quoteAmount": 2.0,
                    "quoteMint": self.WSOL,
                    "signature": "sig",
                }
            ),
            0,
        )
        assert event is not None
        self.assertEqual(event.mint, self.MINT)
        self.assertEqual(event.quote_mint, self.WSOL)
        self.assertEqual(event.quote_amount, 2.0)
        self.assertEqual(event.quote_price_sol, None)
        self.assertEqual(event.signature, "sig")

    def test_usdc_quote_fields_survive_the_live_parser(self) -> None:
        # A USDC-quoted print keeps the quote mint and the SOL conversion, so
        # the live entry filter can price it exactly as the replay does.
        event = parse_pump_event(
            json.dumps(
                {
                    "action": "buy",
                    "mint": self.MINT,
                    "timestamp": 1_800_000_000_000,
                    "priceSol": 0.013226,
                    "tokenAmount": 46.0,
                    "quoteAmount": 0.458,
                    "quoteMint": self.USDC,
                    "quotePriceSol": 0.013226,
                }
            ),
            3,
        )
        assert event is not None
        self.assertEqual(event.quote_mint, self.USDC)
        self.assertAlmostEqual(event.quote_amount or 0.0, 0.458)
        self.assertAlmostEqual(event.quote_price_sol or 0.0, 0.013226)
        self.assertEqual(event.event_index, 3)
        self.assertAlmostEqual(
            canonical_quote_notional_sol(
                quote_amount=event.quote_amount,
                quote_mint=event.quote_mint,
                quote_price_sol=event.quote_price_sol,
                token_amount=event.token_amount,
                price_sol=event.price_sol,
            )
            or 0.0,
            0.458 * 0.013226,
        )

    def test_non_trade_and_invalid_events_are_dropped(self) -> None:
        for payload in (
            '{"action":"transfer","mint":"x","timestamp":1,"priceSol":1}',
            '{"action":"buy","mint":"m","timestamp":1,"priceSol":0}',
            '{"action":"buy","mint":"m","timestamp":1}',
            "not json",
            "[]",
        ):
            self.assertIsNone(parse_pump_event(payload, 0), payload)

    def test_deepest_pair_keeps_liquidity_with_the_price(self) -> None:
        marks = _best_sol_marks(
            [
                {
                    "baseToken": {"address": "mintA"},
                    "quoteToken": {"address": self.WSOL, "symbol": "SOL"},
                    "priceNative": "0.001",
                    "liquidity": {"usd": 30_000},
                },
                {
                    "baseToken": {"address": "mintA"},
                    "quoteToken": {"address": self.WSOL, "symbol": "SOL"},
                    "priceNative": "0.002",
                    "liquidity": {"usd": 90_000},
                },
                {
                    "baseToken": {"address": "mintB"},
                    "quoteToken": {"address": self.WSOL, "symbol": "SOL"},
                    "priceNative": "0.003",
                },
            ]
        )
        self.assertEqual(marks["mintA"], (0.002, 90_000.0))
        self.assertEqual(marks["mintB"], (0.003, 0.0))

    def test_watch_set_can_be_pruned(self) -> None:
        async def on_trade(event: object) -> None:
            return None

        feed = PumpApiFeed(on_trade)
        feed.watch("a")
        feed.watch("b")
        feed.unwatch("a")
        feed.unwatch("never-watched")
        self.assertEqual(feed.watched, {"b"})


class HotMoneyGateTests(unittest.TestCase):
    def gate(self, **features: float | None) -> bool:
        return evaluate_gate(signal(features), GateConfig()).allowed

    def test_passes_above_flow_and_buy_thresholds(self) -> None:
        self.assertTrue(
            self.gate(
                hist_flow_1m_sol=9.47,
                total_buy_sol=3.01,
            )
        )

    def test_rejects_each_threshold_at_the_boundary(self) -> None:
        base = dict(
            hist_flow_1m_sol=20.0,
            total_buy_sol=10.0,
        )
        for key, value in (
            ("hist_flow_1m_sol", 9.456),
            ("total_buy_sol", 3.0),
        ):
            with self.subTest(key=key):
                features = {**base, key: value}
                self.assertFalse(self.gate(**features))

    def test_rejects_late_in_the_run_buys_when_dip_feature_present(self) -> None:
        base = dict(hist_flow_1m_sol=20.0, total_buy_sol=10.0)
        for pre_ret, allowed in ((2.0, True), (2.1, False), (-0.05, True)):
            with self.subTest(pre_ret=pre_ret):
                self.assertEqual(
                    self.gate(**base, hist_pre_ret_15m=pre_ret), allowed
                )

    def test_cold_start_is_rejected_with_a_reason(self) -> None:
        decision = evaluate_gate(signal({}), GateConfig())
        self.assertFalse(decision.allowed)
        self.assertIn("missing_feature:hist_flow_1m_sol", decision.reasons)

    def test_passes_when_dip_feature_missing(self) -> None:
        # Cold start: no price history, gate still passes flow + buy.
        self.assertTrue(
            self.gate(hist_flow_1m_sol=20.0, total_buy_sol=10.0)
        )

    def test_rejects_crowded_pre_signal_signers(self) -> None:
        base = dict(hist_flow_1m_sol=20.0, total_buy_sol=10.0)
        self.assertTrue(self.gate(**base, hist_signers_5m=147))
        self.assertFalse(self.gate(**base, hist_signers_5m=148))

    def test_rejects_crowded_pre_signal_trades(self) -> None:
        base = dict(hist_flow_1m_sol=20.0, total_buy_sol=10.0)
        self.assertTrue(self.gate(**base, hist_trades_5m=611))
        self.assertFalse(self.gate(**base, hist_trades_5m=612))

    def test_passes_when_crowd_features_missing(self) -> None:
        self.assertTrue(
            self.gate(hist_flow_1m_sol=20.0, total_buy_sol=10.0)
        )

    def test_all_overlays_pass_together(self) -> None:
        self.assertTrue(
            self.gate(
                hist_flow_1m_sol=20.0,
                total_buy_sol=10.0,
                hist_pre_ret_15m=0.5,
                hist_signers_5m=10,
                hist_trades_5m=20,
            )
        )

    def test_any_overlay_failing_rejects(self) -> None:
        base = dict(hist_flow_1m_sol=20.0, total_buy_sol=10.0)
        bad = [
            dict(**base, hist_pre_ret_15m=5.0),
            dict(**base, hist_signers_5m=200),
            dict(**base, hist_trades_5m=700),
        ]
        for features in bad:
            with self.subTest(features=features):
                self.assertFalse(self.gate(**features))


class HistoryFeatureTests(unittest.TestCase):
    def test_flow_and_volume_ignore_trades_at_or_after_the_signal(self) -> None:
        history = MarketHistory(horizon_ms=3_600_000)
        # A +10 SOL buy inside the minute and a -2 SOL sell 10 s later.
        history.add(
            TradeEvent(
                mint=MINT,
                timestamp_ms=SIGNAL_TS - 30_000,
                event_index=1,
                action="buy",
                price_sol=1.0,
                token_amount=10.0,
            )
        )
        history.add(
            TradeEvent(
                mint=MINT,
                timestamp_ms=SIGNAL_TS - 20_000,
                event_index=2,
                action="sell",
                price_sol=1.0,
                token_amount=2.0,
            )
        )
        # At the signal and one second after it: must not enter either window.
        history.add(
            TradeEvent(
                mint=MINT,
                timestamp_ms=SIGNAL_TS,
                event_index=3,
                action="buy",
                price_sol=1.0,
                token_amount=100.0,
            )
        )
        history.add(
            TradeEvent(
                mint=MINT,
                timestamp_ms=SIGNAL_TS + 1_000,
                event_index=4,
                action="buy",
                price_sol=1.0,
                token_amount=100.0,
            )
        )
        stats = history.stats(MINT, SIGNAL_TS)
        self.assertAlmostEqual(stats["flow_1m_sol"], 8.0)
        self.assertAlmostEqual(stats["pre_vol_30m_sol"], 12.0)

    def test_pre_ret_15m_is_last_price_over_price_15m_before_signal(self) -> None:
        history = MarketHistory(horizon_ms=3_600_000)
        # Base price at/below the signal-15 min boundary.
        history.add(
            TradeEvent(
                mint=MINT,
                timestamp_ms=SIGNAL_TS - 1_080_000,
                event_index=1,
                action="buy",
                price_sol=1.0,
                token_amount=100.0,
            )
        )
        history.add(
            TradeEvent(
                mint=MINT,
                timestamp_ms=SIGNAL_TS - 950_000,
                event_index=2,
                action="buy",
                price_sol=1.05,
                token_amount=100.0,
            )
        )
        # An event 15+ minutes before the signal sets the base price.
        history.add(
            TradeEvent(
                mint=MINT,
                timestamp_ms=SIGNAL_TS - 300_000,
                event_index=3,
                action="buy",
                price_sol=1.05,
                token_amount=100.0,
            )
        )
        # The last price strictly before the signal; pushed up to show the
        # feature follows it rather than staying pinned at the boundary.
        history.add(
            TradeEvent(
                mint=MINT,
                timestamp_ms=SIGNAL_TS - 60_000,
                event_index=4,
                action="buy",
                price_sol=1.2,
                token_amount=100.0,
            )
        )
        stats = history.stats(MINT, SIGNAL_TS)
        self.assertAlmostEqual(stats["pre_ret_15m"], 1.2 / 1.05 - 1.0)

    def test_pre_ret_15m_is_none_when_no_price_15m_before_signal(self) -> None:
        history = MarketHistory(horizon_ms=3_600_000)
        # First observed trade is 10 minutes before the signal: there is no
        # price at/below the boundary, so the proxy stays None.
        history.add(
            TradeEvent(
                mint=MINT,
                timestamp_ms=SIGNAL_TS - 600_000,
                event_index=1,
                action="buy",
                price_sol=1.0,
                token_amount=100.0,
            )
        )
        stats = history.stats(MINT, SIGNAL_TS)
        self.assertIsNone(stats["pre_ret_15m"])


class EntryBackoffTests(unittest.TestCase):
    def pending(self, trigger_ts_ms: int | None) -> PendingEntry:
        return PendingEntry(
            signal=signal({}),
            eligible_at_ms=SIGNAL_TS,
            expires_at_ms=SIGNAL_TS + 60_000,
            trigger_ts_ms=trigger_ts_ms,
        )

    def test_first_attempt_is_never_blocked(self) -> None:
        self.assertFalse(
            entry_backoff_active(
                self.pending(None), event_ts_ms=SIGNAL_TS, backoff_ms=1_000
            )
        )

    def test_retry_waits_for_the_interval(self) -> None:
        item = self.pending(SIGNAL_TS)
        self.assertTrue(
            entry_backoff_active(
                item, event_ts_ms=SIGNAL_TS + 200, backoff_ms=1_000
            )
        )
        self.assertFalse(
            entry_backoff_active(
                item, event_ts_ms=SIGNAL_TS + 1_000, backoff_ms=1_000
            )
        )

    def test_zero_backoff_disables_the_gate(self) -> None:
        self.assertFalse(
            entry_backoff_active(
                self.pending(SIGNAL_TS), event_ts_ms=SIGNAL_TS, backoff_ms=0
            )
        )


class EngineGateTests(unittest.IsolatedAsyncioTestCase):
    """Intake guards that protect the deployed convention."""

    NOW_MS = 1_800_000_000_000

    def engine(self, *, store: PaperStore | None = None, **overrides: object) -> PaperEngine:
        wallet = PaperWallet(WalletConfig(starting_sol=10.0))
        broker = PaperBroker(BrokerConfig(notional_sol=0.1), wallet=wallet)
        config = EngineConfig(
            entry_delay_ms=5_000,
            max_entry_delay_ms=60_000,
            live=True,
            **overrides,  # type: ignore[arg-type]
        )
        return PaperEngine(
            config=config,
            broker=broker,
            quotes=object(),
            history=MarketHistory(),
            gate=GateConfig(rule="baseline"),
            store=store,
            clock=lambda: self.NOW_MS,
        )

    def make_signal(
        self,
        *,
        received_ts_ms: int | None = None,
        signal_ts_ms: int | None = None,
    ) -> Signal:
        return Signal(
            token_address=MINT,
            signal_ts_ms=(
                self.NOW_MS - 1_000 if signal_ts_ms is None else signal_ts_ms
            ),
            received_ts_ms=(
                self.NOW_MS if received_ts_ms is None else received_ts_ms
            ),
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

    async def test_near_zero_exit_is_classified_separately(self) -> None:
        engine = self.engine()
        position = Position(
            signal=self.make_signal(),
            mint=MINT,
            entry_ts_ms=self.NOW_MS - 300_000,
            entry_price_sol=1e-3,
            size=100.0,
            entry_value_sol=0.1,
            entry_fee_sol=0.00125,
            entry_impact=0.0,
            horizon_ts_ms=self.NOW_MS,
        )
        position.sold_size = 100.0
        position.size = 0.0
        position.exit_price_acc = 1e-6
        position.exit_value_acc = 1e-6
        position.exit_fee_acc = 0.0
        position.pnl_acc = -0.10125 + 1e-6
        position.entry_basis_acc = 0.1
        position.entry_fee_acc = 0.00125
        position.exit_reason = "horizon"
        position.exit_ts_ms = self.NOW_MS
        engine.positions[MINT] = position

        engine._commit_close(
            position, FillQuote(price_sol=1e-8, source="jupiter")
        )

        trade = engine.closed[-1]
        self.assertEqual(trade.exit_status, "near_zero_exit")
        # The PIT reason is untouched; only the outcome classification changed.
        self.assertEqual(trade.exit_reason, "horizon")

    async def test_synthetic_fallback_is_not_reclassified_near_zero(self) -> None:
        engine = self.engine()
        position = Position(
            signal=self.make_signal(),
            mint=MINT,
            entry_ts_ms=self.NOW_MS - 300_000,
            entry_price_sol=1e-3,
            size=100.0,
            entry_value_sol=0.1,
            entry_fee_sol=0.00125,
            entry_impact=0.0,
            horizon_ts_ms=self.NOW_MS,
        )
        position.sold_size = 100.0
        position.size = 0.0
        position.exit_price_acc = 1e-6
        position.exit_value_acc = 1e-6
        position.exit_fee_acc = 0.0
        position.pnl_acc = -0.10125 + 1e-6
        position.entry_basis_acc = 0.1
        position.entry_fee_acc = 0.00125
        position.exit_reason = "horizon"
        position.exit_status = "horizon_complete"
        position.exit_ts_ms = self.NOW_MS
        engine.positions[MINT] = position

        engine._commit_close(
            position, FillQuote(price_sol=1e-8, source="fallback_feed")
        )

        # A synthetic feed mark is already flagged exit_synthetic; it must not
        # masquerade as a priced near-zero sale.
        trade = engine.closed[-1]
        self.assertEqual(trade.exit_status, "horizon_complete")
        self.assertTrue(trade.exit_synthetic)

    async def test_normal_horizon_exit_keeps_horizon_status(self) -> None:
        engine = self.engine()
        position = Position(
            signal=self.make_signal(),
            mint=MINT,
            entry_ts_ms=self.NOW_MS - 300_000,
            entry_price_sol=1e-3,
            size=100.0,
            entry_value_sol=0.1,
            entry_fee_sol=0.00125,
            entry_impact=0.0,
            horizon_ts_ms=self.NOW_MS,
        )
        position.sold_size = 100.0
        position.size = 0.0
        position.exit_price_acc = 1.2
        position.exit_value_acc = 0.12
        position.exit_fee_acc = 0.0
        position.pnl_acc = 0.01875
        position.entry_basis_acc = 0.1
        position.entry_fee_acc = 0.00125
        position.exit_reason = "horizon"
        position.exit_ts_ms = self.NOW_MS
        engine.positions[MINT] = position

        engine._commit_close(
            position, FillQuote(price_sol=1.2e-3, source="jupiter")
        )

        self.assertEqual(engine.closed[-1].exit_status, "horizon_complete")

    async def test_mark_liquidity_is_recorded_not_gating(self) -> None:
        engine = self.engine()
        position = Position(
            signal=self.make_signal(),
            mint=MINT,
            entry_ts_ms=self.NOW_MS - 1_000,
            entry_price_sol=1e-3,
            size=100.0,
            entry_value_sol=0.1,
            entry_fee_sol=0.00125,
            entry_impact=0.0,
            horizon_ts_ms=self.NOW_MS + 299_000,
            last_price_sol=1e-3,
            last_ts_ms=self.NOW_MS - 1_000,
        )
        engine.positions[MINT] = position

        await engine.on_mark(
            MINT, 1.1e-3, self.NOW_MS, liquidity_usd=42_000.0
        )

        self.assertEqual(engine._mark_liquidity[MINT], 42_000.0)
        self.assertEqual(position.last_liquidity_usd, 42_000.0)
        # A mark is a price observation, not an exit signal.
        self.assertFalse(position.exit_requested)

    async def test_persistence_degraded_blocks_new_entries(self) -> None:
        engine = self.engine()
        engine._persistence_healthy = False
        decision = await engine.on_signal(self.make_signal())
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reasons, ["persistence_degraded"])
        self.assertEqual(engine.skips[-1].reason, "persistence_degraded")
        self.assertNotIn(MINT, engine.pending)

    async def test_late_card_is_not_traded(self) -> None:
        engine = self.engine(max_delivery_lag_ms=60_000)
        decision = await engine.on_signal(
            self.make_signal(received_ts_ms=self.NOW_MS + 120_000)
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reasons, ["delivery_lag_exceeded"])
        self.assertEqual(engine.skips[-1].reason, "delivery_lag_exceeded")
        self.assertNotIn(MINT, engine.pending)

    async def test_card_from_the_future_is_rejected_as_clock_skew(self) -> None:
        # Telegram timestamps are server UTC. If receipt time is behind the
        # card, the host clock is behind and "card + 5 s" points into the
        # future; the run would silently price a different window.
        engine = self.engine(max_future_skew_ms=5_000)
        signal_ts = self.NOW_MS + 30_000
        decision = await engine.on_signal(
            self.make_signal(
                signal_ts_ms=signal_ts, received_ts_ms=self.NOW_MS
            )
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reasons, ["signal_timestamp_in_future"])
        self.assertEqual(engine.skips[-1].reason, "signal_timestamp_in_future")
        self.assertIn("clock_skew_ms=30000", engine.skips[-1].detail or "")
        self.assertNotIn(MINT, engine.pending)

    async def test_small_clock_skew_is_tolerated(self) -> None:
        engine = self.engine(max_future_skew_ms=5_000)
        decision = await engine.on_signal(
            self.make_signal(
                signal_ts_ms=self.NOW_MS + 1_000,
                received_ts_ms=self.NOW_MS,
            )
        )
        self.assertTrue(decision.allowed)

    async def test_card_within_the_lag_window_is_traded(self) -> None:
        engine = self.engine(max_delivery_lag_ms=60_000)
        decision = await engine.on_signal(self.make_signal())
        self.assertTrue(decision.allowed)
        self.assertIn(MINT, engine.pending)
        pending = engine.pending[MINT]
        # Anchored on the card, not on Telegram delivery: eligible at card+5 s
        # and expired at card+60 s exactly like the research convention.
        self.assertEqual(
            pending.eligible_at_ms, self.NOW_MS - 1_000 + 5_000
        )
        self.assertEqual(
            pending.expires_at_ms, self.NOW_MS - 1_000 + 60_000
        )

    async def test_card_delivered_after_the_window_is_skipped(self) -> None:
        engine = self.engine(max_delivery_lag_ms=0)
        signal_ts = self.NOW_MS - 1_000
        decision = await engine.on_signal(
            self.make_signal(received_ts_ms=signal_ts + 60_500)
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reasons, ["entry_window_passed"])
        self.assertNotIn(MINT, engine.pending)

    async def test_missed_eligible_print_is_skipped(self) -> None:
        engine = self.engine()
        signal_ts = self.NOW_MS - 1_000
        engine.history.add(
            TradeEvent(
                mint=MINT,
                timestamp_ms=signal_ts + 6_000,
                event_index=0,
                action="buy",
                price_sol=0.02,
                token_amount=100.0,
                quote_amount=2.0,
            )
        )
        decision = await engine.on_signal(
            self.make_signal(received_ts_ms=signal_ts + 20_000)
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reasons, ["entry_missed_eligible_print"])
        self.assertNotIn(MINT, engine.pending)

    async def test_missed_dust_print_does_not_count_as_the_entry(self) -> None:
        engine = self.engine(min_entry_quote_sol=0.01)
        signal_ts = self.NOW_MS - 30_000
        engine.history.add(
            TradeEvent(
                mint=MINT,
                timestamp_ms=signal_ts + 6_000,
                event_index=0,
                action="buy",
                price_sol=0.02,
                token_amount=0.005,
                quote_amount=0.0001,
                quote_mint="So11111111111111111111111111111111111111112",
            )
        )
        decision = await engine.on_signal(
            self.make_signal(
                signal_ts_ms=signal_ts, received_ts_ms=self.NOW_MS - 5_000
            )
        )
        self.assertTrue(decision.allowed)
        self.assertIn(MINT, engine.pending)

    async def test_dust_buy_print_is_not_an_entry_trigger(self) -> None:
        engine = self.engine(min_entry_quote_sol=0.01)
        decision = await engine.on_signal(
            self.make_signal(signal_ts_ms=self.NOW_MS - 10_000)
        )
        self.assertTrue(decision.allowed)
        pending = engine.pending[MINT]
        await engine.on_trade(
            TradeEvent(
                mint=MINT,
                timestamp_ms=self.NOW_MS,
                event_index=0,
                action="buy",
                price_sol=0.02,
                token_amount=0.005,
                quote_amount=0.0001,
                quote_mint="So11111111111111111111111111111111111111112",
            )
        )
        self.assertEqual(pending.dust_prints, 1)
        self.assertNotIn(MINT, engine.positions)
        self.assertIn(MINT, engine.pending)
        await engine.tick(now_ms=self.NOW_MS + 60_000)
        self.assertNotIn(MINT, engine.pending)
        self.assertEqual(engine.skips[-1].reason, "entry_dust_prints_only")
        self.assertIn("dust_prints=1", engine.skips[-1].detail or "")

    async def test_unverified_notional_is_not_an_entry_trigger(self) -> None:
        engine = self.engine(min_entry_quote_sol=0.01)
        await engine.on_signal(
            self.make_signal(signal_ts_ms=self.NOW_MS - 10_000)
        )
        pending = engine.pending[MINT]
        await engine.on_trade(
            TradeEvent(
                mint=MINT,
                timestamp_ms=self.NOW_MS,
                event_index=0,
                action="buy",
                price_sol=0.02,
            )
        )
        self.assertEqual(pending.unverified_prints, 1)
        self.assertIn(MINT, engine.pending)
        self.assertNotIn(MINT, engine.positions)
        await engine.tick(now_ms=self.NOW_MS + 60_000)
        self.assertEqual(
            engine.skips[-1].reason, "entry_unverified_quote_notional"
        )

    async def test_timer_card_received_after_scheduled_entry_is_skipped(self) -> None:
        engine = self.engine(
            entry_mode="timer",
            timer_late_tolerance_ms=1_000,
        )
        # Card + 5 s was 5 s before receipt, well beyond scheduling jitter.
        decision = await engine.on_signal(
            self.make_signal(signal_ts_ms=self.NOW_MS - 10_000)
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reasons, ["entry_timer_late"])
        self.assertNotIn(MINT, engine.pending)

    async def test_timer_task_refuses_to_quote_after_schedule(self) -> None:
        engine = self.engine(entry_mode="timer", timer_late_tolerance_ms=1_000)
        pending = PendingEntry(
            signal=self.make_signal(),
            eligible_at_ms=self.NOW_MS - 5_000,
            expires_at_ms=self.NOW_MS + 60_000,
        )
        engine.pending[MINT] = pending
        await engine._timer_entry(pending)
        self.assertNotIn(MINT, engine.pending)
        self.assertEqual(engine.skips[-1].reason, "entry_timer_late")

    async def test_future_reference_price_is_rejected(self) -> None:
        engine = self.engine(
            entry_mode="timer",
            max_reference_age_ms=30_000,
            max_future_skew_ms=5_000,
        )
        pending = PendingEntry(
            signal=self.make_signal(),
            eligible_at_ms=self.NOW_MS - 1_000,
            expires_at_ms=self.NOW_MS + 60_000,
        )
        engine.pending[MINT] = pending
        engine.history.add(
            TradeEvent(
                mint=MINT,
                timestamp_ms=self.NOW_MS + 60_000,
                event_index=0,
                action="buy",
                price_sol=0.02,
                token_amount=100.0,
                quote_amount=2.0,
            )
        )
        await engine._timer_entry(pending)
        self.assertNotIn(MINT, engine.pending)
        self.assertEqual(
            engine.skips[-1].reason, "reference_timestamp_in_future"
        )

    async def test_timer_mode_rejects_a_stale_reference_price(self) -> None:
        engine = self.engine(
            entry_mode="timer", max_reference_age_ms=10_000
        )
        pending = PendingEntry(
            signal=self.make_signal(),
            eligible_at_ms=self.NOW_MS - 1_000,
            expires_at_ms=self.NOW_MS + 60_000,
        )
        engine.pending[MINT] = pending
        engine.history.add(
            TradeEvent(
                mint=MINT,
                timestamp_ms=self.NOW_MS - 60_000,
                event_index=0,
                action="buy",
                price_sol=0.02,
                token_amount=100.0,
                quote_amount=2.0,
            )
        )
        await engine._timer_entry(pending)
        self.assertNotIn(MINT, engine.pending)
        self.assertEqual(engine.skips[-1].reason, "stale_reference_price")

    def test_daily_pnl_restores_from_persisted_trades(self) -> None:
        from datetime import UTC, datetime

        with tempfile.TemporaryDirectory() as tmp:
            store = PaperStore(Path(tmp))
            day = datetime.fromtimestamp(self.NOW_MS / 1000, UTC).date().isoformat()
            store.record_trade_payload(
                {
                    "exit_ts_ms": self.NOW_MS - 1_000,
                    "pnl_sol": -0.25,
                }
            )
            engine = self.engine(store=store)
        self.assertAlmostEqual(engine._daily_pnl[day], -0.25)

    def test_daily_pnl_restores_across_run_directories(self) -> None:
        from datetime import UTC, datetime

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "risk" / "daily_pnl.json"
            day = datetime.fromtimestamp(self.NOW_MS / 1000, UTC).date().isoformat()
            path.parent.mkdir(parents=True)
            path.write_text(
                json.dumps({"version": 1, "days": {day: -0.4}}),
                encoding="utf-8",
            )
            first = self.engine(
                store=PaperStore(root / "run-first"), risk_state_path=path
            )
            second = self.engine(
                store=PaperStore(root / "run-after-restart"),
                risk_state_path=path,
            )
        self.assertAlmostEqual(first._daily_pnl[day], -0.4)
        self.assertAlmostEqual(second._daily_pnl[day], -0.4)

    def test_corrupt_risk_state_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "daily_pnl.json"
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                self.engine(risk_state_path=path, max_daily_loss_sol=0.5)

    def test_unwritable_risk_state_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / "not-a-directory"
            blocker.write_text("x", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                self.engine(
                    risk_state_path=blocker / "daily_pnl.json",
                    max_daily_loss_sol=0.5,
                )

    def test_first_run_creates_state_and_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "risk" / "daily_pnl.json"
            self.engine(risk_state_path=path, max_daily_loss_sol=0.5)
            self.assertTrue(path.exists())
            self.assertTrue(
                path.with_name(path.name + ".initialized").exists()
            )

    def test_missing_state_after_marker_fails_closed(self) -> None:
        # The lane initialized once, then the state file was deleted. Starting
        # at zero would silently reset the breaker, so refuse to start.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "risk" / "daily_pnl.json"
            path.parent.mkdir(parents=True)
            path.with_name(path.name + ".initialized").write_text(
                json.dumps({"version": 1}), encoding="utf-8"
            )
            with self.assertRaises(RuntimeError):
                self.engine(risk_state_path=path, max_daily_loss_sol=0.5)

    def test_nan_risk_state_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "daily_pnl.json"
            path.write_text(
                '{"version": 1, "days": {"2026-11-23": NaN}}',
                encoding="utf-8",
            )
            with self.assertRaises(RuntimeError):
                self.engine(risk_state_path=path, max_daily_loss_sol=0.5)

    def test_infinite_risk_state_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "daily_pnl.json"
            path.write_text(
                '{"version": 1, "days": {"2026-11-23": Infinity}}',
                encoding="utf-8",
            )
            with self.assertRaises(RuntimeError):
                self.engine(risk_state_path=path, max_daily_loss_sol=0.5)

    def test_unsupported_risk_state_version_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "daily_pnl.json"
            path.write_text(
                json.dumps({"version": 2, "days": {"2026-11-23": -0.1}}),
                encoding="utf-8",
            )
            with self.assertRaises(RuntimeError):
                self.engine(risk_state_path=path, max_daily_loss_sol=0.5)

    def test_invalid_day_key_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "daily_pnl.json"
            path.write_text(
                json.dumps({"version": 1, "days": {"2026-13-40": -0.1}}),
                encoding="utf-8",
            )
            with self.assertRaises(RuntimeError):
                self.engine(risk_state_path=path, max_daily_loss_sol=0.5)

    def test_unreadable_trade_history_fails_closed_on_first_init(self) -> None:
        class BrokenStore:
            def realized_pnl_by_day(self) -> dict[str, float]:
                raise OSError("history unavailable")

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "risk" / "daily_pnl.json"
            with self.assertRaises(RuntimeError):
                self.engine(
                    store=BrokenStore(),  # type: ignore[arg-type]
                    risk_state_path=path,
                    max_daily_loss_sol=0.5,
                )

    async def test_negative_daily_pnl_survives_restart_and_blocks_entry(
        self,
    ) -> None:
        from datetime import UTC, datetime

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "risk" / "daily_pnl.json"
            day = (
                datetime.fromtimestamp(self.NOW_MS / 1000, UTC)
                .date()
                .isoformat()
            )
            path.parent.mkdir(parents=True)
            path.write_text(
                json.dumps({"version": 1, "days": {day: -0.8}}),
                encoding="utf-8",
            )
            path.with_name(path.name + ".initialized").write_text(
                json.dumps({"version": 1}), encoding="utf-8"
            )
            engine = self.engine(
                store=PaperStore(root / "run-after-restart"),
                risk_state_path=path,
                max_daily_loss_sol=0.5,
            )
            decision = await engine.on_signal(self.make_signal())
        self.assertAlmostEqual(engine._day_pnl(self.NOW_MS), -0.8)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reasons, ["daily_loss_limit"])

    def test_non_finite_daily_pnl_is_not_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "daily_pnl.json"
            engine = self.engine(risk_state_path=path)
            path.unlink()
            engine._daily_pnl["2026-11-23"] = float("nan")
            engine._persist_daily_pnl()
            self.assertFalse(path.exists())
            self.assertIsNotNone(engine._risk_state_error)

    def test_corrupt_risk_state_is_ignored_when_cap_is_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "daily_pnl.json"
            path.write_text("{not json", encoding="utf-8")
            engine = self.engine(risk_state_path=path, max_daily_loss_sol=0.0)
        self.assertEqual(engine._daily_pnl, {})

    def test_daily_pnl_is_persisted_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "risk" / "daily_pnl.json"
            engine = self.engine(risk_state_path=path)
            engine._daily_pnl["2026-11-23"] = -0.35
            engine._persist_daily_pnl()
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["days"]["2026-11-23"], -0.35)
            self.assertFalse(path.with_name(path.name + ".tmp").exists())

    async def test_risk_state_write_failure_blocks_new_entries(self) -> None:
        engine = self.engine(max_daily_loss_sol=0.5)
        engine._risk_state_error = "disk full"
        decision = await engine.on_signal(self.make_signal())
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reasons, ["risk_state_unavailable"])

    def test_stale_mark_is_excluded_from_nav(self) -> None:
        engine = self.engine(max_mark_age_ms=10_000)
        wallet = engine.broker.wallet
        assert wallet is not None
        wallet.reserve_entry(MINT, 10.0, 0.1, 0.0)
        engine.history.add(
            TradeEvent(
                mint=MINT,
                timestamp_ms=self.NOW_MS - 60_000,
                event_index=0,
                action="buy",
                price_sol=0.02,
                token_amount=100.0,
                quote_amount=2.0,
            )
        )
        snapshot = engine.wallet_snapshot()
        assert snapshot is not None
        # The stale mark is not current NAV: equity is cash only, while the
        # optimistic view keeps the last known price.
        self.assertAlmostEqual(snapshot["equity_sol"], 9.9)
        self.assertAlmostEqual(snapshot["equity_all_marks_sol"], 10.1)
        self.assertEqual(snapshot["stale_mark_count"], 1)

    async def test_concurrency_cap_counts_pending_entries(self) -> None:
        engine = self.engine(max_concurrent_positions=1)
        engine.pending["other-mint"] = object()  # type: ignore[assignment]
        decision = await engine.on_signal(self.make_signal())
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reasons, ["max_concurrent_positions"])

    async def test_daily_loss_limit_stops_new_entries(self) -> None:
        from datetime import UTC, datetime

        engine = self.engine(max_daily_loss_sol=0.4)
        day = datetime.fromtimestamp(self.NOW_MS / 1000, UTC).date().isoformat()
        engine._daily_pnl[day] = -0.5
        decision = await engine.on_signal(self.make_signal())
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reasons, ["daily_loss_limit"])

    def test_wallet_snapshot_marks_open_tokens(self) -> None:
        engine = self.engine()
        wallet = engine.broker.wallet
        assert wallet is not None
        wallet.reserve_entry(MINT, 10.0, 0.1, 0.0)
        engine.history.add(
            TradeEvent(
                mint=MINT,
                timestamp_ms=self.NOW_MS,
                event_index=0,
                action="buy",
                price_sol=0.02,
                token_amount=100.0,
                quote_amount=2.0,
            )
        )
        snapshot = engine.wallet_snapshot()
        assert snapshot is not None
        # Cash plus 10 tokens marked at 0.02 SOL; the old snapshot reported
        # cash only and understated equity by the open position.
        self.assertAlmostEqual(snapshot["equity_sol"], 9.9 + 0.2)

    def test_reporter_snapshot_uses_the_marked_engine_view(self) -> None:
        engine = self.engine()
        wallet = engine.broker.wallet
        assert wallet is not None
        wallet.reserve_entry(MINT, 10.0, 0.1, 0.0)
        engine.history.add(
            TradeEvent(
                mint=MINT,
                timestamp_ms=self.NOW_MS,
                event_index=0,
                action="buy",
                price_sol=0.02,
                token_amount=100.0,
                quote_amount=2.0,
            )
        )
        reporter = TelegramReporter(ReporterConfig(enabled=False))
        reporter.bind(engine=engine, wallet=wallet)
        snapshot = reporter.wallet_snapshot()
        assert snapshot is not None
        self.assertAlmostEqual(snapshot["equity_sol"], 9.9 + 0.2)

    async def test_tick_prunes_locks_for_dormant_mints(self) -> None:
        # ``_lock`` is keyed by mint; over a 24/7 run one lock per card ever
        # quoted accumulated forever. A lock is needed only while the mint is
        # pending/open, so tick must drop the rest.
        engine = self.engine()
        engine.pending[MINT] = PendingEntry(
            signal=self.make_signal(),
            eligible_at_ms=self.NOW_MS,
            expires_at_ms=self.NOW_MS + 60_000,
        )
        active_lock = engine._lock(MINT)
        engine._lock("dormant-mint")
        await engine.tick()
        self.assertIs(engine._locks[MINT], active_lock)
        self.assertNotIn("dormant-mint", engine._locks)

    async def test_tick_keeps_a_lock_that_is_still_held(self) -> None:
        engine = self.engine()
        lock = engine._lock("dormant-mint")
        async with lock:
            await engine.tick()
        self.assertIn("dormant-mint", engine._locks)


class TakeProfitExitTests(unittest.IsolatedAsyncioTestCase):
    """The TP50 variant must sell at the target, not wait for the horizon."""

    NOW_MS = 1_800_000_000_000

    class FakeQuotes:
        def __init__(self, exit_price: float) -> None:
            self.exit_price = exit_price
            self.exit_calls = 0

        async def exit_quote(
            self,
            mint: str,
            size_tokens: float,
            reference_price_sol: float | None,
            *,
            decimals: int | None = None,
            action: str = "exit",
        ) -> FillQuote:
            self.exit_calls += 1
            return FillQuote(price_sol=self.exit_price, source="test")

    def engine(self, quotes: "TakeProfitExitTests.FakeQuotes") -> PaperEngine:
        wallet = PaperWallet(WalletConfig(starting_sol=10.0))
        broker = PaperBroker(BrokerConfig(notional_sol=0.1), wallet=wallet)
        return PaperEngine(
            config=EngineConfig(
                entry_delay_ms=5_000,
                max_entry_delay_ms=60_000,
                live=True,
                horizon_ms=300_000,
                tp_pct=0.50,
            ),
            broker=broker,
            quotes=quotes,
            history=MarketHistory(),
            gate=GateConfig(rule=SCANNER_TP50_RULE),
            store=None,
            clock=lambda: self.NOW_MS,
        )

    def position(self) -> Position:
        return Position(
            signal=Signal(
                token_address=MINT,
                signal_ts_ms=self.NOW_MS - 130_000,
                received_ts_ms=self.NOW_MS - 128_000,
                channel="@test",
                message_id=1,
                network="solana",
                address_status="confirmed",
                address_type="mint",
                message_type="scanner_pool_launch",
                is_buy_signal=True,
                is_confirmed_buy_signal=True,
                features={},
            ),
            mint=MINT,
            entry_ts_ms=self.NOW_MS - 120_000,
            entry_price_sol=1.0,
            size=100.0,
            entry_value_sol=0.1,
            entry_fee_sol=0.00125,
            entry_impact=0.0,
            horizon_ts_ms=self.NOW_MS + 180_000,
            tp_level=1.5,
            last_price_sol=1.0,
            decimals=6,
        )

    async def test_mark_at_the_target_sells_the_whole_position(self) -> None:
        quotes = self.FakeQuotes(1.5)
        engine = self.engine(quotes)
        position = self.position()
        engine.positions[MINT] = position
        engine.broker.wallet.token_balances[MINT] = position.size

        await engine._on_position_price(
            position, self.NOW_MS - 60_000, 1.55, await_actions=True
        )

        self.assertEqual(position.exit_reason, "take_profit")
        self.assertEqual(position.size, 0.0)
        self.assertEqual(quotes.exit_calls, 1)
        self.assertEqual(engine.closed[-1].exit_status, "tp_hit")

    async def test_mark_below_the_target_keeps_the_position(self) -> None:
        quotes = self.FakeQuotes(1.2)
        engine = self.engine(quotes)
        position = self.position()
        engine.positions[MINT] = position
        engine.broker.wallet.token_balances[MINT] = position.size

        await engine._on_position_price(
            position, self.NOW_MS - 60_000, 1.49, await_actions=True
        )

        self.assertFalse(position.exit_requested)
        self.assertEqual(quotes.exit_calls, 0)
        self.assertEqual(len(engine.closed), 0)

    async def test_trade_trigger_records_its_source_and_quote_leg(self) -> None:
        quotes = self.FakeQuotes(1.55)
        engine = self.engine(quotes)
        position = self.position()
        engine.positions[MINT] = position
        engine.broker.wallet.token_balances[MINT] = position.size
        trigger_ts = self.NOW_MS - 60_000

        await engine.on_trade(
            TradeEvent(
                mint=MINT,
                timestamp_ms=trigger_ts,
                event_index=1,
                action="sell",
                price_sol=1.55,
                token_amount=10.0,
            ),
            await_actions=True,
        )

        trade = engine.closed[-1]
        self.assertEqual(trade.exit_reason, "take_profit")
        # The observed print, not the clamped 1.5 target, is the evidence.
        self.assertEqual(trade.exit_trigger_source, "pumpapi_trade")
        self.assertEqual(trade.exit_trigger_ts_ms, trigger_ts)
        self.assertAlmostEqual(trade.exit_trigger_price_sol, 1.55)
        self.assertEqual(trade.exit_reference_kind, "trigger_observation")
        self.assertAlmostEqual(trade.exit_reference_price_sol, 1.5)
        # One full-size quote, and the leg carries the quote timestamp/amounts.
        self.assertTrue(trade.exit_full_size)
        self.assertEqual(trade.exit_quote_fractions, [1.0])
        self.assertEqual(len(trade.exit_fill_evidence), 1)
        leg = trade.exit_fill_evidence[0]
        self.assertTrue(leg["full_size"])
        self.assertEqual(leg["quote_source"], "test")
        self.assertAlmostEqual(leg["quote_price_sol"], 1.55)
        self.assertIn("quote_ts_ms", leg)
        self.assertIn("out_amount", leg)

    async def test_mark_trigger_is_labelled_as_a_dexscreener_mark(self) -> None:
        quotes = self.FakeQuotes(1.6)
        engine = self.engine(quotes)
        position = self.position()
        engine.positions[MINT] = position
        engine.broker.wallet.token_balances[MINT] = position.size

        await engine.on_mark(
            MINT, 1.6, self.NOW_MS - 30_000, await_actions=True
        )

        trade = engine.closed[-1]
        self.assertEqual(trade.exit_trigger_source, "dexscreener_mark")
        self.assertEqual(trade.exit_trigger_ts_ms, self.NOW_MS - 30_000)
        self.assertAlmostEqual(trade.exit_trigger_price_sol, 1.6)

    async def test_post_horizon_print_keeps_the_in_horizon_reference(self) -> None:
        quotes = self.FakeQuotes(1.05)
        engine = self.engine(quotes)
        position = self.position()
        position.last_price_sol = 1.05
        position.last_ts_ms = self.NOW_MS - 10_000
        engine.positions[MINT] = position
        engine.broker.wallet.token_balances[MINT] = position.size
        woke_at = position.horizon_ts_ms + 1_000

        await engine.on_trade(
            TradeEvent(
                mint=MINT,
                timestamp_ms=woke_at,
                event_index=1,
                action="buy",
                price_sol=1.3,
                token_amount=10.0,
            ),
            await_actions=True,
        )

        trade = engine.closed[-1]
        self.assertEqual(trade.exit_reason, "horizon")
        self.assertEqual(trade.exit_trigger_source, "pumpapi_trade")
        # The print that woke the close and the price that priced it are
        # different observations; both are recorded.
        self.assertEqual(trade.exit_trigger_ts_ms, woke_at)
        self.assertAlmostEqual(trade.exit_trigger_price_sol, 1.3)
        self.assertEqual(trade.exit_ts_ms, self.NOW_MS - 10_000)
        self.assertAlmostEqual(trade.exit_reference_price_sol, 1.05)
        self.assertEqual(trade.exit_reference_kind, "last_in_horizon")

    async def test_horizon_tick_uses_the_last_in_horizon_reference(self) -> None:
        quotes = self.FakeQuotes(1.1)
        engine = self.engine(quotes)
        position = self.position()
        position.last_price_sol = 1.05
        position.last_ts_ms = self.NOW_MS - 10_000
        engine.positions[MINT] = position
        engine.broker.wallet.token_balances[MINT] = position.size
        settle = engine.config.horizon_settle_ms

        await engine.tick(
            position.horizon_ts_ms + settle + 1, await_actions=True
        )

        trade = engine.closed[-1]
        self.assertEqual(trade.exit_reason, "horizon")
        self.assertEqual(trade.exit_trigger_source, "engine_tick")
        # The engine closes on the last in-horizon observation; the replay
        # uses the first print at/after the horizon. Different measurement.
        self.assertEqual(trade.exit_reference_kind, "last_in_horizon")
        self.assertAlmostEqual(trade.exit_reference_price_sol, 1.05)
        self.assertEqual(trade.exit_ts_ms, self.NOW_MS - 10_000)

    async def test_fractional_exit_is_visible_in_the_evidence(self) -> None:
        class LadderQuotes(TakeProfitExitTests.FakeQuotes):
            async def exit_quote(
                self,
                mint: str,
                size_tokens: float,
                reference_price_sol: float | None,
                *,
                decimals: int | None = None,
                action: str = "exit",
            ) -> FillQuote:
                self.exit_calls += 1
                if size_tokens > 60.0:
                    raise QuoteError("quote_no_route", "size")
                return FillQuote(
                    price_sol=1.55, source="test", out_amount=1550
                )

        quotes = LadderQuotes(1.55)
        engine = self.engine(quotes)
        position = self.position()
        engine.positions[MINT] = position
        engine.broker.wallet.token_balances[MINT] = position.size

        await engine._on_position_price(
            position, self.NOW_MS - 60_000, 1.55, await_actions=True
        )

        trade = engine.closed[-1]
        self.assertEqual(trade.exit_status, "tp_hit")
        self.assertEqual(trade.size, position.sold_size)
        # 50 % first because 100 % did not route, then the remainder in full.
        self.assertEqual(trade.exit_quote_fractions, [0.5, 1.0])
        self.assertFalse(trade.exit_full_size)
        self.assertEqual(trade.exit_fill_count, 2)
        self.assertFalse(trade.exit_fill_evidence[0]["full_size"])
        self.assertTrue(trade.exit_fill_evidence[1]["full_size"])
        self.assertAlmostEqual(trade.unsold_size, 0.0)


class QuoteGuardExitTests(unittest.IsolatedAsyncioTestCase):
    """V1: sell when a full-size quote stops recovering the entry cost.

    The guard must fire on the executable quote, not on a low trade print,
    record unavailable routes without selling, and leave the five-minute exit
    as the fallback.
    """

    NOW_MS = 1_800_000_000_000

    class GuardQuotes:
        def __init__(self, price: float, error: str | None = None) -> None:
            self.price = price
            self.error = error
            self.exit_calls = 0
            self.sizes: list[float] = []

        async def exit_quote(
            self,
            mint: str,
            size_tokens: float,
            reference_price_sol: float | None,
            *,
            decimals: int | None = None,
            action: str = "exit",
        ) -> FillQuote:
            self.exit_calls += 1
            self.sizes.append(size_tokens)
            if self.error is not None:
                raise QuoteError(self.error, "guard")
            return FillQuote(
                price_sol=self.price,
                source="test",
                out_amount=int(self.price * size_tokens * 1_000_000_000),
                price_impact_pct=-0.4,
                slippage_bps=50,
                quoted_at_ms=1_800_000_000_000,
            )

    def engine(self, quotes: "QuoteGuardExitTests.GuardQuotes") -> PaperEngine:
        wallet = PaperWallet(WalletConfig(starting_sol=10.0))
        broker = PaperBroker(BrokerConfig(notional_sol=0.1), wallet=wallet)
        return PaperEngine(
            config=EngineConfig(
                entry_delay_ms=5_000,
                max_entry_delay_ms=60_000,
                live=True,
                horizon_ms=300_000,
                quote_guard_ratio=0.50,
                quote_guard_interval_ms=15_000,
            ),
            broker=broker,
            quotes=quotes,
            history=MarketHistory(),
            gate=GateConfig(rule=SCANNER_QUOTE_GUARD_RULE),
            store=None,
            clock=lambda: self.NOW_MS,
        )

    def position(self) -> Position:
        return Position(
            signal=Signal(
                token_address=MINT,
                signal_ts_ms=self.NOW_MS - 130_000,
                received_ts_ms=self.NOW_MS - 128_000,
                channel="@test",
                message_id=1,
                network="solana",
                address_status="confirmed",
                address_type="mint",
                message_type="scanner_pool_launch",
                is_buy_signal=True,
                is_confirmed_buy_signal=True,
                features={},
            ),
            mint=MINT,
            entry_ts_ms=self.NOW_MS - 120_000,
            entry_price_sol=1.0,
            size=100.0,
            entry_value_sol=0.1,
            entry_fee_sol=0.00125,
            entry_impact=0.0,
            horizon_ts_ms=self.NOW_MS + 180_000,
            quote_guard_ratio=0.50,
            last_price_sol=1.0,
            decimals=6,
        )

    async def tick(self, engine: PaperEngine, now_ms: int) -> None:
        await engine.tick(now_ms)
        await engine._drain_tasks(None)

    async def test_low_full_size_quote_closes_the_position(self) -> None:
        quotes = self.GuardQuotes(0.0005)
        engine = self.engine(quotes)
        position = self.position()
        engine.positions[MINT] = position
        engine.broker.wallet.token_balances[MINT] = position.size

        await self.tick(engine, self.NOW_MS)

        trade = engine.closed[-1]
        self.assertEqual(trade.exit_reason, "quote_guard")
        self.assertEqual(trade.exit_status, "quote_guard_exit")
        self.assertEqual(trade.exit_trigger_source, "quote_guard")
        self.assertEqual(trade.exit_trigger_ts_ms, self.NOW_MS)
        self.assertEqual(trade.exit_reference_kind, "trigger_observation")
        self.assertEqual(trade.quote_guard_checks, 1)
        self.assertEqual(trade.quote_guard_unavailable, 0)
        self.assertLess(trade.quote_guard_min_ratio, 0.50)
        # Probe (full size) plus the fresh quote that prices the sale.
        self.assertEqual(quotes.exit_calls, 2)
        self.assertEqual(quotes.sizes, [100.0, 100.0])
        self.assertEqual(trade.quote_guard_evidence[0]["status"], "ok")
        self.assertFalse(trade.exit_synthetic)

    async def test_high_quote_keeps_the_position_and_records_the_probe(self) -> None:
        quotes = self.GuardQuotes(1.2)
        engine = self.engine(quotes)
        position = self.position()
        engine.positions[MINT] = position

        await self.tick(engine, self.NOW_MS)

        self.assertFalse(position.exit_requested)
        self.assertEqual(len(engine.closed), 0)
        self.assertEqual(position.quote_guard_checks, 1)
        self.assertGreater(position.quote_guard_min_ratio, 0.5)
        self.assertEqual(len(position.quote_guard_evidence), 1)

    async def test_guard_waits_for_its_interval(self) -> None:
        quotes = self.GuardQuotes(0.0005)
        engine = self.engine(quotes)
        position = self.position()
        position.quote_guard_last_ts_ms = self.NOW_MS - 5_000
        engine.positions[MINT] = position

        await self.tick(engine, self.NOW_MS)

        self.assertEqual(quotes.exit_calls, 0)
        self.assertEqual(position.quote_guard_checks, 0)

    async def test_unavailable_route_is_recorded_but_does_not_exit(self) -> None:
        quotes = self.GuardQuotes(0.0, error="quote_no_route")
        engine = self.engine(quotes)
        position = self.position()
        engine.positions[MINT] = position

        await self.tick(engine, self.NOW_MS)

        self.assertFalse(position.exit_requested)
        self.assertEqual(len(engine.closed), 0)
        self.assertEqual(position.quote_guard_unavailable, 1)
        self.assertEqual(position.quote_guard_checks, 0)
        evidence = position.quote_guard_evidence[0]
        self.assertEqual(evidence["status"], "unavailable")
        self.assertEqual(evidence["reason"], "quote_no_route")
        self.assertIsNone(evidence["ratio"])

    async def test_unavailable_route_is_retried_on_the_next_interval(self) -> None:
        quotes = self.GuardQuotes(0.0, error="quote_no_route")
        engine = self.engine(quotes)
        position = self.position()
        engine.positions[MINT] = position

        await self.tick(engine, self.NOW_MS)
        quotes.error = None
        quotes.price = 1.2
        await self.tick(engine, self.NOW_MS + 15_000)

        self.assertEqual(quotes.exit_calls, 2)
        self.assertEqual(position.quote_guard_unavailable, 1)
        self.assertEqual(position.quote_guard_checks, 1)
        self.assertFalse(position.exit_requested)

    async def test_horizon_fallback_still_closes_when_the_guard_is_quiet(self) -> None:
        quotes = self.GuardQuotes(1.2)
        engine = self.engine(quotes)
        position = self.position()
        engine.positions[MINT] = position
        engine.broker.wallet.token_balances[MINT] = position.size

        await self.tick(
            engine,
            position.horizon_ts_ms + engine.config.horizon_settle_ms + 1,
        )

        trade = engine.closed[-1]
        self.assertEqual(trade.exit_reason, "horizon")
        self.assertEqual(trade.exit_status, "horizon_complete")
        # The horizon close wins: no probe is spent on a position whose sale
        # is already due.
        self.assertEqual(trade.quote_guard_checks, 0)
        self.assertEqual(quotes.exit_calls, 1)

    async def test_frozen_position_is_never_probed(self) -> None:
        quotes = self.GuardQuotes(0.0005)
        engine = self.engine(quotes)
        position = self.position()
        position.quote_guard_ratio = None
        engine.positions[MINT] = position

        await self.tick(engine, self.NOW_MS)

        self.assertEqual(quotes.exit_calls, 0)
        self.assertFalse(position.exit_requested)

    async def test_guard_threshold_validation(self) -> None:
        for bad in (0.0, 1.0, -0.5):
            with self.assertRaises(ValueError):
                EngineConfig(quote_guard_ratio=bad)
        with self.assertRaises(ValueError):
            EngineConfig(quote_guard_interval_ms=0)


class ScannerCardDedupTests(unittest.TestCase):
    """The first card per pool is the only one that may reach the engine."""

    MINT = "ScDedupMint111111111111111111111111111111"

    def claim(
        self,
        seen: OrderedDict[tuple[str, str], None],
        *,
        recovery: RecoveryStore | None = None,
        lane: str = "A",
        mint: str | None = None,
    ) -> bool:
        return _claim_scanner_card(
            lane_name=lane,
            token_address=self.MINT if mint is None else mint,
            channel="@AveSolanaTokenScanner",
            seen=seen,
            recovery=recovery,
            window=100,
        )

    def test_first_card_is_processed_exactly_once(self) -> None:
        seen: OrderedDict[tuple[str, str], None] = OrderedDict()
        self.assertTrue(self.claim(seen))
        # The key is recorded on the first card, so the next repost is
        # rejected even if the engine no longer has a pending entry or an open
        # position for the mint.
        self.assertFalse(self.claim(seen))

    def test_repost_after_the_tp50_position_closed_is_ignored(self) -> None:
        seen: OrderedDict[tuple[str, str], None] = OrderedDict()
        self.assertTrue(self.claim(seen))
        # Simulate the tp50 exit closing the position: the engine forgets the
        # mint, the dedup set does not.
        engine_positions: dict[str, object] = {self.MINT: object()}
        engine_positions.clear()
        self.assertFalse(self.claim(seen))
        self.assertEqual(engine_positions, {})

    def test_third_and_later_reposts_are_ignored(self) -> None:
        seen: OrderedDict[tuple[str, str], None] = OrderedDict()
        self.assertTrue(self.claim(seen))
        for _ in range(5):
            self.assertFalse(self.claim(seen))
        self.assertEqual(len(seen), 1)

    def test_repost_after_restart_uses_the_durable_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lane.sqlite3"
            store = RecoveryStore(path)
            try:
                self.assertTrue(self.claim(OrderedDict(), recovery=store))
            finally:
                store.close()
            # A restart starts with an empty in-memory set and reopens the
            # same durable lane state.
            reopened = RecoveryStore(path)
            try:
                self.assertFalse(
                    self.claim(OrderedDict(), recovery=reopened)
                )
            finally:
                reopened.close()

    def test_a_different_lane_has_its_own_first_card(self) -> None:
        # A/B lanes share the channel but are separate experiments.
        seen: OrderedDict[tuple[str, str], None] = OrderedDict()
        self.assertTrue(self.claim(seen, lane="A"))
        self.assertTrue(self.claim(seen, lane="B"))
        self.assertFalse(self.claim(seen, lane="A"))

    def test_frozen_lane_keeps_the_same_first_card_behavior(self) -> None:
        # The helper is rule-agnostic; the frozen scanner lane (called with
        # the same lane name in its own run) behaves identically. This pins
        # that the tp50 fix did not special-case or regress it.
        seen: OrderedDict[tuple[str, str], None] = OrderedDict()
        self.assertTrue(self.claim(seen, lane="A"))
        self.assertFalse(self.claim(seen, lane="A"))

    def test_address_less_card_is_left_to_the_parser(self) -> None:
        seen: OrderedDict[tuple[str, str], None] = OrderedDict()
        self.assertTrue(self.claim(seen, mint=""))
        self.assertTrue(self.claim(seen, mint=""))
        self.assertEqual(seen, OrderedDict())


class LiveGuardTests(unittest.TestCase):
    def test_live_mode_refuses_the_paper_only_tp50_rule(self) -> None:
        from paper_bot.live import LiveConfig, run_live

        config = LiveConfig(
            channels=["@test"],
            config_path=Path("config.ini"),
            run_dir=Path("output/paper/run"),
            mode="live",
            rule=SCANNER_TP50_RULE,
        )
        # Refused before any credential is read, so the live opt-in cannot
        # turn the experiment into a funded run.
        with self.assertRaises(ValueError) as caught:
            asyncio.run(run_live(config))
        self.assertIn("paper-only", str(caught.exception))

    def test_live_mode_refuses_the_paper_only_quote_guard_rule(self) -> None:
        from paper_bot.live import LiveConfig, run_live

        config = LiveConfig(
            channels=["@test"],
            config_path=Path("config.ini"),
            run_dir=Path("output/paper/run"),
            mode="live",
            rule=SCANNER_QUOTE_GUARD_RULE,
            quote_guard_ratio=0.50,
        )
        with self.assertRaises(ValueError) as caught:
            asyncio.run(run_live(config))
        self.assertIn("paper-only", str(caught.exception))

    def test_only_the_variants_are_declared_paper_only(self) -> None:
        self.assertIn(SCANNER_TP50_RULE, PAPER_ONLY_RULES)
        self.assertIn(SCANNER_QUOTE_GUARD_RULE, PAPER_ONLY_RULES)
        self.assertNotIn(SCANNER_RULE, PAPER_ONLY_RULES)

    def test_quote_guard_is_part_of_the_recovery_fingerprint(self) -> None:
        from paper_bot.live import LiveConfig, _recovery_fingerprint

        def fingerprint(**overrides: object) -> dict:
            config = LiveConfig(
                channels=["@test"],
                config_path=Path("config.ini"),
                run_dir=Path("output/paper/run"),
                rule=SCANNER_QUOTE_GUARD_RULE,
                **overrides,
            )
            return _recovery_fingerprint(config, config.rule, "trade")

        base = fingerprint(quote_guard_ratio=0.50)
        self.assertEqual(base["quote_guard_ratio"], 0.50)
        self.assertEqual(base["quote_guard_interval_ms"], 15_000)
        # A resumed run may not silently change the guard threshold.
        self.assertNotEqual(base, fingerprint(quote_guard_ratio=0.60))
        self.assertNotEqual(
            base, fingerprint(quote_guard_interval_ms=30_000)
        )

    def test_live_config_fails_closed_without_opt_in(self) -> None:
        config = live_config_from_env(
            {"SOLANA_PRIVATE_KEY": "x"}, enabled=True, slippage_bps=50,
            max_notional_sol=0.1,
        )
        self.assertFalse(config.enabled)

    def test_live_config_fails_closed_without_key(self) -> None:
        with self.assertRaises(ValueError):
            live_config_from_env(
                {"LIVE_TRADING_ENABLED": "1"}, enabled=True, slippage_bps=50,
                max_notional_sol=0.1,
            )

    def test_risk_state_path_is_stable_and_lane_specific(self) -> None:
        from paper_bot.live import LiveConfig, _risk_state_path

        def config() -> LiveConfig:
            return LiveConfig(
                channels=["@test"],
                config_path=Path("config.ini"),
                run_dir=Path("output/paper/run"),
            )

        first = _risk_state_path(config(), "scanner_pumpfun_amm", "trade")
        again = _risk_state_path(config(), "scanner_pumpfun_amm", "trade")
        other = _risk_state_path(config(), "scanner_pumpfun_amm", "timer")
        self.assertEqual(first, again)
        self.assertNotEqual(first, other)
        self.assertEqual(first.parent, Path("output/risk"))
        self.assertTrue(first.name.endswith(".json"))
        self.assertNotIn(" ", first.name)

    def test_notional_cap_and_allowlist_are_enforced(self) -> None:
        executor = LiveSolanaExecutor(
            LiveExecutionConfig(
                enabled=True,
                private_key="unused-for-guard",
                max_notional_sol=0.1,
                allowlist=frozenset({MINT}),
            )
        )
        with self.assertRaises(LiveExecutionError) as caught:
            executor._guard(MINT, 0.11)
        self.assertEqual(caught.exception.reason, "live_notional_cap")
        with self.assertRaises(LiveExecutionError) as caught:
            executor._guard("other-mint", 0.05)
        self.assertEqual(caught.exception.reason, "live_mint_not_allowlisted")

    def test_disabled_executor_refuses_to_trade(self) -> None:
        executor = LiveSolanaExecutor(LiveExecutionConfig(enabled=False))
        with self.assertRaises(LiveExecutionError) as caught:
            executor._guard(MINT, 0.01)
        self.assertEqual(caught.exception.reason, "live_disabled")

    def test_invalid_private_key_never_echoes_the_key(self) -> None:
        secret = "not-a-base58-secret"
        executor = LiveSolanaExecutor(
            LiveExecutionConfig(enabled=True, private_key=secret)
        )
        with self.assertRaises(LiveExecutionError) as caught:
            executor._key()
        self.assertNotIn(secret, caught.exception.detail or "")

    def test_config_accepts_priority_fee_and_jito_tip(self) -> None:
        config = LiveExecutionConfig(
            enabled=True,
            private_key="unused-for-guard",
            priority_fee_lamports=2000,
            jito_tip_lamports=10000,
        )
        self.assertEqual(config.priority_fee_lamports, 2000)
        self.assertEqual(config.jito_tip_lamports, 10000)

    def test_config_rejects_negative_fees(self) -> None:
        with self.assertRaises(ValueError):
            LiveExecutionConfig(
                enabled=True,
                private_key="unused-for-guard",
                priority_fee_lamports=-1,
            )
        with self.assertRaises(ValueError):
            LiveExecutionConfig(
                enabled=True,
                private_key="unused-for-guard",
                jito_tip_lamports=-1,
            )

    def test_config_rejects_max_slippage_below_base(self) -> None:
        with self.assertRaises(ValueError):
            LiveExecutionConfig(
                enabled=True,
                private_key="unused-for-guard",
                slippage_bps=200,
                max_slippage_bps=100,
            )

    def test_live_config_reads_max_slippage_from_env(self) -> None:
        config = live_config_from_env(
            {"LIVE_MAX_SLIPPAGE_BPS": "400"},
            enabled=False,
            slippage_bps=50,
            max_notional_sol=0.1,
        )
        self.assertEqual(config.max_slippage_bps, 400)

    def test_live_config_cli_fees_override_env(self) -> None:
        config = live_config_from_env(
            {
                "LIVE_PRIORITY_FEE_LAMPORTS": "111",
                "LIVE_JITO_TIP_LAMPORTS": "222",
            },
            enabled=False,
            slippage_bps=50,
            max_notional_sol=0.1,
            priority_fee_lamports=2000,
            jito_tip_lamports=10000,
        )
        self.assertEqual(config.priority_fee_lamports, 2000)
        self.assertEqual(config.jito_tip_lamports, 10000)

    def test_live_config_falls_back_to_env_fees(self) -> None:
        config = live_config_from_env(
            {
                "LIVE_PRIORITY_FEE_LAMPORTS": "111",
                "LIVE_JITO_TIP_LAMPORTS": "222",
            },
            enabled=False,
            slippage_bps=50,
            max_notional_sol=0.1,
        )
        self.assertEqual(config.priority_fee_lamports, 111)
        self.assertEqual(config.jito_tip_lamports, 222)

    def test_live_config_explicit_zero_overrides_env(self) -> None:
        config = live_config_from_env(
            {
                "LIVE_PRIORITY_FEE_LAMPORTS": "111",
                "LIVE_JITO_TIP_LAMPORTS": "222",
            },
            enabled=False,
            slippage_bps=50,
            max_notional_sol=0.1,
            priority_fee_lamports=0,
            jito_tip_lamports=0,
        )
        self.assertEqual(config.priority_fee_lamports, 0)
        self.assertEqual(config.jito_tip_lamports, 0)

    def test_sign_reports_v1_unsupported(self) -> None:
        executor = LiveSolanaExecutor(
            LiveExecutionConfig(enabled=True, private_key=TEST_PRIVATE_KEY)
        )
        payload = base64.b64encode(b"\x81\x00\x01\x02").decode()
        with self.assertRaises(LiveExecutionError) as caught:
            executor._sign(payload)
        self.assertEqual(caught.exception.reason, "live_v1_unsupported")


class JupiterErrorReasonTests(unittest.TestCase):
    def test_error_code_1_is_insufficient_funds(self) -> None:
        self.assertEqual(
            jupiter_error_reason(1), "live_insufficient_funds"
        )

    def test_error_code_2_is_insufficient_sol_for_gas(self) -> None:
        self.assertEqual(
            jupiter_error_reason(2), "live_insufficient_sol_for_gas"
        )

    def test_error_code_3_is_below_gasless_min(self) -> None:
        self.assertEqual(
            jupiter_error_reason(3), "live_swap_below_gasless_min"
        )

    def test_unknown_code_falls_back_to_no_transaction(self) -> None:
        self.assertEqual(
            jupiter_error_reason(99), "live_order_no_transaction"
        )
        self.assertEqual(
            jupiter_error_reason(None), "live_order_no_transaction"
        )
        self.assertEqual(
            jupiter_error_reason("not-a-number"), "live_order_no_transaction"
        )

    def test_jupiterz_codes_use_router_specific_meanings(self) -> None:
        self.assertEqual(
            jupiter_error_reason(1, "jupiterz"), "live_insufficient_funds"
        )
        self.assertEqual(
            jupiter_error_reason(2, "jupiterz"), "live_missing_token_account"
        )
        self.assertEqual(
            jupiter_error_reason(3, "jupiterz"), "live_order_build_failed"
        )

    def test_router_match_is_case_insensitive(self) -> None:
        self.assertEqual(
            jupiter_error_reason(2, "JupiterZ"), "live_missing_token_account"
        )


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = ""

    def json(self) -> dict:
        if self._payload is None:
            raise ValueError("no JSON body")
        return self._payload


class _FakeHttpClient:
    """Minimal httpx.AsyncClient stand-in for the /order request path."""

    def __init__(self, responses: list[_FakeResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def get(
        self,
        url: str,
        params: dict | None = None,
        headers: dict | None = None,
    ) -> _FakeResponse:
        self.calls.append(
            {
                "url": url,
                "params": dict(params or {}),
                "headers": dict(headers or {}),
            }
        )
        return self._responses.pop(0)


class LiveOrderTests(unittest.IsolatedAsyncioTestCase):
    def executor(
        self, client: _FakeHttpClient, **overrides: object
    ) -> LiveSolanaExecutor:
        config = LiveExecutionConfig(
            enabled=True,
            private_key=TEST_PRIVATE_KEY,
            **overrides,
        )
        return LiveSolanaExecutor(config, client=client)  # type: ignore[arg-type]

    async def test_order_sends_fee_and_version_params(self) -> None:
        client = _FakeHttpClient(
            [_FakeResponse(200, {"transaction": "signed-tx", "router": "metis"})]
        )
        executor = self.executor(
            client, priority_fee_lamports=2000, jito_tip_lamports=10000
        )
        order = await executor._order({"inputMint": "a"}, action="entry")
        self.assertEqual(order["transaction"], "signed-tx")
        params = client.calls[0]["params"]
        self.assertEqual(params["priorityFeeLamports"], "2000")
        self.assertEqual(params["jitoTipLamports"], "10000")
        self.assertEqual(params["maxSupportedTransactionVersion"], "1")
        self.assertIn("taker", params)

    async def test_order_omits_zero_fees(self) -> None:
        client = _FakeHttpClient([_FakeResponse(200, {"transaction": "signed-tx"})])
        executor = self.executor(client)
        await executor._order({"inputMint": "a"}, action="entry")
        params = client.calls[0]["params"]
        self.assertNotIn("priorityFeeLamports", params)
        self.assertNotIn("jitoTipLamports", params)
        self.assertEqual(params["maxSupportedTransactionVersion"], "1")

    async def test_terminal_error_is_not_retried(self) -> None:
        client = _FakeHttpClient(
            [
                _FakeResponse(
                    200,
                    {
                        "transaction": "",
                        "errorCode": 2,
                        "router": "metis",
                        "errorMessage": "no gas",
                    },
                )
            ]
        )
        executor = self.executor(client, retries=3)
        with self.assertRaises(LiveExecutionError) as caught:
            await executor._order({}, action="entry")
        self.assertEqual(caught.exception.reason, "live_insufficient_sol_for_gas")
        self.assertEqual(len(client.calls), 1)

    async def test_jupiterz_error_code_is_router_specific(self) -> None:
        client = _FakeHttpClient(
            [
                _FakeResponse(
                    200,
                    {
                        "transaction": "",
                        "errorCode": 2,
                        "router": "jupiterz",
                        "errorMessage": "missing ata",
                    },
                )
            ]
        )
        executor = self.executor(client, retries=1)
        with self.assertRaises(LiveExecutionError) as caught:
            await executor._order({}, action="entry")
        self.assertEqual(caught.exception.reason, "live_missing_token_account")
        self.assertIn("errorCode=2", caught.exception.detail)

    async def test_transient_error_is_retried(self) -> None:
        client = _FakeHttpClient(
            [
                _FakeResponse(500),
                _FakeResponse(200, {"transaction": "signed-tx"}),
            ]
        )
        executor = self.executor(client, retries=2)
        order = await executor._order({}, action="entry")
        self.assertEqual(order["transaction"], "signed-tx")
        self.assertEqual(len(client.calls), 2)

    async def test_missing_transaction_widens_slippage(self) -> None:
        client = _FakeHttpClient(
            [
                _FakeResponse(
                    200,
                    {"transaction": "", "errorCode": 99, "router": "metis"},
                ),
                _FakeResponse(200, {"transaction": "signed-tx"}),
            ]
        )
        executor = self.executor(client)
        order = await executor._order({"inputMint": "a"}, action="entry")
        self.assertEqual(order["transaction"], "signed-tx")
        self.assertEqual(
            [call["params"]["slippageBps"] for call in client.calls],
            ["50", "100"],
        )
        self.assertEqual(order["_slippage_bps"], 100)


class QuoteRobustnessTests(unittest.IsolatedAsyncioTestCase):
    """A pool seconds old is often unindexed; both sides must survive that."""

    def test_slippage_ladder_is_monotonic_and_capped(self) -> None:
        self.assertEqual(
            slippage_ladder(50, 1000), (50, 100, 200, 400, 800, 1000)
        )
        self.assertEqual(slippage_ladder(0, 100), (50, 100))
        self.assertEqual(slippage_ladder(300, 200), (300,))
        # A zero ceiling stays zero; the old floor silently exceeded it.
        self.assertEqual(slippage_ladder(0, 0), (0,))

    def test_provider_rejects_a_max_below_the_base(self) -> None:
        with self.assertRaises(ValueError):
            JupiterQuoteProvider(
                api_key=None, slippage_bps=200, max_slippage_bps=100
            )

    def test_no_route_is_transient_for_entries_but_terminal_for_exits(self) -> None:
        self.assertTrue(is_transient_quote_error("quote_no_route"))
        self.assertFalse(is_transient_quote_error("quote_bad_request"))
        self.assertTrue(is_terminal_exit_error("quote_no_route"))
        self.assertFalse(is_terminal_exit_error("quote_server_error"))

    def test_zero_out_is_retried_then_terminal_for_an_exit(self) -> None:
        # A seconds-old pool can quote zero once and route on the next print,
        # but a zero sell after the exit timeout means there is nothing there.
        self.assertTrue(is_transient_quote_error("quote_zero_out"))
        self.assertFalse(is_terminal_exit_error("quote_missing_out_amount"))
        self.assertTrue(is_terminal_exit_error("quote_zero_out"))

    async def test_not_ready_is_not_burst_retried_inside_one_call(self) -> None:
        responses = []
        for _ in range(3):
            response = _FakeResponse(400)
            response.text = "pool not indexed"
            responses.append(response)
        client = _FakeHttpClient(responses)
        provider = JupiterQuoteProvider(
            api_key=None,
            retries=3,
            backoff_s=0.0,
            slippage_bps=50,
            max_slippage_bps=1000,
            client=client,  # type: ignore[arg-type]
        )
        with self.assertRaises(QuoteError) as caught:
            await provider._order({"inputMint": "a"}, action="entry")
        self.assertEqual(caught.exception.reason, "quote_not_ready")
        # One attempt per BUY print: the engine retries with a backoff.
        self.assertEqual(len(client.calls), 1)

    async def test_bad_request_does_not_widen_the_ladder(self) -> None:
        response = _FakeResponse(400)
        response.text = "invalid amount"
        client = _FakeHttpClient([response, response, response])
        provider = JupiterQuoteProvider(
            api_key=None,
            retries=1,
            slippage_bps=50,
            max_slippage_bps=1000,
            client=client,  # type: ignore[arg-type]
        )
        with self.assertRaises(QuoteError) as caught:
            await provider._order_with_escalation(
                {"inputMint": "a"}, action="exit"
            )
        self.assertEqual(caught.exception.reason, "quote_bad_request")
        self.assertEqual(len(client.calls), 1)

    async def test_entry_quote_widens_slippage_until_routed(self) -> None:
        client = _FakeHttpClient(
            [
                _FakeResponse(200, {"transaction": None}),
                _FakeResponse(
                    200, {"transaction": None, "outAmount": "2000000"}
                ),
            ]
        )
        provider = JupiterQuoteProvider(
            api_key=None,
            retries=1,
            slippage_bps=50,
            max_slippage_bps=1000,
            client=client,  # type: ignore[arg-type]
        )
        quote = await provider.entry_quote("mintpump", 0.05, 0.01, decimals=6)
        self.assertEqual(
            [call["params"]["slippageBps"] for call in client.calls],
            ["50", "100"],
        )
        self.assertEqual(quote.slippage_bps, 100)

    async def test_pump_mint_decimals_fall_back_when_unindexed(self) -> None:
        client = _FakeHttpClient([_FakeResponse(404)])
        resolver = TokenMetadataResolver(
            api_key=None, client=client  # type: ignore[arg-type]
        )
        self.assertEqual(await resolver.decimals("brandnewpump"), 6)
        self.assertEqual(await resolver.decimals("brandnewpump"), 6)
        self.assertEqual(len(client.calls), 1)

    async def test_unknown_non_pump_decimals_still_fail(self) -> None:
        client = _FakeHttpClient([_FakeResponse(404)])
        resolver = TokenMetadataResolver(
            api_key=None, client=client  # type: ignore[arg-type]
        )
        with self.assertRaises(QuoteError) as caught:
            await resolver.decimals("someothermint")
        self.assertEqual(caught.exception.reason, "token_metadata_http")


class _FakeExecutor:
    def __init__(self) -> None:
        self.config = LiveExecutionConfig(enabled=True, private_key="x")
        self.orders: list[tuple[dict[str, str], str]] = []
        self.metadata = self

    async def _order(self, params: dict[str, str], *, action: str) -> dict:
        self.orders.append((params, action))
        return {}

    async def decimals(self, mint: str, event_decimals: int | None = None) -> int:
        return 6

    async def buy(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("preflight must not buy")

    async def sell(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("preflight must not sell")


class LiveQuoteProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_preflight_only_builds_an_order(self) -> None:
        executor = _FakeExecutor()
        provider = LiveQuoteProvider(executor)  # type: ignore[arg-type]
        quote = await provider.exit_quote(
            MINT, 1_000.0, 0.01, decimals=6, action="preflight"
        )
        self.assertEqual(quote.source, "live_preflight")
        self.assertEqual(len(executor.orders), 1)
        params, action = executor.orders[0]
        self.assertEqual(action, "preflight")
        self.assertEqual(params["amount"], "1000000000")


class StoreEvidenceTests(unittest.TestCase):
    def test_raw_quotes_are_json_encoded_for_stable_parquet(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = PaperStore(Path(tmp))
            store.record_trade_payload(
                {
                    "mint": "mint",
                    "entry_ts_ms": 1_000,
                    "exit_ts_ms": 2_000,
                    "pnl_sol": 0.0,
                    "return_pct": 0.0,
                    "entry_quote_price_impact_pct": -42.14,
                    "entry_quote_router": "metis",
                    "entry_quote_raw": {
                        "priceImpact": -42.14,
                        "priceImpactPct": "-0.4214",
                        "inUsdValue": 12.5,
                        "outUsdValue": 7.2,
                        "swapUsdValue": 12.5,
                        "routePlan": [{"swapInfo": {"label": "metis"}}],
                    },
                    "exit_quote_raw": {"outAmount": "1"},
                    "exit_quote_raws": [{"outAmount": "1"}],
                }
            )
            parquet = store.finalize()
            self.assertIsNotNone(parquet)
            payload = json.loads(
                (Path(tmp) / "trades.jsonl").read_text().splitlines()[-1]
            )
            self.assertIsInstance(payload["entry_quote_raw"], str)
            self.assertIsInstance(payload["exit_quote_raws"], str)
            self.assertEqual(
                json.loads(payload["entry_quote_raw"])["routePlan"][0][
                    "swapInfo"
                ]["label"],
                "metis",
            )
            audit = json.loads(payload["entry_quote_audit"])
            self.assertEqual(audit["mint"], "mint")
            self.assertEqual(audit["quoted_at_ms"], 1_000)
            self.assertEqual(audit["price_impact"], -42.14)
            self.assertEqual(audit["price_impact_pct"], "-0.4214")
            self.assertEqual(audit["normalized_price_impact_pct"], -42.14)
            self.assertEqual(audit["in_usd_value"], 12.5)
            self.assertEqual(audit["out_usd_value"], 7.2)
            self.assertEqual(audit["swap_usd_value"], 12.5)
            self.assertEqual(audit["router"], "metis")

    def test_exit_fill_evidence_is_json_encoded_and_mixed_runs_finalize(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = PaperStore(Path(tmp))
            store.record_trade_payload(
                {
                    "mint": "mint-a",
                    "entry_ts_ms": 1,
                    "exit_ts_ms": 2,
                    "pnl_sol": 0.0,
                    "return_pct": 0.0,
                    "exit_trigger_source": "pumpapi_trade",
                    "exit_reference_kind": "trigger_observation",
                    "exit_quote_fractions": [0.5, 1.0],
                    "exit_full_size": False,
                    "exit_fill_evidence": [
                        {"fraction": 0.5, "out_amount": 10},
                        {"fraction": 1.0, "out_amount": 20},
                    ],
                    "quote_guard_ratio": 0.5,
                    "quote_guard_checks": 2,
                    "quote_guard_unavailable": 1,
                    "quote_guard_min_ratio": 0.31,
                    "quote_guard_evidence": [
                        {"ts_ms": 1, "status": "unavailable", "ratio": None},
                        {"ts_ms": 2, "status": "ok", "ratio": 0.31},
                    ],
                }
            )
            # A trade with no fills must not change the parquet schema.
            store.record_trade_payload(
                {
                    "mint": "mint-b",
                    "entry_ts_ms": 3,
                    "exit_ts_ms": 4,
                    "pnl_sol": 0.0,
                    "return_pct": 0.0,
                    "exit_quote_fractions": [],
                    "exit_full_size": None,
                    "exit_fill_evidence": [],
                }
            )
            parquet = store.finalize()
            self.assertIsNotNone(parquet)
            frame = pl.read_parquet(parquet)
            self.assertEqual(frame.height, 2)
            self.assertEqual(
                frame["exit_quote_fractions"].to_list(), [[0.5, 1.0], []]
            )
            self.assertEqual(
                frame["exit_trigger_source"].to_list(),
                ["pumpapi_trade", None],
            )
            rows = [
                json.loads(line)
                for line in (Path(tmp) / "trades.jsonl")
                .read_text()
                .splitlines()
            ]
            self.assertIsInstance(rows[0]["exit_fill_evidence"], str)
            self.assertEqual(
                [
                    leg["fraction"]
                    for leg in json.loads(rows[0]["exit_fill_evidence"])
                ],
                [0.5, 1.0],
            )
            self.assertFalse(rows[0]["exit_full_size"])
            self.assertIsInstance(rows[0]["quote_guard_evidence"], str)
            self.assertEqual(
                [
                    row["status"]
                    for row in json.loads(rows[0]["quote_guard_evidence"])
                ],
                ["unavailable", "ok"],
            )
            self.assertEqual(rows[0]["quote_guard_checks"], 2)
            self.assertEqual(rows[0]["quote_guard_unavailable"], 1)

    def test_summary_ledger_reconciles_entries_trades_and_open_positions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = PaperStore(Path(tmp))
            position = Position(
                signal=signal({"rule": SCANNER_RULE}),
                mint=MINT,
                entry_ts_ms=SIGNAL_TS + 5_000,
                entry_price_sol=0.02,
                size=5_000.0,
                entry_value_sol=0.1,
                entry_fee_sol=0.00125,
                entry_impact=0.0,
                horizon_ts_ms=SIGNAL_TS + 305_000,
            )
            store.record_entry(position)
            store.record_trade_payload(
                {
                    "mint": MINT,
                    "entry_ts_ms": SIGNAL_TS + 5_000,
                    "exit_ts_ms": SIGNAL_TS + 305_000,
                    "pnl_sol": 0.0,
                    "return_pct": 0.0,
                }
            )
            store.finalize(extra={"open_positions": 0})
            summary = json.loads((Path(tmp) / "summary.json").read_text())
            self.assertEqual(
                summary["ledger"],
                {
                    "entries": 1,
                    "closed_trades": 1,
                    "open_positions": 0,
                    "balanced": True,
                },
            )

    def test_entry_audit_pins_quote_to_mint_and_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = PaperStore(Path(tmp))
            position = Position(
                signal=signal({"rule": SCANNER_RULE}),
                mint=MINT,
                entry_ts_ms=SIGNAL_TS + 5_000,
                entry_price_sol=0.02,
                size=5_000.0,
                entry_value_sol=0.1,
                entry_fee_sol=0.00125,
                entry_impact=0.0,
                horizon_ts_ms=SIGNAL_TS + 305_000,
                entry_quote=FillQuote(
                    price_sol=0.02,
                    source="jupiter",
                    price_impact_pct=-100.0,
                    router="metis",
                    quoted_at_ms=SIGNAL_TS + 4_900,
                    raw={
                        "priceImpact": -100.0,
                        "priceImpactPct": "-1.0",
                        "inUsdValue": 12.5,
                        "outUsdValue": 0.0,
                        "swapUsdValue": 12.5,
                        "router": "metis",
                    },
                ),
            )
            store.record_entry(position)
            payload = json.loads(
                (Path(tmp) / "entries.jsonl").read_text().splitlines()[-1]
            )
            audit = json.loads(payload["quote_audit"])
            self.assertEqual(audit["mint"], MINT)
            self.assertEqual(audit["quoted_at_ms"], SIGNAL_TS + 4_900)
            self.assertEqual(audit["price_impact"], -100.0)
            self.assertEqual(audit["price_impact_pct"], "-1.0")
            self.assertEqual(audit["normalized_price_impact_pct"], -100.0)
            self.assertEqual(audit["in_usd_value"], 12.5)
            self.assertEqual(audit["out_usd_value"], 0.0)
            self.assertEqual(audit["swap_usd_value"], 12.5)
            self.assertEqual(audit["router"], "metis")


class PreflightEconomicsTests(unittest.IsolatedAsyncioTestCase):
    """Execution-sanity guard: a routable sell is not necessarily usable."""

    NOW_MS = SIGNAL_TS

    class FakeQuotes:
        def __init__(self, *, entry_price: float, exit_price: float) -> None:
            self.entry_price = entry_price
            self.exit_price = exit_price
            self.actions: list[str] = []
            self.exit_calls = 0

        async def entry_quote(
            self,
            mint: str,
            notional_sol: float,
            reference_price_sol: float | None,
            *,
            decimals: int | None = None,
        ) -> FillQuote:
            return FillQuote(price_sol=self.entry_price, source="test")

        async def exit_quote(
            self,
            mint: str,
            size_tokens: float,
            reference_price_sol: float | None,
            *,
            decimals: int | None = None,
            action: str = "exit",
        ) -> FillQuote:
            self.exit_calls += 1
            self.actions.append(action)
            return FillQuote(price_sol=self.exit_price, source="test")

    def make_engine(
        self,
        quotes: "PreflightEconomicsTests.FakeQuotes",
        *,
        minimum: float = 0.90,
    ) -> PaperEngine:
        wallet = PaperWallet(WalletConfig(starting_sol=10.0))
        broker = PaperBroker(
            BrokerConfig(notional_sol=0.1, fee=0.0125, slippage=0.0),
            wallet=wallet,
        )
        return PaperEngine(
            config=EngineConfig(
                entry_delay_ms=5_000,
                max_entry_delay_ms=60_000,
                live=True,
                preflight_exit_route=True,
                min_preflight_exit_value_ratio=minimum,
                horizon_ms=300_000,
            ),
            broker=broker,
            quotes=quotes,
            history=MarketHistory(),
            gate=GateConfig(rule="baseline"),
            store=None,
            clock=lambda: self.NOW_MS,
        )

    def signal(self, features: dict[str, float | None] | None = None) -> Signal:
        return Signal(
            token_address=MINT,
            signal_ts_ms=self.NOW_MS - 10_000,
            received_ts_ms=self.NOW_MS,
            channel="@test",
            message_id=1,
            network="solana",
            address_status="confirmed",
            address_type="mint",
            message_type="buy",
            is_buy_signal=True,
            is_confirmed_buy_signal=True,
            features=dict(features or {}),
        )

    async def enter(
        self,
        engine: PaperEngine,
        signal: Signal | None = None,
    ) -> None:
        await engine.on_signal(signal or self.signal())
        await engine.on_trade(
            TradeEvent(
                mint=MINT,
                timestamp_ms=self.NOW_MS,
                event_index=0,
                action="buy",
                price_sol=0.02,
                token_amount=100.0,
                quote_amount=2.0,
                quote_mint=MINT,
            ),
            await_actions=True,
        )

    async def test_acceptable_route_opens_the_position(self) -> None:
        quotes = self.FakeQuotes(entry_price=0.02, exit_price=0.02)
        engine = self.make_engine(quotes)
        before = engine.broker.wallet.sol_balance
        await self.enter(engine)
        self.assertIn(MINT, engine.positions)
        self.assertLess(engine.broker.wallet.sol_balance, before)
        self.assertEqual(quotes.actions, ["preflight"])
        # The paper path must never drift onto the exit action for preflight.
        self.assertNotIn("exit", quotes.actions)

    async def test_poor_route_is_rejected_before_the_wallet_is_debited(self) -> None:
        # A routable quote returning ~5 % of the entry cost: the old check only
        # proved a route existed, so this used to open a position.
        quotes = self.FakeQuotes(entry_price=0.02, exit_price=0.001)
        engine = self.make_engine(quotes)
        before = engine.broker.wallet.sol_balance
        await self.enter(engine)
        self.assertNotIn(MINT, engine.positions)
        self.assertNotIn(MINT, engine.pending)
        self.assertEqual(engine.broker.wallet.sol_balance, before)
        self.assertEqual(engine.skips[-1].reason, "entry_no_exit_economics")
        detail = engine.skips[-1].detail or ""
        self.assertIn("route=available", detail)
        self.assertIn("min_ratio=0.9000", detail)
        self.assertIn("ratio=", detail)
        # Only the preflight quote was requested; no fill was executed.
        self.assertEqual(quotes.actions, ["preflight"])

    async def test_zero_exit_output_fails_closed(self) -> None:
        quotes = self.FakeQuotes(entry_price=0.02, exit_price=0.0)
        engine = self.make_engine(quotes)
        await self.enter(engine)
        self.assertNotIn(MINT, engine.positions)
        self.assertEqual(engine.skips[-1].reason, "entry_no_exit_economics")

    async def test_exact_threshold_passes_and_just_below_rejects(self) -> None:
        entry_cost = 0.1 * (1 + 0.0125)
        # net = size * price * (1-fee); solve for net == minimum * entry_cost.
        size = 0.1 / 0.02
        price_at_threshold = (0.90 * entry_cost) / (size * (1 - 0.0125))
        quotes = self.FakeQuotes(
            entry_price=0.02, exit_price=price_at_threshold
        )
        engine = self.make_engine(quotes)
        await self.enter(engine)
        self.assertIn(MINT, engine.positions, "ratio == minimum must pass")

        below = self.FakeQuotes(
            entry_price=0.02, exit_price=price_at_threshold * 0.99
        )
        engine = self.make_engine(below)
        await self.enter(engine)
        self.assertNotIn(MINT, engine.positions)
        self.assertEqual(
            engine.skips[-1].reason, "entry_no_exit_economics"
        )

    async def test_exits_still_run_while_entries_are_blocked(self) -> None:
        quotes = self.FakeQuotes(entry_price=0.02, exit_price=0.02)
        engine = self.make_engine(quotes)
        await self.enter(engine)
        self.assertIn(MINT, engine.positions)

        # Snapshot writes have failed: no new entries, but the open position
        # must still be managed and closed at its horizon.
        engine._persistence_healthy = False
        engine._persistence_degraded = True
        await engine.tick(now_ms=self.NOW_MS + 300_001, await_actions=True)

        self.assertNotIn(MINT, engine.positions)
        self.assertEqual(len(engine.closed), 1)
        self.assertEqual(engine.closed[-1].exit_reason, "horizon")
        self.assertIn("exit", quotes.actions)

    async def test_persistence_failure_during_entry_quote_blocks_reserve(
        self,
    ) -> None:
        # The signal is accepted while persistence is healthy, then a snapshot
        # write fails while the entry quote is in flight. The final check
        # inside the mint lock must stop the reserve even though the earlier
        # health check passed.
        class BlockingQuotes(self.FakeQuotes):
            def __init__(self) -> None:
                super().__init__(entry_price=0.02, exit_price=0.02)
                self.started = asyncio.Event()
                self.release = asyncio.Event()

            async def entry_quote(
                self,
                mint: str,
                notional_sol: float,
                reference_price_sol: float | None,
                *,
                decimals: int | None = None,
            ) -> FillQuote:
                self.started.set()
                await self.release.wait()
                return await super().entry_quote(
                    mint,
                    notional_sol,
                    reference_price_sol,
                    decimals=decimals,
                )

        quotes = BlockingQuotes()
        engine = self.make_engine(quotes)
        before = engine.broker.wallet.sol_balance
        task = asyncio.create_task(self.enter(engine))
        await asyncio.wait_for(quotes.started.wait(), timeout=2)

        # Persistence degrades while the quote request is waiting.
        engine._persistence_healthy = False
        engine._persistence_degraded = True
        quotes.release.set()
        await asyncio.wait_for(task, timeout=2)

        self.assertNotIn(MINT, engine.positions)
        self.assertNotIn(MINT, engine.pending)
        self.assertEqual(engine.broker.wallet.sol_balance, before)
        self.assertEqual(engine.skips[-1].reason, "persistence_degraded")
        self.assertIn("in flight", engine.skips[-1].detail or "")
        self.assertNotIn("exit", quotes.actions)

    async def test_entry_liquidity_records_only_the_observed_mark(self) -> None:
        quotes = self.FakeQuotes(entry_price=0.02, exit_price=0.02)
        engine = self.make_engine(quotes)
        engine._mark_liquidity[MINT] = 51_234.0
        await self.enter(
            engine,
            self.signal({"liquidity_usd": 45_000.0}),
        )
        position = engine.positions[MINT]
        self.assertEqual(position.signal_liquidity_usd, 45_000.0)
        self.assertEqual(position.entry_liquidity_usd, 51_234.0)

    async def test_entry_liquidity_is_none_without_a_mark(self) -> None:
        quotes = self.FakeQuotes(entry_price=0.02, exit_price=0.02)
        engine = self.make_engine(quotes)
        await self.enter(
            engine,
            self.signal({"liquidity_usd": 45_000.0}),
        )
        position = engine.positions[MINT]
        self.assertEqual(position.signal_liquidity_usd, 45_000.0)
        self.assertIsNone(position.entry_liquidity_usd)

    def test_fee_and_slippage_are_applied_once_per_side(self) -> None:
        ratio = preflight_exit_value_ratio(
            entry_cost_sol=0.10125,
            size_tokens=5.0,
            exit_price_sol=0.02,
            fee=0.0125,
            slippage=0.0,
        )
        assert ratio is not None
        self.assertAlmostEqual(ratio, 0.09875 / 0.10125, places=12)

    def test_invalid_inputs_return_none(self) -> None:
        cases = [
            dict(entry_cost_sol=0.0, exit_price_sol=0.02),
            dict(entry_cost_sol=0.1, exit_price_sol=None),
            dict(entry_cost_sol=0.1, exit_price_sol=0.0),
            dict(entry_cost_sol=0.1, exit_price_sol=float("nan")),
        ]
        for case in cases:
            with self.subTest(case=case):
                self.assertIsNone(
                    preflight_exit_value_ratio(
                        size_tokens=5.0,
                        fee=0.0125,
                        slippage=0.0,
                        **case,
                    )
                )

    def test_config_rejects_an_out_of_range_ratio(self) -> None:
        for value in (0.0, -0.1, 1.5):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    EngineConfig(
                        live=True,
                        min_preflight_exit_value_ratio=value,
                    )


class PriceImpactNormalizationTests(unittest.TestCase):
    """Jupiter /order ``priceImpact`` is percentage points, not a ratio."""

    def test_percentage_points_are_preserved(self) -> None:
        self.assertEqual(
            normalize_price_impact_pct({"priceImpact": 1.25}), 1.25
        )
        self.assertEqual(
            normalize_price_impact_pct({"priceImpact": 0.5}), 0.5
        )
        self.assertEqual(normalize_price_impact_pct({"priceImpact": 0}), 0.0)

    def test_deprecated_decimal_ratio_is_converted(self) -> None:
        # ``priceImpactPct`` is documented as a decimal ratio.
        self.assertEqual(
            normalize_price_impact_pct({"priceImpactPct": "0.0125"}), 1.25
        )

    def test_primary_field_wins_when_both_are_present(self) -> None:
        self.assertEqual(
            normalize_price_impact_pct(
                {"priceImpact": -0.25, "priceImpactPct": "0.5"}
            ),
            -0.25,
        )

    def test_missing_or_malformed_values_are_unavailable(self) -> None:
        for payload in (
            {},
            {"priceImpact": None},
            {"priceImpact": "abc"},
            {"priceImpact": float("nan")},
            {"priceImpactPct": "not-a-number"},
        ):
            with self.subTest(payload=payload):
                self.assertIsNone(normalize_price_impact_pct(payload))


class ReportTests(unittest.TestCase):
    def startup(self, mode: str) -> str:
        return format_startup(
            rule="hot_money_continuation",
            horizon_min=5,
            notional_sol=0.1,
            starting_sol=10,
            fee=0.0125,
            slippage=0.0,
            channels=["@ave"],
            session_file="bot.session",
            preflight_exit_route=False,
            mode=mode,
        )

    def test_paper_banner_is_explicit(self) -> None:
        text = self.startup("paper")
        self.assertIn("PAPER", text)
        self.assertIn("signed or broadcast", text)

    def test_live_banner_warns_about_broadcast(self) -> None:
        text = self.startup("live")
        self.assertIn("LIVE", text)
        self.assertIn("signed locally", text)
        self.assertNotIn("Paper trading only", text)

    def test_resumed_startup_separates_configured_and_recovered_capital(self) -> None:
        text = format_startup(
            rule="scanner_pumpfun_amm",
            horizon_min=5,
            notional_sol=0.1,
            starting_sol=10.0,
            fee=0.0125,
            slippage=0.0,
            channels=["@ave"],
            session_file="bot.session",
            preflight_exit_route=False,
            mode="paper",
            recovered_cash_sol=9.979139,
            resumed_at="2026-11-23T12:00:00+00:00",
            recovery_status="recovered",
            open_positions=2,
        )
        self.assertIn("Configured initial capital", text)
        self.assertIn("10.000000", text)
        self.assertIn("Recovered cash", text)
        self.assertIn("9.979139", text)
        self.assertIn("Open positions", text)
        self.assertIn("`2`", text)
        self.assertIn("Recovery", text)
        self.assertIn("recovered", text)
        self.assertIn("2026-11-23T12:00:00+00:00", text)
        # Equity cannot be computed without fresh marks; it must not silently
        # show cash-only equity as if the restored tokens were worth zero.
        self.assertIn("Unavailable", text)

    def test_fresh_startup_reports_configured_capital_and_zero_positions(self) -> None:
        text = format_startup(
            rule="scanner_pumpfun_amm",
            horizon_min=5,
            notional_sol=0.1,
            starting_sol=10.0,
            fee=0.0125,
            slippage=0.0,
            channels=["@ave"],
            session_file="bot.session",
            preflight_exit_route=True,
            mode="paper",
            recovered_cash_sol=None,
            recovery_status="fresh",
            open_positions=0,
            equity_sol=10.0,
            min_preflight_exit_value_ratio=0.9,
        )
        self.assertIn("Configured initial capital", text)
        self.assertIn("`10.000000 SOL`", text)
        self.assertIn("Recovery:** `fresh`", text)
        self.assertIn("Open positions:** `0`", text)
        self.assertIn("Equity:** `10.000000 SOL`", text)
        self.assertIn("min exit value 0.90x", text)
        self.assertNotIn("Recovered cash", text)

    def test_open_positions_render_against_the_configured_cap(self) -> None:
        text = format_startup(
            rule="scanner_pumpfun_amm",
            horizon_min=5,
            notional_sol=0.1,
            starting_sol=10.0,
            fee=0.0125,
            slippage=0.0,
            channels=["@ave"],
            session_file="bot.session",
            preflight_exit_route=True,
            mode="paper",
            recovery_status="fresh",
            open_positions=3,
            max_positions=10,
        )
        self.assertIn("Open positions:** `3 / 10`", text)

    def test_status_reports_open_positions_against_the_cap(self) -> None:
        text = format_status(
            stats=ReporterStats(starting_sol=10.0),
            wallet=None,
            positions=[],
            pending=0,
            rule="scanner_pumpfun_amm",
            max_positions=10,
        )
        self.assertIn("Open positions:** `0 / 10`", text)

    def test_reporter_reads_open_and_max_positions_from_engine(self) -> None:
        class _Config:
            max_concurrent_positions = 10

        class _Engine:
            config = _Config()
            positions = {"a": object(), "b": object()}

        reporter = TelegramReporter(ReporterConfig(enabled=False))
        reporter.bind(engine=_Engine())
        self.assertEqual(reporter._position_counts(), (2, 10))

    def test_entry_notification_does_not_copy_signal_liquidity(self) -> None:
        position = Position(
            signal=signal(
                {
                    "rule": SCANNER_RULE,
                    "dex": "Pumpfunamm",
                    "liquidity_usd": 45_000.0,
                }
            ),
            mint=MINT,
            entry_ts_ms=SIGNAL_TS + 5_000,
            entry_price_sol=0.02,
            size=5_000.0,
            entry_value_sol=0.1,
            entry_fee_sol=0.00125,
            entry_impact=0.0,
            horizon_ts_ms=SIGNAL_TS + 305_000,
            entry_quote=FillQuote(
                price_sol=0.02,
                source="jupiter",
                price_impact_pct=1.25,
            ),
            signal_liquidity_usd=45_000.0,
            entry_liquidity_usd=None,
        )
        text = format_entry(position, None, None)
        self.assertIn("fill `Not observed`", text)
        self.assertNotIn("fill `$45,000`", text)
        # Paper slippage is the configured model, not an observed on-chain fill.
        self.assertIn("Modeled slippage", text)
        self.assertIn("Price impact", text)
        self.assertIn("1.25%", text)
        capped = format_entry(position, None, None, open_positions=4, max_positions=10)
        self.assertIn("Open positions:** `4 / 10`", capped)

    def test_scanner_evidence_shows_only_scanner_gates(self) -> None:
        text = "\n".join(
            _signal_evidence(
                {
                    "rule": SCANNER_RULE,
                    "dex": "Pumpfunamm",
                    "liquidity_usd": 45_000.0,
                    "mcap_usd": 0.0,
                    "hist_flow_1m_sol": 1.2,
                }
            )
        )
        self.assertIn("DEX", text)
        self.assertIn("Pumpfunamm", text)
        self.assertIn("Launch liquidity", text)
        self.assertIn("$45,000", text)
        self.assertIn("· pass", text)
        # The scanner rule has no flow/volume gate; the old text claimed one.
        self.assertNotIn("gate `> 9.46`", text)
        self.assertIn("context, no gate", text)

    def test_scanner_evidence_marks_out_of_range_liquidity_failed(self) -> None:
        text = "\n".join(
            _signal_evidence(
                {
                    "rule": SCANNER_RULE,
                    "dex": "Pumpfunamm",
                    "liquidity_usd": 5_000.0,
                }
            )
        )
        self.assertIn("Launch liquidity", text)
        self.assertIn("· fail", text)

    def test_tp50_variant_keeps_the_scanner_gate_labels(self) -> None:
        text = "\n".join(
            _signal_evidence(
                {
                    "rule": SCANNER_TP50_RULE,
                    "dex": "Pumpfunamm",
                    "liquidity_usd": 45_000.0,
                }
            )
        )
        self.assertIn(f"• Rule: `{SCANNER_TP50_RULE}`", text)
        self.assertIn("Launch liquidity", text)
        self.assertIn("· pass", text)
        self.assertNotIn("gate `> 9.46`", text)

    def test_flow_gate_label_only_for_hot_money_rule(self) -> None:
        features = {"hist_flow_1m_sol": 1.2, "total_buy_sol": 1.0}
        baseline = "\n".join(
            _signal_evidence({**features, "rule": "baseline"})
        )
        hot_money = "\n".join(
            _signal_evidence({**features, "rule": "hot_money_continuation"})
        )
        self.assertNotIn("gate `> 9.456 SOL`", baseline)
        self.assertIn("(context)", baseline)
        self.assertIn("gate `> 9.456 SOL`", hot_money)
        self.assertIn("gate `> 3 SOL`", hot_money)
        # Both values are below their gates, so the evidence must show it.
        self.assertIn("· fail", hot_money)

    def test_hot_money_evidence_shows_dip_and_crowd_gates(self) -> None:
        text = "\n".join(
            _signal_evidence(
                {
                    "rule": "hot_money_continuation",
                    "hist_flow_1m_sol": 12.0,
                    "total_buy_sol": 4.0,
                    "hist_pre_ret_15m": 3.5,
                    "hist_signers_5m": 200,
                    "hist_trades_5m": 700,
                }
            )
        )
        self.assertIn("gate `<= 2` · fail", text)
        self.assertIn("gates `<= 147` / `<= 611` · fail", text)
        self.assertIn("gate `> 9.456 SOL` · pass", text)
        self.assertIn("gate `> 3 SOL` · pass", text)

    def test_unknown_rule_evidence_has_no_invented_gates(self) -> None:
        text = "\n".join(
            _signal_evidence(
                {
                    "rule": "some_future_rule",
                    "hist_flow_1m_sol": 100.0,
                    "total_buy_sol": 100.0,
                    "hist_signers_5m": 1,
                    "hist_trades_5m": 1,
                }
            )
        )
        self.assertNotIn("gate `", text)
        self.assertIn("(context)", text)


if __name__ == "__main__":
    unittest.main()
