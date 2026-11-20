#!/usr/bin/env python3
"""Event-driven backtest of the Ave "2nd Vibe Buy" signal on replayed pools.

The strategy is intentionally plain: every routable signal buys a fixed SOL
stake at ``signal timestamp + latency`` in the deepest fresh WSOL-quoted pool
that the replay knows about, and exits the whole position ``hold_minutes``
later by re-resolving the pool (which is what makes migrated tokens exit in
their new pool). No vectorisation, no force-closing: a position whose exit is
still in the future when the archive ends is marked to market and reported as
open.

Execution and mark-to-market both go through ``execution_simulator``, so fills
carry the same fee, reserve and price-impact treatment as the stand-alone
simulator stage.

Examples:
    python tools/backtest/backtest_strategy.py --day 2026-10-03
    python tools/backtest/backtest_strategy.py --all-days \\
        --days 2026-10-03 2026-10-04 2026-10-05
"""

from __future__ import annotations

import argparse
import csv
import heapq
import json
import statistics
import sys
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = Path(__file__).resolve().parent
for entry in (str(REPO_ROOT), str(SCRIPT_DIR)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from execution_simulator import ExecutionSimulator, FillResult  # noqa: E402

DEFAULT_DATASET_ROOT = Path("output/backtest/days")
DEFAULT_SIGNALS_ROOT = Path("output/backtest/signals")
DEFAULT_RUNS_ROOT = Path("output/backtest/runs")

FILL_FIELDS = [
    "signal_id",
    "phase",
    *FillResult.__dataclass_fields__.keys(),
]

TRADE_FIELDS = [
    "status",
    "signal_id",
    "mint",
    "entry_timestamp_ms",
    "entry_timestamp_utc",
    "exit_timestamp_ms",
    "exit_timestamp_utc",
    "hold_seconds",
    "entry_pool_id",
    "entry_pool",
    "exit_pool_id",
    "exit_pool",
    "tokens",
    "stake_sol",
    "entry_price_sol",
    "entry_spot_price_sol",
    "entry_quote_fee_sol",
    "entry_price_impact_bps",
    "exit_quote_gross_sol",
    "exit_quote_fee_sol",
    "exit_quote_net_sol",
    "exit_price_sol",
    "exit_spot_price_sol",
    "exit_price_impact_bps",
    "realized_pnl_sol",
    "return_pct",
    "mark_timestamp_ms",
    "mark_price_sol",
    "mark_value_sol",
    "unrealized_pnl_sol",
    "note",
]

SKIP_FIELDS = [
    "signal_id",
    "signal_timestamp_ms",
    "signal_timestamp_utc",
    "entry_timestamp_ms",
    "mint",
    "reason",
    "detail",
]

EQUITY_FIELDS = [
    "timestamp_ms",
    "timestamp_utc",
    "cash_sol",
    "open_positions",
    "open_value_sol",
    "equity_sol",
]


def _utc(timestamp_ms: int | None) -> str:
    if timestamp_ms is None:
        return ""
    return datetime.fromtimestamp(
        timestamp_ms / 1000.0, tz=timezone.utc
    ).isoformat(timespec="seconds")


def _write_csv(path: Path, rows: Iterable[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    key: "" if row.get(key) is None else row.get(key)
                    for key in fieldnames
                }
            )


def load_signals(path: Path) -> list[dict[str, Any]]:
    required = {"signal_id", "signal_timestamp_ms", "mint"}
    with path.open("r", newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source)
        if reader.fieldnames is None:
            raise ValueError(f"Signals CSV is empty: {path}")
        missing = sorted(required - set(reader.fieldnames))
        if missing:
            raise ValueError(f"Signals CSV is missing columns: {missing}")
        signals = []
        for index, row in enumerate(reader, start=2):
            try:
                timestamp_ms = int(float(row["signal_timestamp_ms"]))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Invalid signal_timestamp_ms on row {index}") from exc
            mint = str(row.get("mint") or "").strip()
            if not mint:
                raise ValueError(f"Empty mint on rows {index}")
            signals.append(
                {
                    "signal_id": str(row.get("signal_id") or index),
                    "signal_timestamp_ms": timestamp_ms,
                    "signal_timestamp_utc": str(
                        row.get("signal_timestamp_utc") or _utc(timestamp_ms)
                    ),
                    "mint": mint,
                }
            )
    signals.sort(key=lambda item: (item["signal_timestamp_ms"], item["signal_id"]))
    return signals


def load_routing(path: Path) -> tuple[dict[str, str], set[str]]:
    """Return non-routable reasons keyed by signal id and by mint."""
    reasons: dict[str, str] = {}
    mints: set[str] = set()
    if not path.exists():
        return reasons, mints
    with path.open("r", newline="", encoding="utf-8-sig") as source:
        for row in csv.DictReader(source):
            routable = str(row.get("dataset_routable", "1")).strip().lower()
            if routable not in {"0", "0.0", "false", "no"}:
                continue
            mint = str(row.get("mint") or "").strip()
            signal_id = str(row.get("signal_id") or "").strip()
            reason = str(row.get("reason") or "not_routable").strip()
            if signal_id:
                reasons[signal_id] = reason
            if mint:
                mints.add(mint)
    return reasons, mints


@dataclass(slots=True)
class Position:
    slot: int
    signal_id: str
    mint: str
    entry_timestamp_ms: int
    exit_due_ms: int
    stake_sol: float
    tokens: float
    entry_fill: FillResult
    last_mark_price_sol: float
    last_mark_timestamp_ms: int
    status: str = "open"
    exit_failure: str | None = None


@dataclass
class RunState:
    cash_sol: float
    open_positions: dict[int, Position] = field(default_factory=dict)
    exit_heap: list[tuple[int, int]] = field(default_factory=list)
    fills: list[dict[str, Any]] = field(default_factory=list)
    trades: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    equity: list[dict[str, Any]] = field(default_factory=list)
    entry_pools: Counter[str] = field(default_factory=Counter)
    exit_pools: Counter[str] = field(default_factory=Counter)
    next_slot: int = 0


def _skip(
    state: RunState,
    signal: dict[str, Any],
    entry_timestamp_ms: int,
    reason: str,
    detail: str = "",
) -> None:
    state.skipped.append(
        {
            "signal_id": signal["signal_id"],
            "signal_timestamp_ms": signal["signal_timestamp_ms"],
            "signal_timestamp_utc": signal["signal_timestamp_utc"],
            "entry_timestamp_ms": entry_timestamp_ms,
            "mint": signal["mint"],
            "reason": reason,
            "detail": detail,
        }
    )


def _fill_row(signal_id: str, phase: str, fill: FillResult) -> dict[str, Any]:
    row = asdict(fill)
    row["signal_id"] = signal_id
    row["phase"] = phase
    return row


def _mark_open_positions(
    simulator: ExecutionSimulator, state: RunState, timestamp_ms: int
) -> float:
    """Value open positions at ``timestamp_ms``.

    A position that is still inside its holding period is marked at the current
    state. One whose exit already failed or fell past the archive keeps the mark
    frozen at that moment, so later (possibly drained) pool states cannot
    inflate the book.
    """
    open_value = 0.0
    for position in state.open_positions.values():
        if position.status == "open":
            latest = simulator.latest_spot_price_sol(position.mint)
            if latest is not None:
                position.last_mark_price_sol = latest[0]
                position.last_mark_timestamp_ms = latest[1]
        open_value += position.tokens * position.last_mark_price_sol
    return open_value


def _record_equity(
    simulator: ExecutionSimulator, state: RunState, timestamp_ms: int
) -> None:
    open_value = _mark_open_positions(simulator, state, timestamp_ms)
    state.equity.append(
        {
            "timestamp_ms": int(timestamp_ms),
            "timestamp_utc": _utc(int(timestamp_ms)),
            "cash_sol": state.cash_sol,
            "open_positions": len(state.open_positions),
            "open_value_sol": open_value,
            "equity_sol": state.cash_sol + open_value,
        }
    )


def _closed_trade_row(
    position: Position, exit_fill: FillResult
) -> dict[str, Any]:
    tokens = position.tokens
    proceeds = exit_fill.quote_net or 0.0
    pnl = proceeds - position.stake_sol
    entry_fill = position.entry_fill
    return {
        "status": "closed",
        "signal_id": position.signal_id,
        "mint": position.mint,
        "entry_timestamp_ms": position.entry_timestamp_ms,
        "entry_timestamp_utc": _utc(position.entry_timestamp_ms),
        "exit_timestamp_ms": exit_fill.timestamp_ms,
        "exit_timestamp_utc": _utc(exit_fill.timestamp_ms),
        "hold_seconds": round(
            (exit_fill.timestamp_ms - position.entry_timestamp_ms) / 1000.0, 3
        ),
        "entry_pool_id": position.entry_fill.pool_id,
        "entry_pool": entry_fill.pool,
        "exit_pool_id": exit_fill.pool_id,
        "exit_pool": exit_fill.pool,
        "tokens": tokens,
        "stake_sol": position.stake_sol,
        "entry_price_sol": position.stake_sol / tokens if tokens else None,
        "entry_spot_price_sol": entry_fill.reference_spot_price,
        "entry_quote_fee_sol": entry_fill.quote_fee,
        "entry_price_impact_bps": entry_fill.price_impact_bps,
        "exit_quote_gross_sol": exit_fill.quote_gross,
        "exit_quote_fee_sol": exit_fill.quote_fee,
        "exit_quote_net_sol": exit_fill.quote_net,
        "exit_price_sol": exit_fill.average_price_quote_per_token,
        "exit_spot_price_sol": exit_fill.reference_spot_price,
        "exit_price_impact_bps": exit_fill.price_impact_bps,
        "realized_pnl_sol": pnl,
        "return_pct": (pnl / position.stake_sol * 100.0)
        if position.stake_sol
        else None,
        "note": "",
    }


def _open_trade_row(
    position: Position, mark_price_sol: float, mark_timestamp_ms: int
) -> dict[str, Any]:
    mark_value = position.tokens * mark_price_sol
    unrealized = mark_value - position.stake_sol
    return {
        "status": position.status,
        "signal_id": position.signal_id,
        "mint": position.mint,
        "entry_timestamp_ms": position.entry_timestamp_ms,
        "entry_timestamp_utc": _utc(position.entry_timestamp_ms),
        "entry_pool_id": position.entry_fill.pool_id,
        "entry_pool": position.entry_fill.pool,
        "tokens": position.tokens,
        "stake_sol": position.stake_sol,
        "entry_price_sol": position.stake_sol / position.tokens
        if position.tokens
        else None,
        "entry_spot_price_sol": position.entry_fill.reference_spot_price,
        "entry_quote_fee_sol": position.entry_fill.quote_fee,
        "entry_price_impact_bps": position.entry_fill.price_impact_bps,
        "mark_timestamp_ms": mark_timestamp_ms,
        "mark_price_sol": mark_price_sol,
        "mark_value_sol": mark_value,
        "unrealized_pnl_sol": unrealized,
        "return_pct": (unrealized / position.stake_sol * 100.0)
        if position.stake_sol
        else None,
        "note": position.exit_failure or "",
    }


def _execute_entry(
    simulator: ExecutionSimulator,
    state: RunState,
    signal: dict[str, Any],
    entry_timestamp_ms: int,
    config: dict[str, Any],
) -> None:
    if len(state.open_positions) >= config["max_concurrent"]:
        _skip(state, signal, entry_timestamp_ms, "max_concurrent_positions")
        return
    if state.cash_sol + 1e-12 < config["stake_sol"]:
        _skip(state, signal, entry_timestamp_ms, "insufficient_cash")
        return

    snapshot = simulator.resolve_pool(signal["mint"], entry_timestamp_ms)
    if snapshot is None:
        _skip(state, signal, entry_timestamp_ms, "no_fresh_pool")
        return

    fill = simulator.execute(
        order_id=f"{signal['signal_id']}:entry",
        timestamp_ms=entry_timestamp_ms,
        mint=signal["mint"],
        pool_id=snapshot.pool_id,
        side="buy",
        amount=config["stake_sol"],
    )
    state.fills.append(_fill_row(signal["signal_id"], "entry", fill))
    if fill.status != "filled":
        _skip(
            state,
            signal,
            entry_timestamp_ms,
            f"entry_{fill.status}",
            fill.reason or "",
        )
        return

    state.cash_sol -= config["stake_sol"]
    position = Position(
        slot=state.next_slot,
        signal_id=signal["signal_id"],
        mint=signal["mint"],
        entry_timestamp_ms=entry_timestamp_ms,
        exit_due_ms=entry_timestamp_ms + config["hold_ms"],
        stake_sol=config["stake_sol"],
        tokens=fill.token_amount or 0.0,
        entry_fill=fill,
        last_mark_price_sol=fill.reference_spot_price or 0.0,
        last_mark_timestamp_ms=entry_timestamp_ms,
    )
    state.next_slot += 1
    state.open_positions[position.slot] = position
    heapq.heappush(state.exit_heap, (position.exit_due_ms, position.slot))
    state.entry_pools[fill.pool or "unknown"] += 1


def _execute_exit(
    simulator: ExecutionSimulator, state: RunState, position: Position
) -> None:
    snapshot = simulator.resolve_pool(position.mint, position.exit_due_ms)
    if (
        snapshot is None
        and simulator.exhausted
        and simulator.last_event_timestamp_ms < position.exit_due_ms
    ):
        # The holding period ends after the archive does. There is no market to
        # sell into yet, so keep the position open and let end-of-run marking
        # value it. This is the "open at replay end" case, not a failed exit.
        mark = simulator.mark_price_sol(position.mint, position.exit_due_ms)
        if mark is not None:
            position.last_mark_price_sol = mark[0]
            position.last_mark_timestamp_ms = mark[1]
        position.status = "open_at_end"
        return
    pool_id = snapshot.pool_id if snapshot is not None else position.entry_fill.pool_id

    fill = simulator.execute(
        order_id=f"{position.signal_id}:exit",
        timestamp_ms=position.exit_due_ms,
        mint=position.mint,
        pool_id=pool_id,
        side="sell",
        amount=position.tokens,
    )
    if fill.status != "filled" and pool_id != position.entry_fill.pool_id:
        # Re-resolving can land on a pool that cannot absorb the position;
        # fall back to the entry pool before giving up on the exit.
        fill = simulator.execute(
            order_id=f"{position.signal_id}:exit-fallback",
            timestamp_ms=position.exit_due_ms,
            mint=position.mint,
            pool_id=position.entry_fill.pool_id,
            side="sell",
            amount=position.tokens,
        )

    state.fills.append(_fill_row(position.signal_id, "exit", fill))
    if fill.status != "filled":
        # It stays in open_positions (it was only popped from the schedule) and
        # will be marked to market at the end of the replay. Freeze the mark at
        # the attempted exit so later states cannot move it.
        mark = simulator.mark_price_sol(position.mint, position.exit_due_ms)
        if mark is not None:
            position.last_mark_price_sol = mark[0]
            position.last_mark_timestamp_ms = mark[1]
        position.status = "open_exit_failed"
        position.exit_failure = fill.reason or fill.status
        return

    state.cash_sol += fill.quote_net or 0.0
    state.trades.append(_closed_trade_row(position, fill))
    state.exit_pools[fill.pool or "unknown"] += 1
    del state.open_positions[position.slot]


def run_backtest(
    events_path: Path,
    signals_path: Path,
    out_dir: Path,
    *,
    routing_path: Path | None = None,
    cash_sol: float = 10.0,
    stake_sol: float = 0.1,
    max_concurrent: int = 10,
    latency_ms: int = 5_000,
    hold_ms: int = 30 * 60 * 1000,
    max_state_age_ms: int | None = 60_000,
    max_price_impact_bps: float | None = None,
) -> dict[str, Any]:
    if cash_sol <= 0:
        raise ValueError("cash_sol must be positive")
    if stake_sol <= 0:
        raise ValueError("stake_sol must be positive")
    if max_concurrent < 1:
        raise ValueError("max_concurrent must be at least 1")
    if latency_ms < 0:
        raise ValueError("latency_ms must be non-negative")
    if hold_ms <= 0:
        raise ValueError("hold_ms must be positive")

    config = {
        "cash_sol": cash_sol,
        "stake_sol": stake_sol,
        "max_concurrent": max_concurrent,
        "latency_ms": latency_ms,
        "hold_ms": hold_ms,
        "hold_minutes": hold_ms / 60_000.0,
        "max_state_age_ms": max_state_age_ms,
        "max_price_impact_bps": max_price_impact_bps,
    }

    signals = load_signals(signals_path)
    routable_reasons, blocked_mints = (
        load_routing(routing_path) if routing_path is not None else ({}, set())
    )

    simulator = ExecutionSimulator(
        events_path,
        max_state_age_ms=max_state_age_ms,
        max_price_impact_bps=max_price_impact_bps,
    )
    state = RunState(cash_sol=cash_sol)

    entries = []
    for signal in signals:
        entry_timestamp_ms = signal["signal_timestamp_ms"] + latency_ms
        reason = routable_reasons.get(signal["signal_id"])
        if reason is None and signal["signal_id"] not in routable_reasons:
            if signal["mint"] in blocked_mints:
                reason = "not_routable"
        if reason is not None:
            _skip(state, signal, entry_timestamp_ms, "dataset_not_routable", reason)
            continue
        entries.append((entry_timestamp_ms, signal))

    entries.sort(key=lambda item: (item[0], item[1]["signal_id"]))
    entry_index = 0
    first_timestamp = entries[0][0] if entries else None
    if first_timestamp is not None:
        _record_equity(simulator, state, first_timestamp)

    INF = 2**62
    while entry_index < len(entries) or state.exit_heap:
        next_entry_ms = entries[entry_index][0] if entry_index < len(entries) else INF
        next_exit_ms = state.exit_heap[0][0] if state.exit_heap else INF
        if next_exit_ms <= next_entry_ms:
            _, slot = heapq.heappop(state.exit_heap)
            position = state.open_positions.get(slot)
            if position is None:
                continue
            _execute_exit(simulator, state, position)
            _record_equity(simulator, state, next_exit_ms)
        else:
            entry_timestamp_ms, signal = entries[entry_index]
            entry_index += 1
            _execute_entry(simulator, state, signal, entry_timestamp_ms, config)
            _record_equity(simulator, state, entry_timestamp_ms)

    simulator.consume_all()
    end_timestamp_ms = max(
        simulator.last_event_timestamp_ms,
        max((row["timestamp_ms"] for row in state.equity), default=0),
    )
    for position in state.open_positions.values():
        if position.status == "open":
            mark = simulator.mark_price_sol(position.mint, end_timestamp_ms)
            if mark is not None:
                position.last_mark_price_sol = mark[0]
                position.last_mark_timestamp_ms = mark[1]
            position.status = "open_at_end"

    open_rows = []
    open_value = 0.0
    for position in state.open_positions.values():
        open_value += position.tokens * position.last_mark_price_sol
        open_rows.append(
            _open_trade_row(
                position,
                position.last_mark_price_sol,
                position.last_mark_timestamp_ms,
            )
        )

    if end_timestamp_ms > 0:
        _record_equity(simulator, state, end_timestamp_ms)

    equity_end = state.cash_sol + open_value
    closed = [row for row in state.trades if row["status"] == "closed"]
    wins = [row for row in closed if (row["realized_pnl_sol"] or 0.0) > 0]
    losses = [row for row in closed if (row["realized_pnl_sol"] or 0.0) <= 0]
    hold_seconds = [row["hold_seconds"] for row in closed]

    equities = [row["equity_sol"] for row in state.equity]
    peak = None
    max_drawdown_pct = 0.0
    for value in equities:
        peak = value if peak is None else max(peak, value)
        if peak and peak > 0:
            max_drawdown_pct = max(max_drawdown_pct, (peak - value) / peak * 100.0)

    gross_fees = sum(
        row["quote_fee"] or 0.0
        for row in state.fills
        if row.get("quote_fee") is not None
    )
    status_counts = Counter(row["status"] for row in state.fills)
    skip_counts = Counter(row["reason"] for row in state.skipped)

    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "trades": out_dir / "trades.csv",
        "skipped": out_dir / "skipped.csv",
        "equity_curve": out_dir / "equity_curve.csv",
        "fills": out_dir / "fills.csv",
    }
    _write_csv(paths["trades"], [*state.trades, *open_rows], TRADE_FIELDS)
    _write_csv(paths["skipped"], state.skipped, SKIP_FIELDS)
    _write_csv(paths["equity_curve"], state.equity, EQUITY_FIELDS)
    _write_csv(paths["fills"], state.fills, FILL_FIELDS)

    report = {
        "stage": "strategy_backtest",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        ),
        "config": config,
        "inputs": {
            "events": str(events_path),
            "signals": str(signals_path),
            "routing": str(routing_path) if routing_path is not None else None,
            "output_dir": str(out_dir),
        },
        "signals": {
            "total": len(signals),
            "routable": len(entries),
            "skipped": len(state.skipped),
        },
        "orders": {
            "entries_attempted": len(entries),
            "entries_filled": state.entry_pools.total(),
            "exits_filled": len(closed),
            "exit_failures": sum(
                1
                for row in state.open_positions.values()
                if row.status == "open_exit_failed"
            ),
        },
        "fills_by_status": dict(sorted(status_counts.items())),
        "skipped_by_reason": dict(sorted(skip_counts.items())),
        "positions": {
            "closed": len(closed),
            "open_at_end": len(open_rows),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": len(wins) / len(closed) if closed else None,
            "avg_hold_seconds": statistics.fmean(hold_seconds)
            if hold_seconds
            else None,
            "median_hold_seconds": statistics.median(hold_seconds)
            if hold_seconds
            else None,
        },
        "pnl": {
            "cash_start_sol": cash_sol,
            "cash_end_sol": state.cash_sol,
            "realized_pnl_sol": state.cash_sol - cash_sol,
            "open_value_sol": open_value,
            "unrealized_pnl_sol": sum(
                (row["unrealized_pnl_sol"] or 0.0) for row in open_rows
            ),
            "equity_end_sol": equity_end,
            "total_return_pct": (equity_end / cash_sol - 1.0) * 100.0,
            "max_drawdown_pct": max_drawdown_pct,
            "gross_fees_sol": gross_fees,
        },
        "pools": {
            "entries_by_pool": dict(sorted(state.entry_pools.items())),
            "exits_by_pool": dict(sorted(state.exit_pools.items())),
        },
        "replay": {
            "last_event_timestamp_ms": simulator.last_event_timestamp_ms,
            "last_event_utc": _utc(simulator.last_event_timestamp_ms),
            "simulator_stats": simulator.stats,
        },
        "open_positions": open_rows,
        "outputs": {key: str(path) for key, path in paths.items()},
    }
    report_path = out_dir / "run_report.json"
    report_path.write_text(
        json.dumps(report, indent=2, sort_keys=False) + "\n", encoding="utf-8"
    )
    report["outputs"]["run_report"] = str(report_path)
    return report


