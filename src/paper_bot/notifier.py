"""Telegram reporting bot for the paper trader.

Uses ``python-telegram-bot`` for delivery and ``telegramify_markdown`` for
Telegram-safe formatting. The reporter is deliberately best-effort: a Telegram
outage must never stop the trading loop, so every send is wrapped and failures
are logged.

Messages are plain Markdown (no tables) with stable icons so they render the
same in Telegram Desktop, Android and iOS.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import telegramify_markdown
from telegram import Bot, LinkPreviewOptions, MessageEntity
from telegram.error import RetryAfter, TelegramError
from telegram.ext import Application, CommandHandler

from .models import ClosedTrade, Position, Signal
from .signals import (
    HOT_MONEY_ALERT_TOTAL_BUY_SOL,
    HOT_MONEY_DIP_PRE_RET_MAX,
    HOT_MONEY_FLOW_1M_SOL,
    HOT_MONEY_SIGNERS_5M_MAX,
    HOT_MONEY_TRADES_5M_MAX,
    SCANNER_ALLOWED_DEX,
    SCANNER_MAX_LIQUIDITY_USD,
    SCANNER_MIN_LIQUIDITY_USD,
    SCANNER_RULE,
    normalize_dex,
)

LOGGER = logging.getLogger(__name__)

DEFAULT_MIN_GAP_S = 1.1
MAX_MESSAGE_CHARS = 4_000

REASON_ICONS = {
    "take_profit": "🎯",
    "stop_loss": "🛑",
    "horizon": "⏰",
    "shutdown": "🔌",
    "replay_truncated": "✂️",
    "no_route": "🚫",
}
STATUS_ICONS = {
    "tp_hit": "🎯",
    "sl_hit": "🛑",
    "horizon_complete": "⏰",
    "replay_truncated": "✂️",
    "forced_shutdown": "🔌",
    "forced_no_price": "⚠️",
    "no_route": "🚫",
    "near_zero_exit": "🕳️",
}


# ---------------------------------------------------------------------------
# Formatting helpers (pure functions, easy to test)
# ---------------------------------------------------------------------------
def fmt_sol(value: float | None, digits: int = 6) -> str:
    if value is None:
        return "—"
    return f"{value:,.{digits}f}"


def fmt_signed_sol(value: float | None, digits: int = 6) -> str:
    if value is None:
        return "—"
    sign = "+" if value >= 0 else "−"
    return f"{sign}{abs(value):,.{digits}f}"


def fmt_pct(value: float | None) -> str:
    if value is None:
        return "—"
    sign = "+" if value >= 0 else "−"
    return f"{sign}{abs(value) * 100:,.2f}%"


def fmt_price(value: float | None) -> str:
    if value is None or value <= 0:
        return "—"
    if value >= 0.001:
        return f"{value:,.8f}".rstrip("0").rstrip(".")
    return f"{value:.6e}"


def fmt_int(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:,.4f}".rstrip("0").rstrip(".")


def fmt_duration(ms: int | None) -> str:
    if ms is None or ms < 0:
        return "—"
    seconds = int(round(ms / 1000))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {seconds:02d}s"
    if minutes:
        return f"{minutes}m {seconds:02d}s"
    return f"{seconds}s"


def fmt_ts(ts_ms: int | None) -> str:
    if ts_ms is None:
        return "—"
    value = datetime.fromtimestamp(ts_ms / 1000, tz=UTC)
    return value.strftime("%Y-%m-%d %H:%M:%S UTC")


def symbol_of(signal: Signal) -> str:
    value = signal.ticker or signal.features.get("symbol")
    return str(value).upper() if value else "UNKNOWN"


def symbol_of_trade(trade: ClosedTrade) -> str:
    value = trade.features.get("symbol")
    return str(value).upper() if value else "UNKNOWN"


def platform_of(signal: Signal) -> str | None:
    value = signal.features.get("platform")
    return str(value) if value else None


def platform_of_trade(trade: ClosedTrade) -> str | None:
    value = trade.features.get("platform")
    return str(value) if value else None


def channel_of(signal: Signal) -> str:
    value = signal.channel or "unknown"
    return value if value.startswith("@") else f"@{value}"


def channel_name(value: str | None) -> str:
    text = value or "unknown"
    return text if text.startswith("@") else f"@{text}"


def ca_of(signal: Signal) -> str:
    return signal.token_address or "—"


def _wallet_sol(snapshot: dict[str, Any] | None) -> float | None:
    if not snapshot:
        return None
    return snapshot.get("sol_balance")


def _wallet_token(snapshot: dict[str, Any] | None, mint: str) -> float | None:
    if not snapshot:
        return None
    balances = snapshot.get("token_balances") or {}
    return balances.get(mint)


def _divider() -> str:
    return "━━━━━━━━━━━━━━━━━━━━"


def _header(icon: str, title: str, symbol: str) -> str:
    return f"{icon} **{title}** — `{symbol}`"


def _core_lines(signal: Signal) -> list[str]:
    return [
        f"🪙 **Token:** `${symbol_of(signal)}`",
        f"📜 **CA:** `{ca_of(signal)}`",
        f"🏦 **Channel:** `{channel_of(signal)}`",
    ]


def _core_lines_trade(trade: ClosedTrade) -> list[str]:
    return [
        f"🪙 **Token:** `${symbol_of_trade(trade)}`",
        f"📜 **CA:** `{trade.mint}`",
        f"🏦 **Channel:** `{channel_name(trade.channel)}`",
    ]


def _strategy_tag(features: dict[str, Any]) -> str | None:
    tag = features.get("strategy_tag")
    return str(tag) if tag else None


def fmt_mc(value: float | None) -> str:
    if value is None:
        return "—"
    return f"${value:,.0f}"


def fmt_ratio(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:,.2f}x"


def _feature(features: dict[str, Any], key: str) -> float | None:
    value = features.get(key)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _gate_status(ok: bool | None) -> str:
    """Short pass/fail marker for one displayed gate."""
    if ok is None:
        return "missing"
    return "pass" if ok else "fail"


def _signal_evidence(features: dict[str, Any]) -> list[str]:
    """A compact, auditable view of the filter inputs for one trade.

    The labels follow the rule that actually accepted the signal. Only the
    conditions ``evaluate_gate`` evaluates may be called a gate; every other
    metric is context so a reader cannot mistake it for an entry requirement.
    """
    max_pump = _feature(features, "max_pump")
    pre_ret = _feature(features, "hist_pre_ret")
    signers = _feature(features, "hist_signers_5m")
    trades = _feature(features, "hist_trades_5m")
    age = _feature(features, "hist_age_min")
    drawdown = _feature(features, "hist_dd_from_high")
    total_buy = _feature(features, "total_buy_sol")
    inflow_n = _feature(features, "inflow_n")
    flow_1m = _feature(features, "hist_flow_1m_sol")
    vol_30m = _feature(features, "hist_pre_vol_30m_sol")
    rule = str(features.get("rule") or "")
    lines: list[str] = []
    if rule == SCANNER_RULE:
        # The scanner lane's gate is DEX + launch-liquidity band. Flow and
        # pre-signal history are displayed as context only: they must not be
        # labelled as gates the scanner rule does not evaluate.
        dex = features.get("dex")
        dex_ok = normalize_dex(dex) == normalize_dex(SCANNER_ALLOWED_DEX)
        lines.append(f"• Rule: `{SCANNER_RULE}`")
        lines.append(
            f"• DEX: `{dex or '—'}` — gate `{SCANNER_ALLOWED_DEX}` · "
            f"{_gate_status(dex_ok)}"
        )
        liquidity = _feature(features, "liquidity_usd")
        if liquidity is None:
            liquidity_ok: bool | None = None
        else:
            liquidity_ok = (
                SCANNER_MIN_LIQUIDITY_USD
                <= liquidity
                < SCANNER_MAX_LIQUIDITY_USD
            )
        lines.append(
            f"• Launch liquidity: `{fmt_mc(liquidity)}` — gate "
            f"`${SCANNER_MIN_LIQUIDITY_USD:,.0f}–"
            f"<${SCANNER_MAX_LIQUIDITY_USD:,.0f}` · "
            f"{_gate_status(liquidity_ok)}"
        )
        mcap = _feature(features, "mcap_usd")
        if mcap is not None:
            lines.append(f"• Card market cap: `{fmt_mc(mcap)}` (context)")
        if flow_1m is not None:
            lines.append(
                f"• Net 1m flow: `{fmt_signed_sol(flow_1m, 2)} SOL` "
                "(context, no gate)"
            )
        if vol_30m is not None:
            lines.append(
                f"• Prior 30m volume: `{fmt_sol(vol_30m, 0)} SOL` "
                "(context, no gate)"
            )
        return lines

    flow_is_gate = rule == "hot_money_continuation"
    if flow_1m is not None:
        if flow_is_gate:
            lines.append(
                f"• Net 1m flow: `{fmt_signed_sol(flow_1m, 2)} SOL` — "
                f"gate `> {HOT_MONEY_FLOW_1M_SOL:g} SOL` · "
                f"{_gate_status(flow_1m > HOT_MONEY_FLOW_1M_SOL)}"
            )
        else:
            lines.append(
                f"• Net 1m flow: `{fmt_signed_sol(flow_1m, 2)} SOL` (context)"
            )
    if vol_30m is not None:
        # Context only: the > 600 SOL leg was dropped from the deployed gate.
        lines.append(
            f"• Prior 30m volume: `{fmt_sol(vol_30m, 0)} SOL` (context)"
        )
    if max_pump is not None:
        lines.append(f"• Max pump: `{fmt_ratio(max_pump)}` (context)")
    if pre_ret is not None:
        lines.append(
            f"• Pre-signal run-up: `{fmt_pct(pre_ret)}` (context)"
        )
    dip_15m = _feature(features, "hist_pre_ret_15m")
    if dip_15m is not None:
        if flow_is_gate:
            lines.append(
                f"• 15m run-up: `{fmt_pct(dip_15m)}` — gate "
                f"`<= {HOT_MONEY_DIP_PRE_RET_MAX:g}` · "
                f"{_gate_status(dip_15m <= HOT_MONEY_DIP_PRE_RET_MAX)}"
            )
        else:
            lines.append(f"• 15m run-up: `{fmt_pct(dip_15m)}` (context)")
    if signers is not None or trades is not None:
        crowd = (
            f"• Prior 5m crowd: "
            f"`{fmt_int(signers)}` signers / `{fmt_int(trades)}` trades"
        )
        if flow_is_gate:
            # The anti-crowd overlay rejects only when the feature is present
            # and above the threshold; an absent feature passes.
            crowd_ok = (
                signers is not None
                and trades is not None
                and signers <= HOT_MONEY_SIGNERS_5M_MAX
                and trades <= HOT_MONEY_TRADES_5M_MAX
            )
            crowd += (
                f" — gates `<= {HOT_MONEY_SIGNERS_5M_MAX}` / "
                f"`<= {HOT_MONEY_TRADES_5M_MAX}` · "
                f"{_gate_status(crowd_ok)}"
            )
        else:
            crowd += " (context)"
        lines.append(crowd)
    if age is not None or drawdown is not None:
        lines.append(
            f"• Age: `{fmt_int(age)} min` · "
            f"Drawdown from high: `{fmt_pct(drawdown)}` (context)"
        )
    if total_buy is not None or inflow_n is not None:
        buys = (
            f"• Message buys: `{fmt_sol(total_buy, 4)} SOL`"
            f" · Inflow: `{fmt_int(inflow_n)}` wallets"
        )
        if flow_is_gate and total_buy is not None:
            buys = (
                f"• Message buys: `{fmt_sol(total_buy, 4)} SOL` — gate "
                f"`> {HOT_MONEY_ALERT_TOTAL_BUY_SOL:g} SOL` · "
                f"{_gate_status(total_buy > HOT_MONEY_ALERT_TOTAL_BUY_SOL)}"
                f" · Inflow: `{fmt_int(inflow_n)}` wallets (context)"
            )
        elif not flow_is_gate:
            buys += " (context)"
        lines.append(buys)
    if not lines and features.get("hist_available") is False:
        lines.append("• History: `cold start — history rule rejected`")
    if not lines:
        return []
    return ["🔎 **Signal evidence**", *lines]


def _wallet_block(
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    mint: str,
    *,
    include_token_balance: bool = True,
) -> list[str]:
    lines = [
        "💼 **Wallet**",
        f"Before: `{fmt_sol(_wallet_sol(before))} SOL`",
        f"After: `{fmt_sol(_wallet_sol(after))} SOL`",
    ]
    if after:
        lines.append(f"Available: `{fmt_sol(after.get('available_sol'))} SOL`")
        lines.append(f"Equity: `{fmt_sol(after.get('equity_sol'))} SOL`")
        if include_token_balance:
            lines.append(
                f"Token balance: `{fmt_int(_wallet_token(after, mint))}`"
            )
    return lines


# ---------------------------------------------------------------------------
# Running statistics
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class ReporterStats:
    starting_sol: float
    trades: int = 0
    wins: int = 0
    losses: int = 0
    breakeven: int = 0
    total_pnl_sol: float = 0.0
    total_return_pct: float = 0.0
    gross_profit_sol: float = 0.0
    gross_loss_sol: float = 0.0
    best_pnl_sol: float | None = None
    worst_pnl_sol: float | None = None
    day: str = ""
    day_trades: int = 0
    day_wins: int = 0
    day_losses: int = 0
    day_breakeven: int = 0
    day_pnl_sol: float = 0.0
    day_best_sol: float | None = None
    day_worst_sol: float | None = None

    def _reset_day(self, day: str) -> None:
        self.day = day
        self.day_trades = 0
        self.day_wins = 0
        self.day_losses = 0
        self.day_breakeven = 0
        self.day_pnl_sol = 0.0
        self.day_best_sol = None
        self.day_worst_sol = None

    def record(self, trade: ClosedTrade) -> None:
        day = datetime.fromtimestamp(trade.exit_ts_ms / 1000, tz=UTC).strftime(
            "%Y-%m-%d"
        )
        if day != self.day:
            self._reset_day(day)
        pnl = float(trade.pnl_sol)
        self.trades += 1
        self.total_pnl_sol += pnl
        self.total_return_pct += float(trade.return_pct)
        self.day_trades += 1
        self.day_pnl_sol += pnl
        if pnl > 0:
            self.wins += 1
            self.day_wins += 1
            self.gross_profit_sol += pnl
        elif pnl < 0:
            self.losses += 1
            self.day_losses += 1
            self.gross_loss_sol += -pnl
        else:
            self.breakeven += 1
            self.day_breakeven += 1
        if self.best_pnl_sol is None or pnl > self.best_pnl_sol:
            self.best_pnl_sol = pnl
        if self.worst_pnl_sol is None or pnl < self.worst_pnl_sol:
            self.worst_pnl_sol = pnl
        if self.day_best_sol is None or pnl > self.day_best_sol:
            self.day_best_sol = pnl
        if self.day_worst_sol is None or pnl < self.day_worst_sol:
            self.day_worst_sol = pnl

    @property
    def win_rate(self) -> float | None:
        if not self.trades:
            return None
        return self.wins / self.trades

    @property
    def profit_factor(self) -> float | None:
        if self.gross_loss_sol <= 0:
            return None
        return self.gross_profit_sol / self.gross_loss_sol

    @property
    def day_win_rate(self) -> float | None:
        if not self.day_trades:
            return None
        return self.day_wins / self.day_trades

    @property
    def expectancy_sol(self) -> float | None:
        if not self.trades:
            return None
        return self.total_pnl_sol / self.trades

    @property
    def avg_win_sol(self) -> float | None:
        if not self.wins:
            return None
        return self.gross_profit_sol / self.wins

    @property
    def avg_loss_sol(self) -> float | None:
        if not self.losses:
            return None
        return -self.gross_loss_sol / self.losses

    @property
    def day_expectancy_sol(self) -> float | None:
        if not self.day_trades:
            return None
        return self.day_pnl_sol / self.day_trades

    @property
    def average_return_pct(self) -> float | None:
        if not self.trades:
            return None
        return self.total_return_pct / self.trades


# ---------------------------------------------------------------------------
# Message builders
# ---------------------------------------------------------------------------
def format_entry(
    position: Position,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> str:
    signal = position.signal
    symbol = symbol_of(signal)
    platform = platform_of(signal)
    mc = _feature(signal.features, "mc_usd")
    entry_quote = position.entry_quote
    lines = [
        _header("🟢", "BUY", symbol),
        _divider(),
        *_core_lines(signal),
    ]
    if signal.message_id is not None:
        lines.append(f"🆔 **Signal ID:** `{signal.message_id}`")
    if platform or mc is not None:
        platform_text = f"`{platform}`" if platform else "`—`"
        lines.append(
            f"🌐 **Platform:** {platform_text} · 🤑 **Market cap:** `{fmt_mc(mc)}`"
        )
    lines.append(f"🎛 **Rule:** `{signal.features.get('rule', '—')}`")
    tag = _strategy_tag(signal.features)
    if tag:
        lines.append(f"🏷 **Lane:** `{tag}`")
    evidence = _signal_evidence(signal.features)
    if evidence:
        lines.append(_divider())
        lines.extend(evidence)
    lines.extend(
        [
            _divider(),
            "🧾 **Fill**",
            f"💵 **Entry price:** `{fmt_price(position.entry_price_sol)} SOL`",
            f"🔢 **Size:** `{fmt_int(position.size)}` tokens",
            f"💸 **Position value:** `{fmt_sol(position.entry_value_sol)} SOL`",
            f"🧾 **Entry fee:** `{fmt_sol(position.entry_fee_sol)} SOL`",
            f"🔒 **Total spent:** "
            f"`{fmt_sol(position.entry_value_sol + position.entry_fee_sol)} SOL`",
            f"📉 **Slippage:** `{position.entry_impact * 100:.2f}%`"
            + (
                f" · **Price impact:** "
                f"`{entry_quote.price_impact_pct:.2f}%`"
                if entry_quote and entry_quote.price_impact_pct is not None
                else ""
            ),
            f"⏱ **Entry delay:** "
            f"`{fmt_duration(position.entry_ts_ms - signal.signal_ts_ms)}` "
            f"(signal to fill)",
            f"🕒 **Entry:** `{fmt_ts(position.entry_ts_ms)}`",
            f"🎯 **Horizon:** `{fmt_ts(position.horizon_ts_ms)}`",
        ]
    )
    if position.signal_liquidity_usd is not None or position.entry_liquidity_usd:
        lines.append(
            f"💧 **Liquidity:** signal `{fmt_mc(position.signal_liquidity_usd)}`"
            f" · fill `{fmt_mc(position.entry_liquidity_usd)}`"
        )
    if entry_quote and entry_quote.router:
        latency = (
            f" · ⚡ `{entry_quote.latency_ms}ms`"
            if entry_quote.latency_ms is not None
            else ""
        )
        lines.append(f"🛣 **Router:** `{entry_quote.router}`{latency}")
    lines.append(_divider())
    lines.extend(_wallet_block(before, after, position.mint))
    lines.append(
        f"🔒 **Locked in position:** "
        f"`{fmt_sol(position.entry_value_sol + position.entry_fee_sol)} SOL`"
    )
    return "\n".join(lines)


def format_exit(
    trade: ClosedTrade,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    stats: ReporterStats,
) -> str:
    symbol = symbol_of_trade(trade)
    pnl = float(trade.pnl_sol)
    if pnl > 0:
        icon, result = "🟢", "PROFIT"
    elif pnl < 0:
        icon, result = "🔴", "LOSS"
    else:
        icon, result = "⚪", "BREAKEVEN"
    reason_icon = REASON_ICONS.get(trade.exit_reason, "ℹ️")
    status_icon = STATUS_ICONS.get(trade.exit_status, "ℹ️")
    lines = [
        _header(icon, f"SELL · {result}", symbol),
        _divider(),
        *_core_lines_trade(trade),
    ]
    if trade.message_id is not None:
        lines.append(f"🆔 **Signal ID:** `{trade.message_id}`")
    platform = platform_of_trade(trade)
    if platform:
        lines.append(f"🌐 **Platform:** `{platform}`")
    lines.append(f"🎛 **Rule:** `{trade.rule}`")
    tag = _strategy_tag(trade.features)
    if tag:
        lines.append(f"🏷 **Lane:** `{tag}`")
    lines.extend(
        [
            f"{reason_icon} **Reason:** `{trade.exit_reason}`",
            f"{status_icon} **Status:** `{trade.exit_status}`",
            _divider(),
            "🧾 **Execution**",
            f"💵 **Entry:** `{fmt_price(trade.entry_price_sol)} SOL`",
            f"💰 **Exit:** `{fmt_price(trade.exit_price_sol)} SOL`",
            f"🔢 **Size:** `{fmt_int(trade.size)}` tokens",
            f"💸 **Entry value:** `{fmt_sol(trade.entry_value_sol)} SOL`",
            f"🏦 **Exit value:** `{fmt_sol(trade.exit_value_sol)} SOL`",
            f"🧾 **Fees:** `{fmt_sol(trade.entry_fee_sol + trade.exit_fee_sol)} SOL`",
        ]
    )
    if (
        trade.signal_liquidity_usd is not None
        or trade.entry_liquidity_usd is not None
        or trade.exit_liquidity_usd is not None
    ):
        lines.append(
            f"💧 **Liquidity:** signal `{fmt_mc(trade.signal_liquidity_usd)}`"
            f" · fill `{fmt_mc(trade.entry_liquidity_usd)}`"
            f" · exit `{fmt_mc(trade.exit_liquidity_usd)}`"
        )
    if trade.unsold_size > 0:
        lines.append(f"🚫 **Unsold:** `{fmt_int(trade.unsold_size)}` tokens")
    if trade.exit_synthetic:
        lines.append(
            "⚠️ **Synthetic close:** quote unavailable; marked at the last "
            "observed feed price."
        )
    lines.extend(
        [
            f"🔁 **Fills:** `{trade.exit_fill_count}` · "
            f"📉 **Exit slippage:** `{trade.exit_impact * 100:.2f}%`",
            f"⏱ **Duration:** `{fmt_duration(trade.hold_ms)}` of "
            f"`{fmt_duration(max(0, trade.expected_exit_ts_ms - trade.entry_ts_ms))}`",
            f"📈 **PnL:** `{fmt_signed_sol(pnl)} SOL` (`{fmt_pct(trade.return_pct)}`)",
            f"🎯 **Notional:** `{fmt_sol(trade.notional_sol)} SOL`",
            _divider(),
            "💼 **Wallet**",
            f"Before: `{fmt_sol(_wallet_sol(before))} SOL`",
            f"After: `{fmt_sol(_wallet_sol(after))} SOL`",
        ]
    )
    if after:
        lines.append(f"Available: `{fmt_sol(after.get('available_sol'))} SOL`")
        lines.append(f"Equity: `{fmt_sol(after.get('equity_sol'))} SOL`")
        lines.append(
            f"Token balance: `{fmt_int(_wallet_token(after, trade.mint))}`"
        )
    lines.extend(
        [
            _divider(),
            "📊 **Running stats**",
            f"Trades: `{stats.trades}` · Win rate: `{fmt_pct(stats.win_rate)}`",
            f"Wins: `{stats.wins}` · Losses: `{stats.losses}`",
            f"Total PnL: `{fmt_signed_sol(stats.total_pnl_sol)} SOL`",
            f"Profit factor: `{stats.profit_factor:.2f}`"
            if stats.profit_factor is not None
            else "Profit factor: `—`",
            f"Expectancy: `{fmt_signed_sol(stats.expectancy_sol)} SOL` "
            f"per trade · Avg return: `{fmt_pct(stats.average_return_pct)}`",
            f"Best: `{fmt_signed_sol(stats.best_pnl_sol)} SOL` · "
            f"Worst: `{fmt_signed_sol(stats.worst_pnl_sol)} SOL`",
            f"Equity: `{fmt_sol(_wallet_sol(after))} SOL`",
            f"🕒 **Exit:** `{fmt_ts(trade.exit_ts_ms)}`",
        ]
    )
    if trade.exit_quote_router:
        lines.append(f"🛣 **Router:** `{trade.exit_quote_router}`")
    if trade.last_quote_error:
        lines.append(f"⚠️ **Last quote error:** `{trade.last_quote_error}`")
    return "\n".join(lines)


def format_startup(
    *,
    rule: str,
    horizon_min: float,
    notional_sol: float,
    starting_sol: float,
    fee: float,
    slippage: float,
    channels: list[str],
    session_file: str,
    preflight_exit_route: bool,
    rule_description: str = "",
    warmup_min: float = 15.0,
    strategy_tag: str | None = None,
    mode: str = "paper",
    restored_sol: float | None = None,
    resumed_at: str | None = None,
    recovered_cash_sol: float | None = None,
    equity_sol: float | None = None,
    open_positions: int | None = None,
    session_pnl_sol: float | None = None,
    recovery_status: str | None = None,
    min_preflight_exit_value_ratio: float | None = None,
) -> str:
    live = mode == "live"
    lines = [
        f"🤖 **TRADING BOT ONLINE · {'LIVE' if live else 'PAPER'}**",
        _divider(),
        f"📊 **Rule:** `{rule}`",
    ]
    if strategy_tag:
        lines.insert(2, f"🏷 **Lane:** `{strategy_tag}`")
    if rule_description:
        lines.append(f"🧠 **Strategy:** {rule_description}")
    preflight = "on" if preflight_exit_route else "off"
    if preflight_exit_route and min_preflight_exit_value_ratio is not None:
        preflight += (
            f" · min exit value {min_preflight_exit_value_ratio:.2f}x"
        )
    lines.extend(
        [
            f"⏳ **Horizon:** `{horizon_min:g} min`",
            f"💵 **Notional:** `{fmt_sol(notional_sol)} SOL`",
            # Deliberately named "configured": on a resumed run this is NOT the
            # wallet's cash balance, which is reported separately below.
            f"🏦 **Configured initial capital:** "
            f"`{fmt_sol(starting_sol)} SOL`",
            f"🧾 **Fee / slippage:** `{fee * 100:.2f}%` / `{slippage * 100:.2f}%`",
            f"🛡 **Exit preflight:** `{preflight}`",
        ]
    )
    # On --resume the wallet carries the previous process's realized PnL, so
    # the configured starting balance cannot reconcile a balance seen at the
    # first fill. Keep both values visible and marked unambiguously.
    recovered = (
        recovered_cash_sol if recovered_cash_sol is not None else restored_sol
    )
    status = recovery_status or (
        "recovered" if resumed_at is not None or recovered is not None else "fresh"
    )
    resumed = f" · run started `{resumed_at}`" if resumed_at else ""
    lines.append(f"📂 **Recovery:** `{status}`{resumed}")
    if recovered is not None:
        lines.append(f"💵 **Recovered cash:** `{fmt_sol(recovered)} SOL`")
    if open_positions is not None:
        lines.append(f"📌 **Open positions:** `{open_positions}`")
    if session_pnl_sol is not None:
        lines.append(
            f"📈 **Session realized PnL:** "
            f"`{fmt_signed_sol(session_pnl_sol)} SOL`"
        )
    if equity_sol is not None:
        lines.append(f"💼 **Equity:** `{fmt_sol(equity_sol)} SOL`")
    elif status == "recovered":
        # A restored token position has no fresh mark yet; cash-only equity
        # would understate it, so do not present a number as reliable.
        lines.append("💼 **Equity:** `Unavailable (no fresh marks)`")
    lines.extend(
        [
            f"📡 **Channels:** `{', '.join(channels) or '—'}`",
            f"🔐 **Session:** `{session_file}`",
            f"🗂 **History warm-up:** `{warmup_min:g} min` of firehose before "
            "history rules can pass",
            _divider(),
        ]
    )
    if live:
        lines.append(
            "🔴 **LIVE EXECUTION.** Every entry and exit is signed locally and "
            "broadcast through Jupiter. Verify the notional cap before starting."
        )
    else:
        lines.append(
            "⚠️ **Paper trading only.** No transaction is signed or broadcast; "
            "fills are read-only Jupiter quotes."
        )
    return "\n".join(lines)


def format_shutdown(
    *,
    reason: str,
    stats: ReporterStats,
    wallet: dict[str, Any] | None,
    runtime_ms: int,
    rule: str | None = None,
    horizon_min: float | None = None,
    strategy_tag: str | None = None,
    integrity: str | None = None,
) -> str:
    lines = [
        "🔌 **PAPER BOT OFFLINE**",
        _divider(),
        f"📌 **Reason:** `{reason}`",
        f"⏱ **Runtime:** `{fmt_duration(runtime_ms)}`",
        f"💼 **Balance:** `{fmt_sol(_wallet_sol(wallet))} SOL`",
    ]
    if integrity:
        lines.append(f"🧪 **Session integrity:** `{integrity}`")
    if strategy_tag:
        lines.insert(2, f"🏷 **Lane:** `{strategy_tag}`")
    if wallet and wallet.get("equity_sol") is not None:
        lines.append(f"📈 **Equity:** `{fmt_sol(wallet.get('equity_sol'))} SOL`")
    if wallet and wallet.get("starting_sol") is not None:
        lines.append(
            f"🏦 **Starting balance:** `{fmt_sol(wallet.get('starting_sol'))} SOL`"
        )
    if rule:
        lines.append(f"🎛 **Rule:** `{rule}`")
    if horizon_min is not None:
        lines.append(f"⏳ **Horizon:** `{horizon_min:g} min`")
    lines.extend(
        [
            f"📈 **Total PnL:** `{fmt_signed_sol(stats.total_pnl_sol)} SOL`",
            f"📊 **Trades:** `{stats.trades}` · Win rate: `{fmt_pct(stats.win_rate)}`",
            f"✅ **Wins:** `{stats.wins}` · ❌ **Losses:** `{stats.losses}`",
            "⚖️ **Profit factor:** "
            + (
                f"`{stats.profit_factor:.2f}`"
                if stats.profit_factor is not None
                else "`—`"
            ),
            f"🎯 **Expectancy:** `{fmt_signed_sol(stats.expectancy_sol)} SOL`",
            f"🕒 `{fmt_ts(int(datetime.now(UTC).timestamp() * 1000))}`",
        ]
    )
    return "\n".join(lines)


def format_daily_summary(stats: ReporterStats, wallet: dict[str, Any] | None) -> str:
    return "\n".join(
        [
            f"📅 **DAILY SUMMARY — {stats.day or 'today'}**",
            _divider(),
            f"📊 **Trades:** `{stats.day_trades}`",
            f"✅ **Wins:** `{stats.day_wins}` · ❌ **Losses:** `{stats.day_losses}`",
            f"🎯 **Win rate:** `{fmt_pct(stats.day_win_rate)}`",
            f"📈 **PnL:** `{fmt_signed_sol(stats.day_pnl_sol)} SOL`",
            f"🎯 **Expectancy:** `{fmt_signed_sol(stats.day_expectancy_sol)} SOL`",
            f"🏆 **Best:** `{fmt_signed_sol(stats.day_best_sol)} SOL`",
            f"💥 **Worst:** `{fmt_signed_sol(stats.day_worst_sol)} SOL`",
            f"💼 **Equity:** `{fmt_sol(_wallet_sol(wallet))} SOL`",
            _divider(),
            f"📈 **All-time PnL:** `{fmt_signed_sol(stats.total_pnl_sol)} SOL`",
            f"📊 **All-time win rate:** `{fmt_pct(stats.win_rate)}`",
            "⚖️ **Profit factor:** "
            + (
                f"`{stats.profit_factor:.2f}`"
                if stats.profit_factor is not None
                else "`—`"
            ),
        ]
    )


def format_status(
    *,
    stats: ReporterStats,
    wallet: dict[str, Any] | None,
    positions: list[dict[str, Any]],
    pending: int,
    rule: str,
) -> str:
    lines = [
        "📊 **PAPER BOT STATUS**",
        _divider(),
        f"⚙️ **Rule:** `{rule}`",
        f"💼 **Balance:** `{fmt_sol(_wallet_sol(wallet))} SOL`",
        f"🪙 **Open positions:** `{len(positions)}` · **Pending:** `{pending}`",
        f"📈 **Total PnL:** `{fmt_signed_sol(stats.total_pnl_sol)} SOL`",
        f"🎯 **Win rate:** `{fmt_pct(stats.win_rate)}` "
        f"(`{stats.wins}`W / `{stats.losses}`L / `{stats.trades}` trades)",
        "⚖️ **Profit factor:** "
        + (
            f"`{stats.profit_factor:.2f}`"
            if stats.profit_factor is not None
            else "`—`"
        ),
        f"🕒 `{fmt_ts(int(datetime.now(UTC).timestamp() * 1000))}`",
    ]
    if wallet and wallet.get("equity_sol") is not None:
        lines.insert(
            4, f"📊 **Equity:** `{fmt_sol(wallet.get('equity_sol'))} SOL`"
        )
    if positions:
        lines.append(_divider())
        for item in positions[:10]:
            lines.append(
                f"• `{item['symbol']}` `{item['mint']}` — "
                f"{fmt_pct(item.get('return_pct'))} · "
                f"{fmt_duration(item.get('hold_ms'))}"
            )
    return "\n".join(lines)


def format_help() -> str:
    return "\n".join(
        [
            "🤖 **Paper Bot commands**",
            _divider(),
            "`/status` — wallet, open positions and pending entries",
            "`/stats` — all-time and daily performance",
            "`/help` — this message",
            _divider(),
            "Reports are sent automatically on every BUY and SELL.",
        ]
    )


# ---------------------------------------------------------------------------
# Reporter
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class ReporterConfig:
    bot_token: str | None = None
    chat_id: int | str | None = None
    enabled: bool = True
    commands: bool = True
    daily_summary: bool = True
    min_gap_s: float = DEFAULT_MIN_GAP_S


class TelegramReporter:
    """Best-effort async Telegram reporter with optional slash commands."""

    def __init__(self, config: ReporterConfig) -> None:
        self.config = config
        self.stats: ReporterStats | None = None
        self.wallet: Any | None = None
        self.engine: Any | None = None
        self.rule: str = "baseline"
        self._bot: Bot | None = None
        self._app: Application | None = None
        self._lock = asyncio.Lock()
        self._last_sent = 0.0
        self._daily_task: asyncio.Task[None] | None = None
        self._pending: set[asyncio.Task[Any]] = set()
        self._started = False

    @classmethod
    def from_env(
        cls,
        env: dict[str, str],
        *,
        commands: bool = True,
        daily_summary: bool = True,
        min_gap_s: float = DEFAULT_MIN_GAP_S,
    ) -> TelegramReporter:
        token = env.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
        raw_chat = env.get("TELEGRAM_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID")
        chat_id: int | str | None = raw_chat
        if raw_chat:
            try:
                chat_id = int(raw_chat)
            except ValueError:
                chat_id = raw_chat
        return cls(
            ReporterConfig(
                bot_token=token,
                chat_id=chat_id,
                enabled=bool(token and chat_id),
                commands=commands,
                daily_summary=daily_summary,
                min_gap_s=min_gap_s,
            )
        )

    @property
    def enabled(self) -> bool:
        return bool(
            self.config.enabled and self.config.bot_token and self.config.chat_id
        )

    def bind(
        self,
        *,
        engine: Any | None = None,
        wallet: Any | None = None,
        stats: ReporterStats | None = None,
        rule: str | None = None,
    ) -> None:
        if engine is not None:
            self.engine = engine
        if wallet is not None:
            self.wallet = wallet
            if stats is not None:
                stats.starting_sol = float(
                    getattr(wallet, "starting_sol", stats.starting_sol)
                )
        if stats is not None:
            self.stats = stats
        if rule is not None:
            self.rule = rule

    def wallet_snapshot(self) -> dict[str, Any] | None:
        """Cash plus open token balances marked at the last feed price.

        ``PaperWallet.equity_sol`` counts an unmarked token as zero, so calling
        ``wallet.snapshot()`` with positions open reported cash only. The
        engine already holds the market history; ask it for the marked view.
        """
        if self.engine is not None:
            snapshot = getattr(self.engine, "wallet_snapshot", None)
            if callable(snapshot):
                marked = snapshot()
                if marked is not None:
                    return marked
        return self.wallet.snapshot() if self.wallet is not None else None

    # -- lifecycle ------------------------------------------------------
    async def start(self) -> bool:
        if not self.enabled or self._started:
            return self._started
        assert self.config.bot_token is not None
        try:
            if self.config.commands:
                app = (
                    Application.builder()
                    .token(self.config.bot_token)
                    .build()
                )
                app.add_handler(CommandHandler("start", self._cmd_help))
                app.add_handler(CommandHandler("help", self._cmd_help))
                app.add_handler(CommandHandler("status", self._cmd_status))
                app.add_handler(CommandHandler("stats", self._cmd_stats))
                await app.initialize()
                await app.start()
                await app.updater.start_polling(
                    allowed_updates=["message"], drop_pending_updates=True
                )
                self._app = app
                self._bot = app.bot
            else:
                bot = Bot(self.config.bot_token)
                await bot.initialize()
                self._bot = bot
            self._started = True
            if self.config.daily_summary:
                self._daily_task = asyncio.create_task(self._daily_loop())
            return True
        except Exception as exc:  # noqa: BLE001 - reporting is optional
            LOGGER.warning("Telegram reporter disabled: %s", exc)
            self._started = False
            return False

    async def stop(self) -> None:
        if self._daily_task is not None:
            self._daily_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._daily_task
            self._daily_task = None
        try:
            if self._app is not None:
                if self._app.updater is not None and self._app.updater.running:
                    await self._app.updater.stop()
                if self._app.running:
                    await self._app.stop()
                await self._app.shutdown()
            elif self._bot is not None:
                await self._bot.shutdown()
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Telegram reporter shutdown failed: %s", exc)
        finally:
            self._app = None
            self._bot = None
            self._started = False

    # -- sending --------------------------------------------------------
    def schedule(self, coro: Any) -> asyncio.Task[Any]:
        """Run a notification in the background and remember it for flush."""
        task = asyncio.create_task(coro)
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)
        return task

    async def flush(self, timeout: float = 20.0) -> None:
        """Wait for queued notifications so shutdown does not drop reports."""
        if not self._pending:
            return
        try:
            await asyncio.wait_for(
                asyncio.gather(*list(self._pending), return_exceptions=True),
                timeout=timeout,
            )
        except TimeoutError:
            LOGGER.warning("Telegram reporter flush timed out")

    async def send_markdown(self, markdown: str, *, silent: bool = False) -> bool:
        if not self.enabled or self._bot is None:
            LOGGER.debug("Telegram reporter disabled; dropping message")
            return False
        if len(markdown) > MAX_MESSAGE_CHARS:
            markdown = markdown[: MAX_MESSAGE_CHARS - 20] + "\n… (truncated)"
        try:
            text, entities = telegramify_markdown.convert(
                markdown, latex_escape=False
            )
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Telegram markdown render failed: %s", exc)
            return False

        ptb_entities = []
        for entity in entities or []:
            try:
                ptb_entities.append(MessageEntity.de_json(entity.to_dict(), self._bot))
            except Exception:  # noqa: BLE001 - skip an entity, keep the text
                continue

        async with self._lock:
            gap = self.config.min_gap_s - (
                asyncio.get_running_loop().time() - self._last_sent
            )
            if gap > 0:
                await asyncio.sleep(gap)
            try:
                await self._bot.send_message(
                    chat_id=self.config.chat_id,
                    text=text,
                    entities=ptb_entities or None,
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                    disable_notification=silent,
                )
                return True
            except RetryAfter as exc:
                LOGGER.warning("Telegram flood wait: %ss", exc.retry_after)
                await asyncio.sleep(float(exc.retry_after) + 1.0)
                return await self._retry_send(text, ptb_entities, silent)
            except TelegramError as exc:
                LOGGER.warning("Telegram send failed: %s", exc)
                return False
            finally:
                self._last_sent = asyncio.get_running_loop().time()

    async def _retry_send(
        self,
        text: str,
        entities: list[MessageEntity],
        silent: bool,
    ) -> bool:
        assert self._bot is not None
        try:
            await self._bot.send_message(
                chat_id=self.config.chat_id,
                text=text,
                entities=entities or None,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
                disable_notification=silent,
            )
            return True
        except TelegramError as exc:
            LOGGER.warning("Telegram retry failed: %s", exc)
            return False

    # -- notifications --------------------------------------------------
    async def notify_startup(
        self,
        *,
        rule: str,
        horizon_min: float,
        notional_sol: float,
        starting_sol: float,
        fee: float,
        slippage: float,
        channels: list[str],
        session_file: str,
        preflight_exit_route: bool,
        rule_description: str = "",
        warmup_min: float = 15.0,
        strategy_tag: str | None = None,
        mode: str = "paper",
        restored_sol: float | None = None,
        resumed_at: str | None = None,
        recovered_cash_sol: float | None = None,
        equity_sol: float | None = None,
        open_positions: int | None = None,
        session_pnl_sol: float | None = None,
        recovery_status: str | None = None,
        min_preflight_exit_value_ratio: float | None = None,
    ) -> bool:
        self.stats = self.stats or ReporterStats(starting_sol=starting_sol)
        return await self.send_markdown(
            format_startup(
                rule=rule,
                horizon_min=horizon_min,
                notional_sol=notional_sol,
                starting_sol=starting_sol,
                fee=fee,
                slippage=slippage,
                channels=channels,
                session_file=session_file,
                preflight_exit_route=preflight_exit_route,
                rule_description=rule_description,
                warmup_min=warmup_min,
                strategy_tag=strategy_tag,
                mode=mode,
                restored_sol=restored_sol,
                resumed_at=resumed_at,
                recovered_cash_sol=recovered_cash_sol,
                equity_sol=equity_sol,
                open_positions=open_positions,
                session_pnl_sol=session_pnl_sol,
                recovery_status=recovery_status,
                min_preflight_exit_value_ratio=min_preflight_exit_value_ratio,
            )
        )

    async def notify_entry(
        self,
        position: Position,
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
    ) -> bool:
        return await self.send_markdown(format_entry(position, before, after))

    async def notify_exit(
        self,
        trade: ClosedTrade,
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
    ) -> bool:
        if self.stats is None:
            self.stats = ReporterStats(starting_sol=0.0)
        self.stats.record(trade)
        return await self.send_markdown(format_exit(trade, before, after, self.stats))

    async def notify_shutdown(
        self,
        *,
        reason: str,
        wallet: dict[str, Any] | None,
        runtime_ms: int,
        rule: str | None = None,
        horizon_min: float | None = None,
        strategy_tag: str | None = None,
        integrity: str | None = None,
    ) -> bool:
        stats = self.stats or ReporterStats(starting_sol=0.0)
        return await self.send_markdown(
            format_shutdown(
                reason=reason,
                stats=stats,
                wallet=wallet,
                runtime_ms=runtime_ms,
                rule=rule,
                horizon_min=horizon_min,
                strategy_tag=strategy_tag,
                integrity=integrity,
            ),
            silent=True,
        )

    async def notify_error(self, message: str) -> bool:
        return await self.send_markdown(
            f"⚠️ **PAPER BOT ERROR**\n{_divider()}\n`{message}`", silent=True
        )

    # -- commands -------------------------------------------------------
    async def _cmd_help(self, update: Any, context: Any) -> None:
        if not self._allowed(update):
            return
        await self.send_markdown(format_help())

    async def _cmd_status(self, update: Any, context: Any) -> None:
        if not self._allowed(update):
            return
        positions: list[dict[str, Any]] = []
        pending = 0
        if self.engine is not None:
            pending = len(getattr(self.engine, "pending", {}))
            for position in getattr(self.engine, "positions", {}).values():
                last = position.last_price_sol or position.entry_price_sol
                cost = position.entry_value_sol + position.entry_fee_sol
                pnl = (last - position.entry_price_sol) * position.size - (
                    position.entry_fee_sol
                )
                positions.append(
                    {
                        "symbol": symbol_of(position.signal),
                        "mint": position.mint,
                        "return_pct": pnl / cost if cost > 0 else None,
                        "hold_ms": int(
                            datetime.now(UTC).timestamp() * 1000
                        )
                        - position.entry_ts_ms,
                    }
                )
        await self.send_markdown(
            format_status(
                stats=self.stats or ReporterStats(starting_sol=0.0),
                wallet=self.wallet_snapshot(),
                positions=positions,
                pending=pending,
                rule=self.rule,
            )
        )

    async def _cmd_stats(self, update: Any, context: Any) -> None:
        if not self._allowed(update):
            return
        stats = self.stats or ReporterStats(starting_sol=0.0)
        await self.send_markdown(
            "\n".join(
                [
                    "📊 **PERFORMANCE**",
                    _divider(),
                    f"Trades: `{stats.trades}`",
                    f"Wins: `{stats.wins}` · Losses: `{stats.losses}` · "
                    f"Breakeven: `{stats.breakeven}`",
                    f"Win rate: `{fmt_pct(stats.win_rate)}`",
                    f"Total PnL: `{fmt_signed_sol(stats.total_pnl_sol)} SOL`",
                    f"Profit factor: "
                    f"`{stats.profit_factor:.2f}`"
                    if stats.profit_factor is not None
                    else "Profit factor: `—`",
                    f"Expectancy: `{fmt_signed_sol(stats.expectancy_sol)} SOL`",
                    f"Avg win / loss: `{fmt_signed_sol(stats.avg_win_sol)}` / "
                    f"`{fmt_signed_sol(stats.avg_loss_sol)} SOL`",
                    f"Best: `{fmt_signed_sol(stats.best_pnl_sol)} SOL`",
                    f"Worst: `{fmt_signed_sol(stats.worst_pnl_sol)} SOL`",
                    _divider(),
                    f"Today: `{stats.day_trades}` trades · "
                    f"`{fmt_signed_sol(stats.day_pnl_sol)} SOL` · "
                    f"win rate `{fmt_pct(stats.day_win_rate)}` · "
                    f"expectancy `{fmt_signed_sol(stats.day_expectancy_sol)} SOL`",
                ]
            )
        )

    def _allowed(self, update: Any) -> bool:
        if self.config.chat_id is None:
            return False
        chat = getattr(update, "effective_chat", None)
        return chat is not None and str(chat.id) == str(self.config.chat_id)

    # -- daily summary --------------------------------------------------
    async def _daily_loop(self) -> None:
        while True:
            now = datetime.now(UTC)
            next_midnight = (now + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            wait_s = max(60.0, (next_midnight - now).total_seconds())
            await asyncio.sleep(wait_s)
            if self.stats is not None:
                wallet = self.wallet_snapshot()
                await self.send_markdown(format_daily_summary(self.stats, wallet))


__all__ = [
    "ReporterConfig",
    "ReporterStats",
    "TelegramReporter",
    "ca_of",
    "channel_of",
    "fmt_duration",
    "fmt_int",
    "fmt_pct",
    "fmt_price",
    "fmt_signed_sol",
    "fmt_sol",
    "format_daily_summary",
    "format_entry",
    "format_exit",
    "format_help",
    "format_shutdown",
    "format_startup",
    "format_status",
    "symbol_of",
]
