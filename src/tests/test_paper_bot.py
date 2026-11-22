"""Focused tests for the Telegram live/paper bot.

The research-facing modules are covered elsewhere; these tests pin the pieces
that decide whether real SOL can move: the deployed rule, the causal history
features, the live-execution guards, and the paper/live report wording.
"""

from __future__ import annotations

import base64
import sys
import unittest
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parents[1]
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from paper_bot.execution import (  # noqa: E402
    LiveExecutionConfig,
    LiveExecutionError,
    LiveQuoteProvider,
    LiveSolanaExecutor,
    jupiter_error_reason,
    live_config_from_env,
)
from paper_bot.history import MarketHistory  # noqa: E402
from paper_bot.models import Signal, TradeEvent  # noqa: E402
from paper_bot.notifier import format_startup  # noqa: E402
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

    def test_card_liquidity_is_judged_against_the_rule(self) -> None:
        result = parse_signal_text(
            SCANNER_CARD,
            signal_ts_ms=SIGNAL_TS,
            received_ts_ms=SIGNAL_TS,
        )
        decision = evaluate_gate(result, GateConfig(rule=SCANNER_RULE))
        self.assertFalse(decision.allowed)
        self.assertIn("rule_liquidity_below_20000", decision.reasons)


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

    def test_no_route_is_transient_for_entries_but_terminal_for_exits(self) -> None:
        self.assertTrue(is_transient_quote_error("quote_no_route"))
        self.assertFalse(is_transient_quote_error("quote_bad_request"))
        self.assertTrue(is_terminal_exit_error("quote_no_route"))
        self.assertFalse(is_terminal_exit_error("quote_server_error"))

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
