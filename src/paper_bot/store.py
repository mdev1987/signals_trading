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
            payload = dict(meta)
            # A resumed run reuses its original directory. Keep the original
            # ``started_at`` if a caller forgot to carry it: restamping the
            # run to the restart time would make a five-day sample look young.
            if payload.get("started_at") is None:
                try:
                    existing = json.loads(
                        self.meta_path.read_text(encoding="utf-8")
                    )
                except (OSError, json.JSONDecodeError):
                    existing = None
                if isinstance(existing, dict) and existing.get("started_at"):
                    payload["started_at"] = existing["started_at"]
            self.meta_path.write_text(
                json.dumps(payload, indent=2, sort_keys=True, default=str),
                encoding="utf-8",
            )

    def closed_position_keys(self) -> set[tuple[str, int]]:
        """``(mint, entry_ts_ms)`` for every trade already on disk.

        Restart recovery uses this to avoid closing a position twice when the
        process died between appending the trade and saving the snapshot.
        """
        keys: set[tuple[str, int]] = set()
        for payload in _read_jsonl(self.trades_path):
            mint = payload.get("mint")
            entry_ts = payload.get("entry_ts_ms")
            if mint is None or entry_ts is None:
                continue
            try:
                keys.add((str(mint), int(entry_ts)))
            except (TypeError, ValueError, OverflowError):
                continue
        return keys

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
                "received_ts_ms": position.signal.received_ts_ms,
                "delivery_lag_ms": max(
                    0,
                    position.signal.received_ts_ms - position.signal.signal_ts_ms,
                ),
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
                "quote_slippage_bps": (
                    position.entry_quote.slippage_bps
                    if position.entry_quote
                    else None
                ),
                "reference_price_sol": position.reference_price_sol,
                "reference_ts_ms": position.reference_ts_ms,
                "reference_age_ms": position.reference_age_ms,
                "reference_source": position.reference_source,
                "entry_reference_deviation_pct": (
                    position.entry_reference_deviation_pct
                ),
                "signal_liquidity_usd": position.signal_liquidity_usd,
                "entry_liquidity_usd": position.entry_liquidity_usd,
                "quote_raw": (
                    json.dumps(
                        position.entry_quote.raw,
                        sort_keys=True,
                        default=str,
                    )
                    if position.entry_quote
                    else None
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
        # Raw quote bodies are nested structures with provider-specific keys;
        # serializing them keeps the parquet schema stable while the JSONL
        # still carries the evidence (as a readable JSON string).
        for key in ("entry_quote_raw", "exit_quote_raw", "exit_quote_raws"):
            value = payload.get(key)
            if isinstance(value, (dict, list)):
                payload = dict(payload)
                payload[key] = json.dumps(value, sort_keys=True, default=str)
        self._append(self.trades_path, payload)

    def record_skip(self, skip: SkipRecord) -> None:
        self._append(self.skips_path, skip.to_dict())

    def realized_pnl_by_day(self) -> dict[str, float]:
        """Sum realized PnL per UTC day from the append-only trades file.

        The engine keeps the same accumulator in memory for the optional daily
        loss cap, but a restart would otherwise reset today's breaker. Reading
        the file back is cheap (one row per closed trade) and keeps the file the
        single source of truth.
        """
        totals: dict[str, float] = {}
        if not self.trades_path.exists():
            return totals
        with self.trades_path.open("rb") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = orjson.loads(line)
                except orjson.JSONDecodeError:
                    # A torn final line after a crash is not a reason to drop
                    # the whole day's history.
                    continue
                exit_ts = payload.get("exit_ts_ms")
                pnl = payload.get("pnl_sol")
                if exit_ts is None or pnl is None:
                    continue
                try:
                    day = (
                        datetime.fromtimestamp(int(exit_ts) / 1000, UTC)
                        .date()
                        .isoformat()
                    )
                    totals[day] = totals.get(day, 0.0) + float(pnl)
                except (TypeError, ValueError, OverflowError, OSError):
                    continue
        return totals

    def finalize(self, *, extra: dict[str, Any] | None = None) -> Path | None:
        # Read the append-only file so a resumed run's parquet contains the
        # earlier process's trades as well as this one's.
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


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    return float(np.quantile(np.asarray(values, dtype=float), q))


def summarize(run_dir: Path) -> dict[str, Any]:
    trades = _read_jsonl(run_dir / "trades.jsonl")
    skips = _read_jsonl(run_dir / "skips.jsonl")
    signals = _read_jsonl(run_dir / "signals.jsonl")

    pnl = [float(row.get("pnl_sol") or 0.0) for row in trades]
    returns = [float(row.get("return_pct") or 0.0) for row in trades]
    holds = [int(row.get("hold_ms") or 0) for row in trades]
    entry_delays = [int(row.get("entry_delay_ms") or 0) for row in trades]
    delivery_lags = [
        int(row.get("delivery_lag_ms") or 0)
        for row in trades
        if row.get("received_ts_ms")
    ]
    wins = [value for value in pnl if value > 0]
    losses = [value for value in pnl if value < 0]
    # A "catastrophic" exit is one that returned at most -90 %: the rug tail
    # that drives this lane's PnL. It is deliberately separate from
    # ``write_offs`` (no_route / unsold), because a drained pool can still
    # return a tiny non-zero quote and fill instead of erroring.
    catastrophic = [value for value in returns if value <= -0.9]
    # Non-overlapping loss buckets, reported next to the frozen catastrophic
    # gate so an 89 % loss is visible instead of silently "normal".
    severe = [value for value in returns if -0.75 < value <= -0.5]
    extreme = [value for value in returns if -0.9 < value <= -0.75]
    total_loss = [value for value in returns if value <= -0.995]
    reference_ages = [
        int(row["reference_age_ms"])
        for row in trades
        if row.get("reference_age_ms") is not None
    ]
    reference_deviations = [
        abs(float(row["entry_reference_deviation_pct"]))
        for row in trades
        if row.get("entry_reference_deviation_pct") is not None
    ]
    reference_source_counts: dict[str, int] = {}
    for row in trades:
        source = str(row.get("reference_source") or "unknown")
        reference_source_counts[source] = (
            reference_source_counts.get(source, 0) + 1
        )
    write_off_rows = [
        row
        for row in trades
        if str(row.get("exit_status") or "") == "no_route"
        or float(row.get("unsold_size") or 0.0) > 0.0
    ]
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
        # ``return_pct`` is a fraction (0.1618 = +16.18 %), matching the
        # engine/broker and the notifier's fmt_pct().
        "avg_return_pct": (sum(returns) / len(returns)) if returns else None,
        "median_return_pct": _percentile(returns, 0.5),
        "p10_return_pct": _percentile(returns, 0.10),
        "p90_return_pct": _percentile(returns, 0.90),
        "catastrophic_exits": len(catastrophic),
        "catastrophic_exit_rate": (
            len(catastrophic) / len(trades) if trades else None
        ),
        "severe_loss_exits": len(severe),
        "extreme_loss_exits": len(extreme),
        "total_loss_exits": len(total_loss),
        "reference_age_p50_ms": _percentile(reference_ages, 0.5),
        "reference_age_p90_ms": _percentile(reference_ages, 0.9),
        "reference_age_max_ms": max(reference_ages) if reference_ages else None,
        "reference_deviation_p90_pct": _percentile(
            reference_deviations, 0.9
        ),
        "entries_without_reference_price": sum(
            1 for row in trades if row.get("reference_price_sol") is None
        ),
        "reference_source_counts": reference_source_counts,
        "write_offs": len(write_off_rows),
        "write_off_rate": (
            len(write_off_rows) / len(trades) if trades else None
        ),
        "avg_hold_ms": (sum(holds) / len(holds)) if holds else None,
        "avg_entry_delay_ms": (
            sum(entry_delays) / len(entry_delays) if entry_delays else None
        ),
        "entry_delay_p50_ms": _percentile(entry_delays, 0.5),
        "entry_delay_p90_ms": _percentile(entry_delays, 0.9),
        "delivery_lag_p50_ms": _percentile(delivery_lags, 0.5),
        "delivery_lag_p90_ms": _percentile(delivery_lags, 0.9),
        "entries_delivered_after_60s": sum(
            1 for value in delivery_lags if value > 60_000
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
