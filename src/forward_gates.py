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
BOOTSTRAP_LOWER_QUANTILE = 0.05
BOOTSTRAP_LOSS_ALLOWANCE = 0.05


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("rb") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                rows.append(payload)
    return rows


def _day(ts_ms: Any) -> str | None:
    try:
        return datetime.fromtimestamp(int(ts_ms) / 1000, UTC).date().isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


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


def evaluate(run_dir: Path, *, starting_sol: float = 10.0) -> dict[str, Any]:
    trades = _read_jsonl(run_dir / "trades.jsonl")
    skips = _read_jsonl(run_dir / "skips.jsonl")
    signals = _read_jsonl(run_dir / "signals.jsonl")
    entries = _read_jsonl(run_dir / "entries.jsonl")

    pnls = [float(row.get("pnl_sol") or 0.0) for row in trades]
    returns = [float(row.get("return_pct") or 0.0) for row in trades]
    mints = [str(row.get("mint") or "unknown") for row in trades]
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
            and catastrophic_rate <= MAX_CATASTROPHIC_RATE
        ),
        "count": catastrophic,
        "rate": catastrophic_rate,
        "required_max": MAX_CATASTROPHIC_RATE,
        "detail": f"{catastrophic}/{len(trades)} <= {MAX_CATASTROPHIC_RATE}",
    }

    write_off_rate = (write_offs / len(trades)) if trades else None
    entry_ok = fill_rate is not None and fill_rate >= MIN_ENTRY_FILL_RATE
    gates["execution"] = {
        "passed": (
            write_off_rate is not None
            and write_off_rate <= MAX_WRITE_OFF_RATE
            and entry_ok
        ),
        "write_offs": write_offs,
        "write_off_rate": write_off_rate,
        "entries": len(entries),
        "signals_allowed": allowed,
        "entry_fill_rate": fill_rate,
        "required_entry_fill_rate": MIN_ENTRY_FILL_RATE,
        "detail": (
            f"write_offs={write_offs}/{len(trades)}; "
            f"fill_rate={fill_rate!s}"
        ),
    }

    skip_counts: dict[str, int] = defaultdict(int)
    for row in skips:
        skip_counts[str(row.get("reason") or "unknown")] += 1

    passed = all(gate["passed"] for gate in gates.values())
    return {
        "run_dir": str(run_dir),
        "generated_at": datetime.now(UTC).isoformat(),
        "starting_sol": starting_sol,
        "trades": len(trades),
        "days": days,
        "skip_reason_counts": dict(sorted(skip_counts.items())),
        "gates": gates,
        "passed": passed,
        "coverage_met": coverage_ok,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--starting-sol", type=float, default=10.0)
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args(argv)

    result = evaluate(args.run_dir, starting_sol=args.starting_sol)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"run: {result['run_dir']}")
        print(
            f"trades: {result['trades']} over {len(result['days'])} days"
        )
        for name, gate in result["gates"].items():
            mark = "PASS" if gate["passed"] else "FAIL"
            print(f"[{mark}] {name}: {gate['detail']}")
        print(f"overall: {'PASS' if result['passed'] else 'FAIL'}")
        if result["skip_reason_counts"]:
            print("skips:")
            for reason, count in result["skip_reason_counts"].items():
                print(f"  {reason}: {count}")

    if not result["coverage_met"]:
        return 2
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
