"""Command line entry point: ``paper-bot`` or ``python -m paper_bot``."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
from datetime import UTC, datetime
from pathlib import Path

from .live import LiveConfig, _load_env, _read_channels, run_live
from .paths import channels_file, config_file, data_home, env_file
from .quotes import DEFAULT_MAX_SLIPPAGE_BPS
from .recovery import default_state_db
from .signals import DEFAULT_RULE

BOT_TOKEN_RE = re.compile(r"(?i)\b(bot\d+:)[A-Za-z0-9_-]{20,}")
API_KEY_RE = re.compile(
    r"(?i)\b(x-api-key|api[_-]?key)([\"']?\s*[:=]\s*[\"']?)"
    r"[A-Za-z0-9._-]{8,}"
)


class RedactSecrets(logging.Filter):
    """Mask Bot API tokens and API keys before a record reaches a handler."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - never break logging
            return True
        redacted = BOT_TOKEN_RE.sub(r"\1<redacted>", message)
        redacted = API_KEY_RE.sub(r"\1\2<redacted>", redacted)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


def _add_replay(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--signals",
        type=Path,
        default=None,
        help="default: <repo>/output/signals/buy_signals.parquet",
    )
    parser.add_argument(
        "--outcomes",
        type=Path,
        default=None,
        help="default: <repo>/output/backtest/scenario_outcomes.parquet",
    )
    parser.add_argument("--scenario", default="hold_15m")
    parser.add_argument("--rule", default="baseline")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--safety-screen", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=None)


