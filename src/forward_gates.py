#!/usr/bin/env python3
"""Evaluate the predeclared forward gates on a paper-run directory.

The gates and their thresholds are frozen in ``docs/SCANNER_EVIDENCE.md``.
This script only measures them; it never changes the strategy, the thresholds
or the run. Evaluating every run with the same code is the point: a gate that
lives only in prose can be reinterpreted after seeing the result.

Usage::

    python src/forward_gates.py --run-dir output/paper/live_20261122_054357
    python src/forward_gates.py --run-dir ... --json

Exit codes: ``0`` all gates pass, ``1`` a gate failed, ``2`` coverage is not
met yet (the other numbers are still printed for context).
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

DEFAULT_SEED = 20_261_121
DEFAULT_RESAMPLES = 10_000
MIN_TRADES = 300
MIN_DAYS = 5
MIN_MEDIAN_RETURN = 0.10
MAX_CATASTROPHIC_RATE = 0.16
MAX_WRITE_OFF_RATE = 0.16
MIN_ENTRY_FILL_RATE = 0.80
# Exclusive bound: the frozen criterion is "below 10 %", so exactly 10 % fails.
MAX_ENTRY_FAILURE_RATE = 0.10
BOOTSTRAP_LOWER_QUANTILE = 0.05
BOOTSTRAP_LOSS_ALLOWANCE = 0.05
SCANNER_MESSAGE_TYPE = "scanner_pool_launch"

# Reasons recorded when an *allowed* signal fails to become an entry. Every
# one of these is emitted by ``PaperEngine._skip_pending`` (or by the expiry
# classifier), so it is an entry-attempt failure, not a gate rejection. Gate
# rejections (``allowed=false``) never enter this set: they are measured as
# censoring, not as execution failures. Used only to attribute a skip row that
# carries no signal identity (legacy/synthetic data); a real row is joined to
# its ``signals.jsonl`` decision instead.
ENTRY_FAILURE_REASONS = frozenset(
    {
        "entry_delay_exceeded",
        "entry_quote_failed",
        "entry_no_exit_route",
        "entry_no_exit_economics",
        "entry_insufficient_sol",
        "persistence_degraded",
        "entry_timer_late",
        "entry_no_reference_price",
        "entry_reference_age_unknown",
        "entry_unverified_quote_notional",
        "entry_dust_prints_only",
        "no_buy_trade_after_signal",
        "reference_timestamp_in_future",
        "stale_reference_price",
        "rule_crowded_post_signal",
    }
)


def _read_jsonl(path: Path) -> tuple[list[dict[str, Any]], int]:
    """Return ``(rows, unparsable)``.

    A torn final line after a hard kill is counted instead of silently
    dropped: losing a trade row flatters the median, and losing a rejection
    row flatters the fill rate. The caller fails the input-integrity gate
    when this is non-zero.
    """
    if not path.exists():
        return [], 0
    rows: list[dict[str, Any]] = []
    corrupt = 0
    with path.open("rb") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                corrupt += 1
                continue
            if isinstance(payload, dict):
                rows.append(payload)
            else:
                corrupt += 1
    return rows, corrupt


def _read_json_object(path: Path) -> dict[str, Any] | None:
    """Read a single JSON object, or ``None`` when missing/unreadable."""
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _signal_identity(row: dict[str, Any]) -> tuple[str, ...]:
    """Identity shared by ``signals.jsonl`` and ``skips.jsonl`` rows.

    ``message_id`` is the natural key; when it is absent (older or synthetic
    rows) fall back to mint + signal timestamp. An empty tuple means the row
    carries no identity and cannot be joined.
    """
    signal = row.get("signal")
    source = signal if isinstance(signal, dict) else row
    message_id = source.get("message_id")
    channel = str(source.get("channel") or "")
    if message_id is not None:
        return ("message", channel, str(message_id))
    mint = str(source.get("token_address") or source.get("mint") or "")
    signal_ts_ms = source.get("signal_ts_ms")
    if mint and signal_ts_ms is not None:
        return ("mint", channel, mint, str(signal_ts_ms))
    return ()


def _day(ts_ms: Any) -> str | None:
    try:
        return datetime.fromtimestamp(int(ts_ms) / 1000, UTC).date().isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _timestamp(ts_ms: Any) -> str | None:
    try:
        return datetime.fromtimestamp(int(ts_ms) / 1000, UTC).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _exit_ts_bounds(trades: list[dict[str, Any]]) -> tuple[int | None, int | None]:
    """First and last ``exit_ts_ms`` in the ledger.

    The bounds pin a reported row to the trades it was computed from: two
    snapshots of the same running lane differ in PnL and in their ratios, so a
    row without its last close cannot be reproduced.
    """
    values: list[int] = []
    for row in trades:
        try:
            values.append(int(row["exit_ts_ms"]))
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    return (min(values), max(values)) if values else (None, None)


def cluster_bootstrap(
    pnls: list[float],
    mints: list[str],
    *,
    resamples: int = DEFAULT_RESAMPLES,
    seed: int = DEFAULT_SEED,
) -> dict[str, float] | None:
    """Resample mints (clusters), not individual trades.

    Two alerts on the same launch share market conditions and failure modes,
    so treating every trade as independent understates the tail. Each cluster
    contributes all of its trades; the statistic is total PnL. The lower bound
    is the 5th percentile of the resampled totals.
    """
    if not pnls:
        return None
    by_mint: dict[str, list[float]] = defaultdict(list)
    for mint, pnl in zip(mints, pnls, strict=False):
        by_mint[mint or "unknown"].append(float(pnl))
    sums = np.asarray([sum(values) for values in by_mint.values()], dtype=float)
    if sums.size == 0:
        return None
    rng = np.random.default_rng(seed)
    picks = rng.integers(0, sums.size, size=(resamples, sums.size))
    totals = sums[picks].sum(axis=1)
    return {
        "clusters": float(sums.size),
        "resamples": float(resamples),
        "seed": float(seed),
        "mean_sol": float(totals.mean()),
        "lower_sol": float(np.quantile(totals, BOOTSTRAP_LOWER_QUANTILE)),
        "upper_sol": float(np.quantile(totals, 1.0 - BOOTSTRAP_LOWER_QUANTILE)),
        "probability_positive": float((totals > 0).mean()),
    }


def wilson_interval(
    successes: int, total: int, *, z: float = 1.96
) -> dict[str, float] | None:
    """Wilson score interval for one proportion.

    Trades are *not* independent — several share a launch hour and a market
    regime — so this interval is optimistic by construction. It is reported to
    bound sampling noise, not to certify the tail rate.
    """
    if total <= 0:
        return None
    p = successes / total
    denominator = 1.0 + (z * z) / total
    centre = (p + (z * z) / (2.0 * total)) / denominator
    half = (
        z * math.sqrt(p * (1.0 - p) / total + (z * z) / (4.0 * total * total))
    ) / denominator
    return {
        "p": p,
        "low": max(0.0, centre - half),
        "high": min(1.0, centre + half),
        "z": z,
        "n": float(total),
    }


def payoff_summary(
    pnls: list[float], returns: list[float]
) -> dict[str, Any] | None:
    """Win rate against the payoff it has to overcome.

    A high win rate is compatible with a negative ledger when the average loss
    is several times the average win. The break-even win rate is
    ``|avg loss| / (avg win + |avg loss|)``; the margin is the observed win
    rate minus it. This block is report-only and never changes a gate.
    """
    if not returns:
        return None
    wins = [value for value in returns if value > 0]
    losses = [value for value in returns if value <= 0]
    tails = [value for value in returns if value <= -0.9]
    non_tails = [value for value in returns if value > -0.9]
    win_rate = len(wins) / len(returns)
    avg_win = float(np.mean(wins)) if wins else 0.0
    avg_loss = float(np.mean(losses)) if losses else 0.0
    denominator = avg_win + abs(avg_loss)
    break_even = (abs(avg_loss) / denominator) if denominator > 0 else None
    # Per-trade expectancy as a function of the tail rate: (1 - q) * m + q * t.
    non_tail_mean = float(np.mean(non_tails)) if non_tails else None
    tail_mean = float(np.mean(tails)) if tails else -1.0
    break_even_tail = (
        non_tail_mean / (non_tail_mean - tail_mean)
        if non_tail_mean is not None and (non_tail_mean - tail_mean) > 0
        else None
    )
    total = sum(pnls)
    ranked = sorted(pnls, reverse=True)
    return {
        "win_rate": win_rate,
        "break_even_win_rate": break_even,
        "win_rate_margin": (win_rate - break_even) if break_even is not None else None,
        "avg_win_return": avg_win,
        "avg_loss_return": avg_loss,
        "catastrophic_rate": len(tails) / len(returns),
        "catastrophic_rate_ci": wilson_interval(len(tails), len(returns)),
        "non_tail_mean_return": non_tail_mean,
        "tail_mean_return": tail_mean,
        "break_even_tail_rate": break_even_tail,
        "pnl_sol": total,
        "pnl_without_best_1_sol": sum(ranked[1:]),
        "pnl_without_best_3_sol": sum(ranked[3:]),
    }


def tail_sensitivity(
    returns: list[float],
    pnls: list[float],
    *,
    steps: tuple[float, ...] = (0.0, 0.01, 0.02, 0.03),
) -> dict[str, Any] | None:
    """Expectancy if the tail rate rises by a few percentage points.

    Holds the observed non-tail payoff fixed and replaces trades at random with
    the observed average catastrophe. ``expected_pnl_sol`` rescales this run's
    book by the ratio of expected to observed mean return, which assumes a
    constant notional per trade. Report only.
    """
    summary = payoff_summary(pnls, returns)
    if summary is None:
        return None
    q_hat = summary["catastrophic_rate"]
    non_tail_mean = summary["non_tail_mean_return"]
    tail_mean = summary["tail_mean_return"]
    break_even = summary["break_even_tail_rate"]
    if non_tail_mean is None or tail_mean is None:
        return None
    observed_mean = float(np.mean(returns))
    observed_pnl = float(sum(pnls))
    scale = observed_pnl / observed_mean if abs(observed_mean) > 1e-9 else None

    def row(rate: float) -> dict[str, float | None]:
        bounded = min(rate, 1.0)
        mean_return = (1.0 - bounded) * non_tail_mean + bounded * tail_mean
        expected = scale * mean_return if scale is not None else None
        return {
            "tail_rate": bounded,
            "mean_return": mean_return,
            "expected_pnl_sol": expected,
        }

    rows = [row(q_hat + step) for step in steps]
    if break_even is not None and break_even <= 1.0:
        rows.append(row(break_even))
    return {
        "observed_tail_rate": q_hat,
        "non_tail_mean_return": non_tail_mean,
        "tail_mean_return": tail_mean,
        "break_even_tail_rate": break_even,
        "rows": rows,
    }


def concentration_summary(trades: list[dict[str, Any]]) -> dict[str, Any] | None:
    """How much of the ledger rests on a few days and a few mints.

    A day counts as *flipped* when it is profitable only because of its single
    best trade. Report only.
    """
    entries: list[tuple[str, str, float]] = []
    for row in trades:
        try:
            pnl = float(row["pnl_sol"])
        except (KeyError, TypeError, ValueError):
            continue
        if not math.isfinite(pnl):
            continue
        mint = str(row.get("mint") or "unknown")
        entries.append((_day(row.get("exit_ts_ms")) or "unknown", mint, pnl))
    if not entries:
        return None

    by_day: dict[str, list[float]] = defaultdict(list)
    by_mint: dict[str, float] = defaultdict(float)
    for day, mint, pnl in entries:
        by_day[day].append(pnl)
        by_mint[mint] += pnl

    days: list[dict[str, Any]] = []
    for day in sorted(by_day):
        values = by_day[day]
        total = sum(values)
        without_best = total - max(values)
        days.append(
            {
                "day": day,
                "trades": len(values),
                "pnl_sol": total,
                "pnl_without_best_1_sol": without_best,
                "flipped_by_best_1": total > 0.0 and without_best <= 0.0,
            }
        )
    ranked_mints = sorted(by_mint.items(), key=lambda item: item[1], reverse=True)
    return {
        "days": days,
        "total_days": len(days),
        "profitable_days": sum(1 for day in days if day["pnl_sol"] > 0.0),
        "days_flipped_by_best_1": sum(1 for day in days if day["flipped_by_best_1"]),
        "unique_mints": len(by_mint),
        "best_mint": ranked_mints[0][0],
        "best_mint_pnl_sol": ranked_mints[0][1],
        "pnl_without_best_mint_sol": sum(pnl for _, pnl in ranked_mints[1:]),
        "top_mints": [
            {"mint": mint, "pnl_sol": pnl} for mint, pnl in ranked_mints[:5]
        ],
    }


def evaluate(run_dir: Path, *, starting_sol: float = 10.0) -> dict[str, Any]:
    trades, corrupt_trades = _read_jsonl(run_dir / "trades.jsonl")
    skips, corrupt_skips = _read_jsonl(run_dir / "skips.jsonl")
    signals, corrupt_signals = _read_jsonl(run_dir / "signals.jsonl")
    entries, corrupt_entries = _read_jsonl(run_dir / "entries.jsonl")
    corrupt_rows = {
        "trades": corrupt_trades,
        "skips": corrupt_skips,
        "signals": corrupt_signals,
        "entries": corrupt_entries,
    }
    corrupt_total = sum(corrupt_rows.values())

    summary = _read_json_object(run_dir / "summary.json")
    meta = _read_json_object(run_dir / "meta.json")
    summary_integrity = summary.get("integrity") if summary else None
    summary_degraded = summary.get("persistence_degraded") if summary else None
    meta_integrity = meta.get("integrity_at_start") if meta else None
    integrity_ok = (
        summary_integrity == "VALID"
        and summary_degraded is False
        and meta_integrity == "VALID"
    )

    pnls: list[float] = []
    returns: list[float] = []
    mints: list[str] = []
    valid_trades: list[dict[str, Any]] = []
    malformed_trades = 0
    for row in trades:
        try:
            pnl = float(row["pnl_sol"])
            ret = float(row["return_pct"])
        except (KeyError, TypeError, ValueError):
            malformed_trades += 1
            continue
        if not (math.isfinite(pnl) and math.isfinite(ret)):
            malformed_trades += 1
            continue
        pnls.append(pnl)
        returns.append(ret)
        mints.append(str(row.get("mint") or "unknown"))
        valid_trades.append(row)
    days = sorted(
        {
            day
            for row in trades
            if (day := _day(row.get("exit_ts_ms"))) is not None
        }
    )
    catastrophic = sum(1 for value in returns if value <= -0.9)
    write_offs = sum(
        1
        for row in trades
        if str(row.get("exit_status") or "") == "no_route"
        or float(row.get("unsold_size") or 0.0) > 0.0
    )
    allowed = sum(
        1
        for row in signals
        if isinstance(row.get("decision"), dict)
        and row["decision"].get("allowed")
    )
    fill_rate = (len(entries) / allowed) if allowed else None
    pnl = sum(pnls)
    bootstrap = cluster_bootstrap(pnls, mints)

    gates: dict[str, dict[str, Any]] = {}

    # Not a strategy gate: an unreadable row means the measurement itself is
    # unreliable, so it is reported as a failed gate instead of being dropped.
    gates["input_integrity"] = {
        "passed": corrupt_total == 0 and malformed_trades == 0,
        "corrupt_rows": corrupt_rows,
        "malformed_trades": malformed_trades,
        "detail": (
            f"corrupt_rows={corrupt_total} "
            f"(trades={corrupt_trades}, skips={corrupt_skips}, "
            f"signals={corrupt_signals}, entries={corrupt_entries}); "
            f"malformed_trades={malformed_trades}"
        ),
    }

    # The run's own persistence/recovery verdict, not just whether the JSONL
    # parses. A DEGRADED or INVALID process made decisions from state that was
    # not durable, so its numbers cannot be pooled with a clean sample even if
    # every arithmetic gate passes. Missing metadata is a failure, never a
    # silent pass: an old run without a summary cannot prove its integrity.
    gates["run_integrity"] = {
        "passed": integrity_ok,
        "summary_integrity": summary_integrity,
        "summary_persistence_degraded": summary_degraded,
        "meta_integrity_at_start": meta_integrity,
        "summary_present": summary is not None,
        "meta_present": meta is not None,
        "detail": (
            f"summary.integrity={summary_integrity!s}; "
            f"summary.persistence_degraded={summary_degraded!s}; "
            f"meta.integrity_at_start={meta_integrity!s}; "
            "required: VALID / false / VALID"
        ),
    }

    coverage_ok = len(trades) >= MIN_TRADES and len(days) >= MIN_DAYS
    gates["coverage"] = {
        "passed": coverage_ok,
        "trades": len(trades),
        "days": len(days),
        "required_trades": MIN_TRADES,
        "required_days": MIN_DAYS,
        "detail": f"{len(trades)} trades over {len(days)} days",
    }

    lower_bound = bootstrap["lower_sol"] if bootstrap else None
    floor = -BOOTSTRAP_LOSS_ALLOWANCE * starting_sol
    pnl_ok = (
        pnl > 0 and lower_bound is not None and lower_bound >= floor
    )
    gates["realized_pnl"] = {
        "passed": pnl_ok,
        "pnl_sol": pnl,
        "bootstrap_lower_sol": lower_bound,
        "bootstrap_floor_sol": floor,
        "bootstrap": bootstrap,
        "detail": (
            f"pnl={pnl:+.6f} SOL; bootstrap 95% lower={lower_bound!s}; "
            f"floor={floor:+.6f}"
        ),
    }

    median = float(np.median(returns)) if returns else None
    gates["median_return"] = {
        "passed": median is not None and median >= MIN_MEDIAN_RETURN,
        "median_return_pct": median,
        "required": MIN_MEDIAN_RETURN,
        "detail": f"median={median!s}; required>={MIN_MEDIAN_RETURN}",
    }

    catastrophic_rate = (catastrophic / len(trades)) if trades else None
    gates["catastrophic_exits"] = {
        "passed": (
            catastrophic_rate is not None
            and catastrophic_rate < MAX_CATASTROPHIC_RATE
        ),
        "count": catastrophic,
        "rate": catastrophic_rate,
        "required_below": MAX_CATASTROPHIC_RATE,
        "detail": f"{catastrophic}/{len(trades)} < {MAX_CATASTROPHIC_RATE}",
    }

    payoff = payoff_summary(pnls, returns)
    tail = tail_sensitivity(returns, pnls)
    concentration = concentration_summary(valid_trades)

    skip_counts: dict[str, int] = defaultdict(int)
    for row in skips:
        skip_counts[str(row.get("reason") or "unknown")] += 1

    # Attribute each skip to the signal decision it belongs to. Only skips for
    # signals that passed the gate are entry failures; intake rejections
    # (``allowed=false``) are censoring and are already outside ``allowed``.
    # This replaces the old two-reason numerator, which missed preflight,
    # persistence, funding and no-print outcomes entirely.
    allowed_identities = {
        identity
        for row in signals
        if isinstance(row.get("decision"), dict)
        and row["decision"].get("allowed")
        and (identity := _signal_identity(row))
    }
    entry_failure_counts: dict[str, int] = defaultdict(int)
    entry_failures = 0
    unattributed_skips = 0
    for row in skips:
        reason = str(row.get("reason") or "unknown")
        identity = _signal_identity(row)
        if identity:
            if identity not in allowed_identities:
                continue
        elif reason not in ENTRY_FAILURE_REASONS:
            # No identity and not a known entry-attempt outcome: cannot be
            # attributed safely, so it is not counted as a failure. Real runs
            # always carry message_id/mint, so this is for legacy data only.
            continue
        else:
            unattributed_skips += 1
        entry_failure_counts[reason] += 1
        entry_failures += 1

    entry_delay_failures = entry_failure_counts.get("entry_delay_exceeded", 0)
    quote_failures = entry_failure_counts.get("entry_quote_failed", 0)
    entry_failure_rate = entry_failures / allowed if allowed else None

    write_off_rate = (write_offs / len(trades)) if trades else None
    entry_ok = fill_rate is not None and fill_rate >= MIN_ENTRY_FILL_RATE
    failure_ok = (
        entry_failure_rate is not None
        and entry_failure_rate < MAX_ENTRY_FAILURE_RATE
    )

    # Visibility, not a gate: fidelity guards (late delivery, missed eligible
    # print, timer lateness, concurrency) reject cards as ``allowed=false``, so
    # they leave both the fill-rate numerator and denominator. Report how much
    # of the scanner population the bot actually traded, and why the rest was
    # dropped, so a "100 % fill rate" on a small subset is visible. The frozen
    # thresholds are unchanged.
    scanner_rows = [
        row
        for row in signals
        if isinstance(row.get("signal"), dict)
        and row["signal"].get("message_type") == SCANNER_MESSAGE_TYPE
    ]
    scanner_cards = len(scanner_rows)
    allowed_scanner = sum(
        1
        for row in scanner_rows
        if isinstance(row.get("decision"), dict)
        and row["decision"].get("allowed")
    )
    rejection_reasons: dict[str, int] = defaultdict(int)
    for row in scanner_rows:
        decision = row.get("decision")
        if not isinstance(decision, dict) or decision.get("allowed"):
            continue
        reasons = decision.get("reasons") or ["unknown"]
        if isinstance(reasons, str):
            reasons = [reasons]
        for reason in reasons:
            rejection_reasons[str(reason)] += 1
    censor_share = (
        (1.0 - allowed_scanner / scanner_cards) if scanner_cards else None
    )

    gates["execution"] = {
        "passed": (
            write_off_rate is not None
            and write_off_rate < MAX_WRITE_OFF_RATE
            and entry_ok
            and failure_ok
        ),
        "write_offs": write_offs,
        "write_off_rate": write_off_rate,
        "required_write_off_below": MAX_WRITE_OFF_RATE,
        "entries": len(entries),
        "signals_allowed": allowed,
        "scanner_cards": scanner_cards,
        "scanner_cards_allowed": allowed_scanner,
        "censor_share": censor_share,
        "rejection_reason_counts": dict(sorted(rejection_reasons.items())),
        "entry_fill_rate": fill_rate,
        "required_entry_fill_rate": MIN_ENTRY_FILL_RATE,
        "entry_delay_exceeded": entry_delay_failures,
        "entry_quote_failed": quote_failures,
        "entry_failures": entry_failures,
        "entry_failure_reason_counts": dict(
            sorted(entry_failure_counts.items())
        ),
        "unattributed_entry_failures": unattributed_skips,
        "entry_failure_rate": entry_failure_rate,
        "required_entry_failure_below": MAX_ENTRY_FAILURE_RATE,
        "detail": (
            f"write_offs={write_offs}/{len(trades)} must be "
            f"< {MAX_WRITE_OFF_RATE}; "
            f"fill_rate={fill_rate!s}; "
            f"entry_failures={entry_failures}/{allowed} "
            f"({entry_failure_rate!s}) must be "
            f"< {MAX_ENTRY_FAILURE_RATE}; "
            f"traded={allowed_scanner}/{scanner_cards} cards"
        ),
    }

    passed = all(gate["passed"] for gate in gates.values())
    generated_at = datetime.now(UTC).isoformat()
    first_exit_ms, last_exit_ms = _exit_ts_bounds(trades)
    provenance = {
        "run_dir": str(run_dir),
        "generated_at": generated_at,
        "meta_present": meta is not None,
        "rule": meta.get("rule") if meta else None,
        "lane": meta.get("lane") if meta else None,
        "mode": meta.get("mode") if meta else None,
        "entry_mode": meta.get("entry_mode") if meta else None,
        "started_at": meta.get("started_at") if meta else None,
        "resumed_at": meta.get("resumed_at") if meta else None,
        "recovery_status": meta.get("recovery_status") if meta else None,
        "trades": len(trades),
        "first_exit_ts_ms": first_exit_ms,
        "last_exit_ts_ms": last_exit_ms,
        "first_exit_at": _timestamp(first_exit_ms),
        "last_exit_at": _timestamp(last_exit_ms),
    }
    return {
        "run_dir": str(run_dir),
        "generated_at": generated_at,
        "provenance": provenance,
        "starting_sol": starting_sol,
        "trades": len(trades),
        "days": days,
        "corrupt_rows": corrupt_rows,
        "malformed_trades": malformed_trades,
        "skip_reason_counts": dict(sorted(skip_counts.items())),
        "payoff": payoff,
        "tail_sensitivity": tail,
        "concentration": concentration,
        "gates": gates,
        "passed": passed,
        "coverage_met": coverage_ok,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--starting-sol", type=float, default=10.0)
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument(
        "--detail",
        action="store_true",
        help="print the per-day and per-mint concentration tables",
    )
    args = parser.parse_args(argv)

    result = evaluate(args.run_dir, starting_sol=args.starting_sol)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        provenance = result.get("provenance") or {}
        print(f"run: {result['run_dir']}")
        print(
            f"provenance: rule={provenance.get('rule') or '?'} "
            f"lane={provenance.get('lane') or '?'} "
            f"mode={provenance.get('mode') or '?'} "
            f"entry={provenance.get('entry_mode') or '?'}"
        )
        print(
            f"  started={provenance.get('started_at') or '?'} "
            f"recovery={provenance.get('recovery_status') or '?'} "
            f"meta={'present' if provenance.get('meta_present') else 'missing'}"
        )
        print(
            f"  trades={result['trades']} over {len(result['days'])} days; "
            f"last exit={provenance.get('last_exit_at') or 'n/a'}; "
            f"generated={provenance.get('generated_at') or '?'}"
        )
        for name, gate in result["gates"].items():
            mark = "PASS" if gate["passed"] else "FAIL"
            print(f"[{mark}] {name}: {gate['detail']}")
        print(f"overall: {'PASS' if result['passed'] else 'FAIL'}")
        payoff = result.get("payoff")
        if payoff:
            break_even = payoff["break_even_win_rate"]
            margin = payoff["win_rate_margin"]
            break_even_text = "  n/a" if break_even is None else f"{break_even * 100:.1f}%"
            margin_text = "  n/a" if margin is None else f"{margin * 100:+.1f}"
            print(
                f"payoff (report only): win={payoff['win_rate'] * 100:.1f}% "
                f"break-even={break_even_text} margin={margin_text} pp; "
                f"avg win={payoff['avg_win_return'] * 100:+.1f}% "
                f"avg loss={payoff['avg_loss_return'] * 100:+.1f}% "
                f"cat={payoff['catastrophic_rate'] * 100:.1f}%"
            )
            print(
                f"  PnL={payoff['pnl_sol']:+.4f} "
                f"without best 3={payoff['pnl_without_best_3_sol']:+.4f} SOL"
            )
            ci = payoff.get("catastrophic_rate_ci")
            if ci:
                print(
                    f"  tail-rate 95% CI (Wilson, independent trades): "
                    f"{ci['low'] * 100:.1f}%-{ci['high'] * 100:.1f}%; "
                    "clustered launches make the true interval wider"
                )
        tail = result.get("tail_sensitivity")
        if tail and tail.get("rows"):
            print("tail sensitivity (report only, non-tail payoff held fixed):")
            for row in tail["rows"]:
                expected = row["expected_pnl_sol"]
                expected_text = "     n/a" if expected is None else f"{expected:+7.3f}"
                tag = (
                    "break-even"
                    if abs(row["mean_return"]) < 1e-9
                    else f"{row['tail_rate'] * 100:5.1f}%"
                )
                print(
                    f"  tail={tag:>10s} mean={row['mean_return'] * 100:+6.2f}%/trade "
                    f"PnL={expected_text} SOL"
                )
        concentration = result.get("concentration")
        if concentration:
            print(
                f"concentration (report only): "
                f"{concentration['profitable_days']}/{concentration['total_days']} "
                f"profitable days, {concentration['days_flipped_by_best_1']} flipped "
                f"by their best trade; best mint {concentration['best_mint'][:12]} "
                f"{concentration['best_mint_pnl_sol']:+.4f} SOL, without it "
                f"{concentration['pnl_without_best_mint_sol']:+.4f} SOL"
            )
            if args.detail:
                print("  day        trades      pnl   w/o best1  flipped")
                for day in concentration["days"]:
                    print(
                        f"  {day['day']:10s} {day['trades']:6d} "
                        f"{day['pnl_sol']:+8.4f} {day['pnl_without_best_1_sol']:+11.4f} "
                        f"{'yes' if day['flipped_by_best_1'] else 'no':>7s}"
                    )
                print("  top mints by PnL")
                for mint in concentration["top_mints"]:
                    print(f"    {mint['mint'][:20]:22s} {mint['pnl_sol']:+8.4f} SOL")
        if result["skip_reason_counts"]:
            print("skips:")
            for reason, count in result["skip_reason_counts"].items():
                print(f"  {reason}: {count}")

    if not result["coverage_met"]:
        return 2
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
