"""Focused tests for the Telegram live/paper bot.

The research-facing modules are covered elsewhere; these tests pin the pieces
that decide whether real SOL can move: the deployed rule, the causal history
features, the live-execution guards, and the paper/live report wording.
"""

from __future__ import annotations

import base64
import json
import sys
import tempfile
import unittest
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parents[1]
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from paper_bot.broker import BrokerConfig, PaperBroker  # noqa: E402
from paper_bot.engine import EngineConfig, PaperEngine, entry_backoff_active  # noqa: E402
from paper_bot.wallet import PaperWallet, WalletConfig  # noqa: E402
from paper_bot.execution import (  # noqa: E402
    LiveExecutionConfig,
    LiveExecutionError,
    LiveQuoteProvider,
    LiveSolanaExecutor,
    jupiter_error_reason,
    live_config_from_env,
)
from paper_bot.feeds import PumpApiFeed, parse_pump_event  # noqa: E402
from paper_bot.history import MarketHistory  # noqa: E402
from paper_bot.models import (  # noqa: E402
    PendingEntry,
    Signal,
    TradeEvent,
    canonical_quote_notional_sol,
)
from paper_bot.notifier import (  # noqa: E402
    ReporterConfig,
    TelegramReporter,
    format_startup,
)
from paper_bot.quotes import (  # noqa: E402
    QuoteError,
    JupiterQuoteProvider,
    TokenMetadataResolver,
    is_terminal_exit_error,
    is_transient_quote_error,
    slippage_ladder,
)
from paper_bot.signal_parser import (  # noqa: E402
    parse_scanner_card,
    parse_signal_message,
)
from paper_bot.store import PaperStore  # noqa: E402
from paper_bot.signals import (  # noqa: E402
    SCANNER_RULE,
    GateConfig,
    evaluate_gate,
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


class LiveGuardTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
