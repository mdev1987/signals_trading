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
from .quotes import JupiterQuoteProvider
from .signals import (
    DEFAULT_RULE,
    RULE_DESCRIPTIONS,
    SUPPORTED_RULES,
    GateConfig,
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
    # Optional second lane. ``ab_rule`` varies the rule and ``ab_entry_mode``
    # varies the execution; either alone (or both) starts an A/B run.
    ab_rule: str | None = None
    entry_mode: str = "timer"
    ab_entry_mode: str | None = None
    horizon_min: float = 5.0
    tp_pct: float | None = None
    sl_pct: float | None = None
    fee: float = 0.0125
    slippage: float = 0.0
    notional_sol: float = 0.1
    slippage_bps: int = 50
    # Priority fee (lamports) and Jito MEV tip (lamports) help live
    # transactions land faster during volatile pump launches, reducing
    # entry slippage. Set to 0 to let Jupiter determine fees automatically.
    priority_fee_lamports: int = 0
    jito_tip_lamports: int = 0
    entry_delay_ms: int = 5_000
    max_entry_delay_ms: int = 120_000
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
    message_dedup_window: int = 10_000
    shutdown_timeout_ms: int = 25_000

    def __post_init__(self) -> None:
        """Fail fast on values that would silently distort every fill."""
        if self.rule not in SUPPORTED_RULES:
            raise ValueError(
                f"Unsupported rule {self.rule!r}; choose from {SUPPORTED_RULES}"
            )
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
        if not 0 <= self.fee < 1:
            raise ValueError("fee must be in [0, 1)")
        if not 0 <= self.slippage < 1:
            raise ValueError("slippage must be in [0, 1)")
        if not 0 <= self.slippage_bps <= 10_000:
            raise ValueError("slippage_bps must be in [0, 10000]")
        if self.priority_fee_lamports < 0:
            raise ValueError("priority_fee_lamports must be non-negative")
        if self.jito_tip_lamports < 0:
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
        if self.exit_quote_timeout_ms <= 0:
            raise ValueError("exit_quote_timeout_ms must be positive")
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


async def _ticker(
    engines: list[PaperEngine],
    dex: DexscreenerPoller,
    *,
    interval_s: float,
    history: MarketHistory | None = None,
    memory_limit_mb: int = 0,
    report_s: float = 300.0,
    on_memory_limit: Callable[[float], None] | None = None,
) -> None:
    next_report = time.monotonic() + report_s
    guard_tripped = False
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
                "history_mints=%s history_events=%s",
                rss,
                _MEMORY["peak_mb"],
                _LOOP_LAG["max_ms"],
                history.mint_count if history is not None else "-",
                history.total_events if history is not None else "-",
            )
            _LOOP_LAG["max_ms"] = 0.0


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


def _lane_meta(
    config: LiveConfig,
    *,
    lane: str,
    rule: str,
    entry_mode: str,
    is_ab: bool,
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
        "tp_pct": config.tp_pct,
        "sl_pct": config.sl_pct,
        "fee": config.fee,
        "slippage": config.slippage,
        "slippage_bps": config.slippage_bps,
        "notional_sol": config.notional_sol,
        "starting_sol": config.starting_sol,
        "preflight_exit_route": config.preflight_exit_route,
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
        "started_at": datetime.now(UTC).isoformat(),
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
            config, lane=name, rule=rule, entry_mode=entry_mode, is_ab=is_ab
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
            horizon_ms=int(config.horizon_min * 60_000),
            tp_pct=config.tp_pct,
            sl_pct=config.sl_pct,
            live=True,
            exit_quote_timeout_ms=config.exit_quote_timeout_ms,
            slippage_bps=config.slippage_bps,
            horizon_settle_ms=config.horizon_settle_ms,
            preflight_exit_route=config.preflight_exit_route,
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


async def run_live(config: LiveConfig) -> dict[str, Any]:
    started_at_ms = int(time.time() * 1000)
    _MEMORY["peak_mb"] = 0.0
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
        )
    lanes: list[StrategyLane] = []
    for index, (name, rule, entry_mode) in enumerate(specs):
        lane_dir = config.run_dir / name if is_ab else config.run_dir
        lanes.append(
            _create_lane(
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
            )
        )
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

    async def handle_mark(mint: str, price_sol: float, ts_ms: int) -> None:
        await asyncio.gather(
            *(
                lane.engine.on_mark(mint, price_sol, ts_ms)
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

    listener: TelegramListener | None = None
    telegram_task: asyncio.Task[Any] | None = None
    # Bounded LRU: Telethon can redeliver a message after a reconnect, but the
    # dedup window must never grow for the life of the process.
    seen: OrderedDict[tuple[str, int], None] = OrderedDict()

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
        for lane in lanes:
            signal = parse_signal_text(
                message,
                signal_ts_ms=ts_ms,
                received_ts_ms=int(time.time() * 1000),
                channel=channel,
                message_id=message_id or None,
            )
            signal.features["strategy_tag"] = lane.name
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
                await lane.reporter.notify_startup(
                    rule=lane.rule,
                    horizon_min=config.horizon_min,
                    notional_sol=config.notional_sol,
                    starting_sol=config.starting_sol,
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
            memory_limit_mb=config.memory_limit_mb,
            report_s=config.memory_report_s,
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
                    )

        await asyncio.gather(*(finalize_lane(lane) for lane in lanes))
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
                    "closed": len(lane.engine.closed),
                    "skips": len(lane.engine.skips),
                    "signals": len(lane.engine.signal_log),
                }
                for lane in lanes
            },
        }
    return {
        "run_dir": str(config.run_dir),
        "closed": len(primary.engine.closed),
        "skips": len(primary.engine.skips),
        "signals": len(primary.engine.signal_log),
    }
