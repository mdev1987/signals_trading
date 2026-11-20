"""Append-only paper-run storage.

Every decision is written as a JSON line immediately, so a crash cannot lose
the audit trail. ``finalize`` also writes a Parquet trades table for analysis.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import orjson
import polars as pl

from .models import ClosedTrade, GateDecision, Position, Signal, SkipRecord


class PaperStore:
    def __init__(self, run_dir: Path, *, meta: dict[str, Any] | None = None) -> None:
        self.run_dir = run_dir
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.signals_path = run_dir / "signals.jsonl"
        self.entries_path = run_dir / "entries.jsonl"
        self.trades_path = run_dir / "trades.jsonl"
        self.skips_path = run_dir / "skips.jsonl"
        self.meta_path = run_dir / "meta.json"
        if meta is not None:
            self.meta_path.write_text(
                json.dumps(meta, indent=2, sort_keys=True, default=str),
                encoding="utf-8",
            )
        self._trades: list[dict[str, Any]] = []

    @staticmethod
    def _append(path: Path, payload: dict[str, Any]) -> None:
        with path.open("ab") as handle:
            handle.write(orjson.dumps(payload))
            handle.write(b"\n")

    def record_signal(self, signal: Signal, decision: GateDecision) -> None:
        self._append(
            self.signals_path,
            {
                "ts_ms": int(datetime.now(UTC).timestamp() * 1000),
                "signal": signal.to_dict(),
                "decision": decision.to_dict(),
            },
        )

    def record_entry(self, position: Position) -> None:
        self._append(
            self.entries_path,
            {
                "mint": position.mint,
                "channel": position.signal.channel,
                "message_id": position.signal.message_id,
                "signal_ts_ms": position.signal.signal_ts_ms,
                "entry_ts_ms": position.entry_ts_ms,
                "entry_trigger_ts_ms": position.entry_trigger_ts_ms,
                "entry_price_sol": position.entry_price_sol,
                "size": position.size,
                "entry_value_sol": position.entry_value_sol,
                "entry_fee_sol": position.entry_fee_sol,
                "horizon_ts_ms": position.horizon_ts_ms,
                "tp_level": position.tp_level,
                "sl_level": position.sl_level,
                "decimals": position.decimals,
                "quote_source": (
                    position.entry_quote.source if position.entry_quote else None
                ),
                "quote_price_impact_pct": (
                    position.entry_quote.price_impact_pct
                    if position.entry_quote
                    else None
                ),
                "quote_router": (
                    position.entry_quote.router if position.entry_quote else None
                ),
                "quote_request_id": (
                    position.entry_quote.request_id if position.entry_quote else None
                ),
            },
        )

    def record_trade(self, trade: ClosedTrade) -> None:
        payload = trade.to_dict()
        # Parquet cannot reliably infer a struct column with per-row keys, so
        # keep the feature bag as a JSON string.
        payload["features"] = json.dumps(
            payload.get("features") or {}, sort_keys=True, default=str
        )
        self.record_trade_payload(payload)

    def record_trade_payload(self, payload: dict[str, Any]) -> None:
        """Append an already-serialized trade (used by the replay harness)."""
        if isinstance(payload.get("features"), dict):
            payload = dict(payload)
            payload["features"] = json.dumps(
                payload["features"], sort_keys=True, default=str
            )
        self._trades.append(payload)
        self._append(self.trades_path, payload)

    def record_skip(self, skip: SkipRecord) -> None:
        self._append(self.skips_path, skip.to_dict())

    def finalize(self, *, extra: dict[str, Any] | None = None) -> Path | None:
        trades = self._trades
        if not trades and self.trades_path.exists():
            trades = _read_jsonl(self.trades_path)
        parquet_path = None
        if trades:
            frame = pl.DataFrame(trades, infer_schema_length=None)
            parquet_path = self.run_dir / "trades.parquet"
            frame.write_parquet(parquet_path, compression="zstd")
        summary = summarize(self.run_dir)
        if extra:
            summary.update(extra)
        (self.run_dir / "summary.json").write_text(
            json.dumps(summary, indent=2, sort_keys=True, default=str),
            encoding="utf-8",
        )
        return parquet_path


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for line in path.read_bytes().splitlines():
        if not line.strip():
            continue
        try:
            value = orjson.loads(line)
        except orjson.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def max_drawdown_sol(pnls: list[float]) -> float:
    """Maximum peak-to-trough drawdown of cumulative PnL, in SOL.

    Vendored from ``backtest_metrics.max_drawdown_sol``. The equity curve starts
    at zero before the first trade, so a first losing position contributes to
    drawdown. The result is ``<= 0``.
    """
    values = np.nan_to_num(
        np.asarray(pnls, dtype=float), nan=0.0, posinf=0.0, neginf=0.0
    )
    if values.size == 0:
        return 0.0
    equity = np.concatenate([[0.0], np.cumsum(values)])
    running_peak = np.maximum.accumulate(equity)
    return float((equity - running_peak).min())


def summarize(run_dir: Path) -> dict[str, Any]:
    trades = _read_jsonl(run_dir / "trades.jsonl")
    skips = _read_jsonl(run_dir / "skips.jsonl")
    signals = _read_jsonl(run_dir / "signals.jsonl")

    pnl = [float(row.get("pnl_sol") or 0.0) for row in trades]
    returns = [float(row.get("return_pct") or 0.0) for row in trades]
    holds = [int(row.get("hold_ms") or 0) for row in trades]
    entry_delays = [int(row.get("entry_delay_ms") or 0) for row in trades]
    wins = [value for value in pnl if value > 0]
    losses = [value for value in pnl if value < 0]
    status_counts: dict[str, int] = {}
    source_counts: dict[str, int] = {}
    skip_counts: dict[str, int] = {}
    for row in trades:
        status = str(row.get("exit_status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
        source = str(row.get("exit_quote_source") or "unknown")
        source_counts[source] = source_counts.get(source, 0) + 1
    for row in skips:
        reason = str(row.get("reason") or "unknown")
        skip_counts[reason] = skip_counts.get(reason, 0) + 1

    signals_allowed = 0
    for row in signals:
        decision = row.get("decision")
        if isinstance(decision, dict) and decision.get("allowed"):
            signals_allowed += 1

    gross_profit = sum(wins)
    gross_loss = -sum(losses)
    synthetic = sum(1 for row in trades if row.get("exit_synthetic"))
    unsold = sum(1 for row in trades if float(row.get("unsold_size") or 0) > 0)
    return {
        "signals_received": len(signals),
        "signals_allowed": signals_allowed,
        "positions_closed": len(trades),
        "skips": len(skips),
        "wins": len(wins),
        "losses": len(losses),
        "breakeven": len(trades) - len(wins) - len(losses),
        "win_rate": (len(wins) / len(trades)) if trades else None,
        "pnl_sol": sum(pnl) if trades else 0.0,
        "avg_pnl_sol": (sum(pnl) / len(pnl)) if pnl else None,
        "expectancy_sol": (sum(pnl) / len(pnl)) if pnl else None,
        "best_pnl_sol": max(pnl) if pnl else None,
        "worst_pnl_sol": min(pnl) if pnl else None,
        "avg_return_pct": (sum(returns) / len(returns)) if returns else None,
        "avg_hold_ms": (sum(holds) / len(holds)) if holds else None,
        "avg_entry_delay_ms": (
            sum(entry_delays) / len(entry_delays) if entry_delays else None
        ),
        "profit_factor": (gross_profit / gross_loss) if gross_loss > 0 else None,
        "max_drawdown_sol": max_drawdown_sol(pnl) if pnl else 0.0,
        "synthetic_trades": synthetic,
        "unsold_trades": unsold,
        "exit_status_counts": status_counts,
        "exit_quote_source_counts": source_counts,
        "skip_reason_counts": skip_counts,
        "generated_at": datetime.now(UTC).isoformat(),
    }