def _day_paths(
    day: str,
    dataset_root: Path,
    signals_root: Path,
    runs_root: Path,
) -> dict[str, Path]:
    day_dir = dataset_root / day
    signals_path = signals_root / day / "strategy_signals.csv"
    report_path = day_dir / "dataset_report.json"
    if report_path.exists():
        dataset_report = json.loads(report_path.read_text())
        recorded = (dataset_report.get("inputs") or {}).get("signals")
        if recorded:
            signals_path = Path(recorded)
    return {
        "events": day_dir / "replay_filtered" / "target_mints_events.jsonl.zst",
        "signals": signals_path,
        "routing": day_dir / "signal_routing.csv",
        "out_dir": runs_root / day,
        "dataset_report": report_path,
    }


def run_days(
    days: list[str],
    *,
    dataset_root: Path = DEFAULT_DATASET_ROOT,
    signals_root: Path = DEFAULT_SIGNALS_ROOT,
    runs_root: Path = DEFAULT_RUNS_ROOT,
    **kwargs: Any,
) -> dict[str, Any]:
    runs = []
    for day in days:
        paths = _day_paths(day, dataset_root, signals_root, runs_root)
        if paths["dataset_report"].exists():
            report = json.loads(paths["dataset_report"].read_text())
            if not report.get("passed"):
                runs.append(
                    {
                        "day": day,
                        "status": "dataset_failed",
                        "detail": "dataset_report.json has passed=false",
                    }
                )
                continue
        if not paths["events"].exists() or not paths["signals"].exists():
            runs.append(
                {
                    "day": day,
                    "status": "missing_input",
                    "detail": (
                        f"events={paths['events'].exists()} "
                        f"signals={paths['signals'].exists()}"
                    ),
                }
            )
            continue
        result = run_backtest(
            paths["events"],
            paths["signals"],
            paths["out_dir"],
            routing_path=paths["routing"],
            **kwargs,
        )
        runs.append(
            {
                "day": day,
                "status": "ok",
                "report": result["outputs"]["run_report"],
                "signals": result["signals"],
                "positions": result["positions"],
                "pnl": result["pnl"],
                "skipped_by_reason": result["skipped_by_reason"],
                "pools": result["pools"],
            }
        )

    ok = [run for run in runs if run["status"] == "ok"]
    summary = {
        "stage": "strategy_backtest_multi_day",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        ),
        "days": days,
        "config": kwargs,
        "runs": runs,
        "aggregate": {
            "days_run": len(ok),
            "days_failed": len(runs) - len(ok),
            "signals_total": sum(run["signals"]["total"] for run in ok),
            "entries_filled": sum(
                run["positions"]["closed"] + run["positions"]["open_at_end"]
                for run in ok
            ),
            "closed_trades": sum(run["positions"]["closed"] for run in ok),
            "open_at_end": sum(run["positions"]["open_at_end"] for run in ok),
            "realized_pnl_sol": sum(run["pnl"]["realized_pnl_sol"] for run in ok),
            "unrealized_pnl_sol": sum(
                run["pnl"]["unrealized_pnl_sol"] for run in ok
            ),
            "equity_start_sol": sum(run["pnl"]["cash_start_sol"] for run in ok),
            "equity_end_sol": sum(run["pnl"]["equity_end_sol"] for run in ok),
            "mean_return_per_day_pct": (
                statistics.fmean(run["pnl"]["total_return_pct"] for run in ok)
                if ok
                else None
            ),
            "worst_day_return_pct": min(
                (run["pnl"]["total_return_pct"] for run in ok), default=None
            ),
            "best_day_return_pct": max(
                (run["pnl"]["total_return_pct"] for run in ok), default=None
            ),
            "smallest_cash_end_sol": min(
                (run["pnl"]["cash_end_sol"] for run in ok), default=None
            ),
        },
    }
    runs_root.mkdir(parents=True, exist_ok=True)
    summary_path = runs_root / "summary.json"
    summary["outputs"] = {"summary": str(summary_path)}
    summary_path.write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Backtest the 2nd Vibe Buy signal with the snapshot execution "
            "simulator."
        )
    )
    parser.add_argument("--day", help="Single day label, e.g. 2026-10-03")
    parser.add_argument(
        "--all-days",
        action="store_true",
        help="Run every day in --days and write a multi-day summary.json",
    )
    parser.add_argument(
        "--days",
        nargs="*",
        default=None,
        help="Day labels for --all-days (default: every directory in --dataset-root)",
    )
    parser.add_argument(
        "--dataset-root", type=Path, default=DEFAULT_DATASET_ROOT
    )
    parser.add_argument(
        "--signals-root", type=Path, default=DEFAULT_SIGNALS_ROOT
    )
    parser.add_argument("--runs-root", type=Path, default=DEFAULT_RUNS_ROOT)
    parser.add_argument("--events", type=Path, help="Override filtered event path")
    parser.add_argument("--signals", type=Path, help="Override signal CSV path")
    parser.add_argument("--routing", type=Path, help="Override signal_routing.csv")
    parser.add_argument("--out-dir", type=Path, help="Override run output dir")
    parser.add_argument("--cash-sol", type=float, default=10.0)
    parser.add_argument("--stake-sol", type=float, default=0.1)
    parser.add_argument("--max-concurrent", type=int, default=10)
    parser.add_argument("--latency-ms", type=int, default=5_000)
    parser.add_argument("--hold-minutes", type=float, default=30.0)
    parser.add_argument(
        "--max-state-age-ms",
        type=int,
        default=60_000,
        help="Reject pools whose latest snapshot is older than this (0 disables)",
    )
    parser.add_argument("--max-price-impact-bps", type=float, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    max_state_age_ms = None if args.max_state_age_ms == 0 else args.max_state_age_ms
    if max_state_age_ms is not None and max_state_age_ms < 0:
        raise SystemExit("--max-state-age-ms must be >= 0")
    hold_ms = int(round(args.hold_minutes * 60_000))
    common = {
        "cash_sol": args.cash_sol,
        "stake_sol": args.stake_sol,
        "max_concurrent": args.max_concurrent,
        "latency_ms": args.latency_ms,
        "hold_ms": hold_ms,
        "max_state_age_ms": max_state_age_ms,
        "max_price_impact_bps": args.max_price_impact_bps,
    }

    if args.all_days:
        days = args.days
        if not days:
            days = sorted(
                path.name
                for path in args.dataset_root.iterdir()
                if path.is_dir()
            )
        summary = run_days(
            days,
            dataset_root=args.dataset_root,
            signals_root=args.signals_root,
            runs_root=args.runs_root,
            **common,
        )
        print(json.dumps(summary["aggregate"], indent=2))
        print(f"Summary written: {summary['outputs']['summary']}")
        return 0

    if args.day:
        paths = _day_paths(
            args.day, args.dataset_root, args.signals_root, args.runs_root
        )
        events_path = args.events or paths["events"]
        signals_path = args.signals or paths["signals"]
        routing_path = args.routing or paths["routing"]
        out_dir = args.out_dir or paths["out_dir"]
    else:
        if args.events is None or args.signals is None:
            raise SystemExit(
                "Provide --day, or both --events and --signals"
            )
        events_path = args.events
        signals_path = args.signals
        routing_path = args.routing
        out_dir = args.out_dir or DEFAULT_RUNS_ROOT / "adhoc"

    report = run_backtest(
        events_path,
        signals_path,
        out_dir,
        routing_path=routing_path if routing_path and routing_path.exists() else None,
        **common,
    )
    print(json.dumps(report["pnl"], indent=2))
    print(f"Run report written: {report['outputs']['run_report']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
