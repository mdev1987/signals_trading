"""Plain data structures shared by the paper bot.

The field names intentionally match the research pipeline (``price_sol``,
``signal_ts_ms``, ``exit_status`` ...) so paper results can be joined with
``output/backtest/scenario_outcomes.parquet`` without a translation layer.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

WSOL_MINT = "So11111111111111111111111111111111111111112"


@dataclass(slots=True)
class TradeEvent:
    """One normalized PumpAPI trade, in the same shape as ``market_replay``."""

    mint: str
    timestamp_ms: int
    event_index: int
    action: str
    price_sol: float | None = None
    token_amount: float | None = None
    quote_amount: float | None = None
    tx_signer: str | None = None
    decimals: int | None = None
    signature: str | None = None
    pool_id: str | None = None

    @property
    def is_buy(self) -> bool:
        return self.action == "buy"

    @property
    def is_sell(self) -> bool:
        return self.action == "sell"

    @property
    def notional_sol(self) -> float:
        """Observed SOL notional, using ``token_amount * price_sol``.

        ``quote_amount`` is not consistently scaled in PumpAPI data, so the
        research pipeline deliberately avoids it for volume.
        """
        if self.price_sol is None or self.token_amount is None:
            return 0.0
        value = self.token_amount * self.price_sol
        if not value > 0:
            return 0.0
        return value

    @property
    def price_valid(self) -> bool:
        return self.price_sol is not None and self.price_sol > 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Signal:
    """A parsed Telegram BUY signal."""

    token_address: str
    signal_ts_ms: int
    received_ts_ms: int
    channel: str = "unknown"
    message_id: int | None = None
    message: str = ""
    network: str = "solana"
    address_status: str = "candidate"
    address_type: str | None = None
    address_extraction_method: str | None = None
    message_type: str = "other"
    is_buy_signal: bool = False
    is_confirmed_buy_signal: bool = False
    ticker: str | None = None
    features: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class GateDecision:
    allowed: bool
    rule: str
    reasons: list[str] = field(default_factory=list)
    features: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class FillQuote:
    """A simulated fill price plus the provenance of that price."""

    price_sol: float
    source: str
    in_amount: int | None = None
    out_amount: int | None = None
    price_impact_pct: float | None = None
    router: str | None = None
    request_id: str | None = None
    latency_ms: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class EntryFill:
    entry_price_sol: float
    size: float
    entry_value_sol: float
    entry_fee_sol: float
    entry_impact: float
    quote_source: str
    quote: FillQuote


@dataclass(slots=True)
class PendingEntry:
    signal: Signal
    eligible_at_ms: int
    expires_at_ms: int
    attempts: int = 0
    in_flight: bool = False
    saw_buy: bool = False
    last_reason: str | None = None
    # Upstream diagnostic for ``last_reason`` (e.g. Jupiter's error body), so a
    # skipped entry can be explained after the fact instead of only classified.
    last_detail: str | None = None
    trigger_ts_ms: int | None = None
    # Set in timer mode so a skip/shutdown can cancel the pending quote task.
    timer_task: Any | None = None

    @property
    def mint(self) -> str:
        return self.signal.token_address


@dataclass(slots=True)
class Position:
    signal: Signal
    mint: str
    entry_ts_ms: int
    entry_price_sol: float
    size: float
    entry_value_sol: float
    entry_fee_sol: float
    entry_impact: float
    horizon_ts_ms: int
    tp_level: float | None = None
    sl_level: float | None = None
    last_price_sol: float | None = None
    last_ts_ms: int | None = None
    decimals: int | None = None
    exit_in_flight: bool = False
    exit_requested: bool = False
    exit_reason: str | None = None
    exit_status: str | None = None
    exit_reference_price: float | None = None
    exit_ts_ms: int | None = None
    exit_requested_ts_ms: int | None = None
    exit_quote_attempts: int = 0
    last_quote_error: str | None = None
    entry_quote: FillQuote | None = None
    entry_trigger_ts_ms: int | None = None
    # Partial-exit accumulator. ``size``/``entry_value_sol``/``entry_fee_sol``
    # above always describe what is still open.
    sold_size: float = 0.0
    exit_price_acc: float = 0.0
    exit_value_acc: float = 0.0
    exit_fee_acc: float = 0.0
    pnl_acc: float = 0.0
    entry_basis_acc: float = 0.0
    entry_fee_acc: float = 0.0
    exit_fill_count: int = 0
    unsold_size: float = 0.0
    wallet_before_exit: dict[str, Any] | None = None

    @property
    def is_open(self) -> bool:
        return self.exit_status is None


@dataclass(slots=True)
class ClosedTrade:
    mint: str
    channel: str
    message_id: int | None
    signal_ts_ms: int
    entry_ts_ms: int
    entry_trigger_ts_ms: int | None
    exit_ts_ms: int
    expected_exit_ts_ms: int
    entry_price_sol: float
    exit_price_sol: float
    last_observed_price_sol: float | None
    size: float
    notional_sol: float
    entry_value_sol: float
    entry_fee_sol: float
    exit_value_sol: float
    exit_fee_sol: float
    pnl_sol: float
    return_pct: float
    exit_reason: str
    exit_status: str
    replay_truncated: bool
    exit_synthetic: bool
    hold_ms: int
    entry_delay_ms: int
    entry_impact: float
    exit_impact: float
    entry_quote_source: str
    exit_quote_source: str
    entry_quote_price_impact_pct: float | None
    exit_quote_price_impact_pct: float | None
    entry_quote_router: str | None
    exit_quote_router: str | None
    entry_quote_request_id: str | None
    exit_quote_request_id: str | None
    exit_quote_attempts: int
    last_quote_error: str | None
    rule: str
    features: dict[str, Any] = field(default_factory=dict)
    exit_fill_count: int = 1
    unsold_size: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class SkipRecord:
    mint: str | None
    signal_ts_ms: int
    ts_ms: int
    reason: str
    detail: str | None = None
    channel: str | None = None
    message_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
