"""Replay parity harness.

Runs the paper engine over the same replay trades and signals as the research
backtest, using a deterministic quote provider that returns the reference
price. The resulting trades should match
``output/backtest/scenario_outcomes.parquet`` row for row (within float
tolerance). This validates the state machine before it is pointed at live
Telegram and PumpAPI data.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from .broker import BrokerConfig, PaperBroker
from .engine import EngineConfig, PaperEngine, SafetyConfig
from .history import MarketHistory
from .models import Signal, TradeEvent
from .paths import data_home
from .quotes import ReplayQuoteProvider
from .signals import GateConfig, parse_signal_text, post_signal_crowded
from .store import PaperStore

# Mirrored from ``backtest_signals`` so importing this development-only module
# never requires the research scripts. Replay validation still imports the
# research loaders lazily inside ``run_validation``.
DEFAULT_ENTRY_DELAY_MS = 5_000
DEFAULT_FEE = 0.001
DEFAULT_MAX_ENTRY_DELAY_MS = 120_000
DEFAULT_NOTIONAL_SOL = 1.0
DEFAULT_SLIPPAGE = 0.005

DEFAULT_SIGNALS = data_home() / "output" / "signals" / "buy_signals.parquet"
DEFAULT_OUTCOMES = (
    data_home() / "output" / "backtest" / "scenario_outcomes.parquet"
)

# The replay harness runs one mint at a time, so it can afford to keep the
# complete pre-signal history. A smaller cap changes ``pre_ret`` on busy mints.
REPLAY_HISTORY_CAP = 500_000

SCENARIOS = {
    "hold_15m": {"horizon_ms": 900_000, "tp_pct": None, "sl_pct": None},
    "hold_1h": {"horizon_ms": 3_600_000, "tp_pct": None, "sl_pct": None},
    "tp_25_hold_1h": {"horizon_ms": 3_600_000, "tp_pct": 0.25, "sl_pct": None},
    "tp_50_hold_1h": {"horizon_ms": 3_600_000, "tp_pct": 0.50, "sl_pct": None},
    "tp_100_hold_1h": {"horizon_ms": 3_600_000, "tp_pct": 1.00, "sl_pct": None},
    "sl_25_hold_1h": {"horizon_ms": 3_600_000, "tp_pct": None, "sl_pct": 0.25},
    "tp_25_sl_25_hold_1h": {
        "horizon_ms": 3_600_000,
        "tp_pct": 0.25,
        "sl_pct": 0.25,
    },
    "tp_50_sl_25_hold_1h": {
        "horizon_ms": 3_600_000,
        "tp_pct": 0.50,
        "sl_pct": 0.25,
    },
}


@dataclass(slots=True)
class ReplayResult:
    mint: str
    signal_ts_ms: int
    closed: dict[str, Any] | None
    skip_reason: str | None
    allowed: bool = True


def _events_for_mint(market: dict[str, Any]) -> list[TradeEvent]:
    ts = market["ts"]
    event_index = market["event_index"]
    price = market["price"]
    notional = market["notional"]
    signer = market["signer"]
    is_buy = market["is_buy"]
    events: list[TradeEvent] = []
    for index in range(len(ts)):
        price_sol = float(price[index])
        token_amount = float(notional[index]) / price_sol if price_sol > 0 else None
        events.append(
            TradeEvent(
                mint="",  # filled by caller
                timestamp_ms=int(ts[index]),
                event_index=int(event_index[index]),
                action="buy" if bool(is_buy[index]) else "sell",
                price_sol=price_sol,
                token_amount=token_amount,
                tx_signer=str(signer[index]) if signer[index] else None,
            )
        )
    return events


async def run_one(
    *,
    signal: Signal,
    market: dict[str, Any],
    scenario: dict[str, Any],
    gate: GateConfig,
    safety: SafetyConfig,
    fee: float,
    slippage: float,
    notional_sol: float,
) -> ReplayResult:
    history = MarketHistory(max_events_per_mint=REPLAY_HISTORY_CAP)
    broker = PaperBroker(
        BrokerConfig(fee=fee, slippage=slippage, notional_sol=notional_sol)
    )
    engine = PaperEngine(
        config=EngineConfig(
            entry_delay_ms=DEFAULT_ENTRY_DELAY_MS,
            max_entry_delay_ms=DEFAULT_MAX_ENTRY_DELAY_MS,
            horizon_ms=int(scenario["horizon_ms"]),
            tp_pct=scenario.get("tp_pct"),
            sl_pct=scenario.get("sl_pct"),
            live=False,
        ),
        broker=broker,
        quotes=ReplayQuoteProvider(),
        history=history,
        gate=gate,
        safety=safety,
    )

    events = _events_for_mint(market)
    for event in events:
        event.mint = signal.token_address
    if not events:
        return ReplayResult(
            signal.token_address, signal.signal_ts_ms, None, "no_replay_data"
        )

    # Feed pre-signal trades first so history features are causal.
    index = 0
    while index < len(events) and events[index].timestamp_ms <= signal.signal_ts_ms:
        await engine.on_trade(events[index], await_actions=True)
        index += 1

    await engine.on_signal(signal)
    allowed = engine.signal_log[-1][1].allowed if engine.signal_log else False

    while index < len(events):
        await engine.on_trade(events[index], await_actions=True)
        index += 1

    await engine.finalize(data_end_ts_ms=int(market["ts"][-1]))

    if engine.closed:
        return ReplayResult(
            signal.token_address,
            signal.signal_ts_ms,
            engine.closed[0].to_dict(),
            None,
            allowed=allowed,
        )
    reason = engine.skips[-1].reason if engine.skips else "no_position"
    detail = engine.skips[-1].detail if engine.skips else None
    return ReplayResult(
        signal.token_address,
        signal.signal_ts_ms,
        None,
        f"{reason}:{detail}" if detail else reason,
        allowed=allowed,
    )


def _load_outcomes(path: Path) -> dict[tuple[str, int, str], dict[str, Any]]:
    frame = pl.read_parquet(path).filter(pl.col("scenario").is_in(list(SCENARIOS)))
    rows: dict[tuple[str, int, str], dict[str, Any]] = {}
    for row in frame.iter_rows(named=True):
        rows[(str(row["mint"]), int(row["signal_ts_ms"]), str(row["scenario"]))] = row
    return rows


def _compare(
    expected: dict[str, Any] | None, actual: dict[str, Any] | None
) -> list[str]:
    if expected is None and actual is None:
        return []
    if expected is None or actual is None:
        return ["presence_mismatch"]
    checks = {
        "entry_ts_ms": 0,
        "entry_price_sol": 1e-12,
        "exit_ts_ms": 0,
        "exit_price_sol": 1e-12,
        "pnl_sol": 1e-9,
        "return": 1e-9,
        "exit_status": 0,
    }
    problems: list[str] = []
    for key, tolerance in checks.items():
        expected_value = expected.get(key)
        actual_key = "return_pct" if key == "return" else key
        actual_value = actual.get(actual_key)
        if isinstance(expected_value, str) or isinstance(actual_value, str):
            if expected_value != actual_value:
                problems.append(f"{key}:{expected_value}!={actual_value}")
            continue
        if expected_value is None and actual_value is None:
            continue
        if expected_value is None or actual_value is None:
            problems.append(f"{key}:presence")
            continue
        if not math.isclose(
            float(expected_value), float(actual_value), abs_tol=tolerance
        ):
            problems.append(
                f"{key}:{float(expected_value):.12g}!={float(actual_value):.12g}"
            )
    return problems


def _post_signal_exclusions(
    signals: pl.DataFrame,
    markets: dict[str, dict[str, Any]],
    expected: dict[tuple[str, int, str], dict[str, Any]],
) -> set[tuple[str, int]]:
    """Recompute the research rule's post-signal clause from replay trades."""
    excluded: set[tuple[str, int]] = set()
    for row in signals.iter_rows(named=True):
        mint = str(row["token_address"])
        signal_ts_ms = int(row["signal_ts_ms"])
        market = markets.get(mint)
        outcome = expected.get((mint, signal_ts_ms, "hold_15m"))
        if market is None or outcome is None:
            continue
        ts = market["ts"]
        notional = market["notional"]
        is_buy = market["is_buy"]
        start = int(np.searchsorted(ts, signal_ts_ms, side="left"))
        end = int(np.searchsorted(ts, signal_ts_ms + 5_000, side="left"))
        total = float(np.nansum(notional[start:end]))
        if total <= 0:
            continue
        buy_share = float(np.nansum(notional[start:end][is_buy[start:end]])) / total
        delay = outcome.get("entry_delay_ms")
        if delay is None:
            continue
        if post_signal_crowded(
            pre_ret=outcome.get("pre_ret"),
            buy_volume_share=buy_share,
            entry_delay_ms=int(delay),
        ):
            excluded.add((mint, signal_ts_ms))
    return excluded


