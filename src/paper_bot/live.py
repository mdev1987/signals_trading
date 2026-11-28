"""Live trading wiring: Telegram -> engine -> PumpAPI feed -> Jupiter.

Paper mode is quote-only: the outbound trading call is Jupiter's read-only
``GET /order`` and nothing is ever signed. ``--mode live`` swaps the quote
provider for :class:`~paper_bot.execution.LiveQuoteProvider`, which signs the
returned transaction locally and broadcasts it through ``POST /execute``. Live
execution additionally requires ``LIVE_TRADING_ENABLED=1``.

Telegram has two independent roles:

* a **user session** (Telethon) reads the signal channels;
* a **bot** (``python-telegram-bot``) posts BUY/SELL reports and answers
  ``/status`` and ``/stats``.

They can be disabled independently with ``--no-telegram`` and ``--no-reports``.

The runner also supports a two-lane A/B mode in paper mode. Both lanes consume
the same Telegram and PumpAPI stream, share one history index, and keep separate
wallets, stores and reporter stats. Live execution is deliberately single-lane:
two lanes would sign with the same wallet and double-spend the notional cap.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import __version__
from .broker import BrokerConfig, PaperBroker
from .engine import EngineConfig, PaperEngine, SafetyConfig
from .execution import (
    LiveQuoteProvider,
    LiveSolanaExecutor,
    live_config_from_env,
)
from .feeds import (
    DEFAULT_DEXSCREENER_URL,
    DEFAULT_PUMPAPI_URL,
    DexscreenerPoller,
    PumpApiFeed,
)
from .history import MarketHistory
from .notifier import ReporterStats, TelegramReporter
from .paths import env_file
from .quotes import DEFAULT_MAX_SLIPPAGE_BPS, JupiterQuoteProvider
from .recovery import RecoveryStore
from .signals import (
    DEFAULT_RULE,
    PAPER_ONLY_RULES,
    RULE_DESCRIPTIONS,
    SCANNER_ALLOWED_DEX,
    SCANNER_MAX_LIQUIDITY_USD,
    SCANNER_MESSAGE_TYPE,
    SCANNER_MIN_LIQUIDITY_USD,
    SCANNER_TP50_QUOTE_GUARD_INTERVAL_MS,
    SCANNER_TP50_QUOTE_GUARD_RATIO,
    SCANNER_TP50_QUOTE_GUARD_RULE,
    SCANNER_TP50_QUOTE_GUARD_TP_PCT,
    SCANNER_TP_CONFIRM_MIN_RATIO,
    SUPPORTED_RULES,
    GateConfig,
    is_scanner_rule,
    parse_signal_text,
)
from .store import PaperStore
from .telegram_client import TelegramListener, load_telegram_config
from .wallet import PaperWallet, WalletConfig

LOGGER = logging.getLogger(__name__)

# Process-level memory telemetry, updated by the ticker and written to the run
# summary at shutdown. A dict avoids module-level global statements.
_MEMORY: dict[str, float] = {"peak_mb": 0.0}
# Event-loop stall telemetry. The 2026-11-16 run showed Telegram signals
# arriving a median ~16 s late; this distinguishes "the process was busy" from
# "Telegram delivered late" without shelling into the host.
_LOOP_LAG: dict[str, float] = {"max_ms": 0.0, "last_ms": 0.0}


@dataclass(slots=True)
class LiveConfig:
    channels: list[str]
    config_path: Path
    run_dir: Path
    mode: str = "paper"
    rule: str = DEFAULT_RULE
    # Paper-mode restart recovery. ``state_db`` holds the wallet/position
    # snapshot and the original run directory so a restarted process continues
    # the same forward sample instead of starting a new one.
    resume: bool = False
    state_db: Path | None = None
    # Optional second lane. ``ab_rule`` varies the rule and ``ab_entry_mode``
    # varies the execution; either alone (or both) starts an A/B run.
    ab_rule: str | None = None
    entry_mode: str = "timer"
    ab_entry_mode: str | None = None
    horizon_min: float = 5.0
    tp_pct: float | None = None
    sl_pct: float | None = None
    # Executable-exit guard for docs/QUOTE_GUARD_VARIANT.md. None disables it
    # (frozen and tp50 lanes). See EngineConfig.quote_guard_ratio.
    quote_guard_ratio: float | None = None
    quote_guard_interval_ms: int = 15_000
    # Combined tp50+guard lane (docs/QUOTE_GUARD_VARIANT.md, V2). The rule
    # forces these on; they stay off for every existing lane so its behaviour
    # is unchanged.
    tp_require_executable_quote: bool = False
    tp_min_executable_ratio: float = 1.0
    quote_guard_fraction_probe: bool = False
    fee: float = 0.0125
    slippage: float = 0.0
    notional_sol: float = 0.1
    slippage_bps: int = 50
    # A fresh Pumpfun AMM pool often has no route at 50 bps. Buy and sell
    # orders widen the tolerance up to this cap before giving up.
    max_slippage_bps: int = DEFAULT_MAX_SLIPPAGE_BPS
    # Priority fee (lamports) and Jito MEV tip (lamports) help live
    # transactions land faster during volatile pump launches, reducing
    # entry slippage. ``None`` means "not passed", so the env var can apply;
    # an explicit 0 lets Jupiter determine fees automatically.
    priority_fee_lamports: int | None = None
    jito_tip_lamports: int | None = None
    entry_delay_ms: int = 5_000
    max_entry_delay_ms: int = 60_000
    # Skip a signal Telegram delivered later than this after the card was
    # posted. The research convention prices card + 5 s and gives up at
    # card + 60 s, so a card that arrives minutes late is not a measured trade.
    # 0 disables the check (replay/testing only).
    max_delivery_lag_ms: int = 60_000
    # The scanner backtest assumed at most 10 concurrent positions; this bound
    # also counts pending entries. 0 means unlimited.
    max_concurrent_positions: int = 10
    # Optional UTC-day realized-loss circuit breaker in SOL. 0 disables it. It
    # is a risk control, not part of the backtested edge.
    max_daily_loss_sol: float = 0.0
    # Minimum gap between quote attempts for the same mint while waiting for
    # its first routable BUY print.
    entry_retry_backoff_ms: int = 1_000
    # Reject a BUY print below this canonical SOL notional as an entry
    # trigger. Matches ``alert_outcomes.py --min-quote-sol``.
    min_entry_quote_sol: float = 0.01
    # Timer mode quotes a time, not a print: refuse a stale reference mark.
    max_reference_age_ms: int = 30_000
    # Marks older than this are excluded from reported NAV. 0 disables the
    # freshness split (all marks are then treated as current).
    max_mark_age_ms: int = 120_000
    # Timer mode: allow at most this much scheduling jitter after the
    # scheduled card + entry_delay moment; a later card is rejected instead of
    # being priced at receipt.
    timer_late_tolerance_ms: int = 1_000
    # Feed timestamps further in the future than this are treated as clock/data
    # errors, not as fresh marks.
    max_future_skew_ms: int = 5_000
    # Stable directory for daily risk state, separate from the timestamped run
    # directory so a restart does not reset the daily loss breaker.
    risk_state_dir: str = "output/risk"
    safety_screen: bool = False
    allow_candidate_address: bool = False
    pumpapi_url: str = DEFAULT_PUMPAPI_URL
    dexscreener_url: str = DEFAULT_DEXSCREENER_URL
    tick_interval_s: float = 1.0
    dex_interval_s: float = 2.0
    exit_quote_timeout_ms: int = 30_000
    horizon_settle_ms: int = 1_000
    telegram: bool = True
    reports: bool = True
    report_commands: bool = True
    daily_summary: bool = True
    session_file: Path | None = None
    max_seconds: float | None = None
    starting_sol: float = 10.0
    preflight_exit_route: bool = True
    # Execution-sanity guard, frozen with the validation sample. See
    # EngineConfig.min_preflight_exit_value_ratio.
    min_preflight_exit_value_ratio: float = 0.90
    exit_fallback_feed: bool = False
    history_horizon_ms: int = 2_100_000
    # Memory budget: real firehose events cost ~300-600 B each (signature and
    # signer strings dominate), so the history caps are sized to hold the index
    # in tens of MB rather than hundreds. See MarketHistory for the rationale.
    history_max_mints: int = 10_000
    history_max_events_per_mint: int = 3_000
    history_max_total_events: int = 150_000
    # Graceful restart guard: on a 1.9 GB host the OOM killer otherwise
    # SIGKILLs the bot (exit 137) before it can close positions or write the
    # run summary. Above this RSS the bot shuts down cleanly and lets oxmgr
    # restart it. 0 disables the guard.
    # 2.7x the measured ~205 MB plateau: a regression trips the guard long
    # before the host's OOM killer does, but normal running never restarts.
    memory_limit_mb: int = 550
    memory_report_s: float = 300.0
    # Diagnostic only: start tracemalloc and log the top allocation growth
    # between memory reports. Off by default because tracing has real CPU and
    # memory overhead; enable it to locate a leak, not for a production run.
    memory_trace: bool = False
    message_dedup_window: int = 10_000
    shutdown_timeout_ms: int = 25_000

    def __post_init__(self) -> None:
        """Fail fast on values that would silently distort every fill."""
        if self.rule not in SUPPORTED_RULES:
            raise ValueError(
                f"Unsupported rule {self.rule!r}; choose from {SUPPORTED_RULES}"
            )
        if self.rule == SCANNER_TP50_QUOTE_GUARD_RULE:
            # Pre-registered values (docs/QUOTE_GUARD_VARIANT.md, V2). A
            # different value would start a different experiment under the same
            # lane name, so it is refused instead of being silently applied.
            if self.tp_pct is None:
                self.tp_pct = SCANNER_TP50_QUOTE_GUARD_TP_PCT
            elif self.tp_pct != SCANNER_TP50_QUOTE_GUARD_TP_PCT:
                raise ValueError(
                    f"{SCANNER_TP50_QUOTE_GUARD_RULE} is pre-registered at "
                    f"tp_pct={SCANNER_TP50_QUOTE_GUARD_TP_PCT}; register a "
                    "new variant instead of retuning this lane"
                )
            if self.quote_guard_ratio is None:
                self.quote_guard_ratio = SCANNER_TP50_QUOTE_GUARD_RATIO
            elif self.quote_guard_ratio != SCANNER_TP50_QUOTE_GUARD_RATIO:
                raise ValueError(
                    f"{SCANNER_TP50_QUOTE_GUARD_RULE} is pre-registered at "
                    f"quote_guard_ratio={SCANNER_TP50_QUOTE_GUARD_RATIO}; "
                    "register a new variant instead of retuning this lane"
                )
            if (
                self.quote_guard_interval_ms
                != SCANNER_TP50_QUOTE_GUARD_INTERVAL_MS
            ):
                raise ValueError(
                    f"{SCANNER_TP50_QUOTE_GUARD_RULE} is pre-registered at "
                    f"quote_guard_interval_ms="
                    f"{SCANNER_TP50_QUOTE_GUARD_INTERVAL_MS}; register a new "
                    "variant instead of retuning this lane"
                )
            self.tp_require_executable_quote = True
            self.tp_min_executable_ratio = SCANNER_TP_CONFIRM_MIN_RATIO
            self.quote_guard_fraction_probe = True
        if self.mode not in ("paper", "live"):
            raise ValueError(f"mode must be 'paper' or 'live', got {self.mode!r}")
        if self.entry_mode not in ("trade", "timer"):
            raise ValueError(
                f"entry_mode must be 'trade' or 'timer', got {self.entry_mode!r}"
            )
        if self.ab_entry_mode is not None and self.ab_entry_mode not in (
            "trade",
            "timer",
        ):
            raise ValueError(
                f"ab_entry_mode must be 'trade' or 'timer', got "
                f"{self.ab_entry_mode!r}"
            )
        if self.ab_rule is not None:
            if self.ab_rule not in SUPPORTED_RULES:
                raise ValueError(
                    f"Unsupported ab_rule {self.ab_rule!r}; "
                    f"choose from {SUPPORTED_RULES}"
                )
            if (
                self.ab_rule == self.rule
                and self.ab_entry_mode in (None, self.entry_mode)
            ):
                raise ValueError(
                    "the A/B lanes must differ in rule or entry_mode"
                )
        if self.ab_rule is None and self.ab_entry_mode == self.entry_mode:
            raise ValueError("the A/B lanes must differ in rule or entry_mode")
        if self.resume or self.state_db is not None:
            if self.mode != "paper":
                raise ValueError(
                    "restart recovery is paper-only; live balances come from "
                    "the chain"
                )
            if self.ab_rule is not None or self.ab_entry_mode is not None:
                raise ValueError(
                    "restart recovery supports a single lane, not A/B"
                )
            if self.state_db is None:
                raise ValueError(
                    "restart recovery requires a state_db path"
                )
        if not 0 <= self.fee < 1:
            raise ValueError("fee must be in [0, 1)")
        if not 0 <= self.slippage < 1:
            raise ValueError("slippage must be in [0, 1)")
        if not 0 <= self.slippage_bps <= 10_000:
            raise ValueError("slippage_bps must be in [0, 10000]")
        if not self.slippage_bps <= self.max_slippage_bps <= 10_000:
            raise ValueError(
                "max_slippage_bps must be between slippage_bps and 10000"
            )
        if (
            self.priority_fee_lamports is not None
            and self.priority_fee_lamports < 0
        ):
            raise ValueError("priority_fee_lamports must be non-negative")
        if self.jito_tip_lamports is not None and self.jito_tip_lamports < 0:
            raise ValueError("jito_tip_lamports must be non-negative")
        if self.notional_sol <= 0:
            raise ValueError("notional_sol must be positive")
        if self.starting_sol <= 0:
            raise ValueError("starting_sol must be positive")
        if self.horizon_min <= 0:
            raise ValueError("horizon_min must be positive")
        if self.entry_delay_ms < 0:
            raise ValueError("entry_delay_ms must be non-negative")
        if self.max_entry_delay_ms < self.entry_delay_ms:
            raise ValueError("max_entry_delay_ms must be >= entry_delay_ms")
        if self.max_delivery_lag_ms < 0:
            raise ValueError("max_delivery_lag_ms must be non-negative")
        if self.max_concurrent_positions < 0:
            raise ValueError("max_concurrent_positions must be non-negative")
        if self.max_daily_loss_sol < 0:
            raise ValueError("max_daily_loss_sol must be non-negative")
        if self.entry_retry_backoff_ms < 0:
            raise ValueError("entry_retry_backoff_ms must be non-negative")
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
        if not self.risk_state_dir.strip():
            raise ValueError("risk_state_dir must not be empty")
        if self.exit_quote_timeout_ms <= 0:
            raise ValueError("exit_quote_timeout_ms must be positive")
        if not 0.0 < self.min_preflight_exit_value_ratio <= 1.0:
            raise ValueError(
                "min_preflight_exit_value_ratio must be in (0, 1]"
            )
        if self.quote_guard_ratio is not None and not (
            0.0 < self.quote_guard_ratio < 1.0
        ):
            raise ValueError("quote_guard_ratio must be in (0, 1)")
        if self.quote_guard_interval_ms <= 0:
            raise ValueError("quote_guard_interval_ms must be positive")
        if self.tp_min_executable_ratio <= 0:
            raise ValueError("tp_min_executable_ratio must be positive")
        if self.shutdown_timeout_ms <= 0:
            raise ValueError("shutdown_timeout_ms must be positive")
        if self.tick_interval_s <= 0 or self.dex_interval_s <= 0:
            raise ValueError("feed intervals must be positive")
        if self.max_seconds is not None and self.max_seconds <= 0:
            raise ValueError("max_seconds must be positive")
        if self.history_horizon_ms <= 0:
            raise ValueError("history_horizon_ms must be positive")
        if self.history_max_mints <= 0 or self.history_max_total_events <= 0:
            raise ValueError("history bounds must be positive")
        if self.history_max_events_per_mint <= 0:
            raise ValueError("history_max_events_per_mint must be positive")
        if self.memory_limit_mb < 0:
            raise ValueError("memory_limit_mb must be non-negative")
        if self.memory_report_s <= 0:
            raise ValueError("memory_report_s must be positive")
        if self.message_dedup_window <= 0:
            raise ValueError("message_dedup_window must be positive")


def _load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _read_channels(path: Path) -> list[str]:
    if not path.exists():
        return []
    channels: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        name = line.strip()
        if not name or name.startswith("#"):
            continue
        channels.append(name if name.startswith("@") else f"@{name}")
    # De-duplicate while preserving order.
    return list(dict.fromkeys(channels))


def _count_scanner_cards(run_dir: Path) -> int:
    """Count scanner cards already recorded in a run's ``signals.jsonl``.

    Used when resuming a run whose scanner counters predate their persistence
    (the legacy JSON snapshot): the summary is per run, not per process.
    """
    path = run_dir / "signals.jsonl"
    if not path.exists():
        return 0
    total = 0
    with path.open("rb") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            signal = row.get("signal") if isinstance(row, dict) else None
            if (
                isinstance(signal, dict)
                and signal.get("message_type") == SCANNER_MESSAGE_TYPE
            ):
                total += 1
    return total


async def _ticker(
    engines: list[PaperEngine],
    dex: DexscreenerPoller,
    *,
    interval_s: float,
    history: MarketHistory | None = None,
    feed: PumpApiFeed | None = None,
    memory_limit_mb: int = 0,
    report_s: float = 300.0,
    memory_trace: bool = False,
    on_memory_limit: Callable[[float], None] | None = None,
) -> None:
    next_report = time.monotonic() + report_s
    guard_tripped = False
    trace_previous: Any = None
    while True:
        before_sleep = time.monotonic()
        await asyncio.sleep(interval_s)
        drift_ms = (time.monotonic() - before_sleep - interval_s) * 1000.0
        _LOOP_LAG["last_ms"] = drift_ms
        _LOOP_LAG["max_ms"] = max(_LOOP_LAG["max_ms"], drift_ms)
        if drift_ms >= 1_000.0:
            LOGGER.warning("event loop stalled %.0f ms", drift_ms)
        now = int(time.time() * 1000)
        active: set[str] = set()
        for engine in engines:
            active |= set(engine.pending) | set(engine.positions)
        for mint in active:
            dex.watch(mint)
        for mint in list(dex.watched):
            if mint not in active:
                dex.unwatch(mint)
        # The PumpAPI feed keeps the same per-card watch set purely for
        # introspection; without this it would hold one dead mint per card for
        # the life of the process.
        if feed is not None:
            for mint in active:
                feed.watch(mint)
            for mint in list(feed.watched):
                if mint not in active:
                    feed.unwatch(mint)
        await asyncio.gather(*(engine.tick(now) for engine in engines))

        rss = _rss_mb()
        if rss is None:
            continue
        _MEMORY["peak_mb"] = max(_MEMORY["peak_mb"], rss)
        if memory_limit_mb and rss >= memory_limit_mb and not guard_tripped:
            guard_tripped = True
            LOGGER.warning(
                "RSS %.0f MB reached the %d MB limit; restarting cleanly",
                rss,
                memory_limit_mb,
            )
            if on_memory_limit is not None:
                on_memory_limit(rss)
        if time.monotonic() >= next_report:
            next_report = time.monotonic() + report_s
            LOGGER.info(
                "memory rss_mb=%.0f peak_mb=%.0f loop_lag_max_ms=%.0f "
                "history_mints=%s history_events=%s locks=%d "
                "feed_watched=%d dex_watched=%d",
                rss,
                _MEMORY["peak_mb"],
                _LOOP_LAG["max_ms"],
                history.mint_count if history is not None else "-",
                history.total_events if history is not None else "-",
                sum(len(engine._locks) for engine in engines),
                len(feed.watched) if feed is not None else -1,
                len(dex.watched),
            )
            _LOOP_LAG["max_ms"] = 0.0
            if memory_trace:
                import tracemalloc

                if tracemalloc.is_tracing():
                    current, peak = tracemalloc.get_traced_memory()
                    snapshot = tracemalloc.take_snapshot()
                    growth = ""
                    if trace_previous is not None:
                        growth = "; ".join(
                            f"{stat.traceback.format()[-1].strip()} "
                            f"{stat.size_diff / 1024:+.0f}KB"
                            for stat in snapshot.compare_to(
                                trace_previous, "lineno"
                            )[:5]
                        )
                    trace_previous = snapshot
                    LOGGER.info(
                        "tracemalloc traced_mb=%.1f peak_mb=%.1f "
                        "growth_since_last_report=[%s]",
                        current / (1024 * 1024),
                        peak / (1024 * 1024),
                        growth,
                    )


def _rss_mb() -> float | None:
    """Resident set size of this process, or ``None`` off Linux."""
    try:
        with open("/proc/self/status", encoding="ascii") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0
    except (OSError, ValueError):  # pragma: no cover - non-Linux
        return None
    return None


def _watch_task(
    task: asyncio.Task[Any], reporter: TelegramReporter, label: str
) -> None:
    """Log a crashed background task and tell the operator, without dying."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is None:
        return
    LOGGER.error("%s task failed: %s", label, exc, exc_info=exc)
    asyncio.create_task(reporter.notify_error(f"{label}: {type(exc).__name__}: {exc}"))


