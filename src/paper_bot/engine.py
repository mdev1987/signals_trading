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
import json
import logging
import math
import os
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
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
from .quotes import (
    QuoteError,
    is_terminal_exit_error,
    is_transient_quote_error,
)
from .safety import safety_check
from .signals import (
    POST_SIGNAL_CROWDED_RULES,
    GateConfig,
    evaluate_gate,
    post_signal_crowded,
)
from .wallet import DUST_SIZE

LOGGER = logging.getLogger(__name__)

# Version 1 is the only supported on-disk daily-risk schema. Bump it when the
# format changes so an older binary refuses to guess at a newer file.
RISK_STATE_VERSION = 1


def _reject_json_constant(value: str) -> None:
    """``json.loads`` hook that refuses NaN/Infinity instead of accepting it."""
    raise ValueError(f"non-finite JSON constant {value!r}")


def _is_utc_day(value: str) -> bool:
    """True only for a canonical ``YYYY-MM-DD`` calendar date."""
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except (TypeError, ValueError):
        return False
    return parsed.strftime("%Y-%m-%d") == value


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
    max_entry_delay_ms: int = 60_000
    # Skip a signal Telegram delivered more than this long after the channel
    # posted it. The research convention prices the first BUY at card + 5 s and
    # gives up at card + 60 s; a card that arrives minutes late would otherwise
    # be traded at a fill the backtest never measured. 0 disables the check.
    max_delivery_lag_ms: int = 60_000
    # Cap simultaneous positions and pending entries. The scanner backtest
    # assumed at most 10 concurrent; the live engine had no cap, so a burst
    # could open more than the evidence covers. 0 means unlimited.
    max_concurrent_positions: int = 10
    # Optional daily loss circuit breaker, in SOL of realized PnL (UTC day).
    # 0 disables it. It is a risk control, not part of the backtested edge.
    max_daily_loss_sol: float = 0.0
    # Reject a BUY print below this canonical SOL notional as an entry trigger.
    # This is the same floor ``alert_outcomes.py --min-quote-sol`` applies, so a
    # dust print cannot trigger a live entry the replay would have skipped.
    min_entry_quote_sol: float = 0.01
    # Timer mode quotes at a time, not on a print. Reject a reference price
    # older than this (0 disables) instead of entering on a dead mark.
    max_reference_age_ms: int = 30_000
    # A mark older than this is excluded from NAV (0 disables); the conservative
    # equity reports cash plus fresh marks only.
    max_mark_age_ms: int = 120_000
    # Timer mode schedules a quote at ``card + entry_delay``. If the card is
    # delivered after that time, quoting at receipt is a late entry the replay
    # never measured; allow only this much scheduling jitter.
    timer_late_tolerance_ms: int = 1_000
    # A feed timestamp this far in the future is a clock/data error, not a
    # fresh mark. Historical ages are clamped to zero; excessive future skew is
    # rejected instead of being reported as current.
    max_future_skew_ms: int = 5_000
    # Stable location for the daily realized-PnL state. Timestamped run
    # directories change on every restart, so the circuit breaker must not
    # depend on them. None keeps the legacy per-run store lookup.
    risk_state_path: Path | None = None
    # "trade": wait for the first BUY print at/after the delay (historical
    # behaviour). "timer": quote at the delay regardless of prints, which is
    # what the research backtest assumes (entry at signal + ~5 s).
    entry_mode: str = "trade"
    # Trade mode re-quotes on every BUY print. A pool that is merely unindexed
    # emits several prints a second, so without a gap one print becomes a
    # burst of quote requests. One attempt per mint per second is enough to
    # catch the first routable print inside the entry window.
    entry_retry_backoff_ms: int = 1_000
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
        if self.entry_delay_ms < 0 or self.max_entry_delay_ms < 0:
            raise ValueError("entry delays must be non-negative")
        if self.max_entry_delay_ms < self.entry_delay_ms:
            raise ValueError("max_entry_delay_ms must be >= entry_delay_ms")
        if self.max_delivery_lag_ms < 0:
            raise ValueError("max_delivery_lag_ms must be non-negative")
        if self.max_concurrent_positions < 0:
            raise ValueError("max_concurrent_positions must be non-negative")
        if self.max_daily_loss_sol < 0:
            raise ValueError("max_daily_loss_sol must be non-negative")
        if self.min_entry_quote_sol < 0:
            raise ValueError("min_entry_quote_sol must be non-negative")
        if self.max_reference_age_ms < 0:
            raise ValueError("max_reference_age_ms must be non-negative")
        if self.max_mark_age_ms < 0:
            raise ValueError("max_mark_age_ms must be non-negative")
        if self.timer_late_tolerance_ms < 0:
            raise ValueError("timer_late_tolerance_ms must be non-negative")
        if self.max_future_skew_ms < 0:
            raise ValueError("max_future_skew_ms must be non-negative")