async def run_validation(
    *,
    signals_path: Path,
    outcomes_path: Path,
    scenario: str,
    rule: str,
    limit: int | None = None,
    safety: bool = False,
    allow_candidate_address: bool = True,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    if scenario not in SCENARIOS:
        raise ValueError(
            f"Unknown scenario {scenario!r}; choose from {sorted(SCENARIOS)}"
        )
    scenario_config = SCENARIOS[scenario]

    # Development-only dependency: the replay archives and loaders live in the
    # research repository. The live bot never imports this function.
    try:
        from backtest_signals import find_trades_files, load_mint_trades, load_signals
    except ImportError as exc:  # pragma: no cover - deployment guard
        raise RuntimeError(
            "Replay validation needs the research repository on PYTHONPATH "
            "(backtest_signals.py). Install from the tg_signals_vbt checkout."
        ) from exc

    signals = load_signals(signals_path)
    if limit is not None:
        signals = signals.head(limit)
    trades_files = find_trades_files()
    if not trades_files:
        raise FileNotFoundError("No output/*/market_replay/trades.parquet files found")
    mints = signals["token_address"].unique().to_list()
    markets = load_mint_trades(trades_files, mints)
    expected = _load_outcomes(outcomes_path)

    store = None
    if output_dir is not None:
        store = PaperStore(
            output_dir,
            meta={
                "mode": "replay_validation",
                "scenario": scenario,
                "rule": rule,
                "safety": safety,
                "signals": str(signals_path),
            },
        )

    gate = GateConfig(
        rule=rule,
        allow_candidate_address=allow_candidate_address,
    )
    safety_config = SafetyConfig(enabled=safety)
    results: list[ReplayResult] = []
    mismatches: list[dict[str, Any]] = []

    # ``exclude_crowded_pre`` has a second clause that only becomes known at
    # the entry trigger. Recompute the research expectation independently from
    # the replay trades so a skipped entry can be told apart from a bug.
    post_exclusions: set[tuple[str, int]] = set()
    if rule == "exclude_crowded_pre":
        post_exclusions = _post_signal_exclusions(signals, markets, expected)

    for row in signals.iter_rows(named=True):
        message = str(row.get("Message") or "")
        signal = parse_signal_text(
            message,
            signal_ts_ms=int(row["signal_ts_ms"]),
            received_ts_ms=int(row["signal_ts_ms"]),
            channel=str(row.get("channel") or "unknown"),
        )
        # Trust the validated research fields over a re-parse.
        signal.token_address = str(row["token_address"])
        signal.network = str(row.get("network") or "solana")
        signal.address_status = str(row.get("address_status") or "confirmed")
        signal.is_buy_signal = bool(row.get("is_buy_signal", True))
        signal.is_confirmed_buy_signal = bool(row.get("is_confirmed_buy_signal", False))

        market = markets.get(signal.token_address)
        if market is None:
            result = ReplayResult(
                signal.token_address, signal.signal_ts_ms, None, "no_replay_data"
            )
        else:
            result = await run_one(
                signal=signal,
                market=market,
                scenario=scenario_config,
                gate=gate,
                safety=safety_config,
                fee=DEFAULT_FEE,
                slippage=DEFAULT_SLIPPAGE,
                notional_sol=DEFAULT_NOTIONAL_SOL,
            )
        results.append(result)

        key = (signal.token_address, signal.signal_ts_ms)
        skip_reason = result.skip_reason or ""
        if not result.allowed:
            # A deliberate pre-trade filter (rule or safety screen) removed the
            # signal; there is no outcome to compare.
            continue
        if skip_reason.startswith("rule_crowded_post_signal"):
            if key in post_exclusions:
                continue
            mismatches.append(
                {
                    "mint": signal.token_address,
                    "signal_ts_ms": signal.signal_ts_ms,
                    "problems": ["unexpected_post_signal_exclusion"],
                    "expected": expected.get(
                        (signal.token_address, signal.signal_ts_ms, scenario)
                    ),
                    "actual": None,
                    "skip_reason": result.skip_reason,
                }
            )
            continue
        if key in post_exclusions:
            mismatches.append(
                {
                    "mint": signal.token_address,
                    "signal_ts_ms": signal.signal_ts_ms,
                    "problems": ["missing_post_signal_exclusion"],
                    "expected": expected.get(
                        (signal.token_address, signal.signal_ts_ms, scenario)
                    ),
                    "actual": result.closed,
                    "skip_reason": result.skip_reason,
                }
            )
            continue
        if skip_reason.startswith(("safety_rejected", "gate_rejected")):
            continue
        expected_row = expected.get(
            (signal.token_address, signal.signal_ts_ms, scenario)
        )
        problems = _compare(expected_row, result.closed)
        if problems:
            mismatches.append(
                {
                    "mint": signal.token_address,
                    "signal_ts_ms": signal.signal_ts_ms,
                    "problems": problems,
                    "expected": expected_row,
                    "actual": result.closed,
                    "skip_reason": result.skip_reason,
                }
            )
        if store is not None and result.closed is not None:
            # Reuse the store's JSONL/parquet schema.
            store.record_trade_payload(result.closed)

    if store is not None:
        store.finalize(
            extra={
                "validation_scenario": scenario,
                "validation_rule": rule,
                "mismatches": len(mismatches),
                "signals": len(results),
            }
        )

    post_signal_skips = sum(
        1
        for result in results
        if (result.skip_reason or "").startswith("rule_crowded_post_signal")
    )
    allowed_keys = {
        (result.mint, result.signal_ts_ms) for result in results if result.allowed
    }
    expected_post = len(post_exclusions & allowed_keys)
    summary = {
        "scenario": scenario,
        "rule": rule,
        "signals": len(results),
        "allowed": sum(1 for result in results if result.allowed),
        "rule_rejected": sum(1 for result in results if not result.allowed),
        "post_signal_excluded": post_signal_skips,
        "expected_post_signal_excluded": expected_post,
        "closed": sum(1 for result in results if result.closed is not None),
        "skipped": sum(1 for result in results if result.closed is None),
        "mismatches": len(mismatches),
        "mismatch_examples": mismatches[:5],
    }
    return summary


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--signals",
        type=Path,
        default=DEFAULT_SIGNALS,
    )
    parser.add_argument("--outcomes", type=Path, default=DEFAULT_OUTCOMES)
    parser.add_argument("--scenario", choices=sorted(SCENARIOS), default="hold_15m")
    parser.add_argument("--rule", default="baseline")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--safety-screen", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=None)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    summary = asyncio.run(
        run_validation(
            signals_path=args.signals,
            outcomes_path=args.outcomes,
            scenario=args.scenario,
            rule=args.rule,
            limit=args.limit,
            safety=args.safety_screen,
            output_dir=args.output_dir,
        )
    )
    print(json.dumps(summary, indent=2, default=str))
    return 0 if summary["mismatches"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