def _add_live(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--channels-file", type=Path, default=channels_file())
    parser.add_argument("--channel", action="append", default=None)
    parser.add_argument("--config", type=Path, default=config_file())
    parser.add_argument("--session-file", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--rule", default=DEFAULT_RULE)
    parser.add_argument(
        "--mode",
        choices=("paper", "live"),
        default="paper",
        help=(
            "paper: quote-only, never sign or broadcast; "
            "live: sign and submit Jupiter swaps (requires LIVE_TRADING_ENABLED=1)"
        ),
    )
    parser.add_argument(
        "--ab-rule",
        default=None,
        help=(
            "optional second rule; runs side by side with --rule on the same "
            "signal stream, with a separate wallet and run directory"
        ),
    )
    parser.add_argument(
        "--entry-mode",
        choices=("trade", "timer"),
        default="timer",
        help=(
            "trade: enter on the first BUY print at/after the delay (historical); "
            "timer: quote at the delay, matching the research entry timing"
        ),
    )
    parser.add_argument(
        "--ab-entry-mode",
        choices=("trade", "timer"),
        default=None,
        help="optional second execution mode for the A/B lane",
    )
    parser.add_argument("--horizon-min", type=float, default=5.0)
    parser.add_argument("--tp-pct", type=float, default=None)
    parser.add_argument("--sl-pct", type=float, default=None)
    parser.add_argument("--fee", type=float, default=0.0125)
    parser.add_argument("--slippage", type=float, default=0.0)
    parser.add_argument("--notional-sol", type=float, default=0.1)
    parser.add_argument(
        "--starting-sol",
        type=float,
        default=10.0,
        help="paper wallet starting SOL balance",
    )
    parser.add_argument(
        "--no-preflight-exit",
        action="store_true",
        help="skip the sell-route check before opening a position",
    )
    parser.add_argument(
        "--exit-fallback-feed",
        action="store_true",
        help=(
            "mark unsellable tokens at the last feed price instead of "
            "writing them off"
        ),
    )
    parser.add_argument(
        "--slippage-bps",
        type=int,
        default=50,
        help=(
            "slippage tolerance for the first quote attempt (default 50). A "
            "fresh pool that does not route is retried with a wider tolerance."
        ),
    )
    parser.add_argument(
        "--max-slippage-bps",
        type=int,
        default=DEFAULT_MAX_SLIPPAGE_BPS,
        help=(
            "widest tolerance buy/sell may try before giving up "
            f"(default {DEFAULT_MAX_SLIPPAGE_BPS} = 10%%)"
        ),
    )
    parser.add_argument(
        "--priority-fee-lamports",
        type=int,
        default=None,
        help=(
            "Priority fee in lamports for live execution "
            "(overrides LIVE_PRIORITY_FEE_LAMPORTS)"
        ),
    )
    parser.add_argument(
        "--jito-tip-lamports",
        type=int,
        default=None,
        help=(
            "Jito MEV tip in lamports for live execution "
            "(overrides LIVE_JITO_TIP_LAMPORTS)"
        ),
    )
    parser.add_argument("--entry-delay-ms", type=int, default=5_000)
    parser.add_argument("--max-entry-delay-ms", type=int, default=60_000)
    parser.add_argument(
        "--max-delivery-lag-ms",
        type=int,
        default=60_000,
        help=(
            "Skip a card Telegram delivered later than this after it was "
            "posted (0 disables). The research convention gives up at 60 s."
        ),
    )
    parser.add_argument(
        "--max-concurrent-positions",
        type=int,
        default=10,
        help=(
            "Cap open positions plus pending entries; the scanner backtest "
            "assumed at most 10 at once (0 = unlimited)."
        ),
    )
    parser.add_argument(
        "--max-daily-loss-sol",
        type=float,
        default=0.0,
        help=(
            "Stop opening positions after this much realized loss in the UTC "
            "day (0 disables). A risk control, not part of the edge."
        ),
    )
    parser.add_argument(
        "--entry-retry-backoff-ms",
        type=int,
        default=1_000,
        help=(
            "Minimum gap between quote attempts for the same mint while "
            "waiting for its first routable BUY print."
        ),
    )
    parser.add_argument(
        "--min-entry-quote-sol",
        type=float,
        default=0.01,
        help=(
            "Reject a BUY print below this canonical SOL notional as an "
            "entry trigger (0 disables). Mirrors alert_outcomes.py "
            "--min-quote-sol so live entries match the backtest."
        ),
    )
    parser.add_argument(
        "--max-reference-age-ms",
        type=int,
        default=30_000,
        help=(
            "Timer mode only: refuse to quote from a reference price older "
            "than this (0 disables)."
        ),
    )
    parser.add_argument(
        "--max-mark-age-ms",
        type=int,
        default=120_000,
        help=(
            "Exclude marks older than this from reported NAV (0 disables). "
            "The optimistic view is still written as equity_all_marks_sol."
        ),
    )
    parser.add_argument(
        "--timer-late-tolerance-ms",
        type=int,
        default=1_000,
        help=(
            "Timer mode: reject a card received later than the scheduled "
            "entry time plus this tolerance instead of quoting at receipt."
        ),
    )
    parser.add_argument(
        "--max-future-skew-ms",
        type=int,
        default=5_000,
        help=(
            "Treat feed timestamps further in the future than this as clock/"
            "data errors, not as fresh marks."
        ),
    )
    parser.add_argument(
        "--risk-state-dir",
        default="output/risk",
        help=(
            "Stable directory for daily realized-PnL state. Must not be the "
            "timestamped run directory, or a restart resets the loss cap."
        ),
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help=(
            "paper only: resume the wallet, open positions and run directory "
            "saved by the previous process, so a restart continues the same "
            "forward sample instead of starting a new one"
        ),
    )
    parser.add_argument(
        "--state-db",
        type=Path,
        default=None,
        help=(
            "paper restart-recovery file used with --resume "
            "(default output/recovery/<rule>_<entry-mode>.json)"
        ),
    )
    parser.add_argument("--safety-screen", action="store_true")
    parser.add_argument("--allow-candidate-address", action="store_true")
    parser.add_argument("--pumpapi-url", default="wss://stream.pumpapi.io/")
    parser.add_argument("--dexscreener-url", default="https://api.dexscreener.com")
    parser.add_argument("--tick-interval", type=float, default=1.0)
    parser.add_argument("--dex-interval", type=float, default=2.0)
    parser.add_argument("--exit-quote-timeout-ms", type=int, default=30_000)
    parser.add_argument(
        "--shutdown-timeout-ms",
        type=int,
        default=25_000,
        help="max time to close open positions on SIGTERM",
    )
    parser.add_argument(
        "--history-horizon-min",
        type=float,
        default=35.0,
        help="minutes of firehose history retained for pre-signal features",
    )
    parser.add_argument(
        "--history-max-mints",
        type=int,
        default=10_000,
        help="LRU cap for the live pre-signal history index",
    )
    parser.add_argument(
        "--history-max-events-per-mint",
        type=int,
        default=3_000,
        help="per-mint cap for buffered firehose trades",
    )
    parser.add_argument(
        "--history-max-total-events",
        type=int,
        default=150_000,
        help="global cap for buffered firehose trades (memory budget)",
    )
    parser.add_argument(
        "--memory-limit-mb",
        type=int,
        default=550,
        help=(
            "restart cleanly above this resident size instead of being "
            "OOM-killed; 0 disables the guard"
        ),
    )
    parser.add_argument(
        "--memory-report-s",
        type=float,
        default=300.0,
        help="seconds between memory telemetry log lines",
    )
    parser.add_argument(
        "--max-seconds",
        type=float,
        default=None,
        help="stop after N seconds (bounded smoke test)",
    )
    parser.add_argument(
        "--no-telegram",
        action="store_true",
        help="skip the Telegram client and only run feeds (requires --max-seconds)",
    )
    parser.add_argument(
        "--no-reports",
        action="store_true",
        help="do not send Telegram reports (trading continues)",
    )
    parser.add_argument(
        "--no-report-commands",
        action="store_true",
        help="do not poll /status and /stats commands",
    )
    parser.add_argument(
        "--no-daily-summary",
        action="store_true",
        help="do not send the 00:00 UTC daily summary",
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    replay = sub.add_parser("replay", help="validate the engine against the backtest")
    _add_replay(replay)

    live = sub.add_parser("live", help="listen to Telegram and paper trade")
    _add_live(live)

    login = sub.add_parser("login", help="authorise the Telethon session once")
    login.add_argument("--config", type=Path, default=config_file())
    login.add_argument("--session-file", type=Path, default=None)

    quote = sub.add_parser("quote", help="print a read-only Jupiter quote")
    quote.add_argument("--mint", required=True)
    quote.add_argument("--notional-sol", type=float, default=1.0)
    quote.add_argument("--slippage-bps", type=int, default=50)
    quote.add_argument(
        "--max-slippage-bps", type=int, default=DEFAULT_MAX_SLIPPAGE_BPS
    )

    report = sub.add_parser("report", help="summarize a paper run directory")
    report.add_argument("run_dir", type=Path)
    return parser.parse_args()


def _logging() -> None:
    level = os.environ.get("PAPER_BOT_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # httpx logs complete request URLs at INFO, and Telegram Bot API URLs embed
    # the bot token. Keep those lines out of the on-disk log and mask any token
    # that still reaches a handler through another library or an error.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    redact = RedactSecrets()
    for handler in logging.getLogger().handlers:
        handler.addFilter(redact)


async def _run_quote(args: argparse.Namespace) -> dict:
    from .quotes import JupiterQuoteProvider

    env = _load_env(env_file())
    api_key = env.get("JUPITER_API_KEY")
    if not api_key:
        raise SystemExit("JUPITER_API_KEY is not set")
    provider = JupiterQuoteProvider(
        api_key=api_key,
        slippage_bps=args.slippage_bps,
        max_slippage_bps=args.max_slippage_bps,
    )
    try:
        quote = await provider.entry_quote(args.mint, args.notional_sol, None)
        return quote.to_dict()
    finally:
        await provider.close()


async def _run_login(args: argparse.Namespace) -> str:
    from .telegram_client import TelegramListener, load_telegram_config

    env = _load_env(env_file())
    config = load_telegram_config(
        args.config, env=env, session_path=args.session_file
    )
    listener = TelegramListener(config, [], lambda **_: None)
    return await listener.login()


def _replay(args: argparse.Namespace) -> dict:
    # Replay is a development feature: it needs the research repository's
    # backtest_signals module and replay archives. Import lazily so the
    # standalone live bot never depends on them.
    from .replay import run_validation

    base = data_home()
    signals_path = args.signals or base / "output" / "signals" / "buy_signals.parquet"
    outcomes_path = (
        args.outcomes
        or base / "output" / "backtest" / "scenario_outcomes.parquet"
    )
    return asyncio.run(
        run_validation(
            signals_path=signals_path,
            outcomes_path=outcomes_path,
            scenario=args.scenario,
            rule=args.rule,
            limit=args.limit,
            safety=args.safety_screen,
            output_dir=args.output_dir,
        )
    )


def main() -> int:
    args = _parse_args()
    _logging()

    if args.command == "replay":
        summary = _replay(args)
        print(json.dumps(summary, indent=2, default=str))
        return 0 if summary["mismatches"] == 0 else 1

    if args.command == "live":
        channels = args.channel or _read_channels(args.channels_file)
        run_dir = args.output_dir or (
            data_home()
            / "output"
            / "paper"
            / f"live_{datetime.now(UTC):%Y%m%d_%H%M%S}"
        )
        # Every single-lane paper run records a restart snapshot, so an
        # unexpected crash can be resumed. A/B and live runs do not.
        paper_lane = args.mode == "paper" and (
            args.ab_rule is None and args.ab_entry_mode is None
        )
        state_db: Path | None = args.state_db
        if paper_lane:
            state_db = state_db or default_state_db(args.rule, args.entry_mode)
        config = LiveConfig(
            channels=channels,
            config_path=args.config,
            session_file=args.session_file,
            run_dir=run_dir,
            mode=args.mode,
            rule=args.rule,
            ab_rule=args.ab_rule,
            entry_mode=args.entry_mode,
            ab_entry_mode=args.ab_entry_mode,
            horizon_min=args.horizon_min,
            tp_pct=args.tp_pct,
            sl_pct=args.sl_pct,
            fee=args.fee,
            slippage=args.slippage,
            notional_sol=args.notional_sol,
            starting_sol=args.starting_sol,
            preflight_exit_route=not args.no_preflight_exit,
            exit_fallback_feed=args.exit_fallback_feed,
            slippage_bps=args.slippage_bps,
            max_slippage_bps=args.max_slippage_bps,
            priority_fee_lamports=args.priority_fee_lamports,
            jito_tip_lamports=args.jito_tip_lamports,
            entry_delay_ms=args.entry_delay_ms,
            max_entry_delay_ms=args.max_entry_delay_ms,
            max_delivery_lag_ms=args.max_delivery_lag_ms,
            max_concurrent_positions=args.max_concurrent_positions,
            max_daily_loss_sol=args.max_daily_loss_sol,
            entry_retry_backoff_ms=args.entry_retry_backoff_ms,
            min_entry_quote_sol=args.min_entry_quote_sol,
            max_reference_age_ms=args.max_reference_age_ms,
            max_mark_age_ms=args.max_mark_age_ms,
            timer_late_tolerance_ms=args.timer_late_tolerance_ms,
            max_future_skew_ms=args.max_future_skew_ms,
            risk_state_dir=args.risk_state_dir,
            resume=args.resume,
            state_db=state_db,
            safety_screen=args.safety_screen,
            allow_candidate_address=args.allow_candidate_address,
            pumpapi_url=args.pumpapi_url,
            dexscreener_url=args.dexscreener_url,
            tick_interval_s=args.tick_interval,
            dex_interval_s=args.dex_interval,
            exit_quote_timeout_ms=args.exit_quote_timeout_ms,
            shutdown_timeout_ms=args.shutdown_timeout_ms,
            history_horizon_ms=int(args.history_horizon_min * 60_000),
            history_max_mints=args.history_max_mints,
            history_max_events_per_mint=args.history_max_events_per_mint,
            history_max_total_events=args.history_max_total_events,
            memory_limit_mb=args.memory_limit_mb,
            memory_report_s=args.memory_report_s,
            telegram=not args.no_telegram,
            reports=not args.no_reports,
            report_commands=not args.no_report_commands,
            daily_summary=not args.no_daily_summary,
            max_seconds=args.max_seconds,
        )
        summary = asyncio.run(run_live(config))
        print(json.dumps(summary, indent=2, default=str))
        return 0

    if args.command == "login":
        username = asyncio.run(_run_login(args))
        print(json.dumps({"authorized": True, "account": username}))
        return 0

    if args.command == "quote":
        quote = asyncio.run(_run_quote(args))
        print(json.dumps(quote, indent=2, default=str))
        return 0

    if args.command == "report":
        if not args.run_dir.is_dir():
            raise SystemExit(f"Not a directory: {args.run_dir}")
        ab_path = args.run_dir / "ab_summary.json"
        summary_path = args.run_dir / "summary.json"
        if ab_path.exists():
            summary = json.loads(ab_path.read_text(encoding="utf-8"))
        elif summary_path.exists():
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        else:
            from .store import summarize

            summary = summarize(args.run_dir)
        print(json.dumps(summary, indent=2, sort_keys=True, default=str))
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