@dataclass(slots=True)
class StrategyLane:
    """One independently funded rule running on the shared market stream."""

    name: str
    rule: str
    entry_mode: str
    run_dir: Path
    wallet: PaperWallet
    broker: PaperBroker
    store: PaperStore
    engine: PaperEngine
    reporter: TelegramReporter
    stats: ReporterStats
    reports_started: bool = False
    wallet_snapshot: dict[str, Any] | None = None
    _persistence_healthy: bool = True
    # Sticky once a snapshot write fails. A later successful write proves the
    # store is writable again but does not prove the process did not already
    # make decisions from a state that was never durable, so new entries stay
    # blocked until a restart recovers the last valid snapshot.
    _persistence_degraded: bool = False
    # VALID / DEGRADED / INVALID, as recorded in the run summary and carried
    # across a resume. INVALID means the state could not be reconciled with
    # the durable evidence (see ``_resume_integrity``).
    _integrity: str = "VALID"


def _risk_state_path(config: LiveConfig, rule: str, entry_mode: str) -> Path:
    """Stable per-lane daily-PnL file, independent of the run directory.

    A/B lanes keep separate breakers; a shared file would let one lane's losses
    stop the other. The name is derived from the rule and entry mode, so a
    restart of the same lane restores its own state.
    """
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", f"{rule}_{entry_mode}").strip("._")
    return Path(config.risk_state_dir) / f"{stem or 'lane'}.json"