def entry_backoff_active(
    pending: PendingEntry, *, event_ts_ms: int, backoff_ms: int
) -> bool:
    """Return whether a failed-entry retry must wait for a later print.

    A pool that is merely unindexed emits several BUY prints a second, and each
    quote attempt walks the slippage ladder. Without this gap one print becomes
    a burst of requests; one attempt per mint per interval is enough to catch
    the first routable print inside the entry window.
    """
    return (
        backoff_ms > 0
        and pending.trigger_ts_ms is not None
        and event_ts_ms - pending.trigger_ts_ms < backoff_ms
    )


# Monetary/quantity fields that must stay finite in a restored snapshot; a
# NaN here would silently break the exit totals or the horizon mark.
_RESTORED_POSITION_NUMBERS = (
    "entry_price_sol",
    "size",
    "entry_value_sol",
    "entry_fee_sol",
    "entry_impact",
    "sold_size",
    "exit_price_acc",
    "exit_value_acc",
    "exit_fee_acc",
    "pnl_acc",
    "entry_basis_acc",
    "entry_fee_acc",
    "unsold_size",
)


def _validate_restored_position(position: Position) -> None:
    """Reject a restored position whose numbers cannot be trusted."""
    for name in _RESTORED_POSITION_NUMBERS:
        value = getattr(position, name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise RuntimeError(
                f"recovery state has a non-numeric {name} for {position.mint}"
            )
        if not math.isfinite(value):
            raise RuntimeError(
                f"recovery state has a non-finite {name} for {position.mint}"
            )
    if position.entry_price_sol <= 0:
        raise RuntimeError(
            f"recovery state has a non-positive entry price for {position.mint}"
        )
    if position.size < 0:
        raise RuntimeError(
            f"recovery state has a negative size for {position.mint}"
        )
    if position.horizon_ts_ms < position.entry_ts_ms:
        raise RuntimeError(
            f"recovery state has a horizon before entry for {position.mint}"
        )


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
        on_state_change: Callable[[dict[str, Any]], None] | None = None,
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
        self.on_state_change = on_state_change

        self.pending: dict[str, PendingEntry] = {}
        self.positions: dict[str, Position] = {}
        self.closed: list[ClosedTrade] = []
        self.skips: list[SkipRecord] = []
        self.signal_log: list[tuple[Signal, GateDecision]] = []
        self._locks: dict[str, asyncio.Lock] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        # Realized PnL by UTC day, used only by the optional daily loss cap.
        # The stable risk-state file wins over the per-run store; the run
        # directory changes on every restart, so it cannot be the only source.
        self._daily_pnl: dict[str, float] = {}
        self._risk_state_error: str | None = None
        if self.config.risk_state_path is not None:
            if self.config.max_daily_loss_sol > 0:
                # Fail closed at startup. A corrupt or unwritable state path,
                # or a state file that vanished after a previous initialization,
                # means the breaker cannot be trusted after a restart, so
                # refusing to start beats running naked.
                self._initialize_risk_state_strict()
            else:
                # Cap disabled: best effort only. A missing/corrupt file or an
                # unwritable directory must not stop a paper run that does not
                # use the breaker.
                try:
                    self._daily_pnl = self._load_risk_state()
                except RuntimeError:
                    LOGGER.exception(
                        "ignoring unreadable risk state; daily cap is disabled"
                    )
                    self._daily_pnl = {}
                if not self._daily_pnl:
                    self._daily_pnl = self._restore_daily_pnl_from_store()
                self._persist_daily_pnl()
        else:
            self._daily_pnl = self._restore_daily_pnl_from_store()

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

    def wallet_snapshot(self) -> dict[str, Any] | None:
        """Snapshot cash plus open token inventory, with mark freshness.

        ``PaperWallet.equity_sol`` treats an unmarked token as zero, so calling
        ``snapshot()`` while a position is open used to report cash only. The
        history holds the last print for every watched mint, but a mark can be
        minutes old on a dead pool; reporting it as current NAV overstates the
        portfolio. The returned ``equity_sol`` is therefore cash plus *fresh*
        marks only. ``equity_all_marks_sol`` is the optimistic view, and the
        per-mint detail makes staleness auditable.
        """
        wallet = getattr(self.broker, "wallet", None)
        if wallet is None:
            return None
        now_ms = self._now()
        marks: dict[str, float] = {}
        details: dict[str, dict[str, Any]] = {}
        fresh_value = 0.0
        all_value = 0.0
        stale = 0
        unmarked = 0
        future_marks = 0
        for mint, size in list(wallet.token_balances.items()):
            price, ts_ms = self.history.last_price(mint)
            age_ms = None if ts_ms is None else now_ms - int(ts_ms)
            future = (
                age_ms is not None
                and age_ms < -self.config.max_future_skew_ms
            )
            fresh = (
                price is not None
                and price > 0
                and not future
                and (
                    self.config.max_mark_age_ms <= 0
                    or (age_ms is not None and age_ms <= self.config.max_mark_age_ms)
                )
            )
            if price is not None and price > 0:
                marks[mint] = price
                all_value += size * price
                if fresh:
                    fresh_value += size * price
                else:
                    stale += 1
                    if future:
                        future_marks += 1
            else:
                unmarked += 1
            details[mint] = {
                "size": size,
                "price_sol": price,
                "mark_ts_ms": ts_ms,
                "mark_age_ms": age_ms,
                "future": future,
                "fresh": fresh,
            }
        snapshot = wallet.snapshot(marks)
        snapshot.update(
            {
                "cash_sol": wallet.sol_balance,
                "equity_sol": wallet.sol_balance + fresh_value,
                "equity_all_marks_sol": wallet.sol_balance + all_value,
                "marked_token_sol": fresh_value,
                "stale_mark_count": stale,
                "future_mark_count": future_marks,
                "unmarked_token_count": unmarked,
                "mark_details": details,
                "max_mark_age_ms": self.config.max_mark_age_ms,
                "max_future_skew_ms": self.config.max_future_skew_ms,
            }
        )
        return snapshot

    # ------------------------------------------------------------------
    # Restart recovery (paper mode)
    # ------------------------------------------------------------------
    def export_state(self) -> dict[str, Any]:
        """Return the restartable wallet/position snapshot.

        Pending entries are deliberately omitted: they live for at most the
        60 s entry window and cannot be re-created with their original market
        prints after a restart.
        """
        wallet = getattr(self.broker, "wallet", None)
        wallet_state: dict[str, Any] = {}
        token_balances: dict[str, float] = {}
        if wallet is not None:
            wallet_state = {
                "sol_balance": float(wallet.sol_balance),
                "fees_paid_sol": float(wallet.fees_paid_sol),
                "entries": int(wallet.entries),
                "exits": int(wallet.exits),
                "write_offs": int(wallet.write_offs),
            }
            token_balances = {
                str(mint): float(size)
                for mint, size in wallet.token_balances.items()
            }
        return {
            "wallet": wallet_state,
            "token_balances": token_balances,
            "positions": [
                position.to_state_dict()
                for position in self.positions.values()
            ],
        }

    def restore_state(self, payload: dict[str, Any]) -> None:
        """Apply a snapshot produced by :meth:`export_state`.

        Positions already closed in ``trades.jsonl`` are dropped, which makes
        a crash between the trade write and the snapshot write safe: the
        position is not closed a second time.
        """
        wallet = getattr(self.broker, "wallet", None)
        if wallet is None:
            raise RuntimeError("cannot restore state without a paper wallet")
        wallet_state = payload.get("wallet")
        if not isinstance(wallet_state, dict):
            raise RuntimeError("recovery state has no wallet object")
        balances = payload.get("token_balances")
        if not isinstance(balances, dict):
            raise RuntimeError("recovery state has no token balances")
        positions = payload.get("positions")
        if not isinstance(positions, list):
            raise RuntimeError("recovery state has no position list")

        try:
            sol_balance = float(wallet_state["sol_balance"])
            fees_paid = float(wallet_state.get("fees_paid_sol", 0.0))
            entries = int(wallet_state.get("entries", 0))
            exits = int(wallet_state.get("exits", 0))
            write_offs = int(wallet_state.get("write_offs", 0))
            restored_balances = {
                str(mint): float(size) for mint, size in balances.items()
            }
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"recovery state has malformed wallet data: {exc}"
            ) from exc
        for label, value in (
            ("sol_balance", sol_balance),
            ("fees_paid_sol", fees_paid),
            *((f"token balance {mint}", size) for mint, size in restored_balances.items()),
        ):
            if not math.isfinite(value):
                raise RuntimeError(
                    f"recovery state has a non-finite {label}"
                )

        closed: set[tuple[str, int]] = set()
        loader = getattr(self.store, "closed_position_keys", None)
        if callable(loader):
            try:
                closed = set(loader())
            except Exception:  # noqa: BLE001 - do not abort a restart for this
                LOGGER.exception("could not read closed positions for recovery")

        restored: dict[str, Position] = {}
        for item in positions:
            try:
                position = Position.from_state_dict(item)
                key = (position.mint, int(position.entry_ts_ms))
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    f"recovery state has a malformed position: {exc}"
                ) from exc
            _validate_restored_position(position)
            # A crash cannot leave a quote in flight: reset the guard so the
            # ticker may retry the exit instead of waiting forever.
            position.exit_in_flight = False
            if key in closed:
                LOGGER.warning(
                    "recovery skipped already-closed position %s @ %s",
                    position.mint,
                    position.entry_ts_ms,
                )
                continue
            restored[position.mint] = position

        wallet.sol_balance = sol_balance
        wallet.fees_paid_sol = fees_paid
        wallet.entries = entries
        wallet.exits = exits
        wallet.write_offs = write_offs
        wallet.token_balances = restored_balances
        # Update in place: the ticker and shutdown hooks may hold this dict.
        self.positions.clear()
        self.positions.update(restored)

    def persist_state(self) -> None:
        """Write a recovery snapshot now (no-op without a state callback)."""
        self._notify_state_change()

    def _notify_state_change(self) -> None:
        """Persist a recovery snapshot after an entry, partial or close."""
        if self.on_state_change is None:
            return
        try:
            self.on_state_change(self.export_state())
        except Exception:  # noqa: BLE001 - never wedge trading on a snapshot
            LOGGER.exception("could not persist restart state")

    @staticmethod
    def _day_key(ts_ms: int) -> str:
        return datetime.fromtimestamp(ts_ms / 1000, UTC).date().isoformat()

    def _day_pnl(self, ts_ms: int) -> float:
        return self._daily_pnl.get(self._day_key(ts_ms), 0.0)

    def _risk_marker_path(self) -> Path | None:
        """Marker that distinguishes a first install from a deleted state file."""
        path = self.config.risk_state_path
        if path is None:
            return None
        return path.with_name(path.name + ".initialized")

    def _initialize_risk_state_strict(self) -> None:
        """Load or bootstrap the breaker state, failing closed on any doubt.

        Called only when ``max_daily_loss_sol`` is enabled. A missing state
        file is legitimate on the very first start, but not after the marker
        file has recorded an earlier initialization: silently starting at zero
        would reset the breaker.
        """
        path = self.config.risk_state_path
        marker = self._risk_marker_path()
        assert path is not None and marker is not None
        if path.exists():
            self._daily_pnl = self._load_risk_state()
        elif marker.exists():
            raise RuntimeError(
                f"daily risk state at {path} is missing although {marker} "
                "records a previous initialization; refusing to reset the "
                "loss breaker"
            )
        else:
            # First initialization for this lane. Recover today's PnL from
            # this run's trade log if one exists. A missing log is a normal
            # fresh start; an unreadable one fails closed.
            self._daily_pnl = self._restore_daily_pnl_from_store(strict=True)
        self._persist_daily_pnl(raise_on_error=True)
        self._write_risk_marker(raise_on_error=True)

    def _restore_daily_pnl_from_store(
        self, *, strict: bool = False
    ) -> dict[str, float]:
        loader = getattr(self.store, "realized_pnl_by_day", None)
        if not callable(loader):
            return {}
        try:
            restored = loader()
        except Exception as exc:  # noqa: BLE001 - a corrupt tail must not stop the bot
            LOGGER.exception("could not restore daily PnL from the store")
            if strict:
                raise RuntimeError(
                    "cannot recover daily PnL from the trade history while "
                    "the daily loss cap is enabled"
                ) from exc
            return {}
        if not isinstance(restored, dict):
            if strict:
                raise RuntimeError(
                    "trade history returned a non-dict daily PnL payload"
                )
            return {}
        result: dict[str, float] = {}
        for key, value in restored.items():
            try:
                number = float(value)
            except (TypeError, ValueError, OverflowError):
                if strict:
                    raise RuntimeError(
                        f"trade history has a non-numeric daily PnL for {key!r}"
                    ) from None
                continue
            if not math.isfinite(number):
                if strict:
                    raise RuntimeError(
                        f"trade history has a non-finite daily PnL for {key!r}"
                    )
                continue
            result[str(key)] = number
        return result

    def _load_risk_state(self) -> dict[str, float]:
        """Read the stable daily-PnL file, or start empty if it is absent.

        An unreadable, corrupt, mistyped, non-finite or version-mismatched file
        is not a fresh start: silently resetting to zero would disable the
        circuit breaker the operator asked for, so startup fails instead.
        """
        path = self.config.risk_state_path
        assert path is not None
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise RuntimeError(
                f"cannot read daily risk state at {path}: {exc}"
            ) from exc
        try:
            payload = json.loads(raw, parse_constant=_reject_json_constant)
        except (json.JSONDecodeError, ValueError) as exc:
            raise RuntimeError(
                f"daily risk state at {path} is not valid JSON: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise RuntimeError(
                f"daily risk state at {path} is not a JSON object"
            )
        version = payload.get("version")
        if version != RISK_STATE_VERSION:
            raise RuntimeError(
                f"daily risk state at {path} has unsupported version "
                f"{version!r}; expected {RISK_STATE_VERSION}"
            )
        days = payload.get("days")
        if not isinstance(days, dict):
            raise RuntimeError(
                f"daily risk state at {path} has no 'days' object"
            )
        restored: dict[str, float] = {}
        for key, value in days.items():
            if not isinstance(key, str) or not _is_utc_day(key):
                raise RuntimeError(
                    f"daily risk state at {path} has an invalid UTC day "
                    f"{key!r}"
                )
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
            ):
                raise RuntimeError(
                    f"daily risk state at {path} has a non-finite or "
                    f"non-numeric value for {key!r}"
                )
            restored[key] = float(value)
        return restored

    def _write_risk_marker(self, *, raise_on_error: bool = False) -> None:
        """Record that this lane has initialized its risk state once."""
        marker = self._risk_marker_path()
        if marker is None:
            return
        payload = {
            "version": RISK_STATE_VERSION,
            "initialized_at": datetime.now(UTC).isoformat(),
        }
        tmp = marker.with_name(marker.name + ".tmp")
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            os.replace(tmp, marker)
        except OSError as exc:
            self._risk_state_error = f"{type(exc).__name__}: {exc}"
            LOGGER.exception("could not write risk-state marker %s", marker)
            if raise_on_error:
                raise RuntimeError(
                    f"cannot write risk-state marker at {marker}: {exc}"
                ) from exc
            return
        self._risk_state_error = None

    def _persist_daily_pnl(self, *, raise_on_error: bool = False) -> None:
        """Atomically write today's realized PnL to the stable risk state."""
        path = self.config.risk_state_path
        if path is None:
            return
        for key, value in self._daily_pnl.items():
            if not math.isfinite(value):
                self._risk_state_error = (
                    f"non-finite daily PnL for {key!r}"
                )
                LOGGER.error(
                    "refusing to persist non-finite daily PnL for %s", key
                )
                if raise_on_error:
                    raise RuntimeError(
                        f"cannot persist non-finite daily PnL for {key!r}"
                    )
                return
        payload = {
            "version": RISK_STATE_VERSION,
            "updated_at": datetime.now(UTC).isoformat(),
            "days": dict(sorted(self._daily_pnl.items())),
        }
        tmp = path.with_name(path.name + ".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            self._risk_state_error = f"{type(exc).__name__}: {exc}"
            LOGGER.exception("could not persist daily risk state to %s", path)
            if raise_on_error:
                raise RuntimeError(
                    f"cannot persist daily risk state at {path}: {exc}"
                ) from exc
            return
        self._risk_state_error = None

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

    def _entry_expiry(
        self, pending: PendingEntry, default_reason: str
    ) -> tuple[str, str | None]:
        """Classify why an unfilled entry window closed.

        A window that saw only dust or unclassifiable BUY prints must say so in
        ``skips.jsonl``; otherwise the operator cannot distinguish "the token
        never traded" from "the feed delivered sub-cent noise".
        """
        reason = default_reason
        detail = quote_detail(pending.last_reason, pending.last_detail)
        if pending.last_reason is not None:
            reason = "entry_quote_failed"
        elif pending.dust_prints and not pending.saw_buy:
            reason = "entry_dust_prints_only"
        elif pending.unverified_prints and not pending.saw_buy:
            reason = "entry_unverified_quote_notional"
        if pending.dust_prints or pending.unverified_prints:
            counts = (
                f"dust_prints={pending.dust_prints};"
                f"unverified_prints={pending.unverified_prints}"
            )
            detail = f"{counts};{detail}" if detail else counts
        return reason, detail

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
            # Both entry modes anchor on the card timestamp, exactly like the
            # research convention (eligible at card + 5 s, expires at
            # card + 60 s). Using ``max(signal, received)`` shifted the whole
            # window forward by the Telegram lag, which made the live fill a
            # different trade from the one the backtest measured.
            base_ts = signal.signal_ts_ms
            eligible_at_ms = base_ts + self.config.entry_delay_ms
            expires_at_ms = base_ts + self.config.max_entry_delay_ms
            received_ms = signal.received_ts_ms
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
            elif (
                self.config.max_delivery_lag_ms > 0
                and received_ms > 0
                and received_ms - signal.signal_ts_ms
                > self.config.max_delivery_lag_ms
            ):
                # The card itself is stale. Both entry modes anchor on the
                # card, so acting now would price a fill the research
                # convention never measured.
                lag_ms = received_ms - signal.signal_ts_ms
                final = GateDecision(
                    allowed=False,
                    rule=decision.rule,
                    reasons=["delivery_lag_exceeded"],
                    features=decision.features,
                )
                rejection_reason = "delivery_lag_exceeded"
                rejection_detail = (
                    f"delivery_lag_ms={lag_ms};"
                    f"max={self.config.max_delivery_lag_ms}"
                )
            elif received_ms > 0 and received_ms >= expires_at_ms:
                # Telegram delivered the card after the research entry window
                # had already closed. Never open a fresh window in the present.
                final = GateDecision(
                    allowed=False,
                    rule=decision.rule,
                    reasons=["entry_window_passed"],
                    features=decision.features,
                )
                rejection_reason = "entry_window_passed"
                rejection_detail = (
                    f"received_ts_ms={received_ms};"
                    f"expires_at_ms={expires_at_ms}"
                )
            elif (
                self.config.entry_mode == "timer"
                and received_ms > 0
                and received_ms
                > eligible_at_ms + self.config.timer_late_tolerance_ms
            ):
                # Timer mode must quote at card + 5 s. A card delivered after
                # that time would be priced at receipt, a different (and
                # systematically worse) fill than the research convention.
                final = GateDecision(
                    allowed=False,
                    rule=decision.rule,
                    reasons=["entry_timer_late"],
                    features=decision.features,
                )
                rejection_reason = "entry_timer_late"
                rejection_detail = (
                    f"received_ts_ms={received_ms};"
                    f"eligible_at_ms={eligible_at_ms};"
                    f"tolerance_ms={self.config.timer_late_tolerance_ms}"
                )
            elif (
                self.config.entry_mode == "trade"
                and received_ms > eligible_at_ms
                and (
                    missed_ts := self.history.first_buy_between(
                        mint,
                        eligible_at_ms,
                        min(received_ms, expires_at_ms),
                        min_notional_sol=self.config.min_entry_quote_sol,
                    )
                )
                is not None
            ):
                # The first BUY print at/after card + 5 s already happened
                # before Telegram delivered the card. The research entry is
                # that print, not a later one, and the bot cannot buy in the
                # past, so the signal is not tradable under this convention.
                final = GateDecision(
                    allowed=False,
                    rule=decision.rule,
                    reasons=["entry_missed_eligible_print"],
                    features=decision.features,
                )
                rejection_reason = "entry_missed_eligible_print"
                rejection_detail = (
                    f"first_print_ts_ms={missed_ts};"
                    f"eligible_at_ms={eligible_at_ms};"
                    f"received_ts_ms={received_ms}"
                )
            elif (
                self.config.max_concurrent_positions > 0
                and len(self.positions) + len(self.pending)
                >= self.config.max_concurrent_positions
            ):
                # Count pending entries too: they will become positions, and
                # the backtest only covers the configured number at once.
                final = GateDecision(
                    allowed=False,
                    rule=decision.rule,
                    reasons=["max_concurrent_positions"],
                    features=decision.features,
                )
                rejection_reason = "max_concurrent_positions"
                rejection_detail = (
                    f"open={len(self.positions)};pending={len(self.pending)};"
                    f"max={self.config.max_concurrent_positions}"
                )
            elif (
                self.config.max_daily_loss_sol > 0
                and self._risk_state_error is not None
            ):
                # The breaker is enabled but its state cannot be persisted, so
                # a restart could forget today's loss. Stop opening positions
                # rather than running unprotected; exits still proceed.
                final = GateDecision(
                    allowed=False,
                    rule=decision.rule,
                    reasons=["risk_state_unavailable"],
                    features=decision.features,
                )
                rejection_reason = "risk_state_unavailable"
                rejection_detail = self._risk_state_error
            elif (
                self.config.max_daily_loss_sol > 0
                and self._day_pnl(self._now())
                <= -self.config.max_daily_loss_sol
            ):
                # A risk control, not part of the backtested edge: stop opening
                # new positions once the UTC day's realized loss hits the cap.
                final = GateDecision(
                    allowed=False,
                    rule=decision.rule,
                    reasons=["daily_loss_limit"],
                    features=decision.features,
                )
                rejection_reason = "daily_loss_limit"
                rejection_detail = (
                    f"day={self._day_key(self._now())};"
                    f"pnl={self._day_pnl(self._now()):.6f};"
                    f"limit=-{self.config.max_daily_loss_sol:.6f}"
                )

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
            self.pending[mint] = PendingEntry(
                signal=signal,
                eligible_at_ms=eligible_at_ms,
                expires_at_ms=expires_at_ms,
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
            if self.config.min_entry_quote_sol > 0:
                # Mirror ``alert_outcomes.py --min-quote-sol`` exactly. The
                # replay drops a sub-cent dust print whose priceSol is a feed
                # artifact; if the live engine accepted it, the recorded entry
                # would not be the trade the 97.2 % backtest measured. The
                # notional is canonical (quote-mint aware), not raw
                # quoteAmount, so USDC-quoted prints are compared as SOL.
                notional_sol = event.quote_notional_sol
                if notional_sol is None:
                    # Cannot classify — not a usable trigger, but keep the
                    # window open for a print whose notional is known.
                    pending.unverified_prints += 1
                    return
                if notional_sol < self.config.min_entry_quote_sol:
                    pending.dust_prints += 1
                    return
            if entry_backoff_active(
                pending,
                event_ts_ms=event.timestamp_ms,
                backoff_ms=self.config.entry_retry_backoff_ms,
            ):
                return
            pending.saw_buy = True
            pending.reference_price_sol = float(event.price_sol)
            pending.reference_ts_ms = event.timestamp_ms
            pending.reference_age_ms = 0
            pending.reference_source = "trade"
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
        if (
            trigger_ts_ms
            > pending.eligible_at_ms + self.config.timer_late_tolerance_ms
        ):
            # The timer woke after the scheduled entry, for example because
            # the signal arrived late or the loop stalled. Quoting now would
            # record a delayed entry as a timer entry.
            async with self._lock(pending.mint):
                if self.pending.get(pending.mint) is pending:
                    self._skip_pending(
                        pending,
                        "entry_timer_late",
                        f"trigger_ts_ms={trigger_ts_ms};"
                        f"eligible_at_ms={pending.eligible_at_ms};"
                        f"tolerance_ms={self.config.timer_late_tolerance_ms}",
                    )
            return
        price_sol, reference_ts_ms, decimals = self.history.last_trade(
            pending.mint
        )
        reference_source = "history"
        if price_sol is None or price_sol <= 0:
            fallback = pending.signal.features.get("hist_last_price_sol")
            fallback_ts = pending.signal.features.get("hist_last_ts_ms")
            try:
                price_sol = float(fallback) if fallback is not None else None
            except (TypeError, ValueError):
                price_sol = None
            try:
                reference_ts_ms = (
                    int(fallback_ts) if fallback_ts is not None else None
                )
            except (TypeError, ValueError):
                reference_ts_ms = None
            reference_source = "signal_meta"
        if price_sol is None or price_sol <= 0:
            async with self._lock(pending.mint):
                if self.pending.get(pending.mint) is pending:
                    self._skip_pending(
                        pending, "entry_no_reference_price", None
                    )
            return
        reference_age_ms = (
            None
            if reference_ts_ms is None
            else trigger_ts_ms - int(reference_ts_ms)
        )
        if (
            reference_age_ms is not None
            and reference_age_ms < -self.config.max_future_skew_ms
        ):
            # A reference mark dated far in the future is a clock/feed error;
            # clamping its age to zero would present it as perfectly fresh.
            async with self._lock(pending.mint):
                if self.pending.get(pending.mint) is pending:
                    self._skip_pending(
                        pending,
                        "reference_timestamp_in_future",
                        f"reference_age_ms={reference_age_ms};"
                        f"source={reference_source};"
                        f"max_skew_ms={self.config.max_future_skew_ms}",
                    )
            return
        if self.config.max_reference_age_ms > 0 and reference_age_ms is None:
            # A price without a timestamp cannot be proven current. Treat the
            # unknown age as stale rather than as valid.
            async with self._lock(pending.mint):
                if self.pending.get(pending.mint) is pending:
                    self._skip_pending(
                        pending,
                        "entry_reference_age_unknown",
                        f"source={reference_source};"
                        f"max={self.config.max_reference_age_ms}",
                    )
            return
        if (
            self.config.max_reference_age_ms > 0
            and reference_age_ms is not None
            and reference_age_ms > self.config.max_reference_age_ms
        ):
            # A dead pool still carries its last price in history. Quoting a
            # 5-minute-old mark at the research delay is not the trade the
            # replay measured, so refuse it.
            async with self._lock(pending.mint):
                if self.pending.get(pending.mint) is pending:
                    self._skip_pending(
                        pending,
                        "stale_reference_price",
                        f"reference_age_ms={reference_age_ms};"
                        f"source={reference_source};"
                        f"max={self.config.max_reference_age_ms}",
                    )
            return
        pending.reference_price_sol = float(price_sol)
        pending.reference_ts_ms = reference_ts_ms
        pending.reference_age_ms = reference_age_ms
        pending.reference_source = reference_source
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
                    wallet_before = self.wallet_snapshot()
                    try:
                        self.broker.reserve(entry, pending.mint)
                    except QuoteError as exc:
                        pending.last_reason = exc.reason
                        self._skip_pending(
                            pending, "entry_insufficient_sol", exc.reason
                        )
                        return
                    wallet_after = self.wallet_snapshot()
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
                        reference_price_sol=pending.reference_price_sol,
                        reference_ts_ms=pending.reference_ts_ms,
                        reference_age_ms=pending.reference_age_ms,
                        reference_source=pending.reference_source,
                        entry_reference_deviation_pct=(
                            (
                                (entry.entry_price_sol - pending.reference_price_sol)
                                / pending.reference_price_sol
                                * 100.0
                            )
                            if pending.reference_price_sol
                            and pending.reference_price_sol > 0
                            else None
                        ),
                    )
                    self.positions[pending.mint] = position
                    if self.store is not None:
                        self.store.record_entry(position)
                    self._emit(self.on_entry, position, wallet_before, wallet_after)
                    self._notify_state_change()
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
        position.wallet_before_exit = self.wallet_snapshot()
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
                # worth zero. ``quote_no_route`` is terminal for a sale but
                # retryable for an entry, so it needs the exit-specific test.
                transient = not is_terminal_exit_error(position.last_quote_error)
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
        self._notify_state_change()

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
            entry_quote_slippage_bps=(
                entry_quote.slippage_bps if entry_quote else None
            ),
            exit_quote_slippage_bps=quote.slippage_bps,
            exit_quote_attempts=position.exit_quote_attempts,
            last_quote_error=position.last_quote_error,
            rule=self.gate.rule,
            features=dict(position.signal.features),
            exit_fill_count=position.exit_fill_count,
            unsold_size=position.unsold_size,
            received_ts_ms=position.signal.received_ts_ms,
            delivery_lag_ms=max(
                0,
                position.signal.received_ts_ms - position.signal.signal_ts_ms,
            ),
            reference_price_sol=position.reference_price_sol,
            reference_ts_ms=position.reference_ts_ms,
            reference_age_ms=position.reference_age_ms,
            reference_source=position.reference_source,
            entry_reference_deviation_pct=(
                position.entry_reference_deviation_pct
            ),
        )
        self.closed.append(trade)
        day = self._day_key(trade.exit_ts_ms)
        self._daily_pnl[day] = self._daily_pnl.get(day, 0.0) + trade.pnl_sol
        self._persist_daily_pnl()
        if self.store is not None:
            self.store.record_trade(trade)
        self.positions.pop(position.mint, None)
        self._notify_state_change()
        self._emit(
            self.on_close,
            trade,
            position.wallet_before_exit,
            self.wallet_snapshot(),
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
                reason, detail = self._entry_expiry(
                    pending, "no_buy_trade_after_signal"
                )
                self._skip_pending(pending, reason, detail)

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
            reason, detail = self._entry_expiry(
                pending,
                "entry_timer_no_fill"
                if self.config.entry_mode == "timer"
                else "no_buy_trade_after_signal",
            )
            self._skip_pending(pending, reason, detail)

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
