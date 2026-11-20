"""Event-driven paper-trading state machine.

The state machine mirrors ``backtest_signals.simulate_position``:

1. A signal becomes eligible at ``signal_ts + entry_delay``.
2. Entry is the first BUY trade at or after that time, within
   ``max_entry_delay``.
3. TP/SL are checked on every trade up to the horizon.
4. The position closes at the last trade at or before the horizon.
5. If the market data ends before the horizon, the close is marked
   ``replay_truncated`` rather than treated as a normal horizon exit.

Live mode differs only in where the fill price comes from: a Jupiter quote
instead of the replay trade price. The accounting is identical.

Robustness rules enforced here:

* every background entry/exit task is tracked and drained on shutdown;
* a task exception always clears ``in_flight`` so a pending entry cannot wedge;
* every open position is closed on shutdown, even when quotes hang;
* a genuine no-route write-off is distinguished from a transient Jupiter
  outage, which is marked at the last observed feed price instead of costing
  the whole position.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from .broker import ExitFill, PaperBroker
from .history import MarketHistory, attach_history_features
from .models import (
    ClosedTrade,
    FillQuote,
    GateDecision,
    PendingEntry,
    Position,
    Signal,
    SkipRecord,
    TradeEvent,
)
from .quotes import QuoteError, is_transient_quote_error
from .safety import safety_check
from .signals import (
    POST_SIGNAL_CROWDED_RULES,
    GateConfig,
    evaluate_gate,
    post_signal_crowded,
)
from .wallet import DUST_SIZE

LOGGER = logging.getLogger(__name__)


def quote_detail(reason: str | None, detail: str | None) -> str | None:
    """Combine a machine quote reason with its upstream body for the record.

    The skip/quote telemetry keeps a single ``detail`` string; providers return
    a stable reason plus a raw body, and losing the body made a rejected entry
    impossible to explain later (see the 2026-11-16 `quote_bad_request` miss).
    """
    if reason is None:
        return None
    if not detail:
        return reason
    text = " ".join(str(detail).split())
    return f"{reason}: {text}"[:300]


@dataclass(slots=True)
class SafetyConfig:
    """Thresholds for the replay-derived liquidity/tail screen."""

    enabled: bool = False
    min_pre_trades: int = 20
    min_pre_signers: int = 10
    min_age_min: float = 1.0
    min_pre_volume_sol: float = 5.0
    max_dd_from_high: float = -0.75

    def as_namespace(self) -> SimpleNamespace:
        return SimpleNamespace(
            min_pre_trades=self.min_pre_trades,
            min_pre_signers=self.min_pre_signers,
            min_age_min=self.min_age_min,
            min_pre_volume_sol=self.min_pre_volume_sol,
            max_dd_from_high=self.max_dd_from_high,
        )


@dataclass(slots=True)
class EngineConfig:
    entry_delay_ms: int = 5_000
    max_entry_delay_ms: int = 120_000
    # "trade": wait for the first BUY print at/after the delay (historical
    # behaviour). "timer": quote at the delay regardless of prints, which is
    # what the research backtest assumes (entry at signal + ~5 s).
    entry_mode: str = "trade"
    horizon_ms: int = 900_000
    tp_pct: float | None = None
    sl_pct: float | None = None
    live: bool = False
    exit_quote_timeout_ms: int = 30_000
    slippage_bps: int = 50
    horizon_settle_ms: int = 0
    preflight_exit_route: bool = False
    exit_fractions: tuple[float, ...] = (1.0, 0.5, 0.25, 0.1, 0.01)
    exit_max_fills: int = 8
    exit_fallback_feed: bool = False
    shutdown_timeout_ms: int = 25_000

    def __post_init__(self) -> None:
        if self.entry_mode not in ("trade", "timer"):
            raise ValueError(
                f"entry_mode must be 'trade' or 'timer', got {self.entry_mode!r}"
            )
        if self.entry_mode == "timer" and not self.live:
            raise ValueError("entry_mode='timer' requires live=True")


class PaperEngine:
    def __init__(
        self,
        *,
        config: EngineConfig,
        broker: PaperBroker,
        quotes: Any,
        history: MarketHistory,
        gate: GateConfig,
        safety: SafetyConfig | None = None,
        store: Any | None = None,
        clock: Callable[[], int] | None = None,
        on_watch: Callable[[str], None] | None = None,
        on_entry: Callable[
            [Position, dict[str, Any] | None, dict[str, Any] | None], None
        ]
        | None = None,
        on_close: Callable[
            [ClosedTrade, dict[str, Any] | None, dict[str, Any] | None], None
        ]
        | None = None,
    ) -> None:
        self.config = config
        self.broker = broker
        self.quotes = quotes
        self.history = history
        self.gate = gate
        self.safety = safety or SafetyConfig()
        self.store = store
        self._clock = clock or (lambda: int(time.time() * 1000))
        self.on_watch = on_watch
        self.on_entry = on_entry
        self.on_close = on_close

        self.pending: dict[str, PendingEntry] = {}
        self.positions: dict[str, Position] = {}
        self.closed: list[ClosedTrade] = []
        self.skips: list[SkipRecord] = []
        self.signal_log: list[tuple[Signal, GateDecision]] = []
        self._locks: dict[str, asyncio.Lock] = {}
        self._tasks: set[asyncio.Task[Any]] = set()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _now(self) -> int:
        return int(self._clock())

    def _lock(self, mint: str) -> asyncio.Lock:
        lock = self._locks.get(mint)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[mint] = lock
        return lock

    def _wallet_snapshot(self) -> dict[str, Any] | None:
        wallet = getattr(self.broker, "wallet", None)
        return wallet.snapshot() if wallet is not None else None

    @staticmethod
    def _emit(callback: Any, *args: Any) -> None:
        """Call a reporting hook without letting it break trading."""
        if callback is None:
            return
        try:
            callback(*args)
        except Exception:  # noqa: BLE001 - reporting is optional
            LOGGER.exception("paper-bot reporting hook failed")

    def _spawn(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
        """Track a background action so shutdown can wait for it."""
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        try:
            exc = task.exception()
        except Exception:  # noqa: BLE001 - never raise from a done callback
            return
        if exc is not None:
            LOGGER.error(
                "paper-bot background task failed: %s", exc, exc_info=exc
            )

    def _release_mint(self, mint: str) -> None:
        """Unpin history once a mint is no longer pending or open."""
        if mint not in self.pending and mint not in self.positions:
            self.history.unpin(mint)

    def _record_skip(
        self,
        *,
        mint: str | None,
        signal_ts_ms: int,
        ts_ms: int,
        reason: str,
        detail: str | None = None,
        channel: str | None = None,
        message_id: int | None = None,
    ) -> None:
        skip = SkipRecord(
            mint=mint,
            signal_ts_ms=signal_ts_ms,
            ts_ms=ts_ms,
            reason=reason,
            detail=detail,
            channel=channel,
            message_id=message_id,
        )
        self.skips.append(skip)
        if self.store is not None:
            self.store.record_skip(skip)

    @staticmethod
    def _cancel_timer(pending: PendingEntry) -> None:
        """Cancel a timer-entry task unless we are that task.

        A timer task may itself decide to skip the entry; cancelling the
        current task there would raise ``CancelledError`` at the next await.
        """
        task = pending.timer_task
        pending.timer_task = None
        if task is None or task.done():
            return
        try:
            if task is asyncio.current_task():
                return
        except RuntimeError:  # no running loop (tests / teardown)
            return
        task.cancel()

    def _skip_pending(
        self, pending: PendingEntry, reason: str, detail: str | None
    ) -> None:
        if self.pending.get(pending.mint) is not pending:
            return
        del self.pending[pending.mint]
        self._cancel_timer(pending)
        self._record_skip(
            mint=pending.mint,
            signal_ts_ms=pending.signal.signal_ts_ms,
            ts_ms=self._now(),
            reason=reason,
            detail=detail,
            channel=pending.signal.channel,
            message_id=pending.signal.message_id,
        )
        self._release_mint(pending.mint)

    # ------------------------------------------------------------------
    # Signal intake
    # ------------------------------------------------------------------
    async def on_signal(self, signal: Signal) -> GateDecision:
        mint = signal.token_address
        stats: dict[str, Any] = {}
        if mint:
            stats = self.history.stats(mint, signal.signal_ts_ms)
            signal.features = attach_history_features(signal.features, stats)

        decision = evaluate_gate(signal, self.gate)
        signal.features["rule"] = decision.rule
        decision.features["rule"] = decision.rule
        final = decision
        rejection_reason: str | None = None
        rejection_detail: str | None = None

        if not decision.allowed:
            rejection_reason = "gate_rejected"
            rejection_detail = ";".join(decision.reasons)
        elif self.safety.enabled:
            passed, reasons = safety_check(stats, self.safety.as_namespace())
            if not passed:
                final = GateDecision(
                    allowed=False,
                    rule=decision.rule,
                    reasons=list(reasons),
                    features=decision.features,
                )
                rejection_reason = "safety_rejected"
                rejection_detail = ";".join(reasons)
        elif not mint:
            # Defensive: evaluate_gate already rejects an empty address.
            final = GateDecision(
                allowed=False,
                rule=decision.rule,
                reasons=["missing_token_address"],
                features=decision.features,
            )
            rejection_reason = "gate_rejected"
            rejection_detail = "missing_token_address"

        if rejection_reason is None:
            if mint in self.positions:
                final = GateDecision(
                    allowed=False,
                    rule=decision.rule,
                    reasons=["position_already_open"],
                    features=decision.features,
                )
                rejection_reason = "position_already_open"
            elif mint in self.pending:
                final = GateDecision(
                    allowed=False,
                    rule=decision.rule,
                    reasons=["duplicate_pending_signal"],
                    features=decision.features,
                )
                rejection_reason = "duplicate_pending_signal"

        if rejection_reason is not None:
            if final.features is not decision.features:
                final.features = decision.features
            self._record_skip(
                mint=mint or None,
                signal_ts_ms=signal.signal_ts_ms,
                ts_ms=self._now(),
                reason=rejection_reason,
                detail=rejection_detail,
                channel=signal.channel,
                message_id=signal.message_id,
            )
        else:
            # Timer mode reproduces the research entry, which is anchored on
            # the *signal* timestamp (~5 s after the message). Trade mode keeps
            # the historical anchor: the later of signal and receipt, so it
            # never acts on a signal that was already stale when it arrived.
            if self.config.live and self.config.entry_mode != "timer":
                base_ts = max(signal.signal_ts_ms, signal.received_ts_ms)
            else:
                base_ts = signal.signal_ts_ms
            self.pending[mint] = PendingEntry(
                signal=signal,
                eligible_at_ms=base_ts + self.config.entry_delay_ms,
                expires_at_ms=base_ts + self.config.max_entry_delay_ms,
            )
            self.history.pin(mint)
            if self.on_watch is not None:
                self.on_watch(mint)
            if self.config.entry_mode == "timer":
                pending = self.pending[mint]
                pending.timer_task = self._spawn(self._timer_entry(pending))

        # Record the *final* decision once. Previously signals.jsonl could say
        # ``allowed=true`` for a signal the safety screen or duplicate check
        # rejected, which made replay/audit reconciliation impossible.
        self.signal_log.append((signal, final))
        if self.store is not None:
            self.store.record_signal(signal, final)
        return final

    # ------------------------------------------------------------------
    # Market events
    # ------------------------------------------------------------------
    async def on_trade(
        self,
        event: TradeEvent,
        *,
        await_actions: bool = False,
        history_recorded: bool = False,
    ) -> None:
        """Ingest one trade.

        ``history_recorded`` lets an A/B pair share a single history index:
        the caller adds the event once and both engines consume it without
        double-counting.
        """
        if not event.price_valid:
            return
        if not history_recorded:
            self.history.add(event)

        pending = self.pending.get(event.mint)
        if (
            pending is not None
            and event.is_buy
            and self.config.entry_mode == "trade"
        ):
            if event.timestamp_ms < pending.eligible_at_ms:
                return
            if event.timestamp_ms > pending.expires_at_ms:
                self._skip_pending(pending, "entry_delay_exceeded", None)
                return
            pending.saw_buy = True
            await self._trigger_entry(
                pending,
                trigger_ts_ms=event.timestamp_ms,
                price_sol=event.price_sol,
                decimals=event.decimals,
                await_actions=await_actions,
            )
            return

        position = self.positions.get(event.mint)
        if position is not None:
            await self._on_position_price(
                position,
                event.timestamp_ms,
                float(event.price_sol),
                await_actions=await_actions,
            )

    async def on_mark(
        self,
        mint: str,
        price_sol: float,
        ts_ms: int,
        *,
        await_actions: bool = False,
    ) -> None:
        """Apply a fallback mark (for example a Dexscreener poll)."""
        if not math.isfinite(price_sol) or price_sol <= 0:
            return
        position = self.positions.get(mint)
        if position is not None:
            await self._on_position_price(
                position, ts_ms, price_sol, await_actions=await_actions
            )

    # ------------------------------------------------------------------
    # Entry
    # ------------------------------------------------------------------
    async def _timer_entry(self, pending: PendingEntry) -> None:
        """Quote at ``signal + entry_delay`` instead of waiting for a print.

        The live PumpAPI stream is far sparser per mint than the replay
        archive: the first BUY at/after ``signal + 5 s`` arrives after a median
        of ~21 s (p90 ~49 s), while the research backtest entered at ~5.3 s.
        Waiting for that print silently turns the strategy into a late chase.
        Timer mode quotes at the research delay using the last observed feed
        price; exits, fees and the rest of the pipeline are unchanged.
        """
        wait_ms = pending.eligible_at_ms - self._now()
        if wait_ms > 0:
            await asyncio.sleep(wait_ms / 1000.0)
        if self.pending.get(pending.mint) is not pending:
            return
        trigger_ts_ms = self._now()
        if trigger_ts_ms >= pending.expires_at_ms:
            async with self._lock(pending.mint):
                if self.pending.get(pending.mint) is pending:
                    self._skip_pending(pending, "entry_delay_exceeded", None)
            return
        price_sol, _ts_ms, decimals = self.history.last_trade(pending.mint)
        if price_sol is None or price_sol <= 0:
            fallback = pending.signal.features.get("hist_last_price_sol")
            try:
                price_sol = float(fallback) if fallback is not None else None
            except (TypeError, ValueError):
                price_sol = None
        if price_sol is None or price_sol <= 0:
            async with self._lock(pending.mint):
                if self.pending.get(pending.mint) is pending:
                    self._skip_pending(
                        pending, "entry_no_reference_price", None
                    )
            return
        await self._trigger_entry(
            pending,
            trigger_ts_ms=trigger_ts_ms,
            price_sol=price_sol,
            decimals=decimals,
            await_actions=True,
        )
        # Trade mode retries a transient quote failure on the next BUY print.
        # Timer mode has no print to wait for, so retry on a short timer until
        # the entry window expires instead of dropping the trade.
        while self.pending.get(pending.mint) is pending:
            if (
                pending.last_reason is not None
                and not is_transient_quote_error(pending.last_reason)
            ):
                return
            now_ms = self._now()
            if now_ms >= pending.expires_at_ms:
                async with self._lock(pending.mint):
                    if self.pending.get(pending.mint) is pending:
                        self._skip_pending(
                            pending,
                            "entry_delay_exceeded",
                            quote_detail(
                                pending.last_reason, pending.last_detail
                            ),
                        )
                return
            # Exponential backoff keeps a persistently failing route from
            # burning ~120 quotes inside the entry window.
            backoff_s = min(8.0, 0.5 * (2 ** min(pending.attempts, 5)))
            await asyncio.sleep(
                min(backoff_s, max(0.05, (pending.expires_at_ms - now_ms) / 1000))
            )
            if self.pending.get(pending.mint) is not pending:
                return
            await self._trigger_entry(
                pending,
                trigger_ts_ms=self._now(),
                price_sol=price_sol,
                decimals=decimals,
                await_actions=True,
            )

    async def _trigger_entry(
        self,
        pending: PendingEntry,
        *,
        trigger_ts_ms: int,
        price_sol: float | None,
        decimals: int | None,
        await_actions: bool,
    ) -> None:
        if pending.in_flight:
            return
        pending.in_flight = True
        pending.attempts += 1
        pending.trigger_ts_ms = trigger_ts_ms
        event_ts_ms = trigger_ts_ms

        async def attempt() -> None:
            try:
                try:
                    quote = await self.quotes.entry_quote(
                        pending.mint,
                        self.broker.config.notional_sol,
                        price_sol,
                        decimals=decimals,
                    )
                except QuoteError as exc:
                    pending.last_reason = exc.reason
                    pending.last_detail = exc.detail
                    if not is_transient_quote_error(exc.reason):
                        async with self._lock(pending.mint):
                            self._skip_pending(
                                pending,
                                "entry_quote_failed",
                                quote_detail(exc.reason, exc.detail),
                            )
                    return

                entry_ts = (
                    max(self._now(), event_ts_ms)
                    if self.config.live
                    else event_ts_ms
                )
                entry_delay_ms = max(
                    0, entry_ts - pending.signal.signal_ts_ms
                )

                # Post-signal clause of ``exclude_crowded_pre``. The first 5 s
                # of trades and the actual entry delay are known before a fill.
                if self.gate.rule in POST_SIGNAL_CROWDED_RULES:
                    buy_share = self.history.buy_volume_share(
                        pending.mint,
                        pending.signal.signal_ts_ms,
                        pending.signal.signal_ts_ms + 5_000,
                    )
                    pre_ret = pending.signal.features.get("hist_pre_ret")
                    if post_signal_crowded(
                        pre_ret=pre_ret,
                        buy_volume_share=buy_share,
                        entry_delay_ms=entry_delay_ms,
                    ):
                        async with self._lock(pending.mint):
                            self._skip_pending(
                                pending,
                                "rule_crowded_post_signal",
                                f"bs_0_5s={buy_share};pre_ret={pre_ret};"
                                f"entry_delay_ms={entry_delay_ms}",
                            )
                        return

                try:
                    entry = self.broker.open(quote)
                except QuoteError as exc:
                    pending.last_reason = exc.reason
                    pending.last_detail = exc.detail
                    async with self._lock(pending.mint):
                        self._skip_pending(
                            pending,
                            "entry_quote_failed",
                            quote_detail(exc.reason, exc.detail),
                        )
                    return

                # Verify the position can be sold again before spending SOL on
                # the buy. This is the live-wallet failure the paper run must
                # surface. Transient failures retry on the next BUY trade.
                if self.config.preflight_exit_route:
                    try:
                        await self.quotes.exit_quote(
                            pending.mint,
                            entry.size,
                            entry.entry_price_sol,
                            decimals=decimals,
                            action="preflight",
                        )
                    except QuoteError as exc:
                        pending.last_reason = exc.reason
                        pending.last_detail = exc.detail
                        if not is_transient_quote_error(exc.reason):
                            async with self._lock(pending.mint):
                                self._skip_pending(
                                    pending,
                                    "entry_no_exit_route",
                                    quote_detail(exc.reason, exc.detail),
                                )
                        return

                async with self._lock(pending.mint):
                    if self.pending.get(pending.mint) is not pending:
                        return
                    wallet_before = self._wallet_snapshot()
                    try:
                        self.broker.reserve(entry, pending.mint)
                    except QuoteError as exc:
                        pending.last_reason = exc.reason
                        self._skip_pending(
                            pending, "entry_insufficient_sol", exc.reason
                        )
                        return
                    wallet_after = self._wallet_snapshot()
                    del self.pending[pending.mint]
                    pending.timer_task = None
                    tp_level = (
                        entry.entry_price_sol * (1.0 + self.config.tp_pct)
                        if self.config.tp_pct is not None
                        else None
                    )
                    sl_level = (
                        entry.entry_price_sol * (1.0 - self.config.sl_pct)
                        if self.config.sl_pct is not None
                        else None
                    )
                    position = Position(
                        signal=pending.signal,
                        mint=pending.mint,
                        entry_ts_ms=entry_ts,
                        entry_price_sol=entry.entry_price_sol,
                        size=entry.size,
                        entry_value_sol=entry.entry_value_sol,
                        entry_fee_sol=entry.entry_fee_sol,
                        entry_impact=entry.entry_impact,
                        horizon_ts_ms=entry_ts + self.config.horizon_ms,
                        tp_level=tp_level,
                        sl_level=sl_level,
                        last_price_sol=entry.entry_price_sol,
                        last_ts_ms=entry_ts,
                        decimals=decimals,
                        entry_quote=quote,
                        entry_trigger_ts_ms=trigger_ts_ms,
                    )
                    self.positions[pending.mint] = position
                    if self.store is not None:
                        self.store.record_entry(position)
                    self._emit(self.on_entry, position, wallet_before, wallet_after)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a task must never wedge
                LOGGER.exception(
                    "entry attempt failed for %s: %s", pending.mint, exc
                )
                pending.last_reason = f"entry_error:{type(exc).__name__}"
            finally:
                async with self._lock(pending.mint):
                    if (
                        self.pending.get(pending.mint) is pending
                        and pending.in_flight
                    ):
                        pending.in_flight = False
                        if event_ts_ms >= pending.expires_at_ms:
                            self._skip_pending(
                                pending,
                                "entry_quote_failed",
                                quote_detail(
                                    pending.last_reason, pending.last_detail
                                ),
                            )

        if await_actions:
            await attempt()
        else:
            self._spawn(attempt())

    # ------------------------------------------------------------------
    # Exit
    # ------------------------------------------------------------------
    async def _on_position_price(
        self,
        position: Position,
        ts_ms: int,
        price_sol: float,
        *,
        await_actions: bool,
    ) -> None:
        if position.exit_requested:
            return

        # A trade after the horizon must not overwrite the last in-horizon
        # price; the close uses the last trade at or before the horizon.
        if ts_ms > position.horizon_ts_ms:
            reference = position.last_price_sol or position.entry_price_sol
            exit_ts = (
                position.last_ts_ms
                if position.last_ts_ms is not None
                and position.last_ts_ms > position.entry_ts_ms
                else position.horizon_ts_ms
            )
            await self._trigger_exit(
                position,
                "horizon",
                "horizon_complete",
                reference,
                exit_ts,
                await_actions=await_actions,
            )
            return

        position.last_price_sol = price_sol
        position.last_ts_ms = ts_ms

        if position.tp_level is not None and price_sol >= position.tp_level:
            await self._trigger_exit(
                position,
                "take_profit",
                "tp_hit",
                position.tp_level,
                ts_ms,
                await_actions=await_actions,
            )
            return
        if position.sl_level is not None and price_sol <= position.sl_level:
            await self._trigger_exit(
                position,
                "stop_loss",
                "sl_hit",
                position.sl_level,
                ts_ms,
                await_actions=await_actions,
            )
            return

        # Do not exit on the first trade at the horizon timestamp: the
        # research backtest uses the last trade at or before the horizon, and
        # several events can share that millisecond. ``tick``/``finalize``
        # close after the timestamp batch has been consumed.

    async def _trigger_exit(
        self,
        position: Position,
        reason: str,
        status: str,
        reference_price: float | None,
        exit_ts_ms: int,
        *,
        await_actions: bool,
    ) -> None:
        if position.exit_requested:
            return
        position.wallet_before_exit = self._wallet_snapshot()
        position.exit_requested = True
        position.exit_reason = reason
        position.exit_status = status
        position.exit_reference_price = reference_price
        position.exit_ts_ms = exit_ts_ms
        position.exit_requested_ts_ms = self._now()
        if await_actions:
            await self._attempt_exit(position)
        else:
            self._spawn(self._attempt_exit(position))

    async def _quote_exit_any(
        self, position: Position
    ) -> tuple[FillQuote | None, float, str | None]:
        """Return the first exit fraction Jupiter can route, if any.

        A token can quote for a full position but fail for a smaller size (or
        the other way round), so the engine walks the ladder instead of
        assuming the full amount is always executable.
        """
        last_error: str | None = None
        for fraction in self.config.exit_fractions:
            fraction = min(max(float(fraction), 0.0), 1.0)
            size = position.size if fraction >= 1.0 else position.size * fraction
            if size <= DUST_SIZE:
                continue
            try:
                quote = await self.quotes.exit_quote(
                    position.mint,
                    size,
                    position.exit_reference_price,
                    decimals=position.decimals,
                )
            except QuoteError as exc:
                last_error = exc.reason
                continue
            actual = min(1.0, size / position.size) if position.size > 0 else 1.0
            return quote, actual, None
        position.last_quote_error = last_error
        return None, 0.0, last_error

    async def _attempt_exit(self, position: Position) -> None:
        if self.positions.get(position.mint) is not position:
            return
        if not position.exit_requested or position.exit_in_flight:
            return
        position.exit_in_flight = True
        position.exit_quote_attempts += 1
        last_quote: FillQuote | None = None
        try:
            while position.size > DUST_SIZE:
                if position.exit_fill_count >= self.config.exit_max_fills:
                    break
                quote, fraction, _ = await self._quote_exit_any(position)
                if quote is None:
                    break
                async with self._lock(position.mint):
                    if self.positions.get(position.mint) is not position:
                        return
                    fill = self.broker.close(position, quote, fraction=fraction)
                    self.broker.settle(position, fill)
                    self._apply_partial(position, fill)
                    last_quote = quote
                    if position.size <= DUST_SIZE:
                        self._commit_close(position, quote)
                        return

            async with self._lock(position.mint):
                if self.positions.get(position.mint) is not position:
                    return
                if position.size <= DUST_SIZE:
                    if last_quote is not None:
                        self._commit_close(position, last_quote)
                    return
                now = self._now()
                requested = position.exit_requested_ts_ms
                if requested is None:
                    requested = now
                if now - requested < self.config.exit_quote_timeout_ms:
                    return

                # Timeout expired. Only a genuine route failure is written
                # off. A timeout/5xx/auth failure is a venue outage, so mark
                # the remainder at the last observed feed price and flag the
                # trade as synthetic instead of pretending the tokens are
                # worth zero.
                transient = is_transient_quote_error(position.last_quote_error)
                if self.config.exit_fallback_feed:
                    self._finalize_fallback(position)
                elif transient:
                    self._finalize_fallback(
                        position,
                        status="forced_no_quote",
                        reason="exit_quote_unavailable",
                    )
                else:
                    self._finalize_unsold(position)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - keep the position retryable
            LOGGER.exception(
                "exit attempt failed for %s: %s", position.mint, exc
            )
            position.last_quote_error = f"exit_error:{type(exc).__name__}"
        finally:
            if self.positions.get(position.mint) is position:
                position.exit_in_flight = False

    def _apply_partial(self, position: Position, fill: ExitFill) -> None:
        """Accumulate a priced sale into the position's exit totals."""
        position.sold_size += fill.size_sold
        position.exit_price_acc += fill.exit_price_sol * fill.size_sold
        position.exit_value_acc += fill.exit_value_sol
        position.exit_fee_acc += fill.exit_fee_sol
        position.pnl_acc += fill.pnl_sol
        position.entry_basis_acc += fill.entry_basis_sol
        position.entry_fee_acc += fill.entry_fee_sol
        position.size = max(0.0, position.size - fill.size_sold)
        position.entry_value_sol = max(
            0.0, position.entry_value_sol - fill.entry_basis_sol
        )
        position.entry_fee_sol = max(
            0.0, position.entry_fee_sol - fill.entry_fee_sol
        )
        position.exit_fill_count += 1

    def _finalize_unsold(self, position: Position) -> None:
        """Write off tokens that could not be sold at any tested size.

        This is the honest live outcome when no route exists: the SOL is gone
        and the tokens are stuck. Reporting a feed-price exit here would hide
        exactly the failure a real wallet would suffer.
        """
        unsold = position.size
        if unsold <= DUST_SIZE:
            return
        position.unsold_size = unsold
        position.entry_basis_acc += position.entry_value_sol
        position.entry_fee_acc += position.entry_fee_sol
        position.pnl_acc -= position.entry_value_sol + position.entry_fee_sol
        self.broker.write_off(position)
        position.size = 0.0
        position.entry_value_sol = 0.0
        position.entry_fee_sol = 0.0
        position.exit_status = "no_route"
        position.exit_reason = "no_route"
        self._commit_close(position, FillQuote(price_sol=0.0, source="no_route"))

    def _finalize_fallback(
        self,
        position: Position,
        *,
        status: str | None = None,
        reason: str | None = None,
    ) -> None:
        """Mark the remainder at the last feed price and close the record."""
        fallback = position.last_price_sol or position.entry_price_sol
        quote = FillQuote(price_sol=fallback, source="fallback_feed")
        if position.size <= DUST_SIZE:
            if status is not None:
                position.exit_status = status
            if reason is not None:
                position.exit_reason = reason
            self._commit_close(position, quote)
            return
        try:
            fill = self.broker.close(position, quote, fraction=1.0)
            self.broker.settle(position, fill)
            self._apply_partial(position, fill)
        except (QuoteError, ValueError) as exc:
            LOGGER.warning(
                "fallback close failed for %s: %s", position.mint, exc
            )
            self._finalize_unsold(position)
            return
        if status is not None:
            position.exit_status = status
        elif position.last_price_sol is None:
            position.exit_status = "forced_no_price"
        if reason is not None:
            position.exit_reason = reason
        self._commit_close(position, quote)

    def _force_close_reference(
        self, position: Position, *, status: str, reason: str
    ) -> None:
        """Guarantee a closed trade record when shutdown work times out."""
        if self.positions.get(position.mint) is not position:
            return
        position.exit_status = status
        position.exit_reason = reason
        position.exit_requested_ts_ms = 0
        self._finalize_fallback(position, status=status, reason=reason)
        if self.positions.get(position.mint) is position:
            # Last resort: record the position without mutating the wallet.
            position.exit_status = status
            position.exit_reason = reason
            position.exit_ts_ms = (
                position.last_ts_ms or position.exit_ts_ms or self._now()
            )
            self._commit_close(
                position, FillQuote(price_sol=0.0, source="forced")
            )

    def _commit_close(
        self, position: Position, quote: FillQuote, *, forced: bool = False
    ) -> None:
        if self.positions.get(position.mint) is not position:
            return
        last_observed, _ = self.history.last_price(position.mint)
        exit_status = position.exit_status or "horizon_complete"
        if forced and quote.source == "fallback_feed":
            exit_status = "forced_no_price"
        exit_ts = (
            position.exit_ts_ms or position.exit_requested_ts_ms or position.entry_ts_ms
        )
        synthetic = quote.source in {"fallback_feed", "forced", "no_route"} or (
            position.last_ts_ms is not None
            and position.last_ts_ms <= position.entry_ts_ms
            and position.exit_reason == "horizon"
        )
        cost_basis = position.entry_basis_acc + position.entry_fee_acc
        total_size = position.sold_size + position.unsold_size
        avg_exit_price = (
            position.exit_price_acc / position.sold_size
            if position.sold_size > 0
            else 0.0
        )
        entry_quote = position.entry_quote
        trade = ClosedTrade(
            mint=position.mint,
            channel=position.signal.channel,
            message_id=position.signal.message_id,
            signal_ts_ms=position.signal.signal_ts_ms,
            entry_ts_ms=position.entry_ts_ms,
            entry_trigger_ts_ms=position.entry_trigger_ts_ms,
            exit_ts_ms=exit_ts,
            expected_exit_ts_ms=position.horizon_ts_ms,
            entry_price_sol=position.entry_price_sol,
            exit_price_sol=avg_exit_price,
            last_observed_price_sol=last_observed,
            size=total_size,
            notional_sol=self.broker.config.notional_sol,
            entry_value_sol=position.entry_basis_acc,
            entry_fee_sol=position.entry_fee_acc,
            exit_value_sol=position.exit_value_acc,
            exit_fee_sol=position.exit_fee_acc,
            pnl_sol=position.pnl_acc,
            return_pct=position.pnl_acc / cost_basis if cost_basis > 0 else 0.0,
            exit_reason=position.exit_reason or "horizon",
            exit_status=exit_status,
            replay_truncated=exit_status == "replay_truncated",
            exit_synthetic=synthetic,
            hold_ms=exit_ts - position.entry_ts_ms,
            entry_delay_ms=position.entry_ts_ms - position.signal.signal_ts_ms,
            entry_impact=position.entry_impact,
            exit_impact=self.broker.config.slippage,
            entry_quote_source=entry_quote.source if entry_quote else "unknown",
            exit_quote_source=quote.source,
            entry_quote_price_impact_pct=(
                entry_quote.price_impact_pct if entry_quote else None
            ),
            exit_quote_price_impact_pct=quote.price_impact_pct,
            entry_quote_router=entry_quote.router if entry_quote else None,
            exit_quote_router=quote.router,
            entry_quote_request_id=entry_quote.request_id if entry_quote else None,
            exit_quote_request_id=quote.request_id,
            exit_quote_attempts=position.exit_quote_attempts,
            last_quote_error=position.last_quote_error,
            rule=self.gate.rule,
            features=dict(position.signal.features),
            exit_fill_count=position.exit_fill_count,
            unsold_size=position.unsold_size,
        )
        self.closed.append(trade)
        if self.store is not None:
            self.store.record_trade(trade)
        self.positions.pop(position.mint, None)
        self._emit(
            self.on_close,
            trade,
            position.wallet_before_exit,
            self._wallet_snapshot(),
        )
        self._release_mint(position.mint)

    # ------------------------------------------------------------------
    # Timers / shutdown
    # ------------------------------------------------------------------
    async def tick(
        self, now_ms: int | None = None, *, await_actions: bool = False
    ) -> None:
        now = self._now() if now_ms is None else int(now_ms)

        for pending in list(self.pending.values()):
            if pending.in_flight:
                continue
            if now > pending.expires_at_ms:
                reason = (
                    "entry_quote_failed"
                    if pending.saw_buy and pending.last_reason
                    else "no_buy_trade_after_signal"
                )
                self._skip_pending(
                    pending,
                    reason,
                    quote_detail(pending.last_reason, pending.last_detail),
                )

        for position in list(self.positions.values()):
            if position.exit_requested:
                if not position.exit_in_flight:
                    if await_actions:
                        await self._attempt_exit(position)
                    else:
                        self._spawn(self._attempt_exit(position))
                continue
            if now >= position.horizon_ts_ms + self.config.horizon_settle_ms:
                reference = position.last_price_sol or position.entry_price_sol
                exit_ts = (
                    position.last_ts_ms
                    if position.last_ts_ms is not None
                    and position.last_ts_ms > position.entry_ts_ms
                    else position.horizon_ts_ms
                )
                await self._trigger_exit(
                    position,
                    "horizon",
                    "horizon_complete",
                    reference,
                    exit_ts,
                    await_actions=await_actions,
                )

    async def _drain_tasks(self, timeout_ms: int | None) -> None:
        tasks = [task for task in self._tasks if not task.done()]
        if not tasks:
            return
        if timeout_ms is None:
            await asyncio.gather(*tasks, return_exceptions=True)
            return
        _, pending = await asyncio.wait(
            tasks, timeout=max(0, timeout_ms) / 1000
        )
        if pending:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    async def finalize(
        self,
        data_end_ts_ms: int | None = None,
        *,
        force: bool = False,
        timeout_ms: int | None = None,
    ) -> None:
        for pending in list(self.pending.values()):
            if pending.last_reason is not None:
                reason = "entry_quote_failed"
            elif self.config.entry_mode == "timer":
                reason = "entry_timer_no_fill"
            else:
                reason = "no_buy_trade_after_signal"
            self._skip_pending(
                pending,
                reason,
                quote_detail(pending.last_reason, pending.last_detail),
            )

        for position in list(self.positions.values()):
            if position.exit_requested:
                if force:
                    position.exit_requested_ts_ms = 0
                if not position.exit_in_flight:
                    self._spawn(self._attempt_exit(position))
                continue

            truncated = (
                data_end_ts_ms is not None and position.horizon_ts_ms > data_end_ts_ms
            )
            if truncated:
                status = "replay_truncated"
                reason = "replay_truncated"
                exit_ts = position.last_ts_ms or position.entry_ts_ms
            elif force and position.horizon_ts_ms > self._now():
                # Live shutdown before the horizon: close at the last observed
                # price, but do not report it as a completed holding period.
                status = "forced_shutdown"
                reason = "shutdown"
                exit_ts = position.last_ts_ms or position.entry_ts_ms
            else:
                status = "horizon_complete"
                reason = "horizon"
                if (
                    position.last_ts_ms is not None
                    and position.last_ts_ms > position.entry_ts_ms
                ):
                    exit_ts = position.last_ts_ms
                else:
                    exit_ts = position.horizon_ts_ms
            reference = position.last_price_sol or position.entry_price_sol
            await self._trigger_exit(
                position,
                reason,
                status,
                reference,
                exit_ts,
                await_actions=False,
            )
            if force:
                # Skip the normal quote-timeout grace period on shutdown.
                position.exit_requested_ts_ms = 0

        effective_timeout = timeout_ms
        if force and effective_timeout is None:
            effective_timeout = self.config.shutdown_timeout_ms
        await self._drain_tasks(effective_timeout)

        if force:
            # Never lose an open position on shutdown. If Jupiter is still
            # hanging or returning errors, record a synthetic close at the last
            # observed price and flag it in the report.
            for position in list(self.positions.values()):
                self._force_close_reference(
                    position,
                    status=position.exit_status or "forced_shutdown",
                    reason=position.exit_reason or "shutdown",
                )