def _recovery_fingerprint(
    config: LiveConfig, rule: str, entry_mode: str
) -> dict[str, Any]:
    """Configuration that must not change while resuming a forward run.

    Anything that changes which prints become trades, how they are priced or
    when they are closed belongs here. A run resumed under a different guard is
    a different experiment, even if the wallet and positions restore cleanly,
    so the resumed sample would no longer be gate-eligible as one run.
    """
    return {
        "mode": config.mode,
        "rule": rule,
        "entry_mode": entry_mode,
        "starting_sol": config.starting_sol,
        "notional_sol": config.notional_sol,
        "fee": config.fee,
        "slippage": config.slippage,
        "slippage_bps": config.slippage_bps,
        "max_slippage_bps": config.max_slippage_bps,
        # Exit convention.
        "horizon_min": config.horizon_min,
        "horizon_settle_ms": config.horizon_settle_ms,
        "tp_pct": config.tp_pct,
        "sl_pct": config.sl_pct,
        "quote_guard_ratio": config.quote_guard_ratio,
        "quote_guard_interval_ms": config.quote_guard_interval_ms,
        "tp_require_executable_quote": config.tp_require_executable_quote,
        "tp_min_executable_ratio": config.tp_min_executable_ratio,
        "quote_guard_fraction_probe": config.quote_guard_fraction_probe,
        "exit_quote_timeout_ms": config.exit_quote_timeout_ms,
        "preflight_exit_route": config.preflight_exit_route,
        "min_preflight_exit_value_ratio": (
            config.min_preflight_exit_value_ratio
        ),
        "exit_fallback_feed": config.exit_fallback_feed,
        # Entry convention and fidelity guards.
        "entry_delay_ms": config.entry_delay_ms,
        "max_entry_delay_ms": config.max_entry_delay_ms,
        "max_delivery_lag_ms": config.max_delivery_lag_ms,
        "entry_retry_backoff_ms": config.entry_retry_backoff_ms,
        "min_entry_quote_sol": config.min_entry_quote_sol,
        "max_reference_age_ms": config.max_reference_age_ms,
        "max_mark_age_ms": config.max_mark_age_ms,
        "max_concurrent_positions": config.max_concurrent_positions,
        "max_daily_loss_sol": config.max_daily_loss_sol,
        "timer_late_tolerance_ms": config.timer_late_tolerance_ms,
        "max_future_skew_ms": config.max_future_skew_ms,
        # The history index supplies the marks/prices a resumed position is
        # valued against, so its bounds are part of the measurement too.
        "history": {
            "horizon_ms": config.history_horizon_ms,
            "max_mints": config.history_max_mints,
            "max_events_per_mint": config.history_max_events_per_mint,
            "max_total_events": config.history_max_total_events,
        },
    }


def _fingerprint_subset(saved: Any, current: Any) -> bool:
    """True when every field in ``saved`` matches ``current``.

    Used only to upgrade a snapshot written by an older build whose fingerprint
    had fewer fields. Shared fields must still match exactly, so changing a
    guard that the old build *did* track is refused; fields the old build never
    recorded are assumed to have been the deployed values, and the warning in
    ``run_live`` makes the upgrade visible.
    """
    if isinstance(saved, dict):
        if not isinstance(current, dict):
            return False
        # Every key in saved must either:
        # - exist in current with a matching value (recursive), or
        # - not exist in current (assumed deployed value, no conflict)
        return all(
            key not in current or _fingerprint_subset(value, current[key])
            for key, value in saved.items()
        )
    return saved == current


def _parse_iso_utc(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def _run_started_at(
    recovered: dict[str, Any], run_dir: Path, fallback: datetime
) -> datetime:
    """Original run start, so a resume does not restamp the sample.

    The snapshot carries ``started_at`` for runs written after this field was
    added; older snapshots fall back to the run's ``meta.json``. If neither is
    readable the process start is used rather than refusing a recovery that
    holds real open positions; the log makes the downgrade visible.
    """
    from_snapshot = _parse_iso_utc(recovered.get("started_at"))
    if from_snapshot is not None:
        return from_snapshot
    meta_path = run_dir / "meta.json"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        meta = None
    if isinstance(meta, dict):
        from_meta = _parse_iso_utc(meta.get("started_at"))
        if from_meta is not None:
            return from_meta
    LOGGER.warning(
        "could not recover the original started_at for %s; using %s",
        run_dir,
        fallback.isoformat(),
    )
    return fallback


def _resume_integrity(
    saved_integrity: Any,
    degradation: dict[str, Any] | None,
    run_dir: Path,
) -> str:
    """Reconcile the last snapshot with an independent degradation marker.

    ``VALID``    no failed-write evidence.
    ``DEGRADED`` the snapshot already records a persistence failure.
    ``INVALID``  a marker exists for this run but the snapshot still claims
                 clean state. That means the process may have made entry
                 decisions after a write failed and before a later write
                 could record the degradation; the session must not be used
                 for valid performance conclusions.

    A marker for a different run is ignored: an operator who archived the
    database and started a new sample does not inherit the old failure.
    """
    status = (
        saved_integrity
        if saved_integrity in {"VALID", "DEGRADED", "INVALID"}
        else "VALID"
    )
    if degradation is None:
        return status
    marker_run = degradation.get("run_dir")
    if marker_run is not None and str(run_dir) != str(marker_run):
        return status
    return "INVALID" if status == "VALID" else status


def _lane_integrity(lane: StrategyLane) -> str:
    """Current status for the summary, never less severe than the start state."""
    if lane._integrity == "INVALID":
        return "INVALID"
    if lane._persistence_degraded or lane._integrity == "DEGRADED":
        return "DEGRADED"
    return "VALID"


def _state_saver(
    store: RecoveryStore,
    lane: StrategyLane,
    fingerprint: dict[str, Any],
    *,
    started_at: str | None = None,
) -> Callable[[dict[str, Any]], None]:
    """Return a callback that persists one lane's restart snapshot.

    The run directory, original start time and configuration fingerprint travel
    with the snapshot so a later ``--resume`` cannot attach a different lane or
    a changed strategy to an existing five-day sample.

    Persistence health is tracked on the lane (_persistence_healthy) so that
    the engine can block new entries when the durable state is no longer
    reliable. The engine mirrors both the health and the sticky degraded flag.

    * True — every snapshot write has succeeded; normal entry behavior.
    * False — a snapshot write failed at least once; new entries stay blocked
      for the rest of the process. Existing positions continue to be managed
      and exited. Only a restart that loads the last valid snapshot clears it.

    A failure also writes a sidecar marker outside the failing snapshot store
    (``RecoveryStore.mark_degraded``) so an abrupt restart can still detect
    the degradation. ``_resume_integrity`` upgrades a snapshot that still
    claims VALID to INVALID when that marker exists. If the marker itself
    cannot be written the run is INVALID immediately, because a restart would
    have no reliable evidence; this is a hard-fail condition, not a warning.
    """

    def _mirror(healthy: bool) -> None:
        lane._persistence_healthy = healthy
        if hasattr(lane.engine, "_persistence_healthy"):
            lane.engine._persistence_healthy = healthy
        if hasattr(lane.engine, "_persistence_degraded"):
            lane.engine._persistence_degraded = lane._persistence_degraded

    def save(state: dict[str, Any]) -> None:
        payload: dict[str, Any] = {
            "run_dir": str(lane.run_dir),
            "fingerprint": fingerprint,
            "wallet": state["wallet"],
            "token_balances": state["token_balances"],
            "positions": state["positions"],
        }
        if started_at is not None:
            payload["started_at"] = started_at
        # Persist the integrity flag whenever the store is writable: a later
        # successful write then carries DEGRADED/INVALID into the next restart.
        payload["integrity"] = _lane_integrity(lane)
        payload["persistence_degraded"] = lane._persistence_degraded
        try:
            store.save(payload)
            if lane._persistence_degraded:
                # Do not silently re-enable entries: a successful write only
                # proves the store works now, not that earlier decisions were
                # made from durable state. A restart is the recovery path.
                LOGGER.warning(
                    "restart snapshot is writable again, but lane %s stays "
                    "entry-blocked until a clean restart restores the last "
                    "valid snapshot",
                    lane.name,
                )
            else:
                _mirror(True)
        except RuntimeError:
            first_failure = not lane._persistence_degraded
            lane._persistence_degraded = True
            if lane._integrity == "VALID":
                lane._integrity = "DEGRADED"
            # Persistence failure: block new entries while preserving exits.
            _mirror(False)
            LOGGER.exception(
                "PERSISTENCE DEGRADED: could not write the restart snapshot "
                "for lane %s; new entries are blocked, exits continue. The "
                "session is marked DEGRADED and must not be used as a clean "
                "validation sample.",
                lane.name,
            )
            if first_failure or lane._integrity == "INVALID":
                mark_degraded = getattr(store, "mark_degraded", None)
                if callable(mark_degraded):
                    try:
                        mark_degraded(
                            run_dir=str(lane.run_dir),
                            lane=lane.name,
                            reason="restart snapshot write failed",
                        )
                    except RuntimeError:
                        # Without a durable marker a restart may see the last
                        # snapshot as clean. That is an unreconciled research
                        # state, so do not let it masquerade as VALID.
                        lane._integrity = "INVALID"
                        LOGGER.critical(
                            "PERSISTENCE INVALID: the durable degradation "
                            "marker could not be written for lane %s; a "
                            "restart cannot prove this run's state was "
                            "durable",
                            lane.name,
                            exc_info=True,
                        )
                schedule = getattr(lane.reporter, "schedule", None)
                notify = getattr(lane.reporter, "notify_error", None)
                if schedule is not None and notify is not None:
                    try:
                        schedule(
                            notify(
                                "persistence degraded: restart snapshot "
                                "write failed; new entries blocked, exits "
                                "still running"
                            )
                        )
                    except (AttributeError, RuntimeError):
                        # No running loop (for example a synchronous test or
                        # an early startup path): the log above is the
                        # notification.
                        pass

    return save


def _lane_meta(
    config: LiveConfig,
    *,
    lane: str,
    rule: str,
    entry_mode: str,
    is_ab: bool,
    started_at: str,
    resumed_at: str | None = None,
    integrity_at_start: str = "VALID",
) -> dict[str, Any]:
    return {
        "mode": (
            f"live_{'paper' if config.mode == 'paper' else 'exec'}"
            + ("_ab" if is_ab else "")
        ),
        "lane": lane,
        "version": __version__,
        "pid": os.getpid(),
        "rule": rule,
        "rule_description": RULE_DESCRIPTIONS.get(rule, ""),
        "entry_mode": entry_mode,
        "horizon_min": config.horizon_min,
        "entry_delay_ms": config.entry_delay_ms,
        "max_entry_delay_ms": config.max_entry_delay_ms,
        "max_delivery_lag_ms": config.max_delivery_lag_ms,
        "max_concurrent_positions": config.max_concurrent_positions,
        "max_daily_loss_sol": config.max_daily_loss_sol,
        "entry_retry_backoff_ms": config.entry_retry_backoff_ms,
        "min_entry_quote_sol": config.min_entry_quote_sol,
        "max_reference_age_ms": config.max_reference_age_ms,
        "max_mark_age_ms": config.max_mark_age_ms,
        "timer_late_tolerance_ms": config.timer_late_tolerance_ms,
        "max_future_skew_ms": config.max_future_skew_ms,
        "risk_state_dir": config.risk_state_dir,
        "risk_state_file": str(_risk_state_path(config, rule, entry_mode)),
        "resume": config.resume,
        "started_at": started_at,
        # Present only when this process continued an existing run; the run's
        # original start stays in ``started_at`` so the sample is not restamped.
        "resumed_at": resumed_at,
        "recovery_status": "recovered" if resumed_at else "fresh",
        # Integrity is authoritative in the final summary; this records the
        # state the process started from so a crash cannot look clean.
        "integrity_at_start": integrity_at_start,
        # Gate inputs the engine actually evaluates for this rule. Frozen
        # constants, recorded so a later reader can tell what accepted a card.
        "scanner_gate": {
            "allowed_dex": SCANNER_ALLOWED_DEX,
            "min_liquidity_usd": SCANNER_MIN_LIQUIDITY_USD,
            "max_liquidity_usd_exclusive": SCANNER_MAX_LIQUIDITY_USD,
        },
        "recovery_state_db": (
            str(config.state_db) if config.state_db is not None else None
        ),
        "tp_pct": config.tp_pct,
        "sl_pct": config.sl_pct,
        "quote_guard_ratio": config.quote_guard_ratio,
        "quote_guard_interval_ms": config.quote_guard_interval_ms,
        "tp_require_executable_quote": config.tp_require_executable_quote,
        "tp_min_executable_ratio": config.tp_min_executable_ratio,
        "quote_guard_fraction_probe": config.quote_guard_fraction_probe,
        "fee": config.fee,
        "slippage": config.slippage,
        "slippage_bps": config.slippage_bps,
        "max_slippage_bps": config.max_slippage_bps,
        "notional_sol": config.notional_sol,
        "starting_sol": config.starting_sol,
        "preflight_exit_route": config.preflight_exit_route,
        "min_preflight_exit_value_ratio": (
            config.min_preflight_exit_value_ratio
        ),
        "exit_fallback_feed": config.exit_fallback_feed,
        "exit_quote_timeout_ms": config.exit_quote_timeout_ms,
        "safety_screen": config.safety_screen,
        "history": {
            "horizon_ms": config.history_horizon_ms,
            "max_mints": config.history_max_mints,
            "max_events_per_mint": config.history_max_events_per_mint,
            "max_total_events": config.history_max_total_events,
        },
        "channels": config.channels,
        "pumpapi_url": config.pumpapi_url,
        "dexscreener_url": config.dexscreener_url,
        "telegram": config.telegram,
        "reports": config.reports,
        "max_seconds": config.max_seconds,
        "ab_rule": config.ab_rule,
    }


def _create_lane(
    config: LiveConfig,
    *,
    name: str,
    rule: str,
    entry_mode: str,
    run_dir: Path,
    history: MarketHistory,
    quotes: Any,
    env: dict[str, str],
    primary: bool,
    is_ab: bool,
    started_at: str,
    resumed_at: str | None,
    start_integrity: str = "VALID",
) -> StrategyLane:
    wallet = PaperWallet(WalletConfig(starting_sol=config.starting_sol))
    broker = PaperBroker(
        BrokerConfig(
            fee=config.fee,
            slippage=config.slippage,
            notional_sol=config.notional_sol,
        ),
        wallet=wallet,
    )
    store = PaperStore(
        run_dir,
        meta=_lane_meta(
            config,
            lane=name,
            rule=rule,
            entry_mode=entry_mode,
            is_ab=is_ab,
            started_at=started_at,
            resumed_at=resumed_at,
            integrity_at_start=start_integrity,
        ),
    )
    reporter = TelegramReporter.from_env(
        env,
        commands=primary and config.report_commands,
        daily_summary=primary and config.daily_summary,
    )
    if not config.reports:
        reporter.config.enabled = False
    stats = ReporterStats(starting_sol=config.starting_sol)

    def on_entry(
        position: Any,
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
    ) -> None:
        reporter.schedule(reporter.notify_entry(position, before, after))

    def on_close(
        trade: Any,
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
    ) -> None:
        reporter.schedule(reporter.notify_exit(trade, before, after))

    engine = PaperEngine(
        config=EngineConfig(
            entry_delay_ms=config.entry_delay_ms,
            entry_mode=entry_mode,
            max_entry_delay_ms=config.max_entry_delay_ms,
            max_delivery_lag_ms=config.max_delivery_lag_ms,
            max_concurrent_positions=config.max_concurrent_positions,
            max_daily_loss_sol=config.max_daily_loss_sol,
            entry_retry_backoff_ms=config.entry_retry_backoff_ms,
            min_entry_quote_sol=config.min_entry_quote_sol,
            max_reference_age_ms=config.max_reference_age_ms,
            max_mark_age_ms=config.max_mark_age_ms,
            timer_late_tolerance_ms=config.timer_late_tolerance_ms,
            max_future_skew_ms=config.max_future_skew_ms,
            risk_state_path=_risk_state_path(config, rule, entry_mode),
            horizon_ms=int(config.horizon_min * 60_000),
            tp_pct=config.tp_pct,
            sl_pct=config.sl_pct,
            quote_guard_ratio=config.quote_guard_ratio,
            quote_guard_interval_ms=config.quote_guard_interval_ms,
            tp_require_executable_quote=config.tp_require_executable_quote,
            tp_min_executable_ratio=config.tp_min_executable_ratio,
            quote_guard_fraction_probe=config.quote_guard_fraction_probe,
            live=True,
            exit_quote_timeout_ms=config.exit_quote_timeout_ms,
            slippage_bps=config.slippage_bps,
            horizon_settle_ms=config.horizon_settle_ms,
            preflight_exit_route=config.preflight_exit_route,
            min_preflight_exit_value_ratio=(
                config.min_preflight_exit_value_ratio
            ),
            exit_fallback_feed=config.exit_fallback_feed,
            shutdown_timeout_ms=config.shutdown_timeout_ms,
        ),
        broker=broker,
        quotes=quotes,
        history=history,
        gate=GateConfig(
            rule=rule,
            allow_candidate_address=config.allow_candidate_address,
        ),
        safety=SafetyConfig(enabled=config.safety_screen),
        store=store,
        on_entry=on_entry,
        on_close=on_close,
    )
    reporter.bind(engine=engine, wallet=wallet, stats=stats, rule=rule)
    return StrategyLane(
        name=name,
        rule=rule,
        entry_mode=entry_mode,
        run_dir=run_dir,
        wallet=wallet,
        broker=broker,
        store=store,
        engine=engine,
        reporter=reporter,
        stats=stats,
        _integrity=start_integrity,
    )


def _write_ab_summary(
    root: Path,
    lanes: list[StrategyLane],
    *,
    started_at_ms: int,
    shutdown_reason: str,
) -> None:
    """Write one side-by-side file for analysis, in addition to each lane."""
    payload: dict[str, Any] = {
        "mode": "live_paper_ab",
        "started_at": datetime.fromtimestamp(started_at_ms / 1000, UTC).isoformat(),
        "runtime_ms": int(time.time() * 1000) - started_at_ms,
        "shutdown_reason": shutdown_reason,
        "lanes": {},
    }
    for lane in lanes:
        summary: dict[str, Any] = {}
        summary_path = lane.run_dir / "summary.json"
        if summary_path.exists():
            try:
                summary = json.loads(summary_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                LOGGER.warning("could not parse %s", summary_path)
        payload["lanes"][lane.name] = {
            "rule": lane.rule,
            "entry_mode": lane.entry_mode,
            "rule_description": RULE_DESCRIPTIONS.get(lane.rule, ""),
            "run_dir": str(lane.run_dir),
            "summary": summary,
        }
    (root / "ab_summary.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )


def _remember_scanner_seen(
    seen: OrderedDict[tuple[str, str], None],
    key: tuple[str, str],
    window: int,
) -> None:
    seen[key] = None
    if len(seen) > window:
        seen.popitem(last=False)


def _claim_scanner_card(
    *,
    lane_name: str,
    token_address: str | None,
    channel: str,
    seen: OrderedDict[tuple[str, str], None],
    recovery: RecoveryStore | None,
    window: int,
) -> bool:
    """Return True only for the first card of a pool on this lane.

    The in-memory key is recorded on the first card, not only on a rejected
    repost. That matters because the engine only ignores duplicates while a
    mint has a pending entry or an open position; once a position closes early
    (the tp50 lane's whole point) only this set still enforces the documented
    "first card per pool" rule. The durable ``scanner_trade`` claim covers a
    repost delivered after a restart.
    """
    if not token_address:
        # Not a pool identity we can deduplicate on; let the parser/engine
        # reject the card rather than blocking every later address-less card.
        return True
    key = (lane_name, token_address)
    if key in seen:
        return False
    if recovery is not None and not recovery.claim_event(
        f"scanner_trade:{lane_name}:{channel}:{token_address}"
    ):
        LOGGER.debug(
            "scanner card lane=%s %s was traded before the restart; "
            "ignoring the repost",
            lane_name,
            token_address,
        )
        _remember_scanner_seen(seen, key, window)
        return False
    _remember_scanner_seen(seen, key, window)
    return True


async def run_live(config: LiveConfig) -> dict[str, Any]:
    process_started_at = datetime.now(UTC)
    started_at_ms = int(process_started_at.timestamp() * 1000)
    if config.memory_trace:
        import tracemalloc

        if not tracemalloc.is_tracing():
            tracemalloc.start(10)
        LOGGER.warning(
            "tracemalloc diagnostics enabled; expect extra overhead "
            "(this is a leak-hunt run, not a gate sample)"
        )
    _MEMORY["peak_mb"] = 0.0
    # The tp50 exit is a pre-registered paper experiment
    # (docs/TP50_VARIANT.md). Refuse live before any credentials are read: the
    # wrapper refuses it too, but this holds for a direct CLI invocation.
    if config.mode == "live" and config.rule in PAPER_ONLY_RULES:
        raise ValueError(
            f"{config.rule} is a paper-only pre-registered variant; "
            "live execution is refused"
        )
    env = _load_env(env_file())
    api_key = env.get("JUPITER_API_KEY") or os.environ.get("JUPITER_API_KEY")
    if not api_key:
        raise RuntimeError(
            "JUPITER_API_KEY is not set; the paper broker needs Jupiter quotes"
        )
    if config.telegram:
        if not config.config_path.exists():
            raise FileNotFoundError(f"Telegram config not found: {config.config_path}")
        if not config.channels:
            raise ValueError("No Telegram channels configured")
    elif config.max_seconds is None:
        raise ValueError("--no-telegram requires --max-seconds")

    is_ab = config.ab_rule is not None or config.ab_entry_mode is not None
    specs: list[tuple[str, str, str]] = [
        ("A", config.rule, config.entry_mode)
    ]
    if is_ab:
        specs.append(
            (
                "B",
                config.ab_rule or config.rule,
                config.ab_entry_mode or config.entry_mode,
            )
        )

    # Paper restart recovery: keep the wallet, open positions and run directory
    # consistent across process restarts. The snapshot is written for every
    # single-lane paper run, so an unexpected crash can be resumed with
    # ``--resume``; anything inconsistent fails closed instead of silently
    # starting a new forward sample.
    recovery: RecoveryStore | None = None
    recovered: dict[str, Any] | None = None
    fingerprint: dict[str, Any] | None = None
    run_started_at = process_started_at
    resumed_at: datetime | None = None
    # Integrity of the run this process starts from. A resumed run adopts the
    # snapshot's flag so a degraded session cannot look clean after a restart.
    start_integrity = "VALID"
    if config.state_db is not None:
        recovery = RecoveryStore(config.state_db)
        try:
            pruned = recovery.prune_events(time.time() - 30 * 86400)
            if pruned:
                LOGGER.info(
                    "pruned %d processed-event claims older than 30 days",
                    pruned,
                )
        except RuntimeError:
            LOGGER.exception("could not prune processed-event claims")
        fingerprint = _recovery_fingerprint(
            config, config.rule, config.entry_mode
        )
        if config.resume:
            recovered = recovery.load()
            if recovered is None:
                raise RuntimeError(
                    f"--resume was given but {config.state_db} holds no "
                    "saved state; start once without --resume to create it"
                )
            if recovered["fingerprint"] != fingerprint:
                if not _fingerprint_subset(
                    recovered["fingerprint"], fingerprint
                ):
                    raise RuntimeError(
                        "recovery state was written for a different "
                        f"configuration: saved={recovered['fingerprint']} "
                        f"current={fingerprint}"
                    )
                saved_guard = recovered["fingerprint"].get(
                    "min_preflight_exit_value_ratio"
                )
                if (
                    saved_guard is None
                    and config.min_preflight_exit_value_ratio is not None
                ):
                    # Before this guard existed entries were accepted on route
                    # existence alone. Continuing the same ``started_at``
                    # would pool trades accepted under two different
                    # execution rules, which the validation gates forbid.
                    raise RuntimeError(
                        "saved run predates the preflight exit-economics "
                        f"guard (now {config.min_preflight_exit_value_ratio:.2f}x). "
                        "Resuming would pool entries accepted under a "
                        "different rule. Archive the state database and "
                        "start a new validation sample."
                    )
                LOGGER.warning(
                    "upgrading a recovery fingerprint written by an older "
                    "build (saved=%s); shared fields match, newly tracked "
                    "guards take the deployed values",
                    recovered["fingerprint"],
                )
            config.run_dir = Path(recovered["run_dir"])
            run_started_at = _run_started_at(
                recovered, config.run_dir, process_started_at
            )
            resumed_at = process_started_at
            # Reconcile the last snapshot with the sidecar marker written when
            # a snapshot write failed. A marker plus a snapshot that still
            # claims VALID is an unreconciled run (INVALID): entries may have
            # been accepted after durable state was already unreliable. An
            # unreadable marker raises here, so resume fails closed.
            degradation = recovery.load_degradation()
            start_integrity = _resume_integrity(
                recovered.get("integrity"), degradation, config.run_dir
            )
            if degradation is not None:
                LOGGER.warning(
                    "durable persistence degradation marker for %s: %s",
                    config.run_dir,
                    degradation,
                )
            if start_integrity != "VALID":
                LOGGER.warning(
                    "resuming a session recorded as %s; new entries stay "
                    "blocked until the run is restarted from a clean "
                    "snapshot",
                    start_integrity,
                )
            LOGGER.warning(
                "resuming run %s from %s (open positions=%d); run started %s",
                config.run_dir,
                config.state_db,
                len(recovered["positions"]),
                run_started_at.isoformat(),
            )
        else:
            LOGGER.info(
                "restart recovery state will be written to %s",
                config.state_db,
            )

    history = MarketHistory(
        max_events_per_mint=config.history_max_events_per_mint,
        max_mints=config.history_max_mints,
        max_total_events=config.history_max_total_events,
        horizon_ms=config.history_horizon_ms,
    )
    quotes: Any
    if config.mode == "live":
        if is_ab:
            raise ValueError(
                "live execution supports a single lane; A/B mode is paper-only"
            )
        live_config = live_config_from_env(
            env,
            enabled=True,
            slippage_bps=config.slippage_bps,
            max_slippage_bps=config.max_slippage_bps,
            max_notional_sol=config.notional_sol,
            priority_fee_lamports=config.priority_fee_lamports,
            jito_tip_lamports=config.jito_tip_lamports,
        )
        if not live_config.enabled:
            raise RuntimeError(
                "live mode requires LIVE_TRADING_ENABLED=1 in the environment"
            )
        # The on-chain swap already paid the venue fee and its own slippage.
        # Applying the research cost model on top would double-count both.
        if config.fee or config.slippage:
            LOGGER.warning(
                "live mode ignores --fee/--slippage (%.4f/%.4f); "
                "executed amounts are booked as-is",
                config.fee,
                config.slippage,
            )
            config.fee = 0.0
            config.slippage = 0.0
        # Preflight would run after the real buy and, if it failed, leave the
        # bot holding tokens it already paid for. The executed fill is the
        # route proof in live mode.
        config.preflight_exit_route = False
        live_starting = env.get("LIVE_STARTING_SOL") or os.environ.get(
            "LIVE_STARTING_SOL"
        )
        if live_starting:
            try:
                config.starting_sol = float(live_starting)
            except ValueError as exc:
                raise ValueError("LIVE_STARTING_SOL must be numeric") from exc
        quotes = LiveQuoteProvider(LiveSolanaExecutor(live_config))
        LOGGER.warning(
            "LIVE EXECUTION ENABLED: swaps will be signed and broadcast "
            "(notional %.3f SOL, cap %.3f SOL)",
            config.notional_sol,
            live_config.max_notional_sol,
        )
    else:
        quotes = JupiterQuoteProvider(
            api_key=api_key,
            slippage_bps=config.slippage_bps,
            max_slippage_bps=config.max_slippage_bps,
        )
    lanes: list[StrategyLane] = []
    for index, (name, rule, entry_mode) in enumerate(specs):
        lane_dir = config.run_dir / name if is_ab else config.run_dir
        lane = _create_lane(
            config,
            name=name,
            rule=rule,
            entry_mode=entry_mode,
            run_dir=lane_dir,
            history=history,
            quotes=quotes,
            env=env,
            primary=index == 0,
            is_ab=is_ab,
            started_at=run_started_at.isoformat(),
            resumed_at=(
                resumed_at.isoformat() if resumed_at is not None else None
            ),
            start_integrity=start_integrity,
        )
        if recovery is not None and fingerprint is not None:
            lane.engine.on_state_change = _state_saver(
                recovery,
                lane,
                fingerprint,
                started_at=run_started_at.isoformat(),
            )
            if start_integrity != "VALID":
                # The snapshot this process recovered already recorded a
                # persistence failure or an unreconciled state. Keep entries
                # blocked; exits still run.
                lane._persistence_degraded = True
                lane._persistence_healthy = False
                lane.engine._persistence_degraded = True
                lane.engine._persistence_healthy = False
            if recovered is not None:
                lane.engine.restore_state(recovered)
                # Rewrite the snapshot immediately: this upgrades a legacy
                # fingerprint to the full one and records this resumed run's
                # original started_at in the new store.
                lane.engine.persist_state()
                LOGGER.warning(
                    "restored lane %s: cash=%.6f SOL, token_balances=%d",
                    lane.name,
                    lane.wallet.sol_balance,
                    len(lane.wallet.token_balances),
                )
            else:
                # A fresh sample must not inherit the old run's marker. Clear
                # it before the first write: if that write fails, the saver
                # immediately recreates the marker for this run.
                try:
                    if recovery.clear_degradation():
                        LOGGER.warning(
                            "cleared a stale persistence degradation marker "
                            "for a fresh sample"
                        )
                except RuntimeError:
                    LOGGER.exception(
                        "could not clear the stale degradation marker; the "
                        "fresh run is marked INVALID"
                    )
                    lane._integrity = "INVALID"
                    lane._persistence_degraded = True
                    lane._persistence_healthy = False
                    lane.engine._persistence_degraded = True
                    lane.engine._persistence_healthy = False
                # Write the snapshot immediately: a crash before the first
                # trade should still be resumable into this same run.
                lane.engine.persist_state()
        lanes.append(lane)
    primary = lanes[0]
    reporter = primary.reporter

    async def handle_trade(event: Any) -> None:
        # ``signature``/``pool_id`` are unique per trade and never read by the
        # pipeline; keeping them in the rolling index costs ~40% of its memory.
        event.signature = None
        event.pool_id = None
        # Add once, then let both lanes consume the same causal history.
        history.add(event)
        await asyncio.gather(
            *(
                lane.engine.on_trade(event, history_recorded=True)
                for lane in lanes
            )
        )

    async def handle_mark(
        mint: str,
        price_sol: float,
        ts_ms: int,
        *,
        liquidity_usd: float | None = None,
    ) -> None:
        await asyncio.gather(
            *(
                lane.engine.on_mark(
                    mint, price_sol, ts_ms, liquidity_usd=liquidity_usd
                )
                for lane in lanes
            )
        )

    feed = PumpApiFeed(handle_trade, url=config.pumpapi_url)
    dex = DexscreenerPoller(
        handle_mark,
        base_url=config.dexscreener_url,
        interval_s=config.dex_interval_s,
    )

    def watch(mint: str) -> None:
        feed.watch(mint)
        dex.watch(mint)

    for lane in lanes:
        lane.engine.on_watch = watch
        # A resumed lane holds positions opened before the restart; subscribe
        # them again so marks and exit prices keep flowing.
        for mint in list(lane.engine.positions):
            watch(mint)

    listener: TelegramListener | None = None
    telegram_task: asyncio.Task[Any] | None = None
    # Bounded LRU: Telethon can redeliver a message after a reconnect, but the
    # dedup window must never grow for the life of the process.
    seen: OrderedDict[tuple[str, int], None] = OrderedDict()
    # The scanner channel posts each new pool about three times. The research
    # signal is the first card only, so keep the first mint seen per lane and
    # ignore the repeats (bounded like ``seen``).
    scanner_seen: OrderedDict[tuple[str, str], None] = OrderedDict()

    # Scanner feed telemetry, local to this run: the channel posts each pool
    # ~3x, and only the first card is traded. Keyed by (channel, mint) so an
    # A/B run with two scanner lanes counts each card once, not once per lane.
    scanner_stats: dict[str, int] = {"cards": 0, "duplicates": 0}
    scanner_lane = next(
        (lane for lane in lanes if is_scanner_rule(lane.rule)), None
    )
    scanner_counted: OrderedDict[tuple[str, str], None] = OrderedDict()

    # The run summary's scanner totals are cumulative for the *run*, not for
    # this process: a resumed sample must not report only the cards seen since
    # the restart. Counters are persisted next to the recovery snapshot and
    # are only restored when this process actually resumed that run.
    if recovery is not None and recovered is not None:
        try:
            saved_stats = recovery.load_aux("scanner_stats")
        except RuntimeError:
            LOGGER.exception("could not read persisted scanner counters")
            saved_stats = None
        if isinstance(saved_stats, dict):
            for name in ("cards", "duplicates"):
                try:
                    scanner_stats[name] = int(saved_stats.get(name) or 0)
                except (TypeError, ValueError):
                    pass
        elif recovered is not None:
            # A legacy snapshot (or a run that predates counter persistence)
            # has no aux row; seed the card count from the audit trail so the
            # resumed summary does not restart at zero. Duplicates cannot be
            # reconstructed, so they stay at zero for the migrated run.
            scanner_stats["cards"] = _count_scanner_cards(
                Path(recovered["run_dir"])
            )
    def persist_scanner_stats() -> None:
        if recovery is None:
            return
        try:
            recovery.save_aux("scanner_stats", dict(scanner_stats))
        except RuntimeError:
            LOGGER.exception("could not persist scanner counters")

    async def on_message(
        *,
        channel: str,
        message: str,
        message_id: int,
        ts_ms: int,
    ) -> None:
        key = (channel, message_id)
        if key in seen:
            return
        seen[key] = None
        if len(seen) > config.message_dedup_window:
            seen.popitem(last=False)
        if (
            recovery is not None
            and message_id
            and not recovery.claim_event(f"message:{channel}:{message_id}")
        ):
            LOGGER.debug(
                "message %s/%s was already processed before the restart",
                channel,
                message_id,
            )
            return
        for lane in lanes:
            signal = parse_signal_text(
                message,
                signal_ts_ms=ts_ms,
                received_ts_ms=int(time.time() * 1000),
                channel=channel,
                message_id=message_id or None,
            )
            signal.features["strategy_tag"] = lane.name
            if (
                lane is scanner_lane
                and signal.message_type == SCANNER_MESSAGE_TYPE
            ):
                count_key = (channel, signal.token_address)
                # Durable pool identity: a repost delivered after a restart is
                # the same pool, so it must not count as a new card or become
                # a second trade.
                already_claimed = count_key in scanner_counted
                if (
                    not already_claimed
                    and recovery is not None
                    and signal.token_address
                ):
                    already_claimed = not recovery.claim_event(
                        f"scanner:{channel}:{signal.token_address}"
                    )
                if already_claimed:
                    scanner_stats["duplicates"] += 1
                else:
                    scanner_stats["cards"] += 1
                    scanner_counted[count_key] = None
                    if len(scanner_counted) > config.message_dedup_window:
                        scanner_counted.popitem(last=False)
                persist_scanner_stats()
            if (
                is_scanner_rule(lane.rule)
                and signal.message_type == SCANNER_MESSAGE_TYPE
                and not _claim_scanner_card(
                    lane_name=lane.name,
                    token_address=signal.token_address,
                    channel=channel,
                    seen=scanner_seen,
                    recovery=recovery,
                    window=config.message_dedup_window,
                )
            ):
                LOGGER.debug(
                    "scanner duplicate card lane=%s %s",
                    lane.name,
                    signal.token_address,
                )
                continue
            # Persistence health is enforced inside ``engine.on_signal``:
            # it records a ``persistence_degraded`` skip in the audit trail
            # and keeps exits running. Do not duplicate that check here —
            # a signal-level ``continue`` would silently omit both the
            # signal and the skip from the forward-gate accounting.
            decision = await lane.engine.on_signal(signal)
            LOGGER.info(
                "signal lane=%s %s %s allowed=%s lag_ms=%s reasons=%s",
                lane.name,
                channel,
                signal.token_address or "<none>",
                decision.allowed,
                int(signal.features.get("delivery_lag_ms") or 0),
                ",".join(decision.reasons) or "-",
            )

    if config.telegram:
        telegram_config = load_telegram_config(
            config.config_path, env=env, session_path=config.session_file
        )
        listener = TelegramListener(telegram_config, config.channels, on_message)

    for lane in lanes:
        if config.reports:
            lane.reports_started = await lane.reporter.start()
            if lane.reports_started:
                # Report recovered cash/equity from the actual wallet, not from
                # ``starting_sol``. Equity is only shown when every restored
                # token has a fresh mark; otherwise cash-only equity would
                # understate the position and look like a loss.
                marks: dict[str, float] = {}
                if resumed_at is not None:
                    for mint in list(lane.wallet.token_balances):
                        price, _ = history.last_price(mint)
                        if price is not None and price > 0:
                            marks[mint] = price
                unmarked = any(
                    mint not in marks for mint in lane.wallet.token_balances
                )
                equity = (
                    None
                    if unmarked
                    else lane.wallet.equity_sol(marks)
                )
                session_pnl: float | None
                try:
                    session_pnl = sum(
                        lane.store.realized_pnl_by_day().values()
                    )
                except OSError:
                    session_pnl = None
                await lane.reporter.notify_startup(
                    rule=lane.rule,
                    horizon_min=config.horizon_min,
                    notional_sol=config.notional_sol,
                    starting_sol=config.starting_sol,
                    recovered_cash_sol=(
                        lane.wallet.sol_balance
                        if resumed_at is not None
                        else None
                    ),
                    equity_sol=equity,
                    open_positions=len(lane.engine.positions),
                    max_positions=config.max_concurrent_positions,
                    session_pnl_sol=session_pnl,
                    recovery_status=(
                        "recovered" if resumed_at is not None else "fresh"
                    ),
                    min_preflight_exit_value_ratio=(
                        config.min_preflight_exit_value_ratio
                    ),
                    resumed_at=(
                        run_started_at.isoformat()
                        if resumed_at is not None
                        else None
                    ),
                    fee=config.fee,
                    slippage=config.slippage,
                    channels=config.channels,
                    session_file=str(
                        listener.config.session_file if listener is not None else "—"
                    ),
                    preflight_exit_route=config.preflight_exit_route,
                    rule_description=(
                        f"{RULE_DESCRIPTIONS.get(lane.rule, '')} "
                        f"(entry: {lane.entry_mode})"
                    ).strip(),
                    warmup_min=config.history_horizon_ms / 60_000,
                    strategy_tag=lane.name if is_ab else None,
                    mode=config.mode,
                )

    if listener is not None:
        telegram_task = asyncio.create_task(listener.run())
        telegram_task.add_done_callback(
            lambda task: _watch_task(task, reporter, "telegram")
        )

    shutdown_reason = "signal"
    shutdown_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed_signals: list[signal.Signals] = []

    def request_shutdown(reason: str) -> None:
        """Record why the process is stopping; the first reason wins."""
        nonlocal shutdown_reason
        if shutdown_event.is_set():
            return
        shutdown_reason = reason
        LOGGER.info("%s; shutting down", reason)
        shutdown_event.set()

    ticker = asyncio.create_task(
        _ticker(
            [lane.engine for lane in lanes],
            dex,
            interval_s=config.tick_interval_s,
            history=history,
            feed=feed,
            memory_limit_mb=config.memory_limit_mb,
            report_s=config.memory_report_s,
            memory_trace=config.memory_trace,
            on_memory_limit=lambda rss: request_shutdown(
                f"memory_limit:{rss:.0f}MB"
            ),
        )
    )
    feed_task = asyncio.create_task(feed.run())
    dex_task = asyncio.create_task(dex.run())
    for task, label in (
        (feed_task, "pumpapi"),
        (dex_task, "dexscreener"),
        (ticker, "ticker"),
    ):
        task.add_done_callback(lambda t, name=label: _watch_task(t, reporter, name))

    # oxmgr sends SIGTERM. Without a handler the process dies immediately and
    # the final report and summary.json are never written.
    for signame in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(signame, request_shutdown, signame.name)
            installed_signals.append(signame)
        except (NotImplementedError, RuntimeError):  # pragma: no cover - non-Unix
            pass

    shutdown_waiter = asyncio.create_task(shutdown_event.wait())
    try:
        labels = {
            feed_task: "pumpapi",
            dex_task: "dexscreener",
            ticker: "ticker",
        }
        if telegram_task is not None:
            labels[telegram_task] = "telegram"
        waiters: set[asyncio.Task[Any]] = {shutdown_waiter, *labels}
        done, _ = await asyncio.wait(
            waiters,
            timeout=config.max_seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            shutdown_reason = "max_seconds"
            LOGGER.info("max_seconds=%s reached; shutting down", config.max_seconds)
        elif shutdown_waiter in done:
            # ``request_shutdown`` already recorded the reason (signal or the
            # memory guard); keep it.
            pass
        else:
            # A crashed feed/ticker/reporter would otherwise leave a zombie
            # process that receives no signals. Exit so oxmgr can restart it.
            crashed = next(
                (task for task in labels if task in done and not task.cancelled()),
                None,
            )
            if crashed is not None:
                exc = crashed.exception()
                if exc is not None:
                    raise exc
                shutdown_reason = f"task_stopped:{labels[crashed]}"
            else:
                shutdown_reason = "task_stopped"
    except (KeyboardInterrupt, asyncio.CancelledError):
        shutdown_reason = "cancelled"
        LOGGER.info("shutdown requested")
    except Exception as exc:
        # A failed Telegram listener means no new signals. Let the process exit
        # so the restart policy can rebuild the session instead of idling.
        shutdown_reason = f"telegram_error: {type(exc).__name__}"
        LOGGER.exception("Telegram listener failed")
        raise
    finally:
        shutdown_waiter.cancel()
        for signame in installed_signals:
            loop.remove_signal_handler(signame)
        feed.stop()
        dex.stop()
        ticker.cancel()
        tasks = [feed_task, dex_task, ticker, shutdown_waiter]
        if telegram_task is not None:
            telegram_task.cancel()
            tasks.append(telegram_task)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

        # Closing positions is the most important shutdown step: a SIGTERM
        # from oxmgr must never leave an open trade out of the audit trail.
        async def finalize_lane(lane: StrategyLane) -> None:
            try:
                await lane.engine.finalize(
                    force=True, timeout_ms=config.shutdown_timeout_ms
                )
            except Exception:  # noqa: BLE001 - still write the run summary
                LOGGER.exception("engine finalize failed for lane %s", lane.name)
            finally:
                if lane.reports_started:
                    await lane.reporter.flush()
                marks = {
                    mint: price
                    for mint in list(lane.wallet.token_balances)
                    if (price := history.last_price(mint)[0]) is not None
                }
                lane.wallet_snapshot = lane.wallet.snapshot(marks)
                lane.store.finalize(
                    extra={
                        "lane": lane.name,
                        "rule": lane.rule,
                        "entry_mode": lane.entry_mode,
                        "feed_received": feed.received,
                        "feed_delivered": feed.delivered,
                        "feed_connections": feed.connections,
                        "history_mints": history.mint_count,
                        "history_events": history.total_events,
                        "peak_rss_mb": round(_MEMORY["peak_mb"], 1),
                        "open_positions": len(lane.engine.positions),
                        "pending_entries": len(lane.engine.pending),
                        "wallet": lane.wallet_snapshot,
                        "shutdown_reason": shutdown_reason,
                        "scanner_cards": scanner_stats["cards"],
                        "scanner_duplicates": scanner_stats["duplicates"],
                        # Research integrity: a failed snapshot write means
                        # part of this process ran without durable state, so
                        # the run cannot be pooled with clean validation runs.
                        # INVALID additionally means the durable evidence and
                        # the last snapshot could not be reconciled.
                        "integrity": _lane_integrity(lane),
                        "persistence_degraded": lane._persistence_degraded,
                        "integrity_at_start": start_integrity,
                        "preflight_exit_min_ratio": (
                            config.min_preflight_exit_value_ratio
                        ),
                        "recovery_status": (
                            "recovered" if resumed_at is not None else "fresh"
                        ),
                        # ``started_at`` is the original run start (preserved
                        # across resumes); the process timings are this
                        # process only, so a restarted sample is not reported
                        # as younger than it is.
                        "started_at": run_started_at.isoformat(),
                        "resumed_at": (
                            resumed_at.isoformat()
                            if resumed_at is not None
                            else None
                        ),
                        "process_started_at": process_started_at.isoformat(),
                        "process_runtime_ms": int(time.time() * 1000)
                        - started_at_ms,
                        "stopped_at": datetime.now(UTC).isoformat(),
                    }
                )
                if lane.reports_started:
                    await lane.reporter.notify_shutdown(
                        reason=shutdown_reason,
                        wallet=lane.wallet_snapshot,
                        runtime_ms=int(time.time() * 1000) - started_at_ms,
                        rule=lane.rule,
                        horizon_min=config.horizon_min,
                        strategy_tag=lane.name if is_ab else None,
                        integrity=_lane_integrity(lane),
                    )

        await asyncio.gather(*(finalize_lane(lane) for lane in lanes))
        if recovery is not None:
            try:
                recovery.close()
            except Exception:  # noqa: BLE001 - shutdown must not mask results
                LOGGER.exception("could not close the recovery state store")
        if is_ab:
            try:
                _write_ab_summary(
                    config.run_dir,
                    lanes,
                    started_at_ms=started_at_ms,
                    shutdown_reason=shutdown_reason,
                )
            except Exception:  # noqa: BLE001 - never mask the run summary
                LOGGER.exception("could not write A/B summary")
        await quotes.close()
        await dex.close()
        if listener is not None:
            try:
                await listener.close()
            except Exception as exc:  # shutdown must not mask results
                LOGGER.warning("Telegram listener close failed: %s", exc)
        for lane in lanes:
            await lane.reporter.stop()

    if is_ab:
        return {
            "run_dir": str(config.run_dir),
            "mode": "ab",
            "lanes": {
                lane.name: {
                    "rule": lane.rule,
                    "entry_mode": lane.entry_mode,
                    "run_dir": str(lane.run_dir),
                    "closed": lane.engine.closed_total,
                    "skips": lane.engine.skip_total,
                    "signals": lane.engine.signal_total,
                }
                for lane in lanes
            },
        }
    return {
        "run_dir": str(config.run_dir),
        "closed": primary.engine.closed_total,
        "skips": primary.engine.skip_total,
        "signals": primary.engine.signal_total,
    }
