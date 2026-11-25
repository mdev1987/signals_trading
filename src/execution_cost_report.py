"""Quote-derived execution-cost report for one paper run.

Read-only: this module never trades, never posts and never modifies a run. It
turns the evidence the bot already records (entry/exit quote prices, modeled
fee and slippage, Jupiter price impact, quote timestamps, full-size vs
fractional exits) into the numbers needed before changing any strategy
parameter:

* what the modeled round-trip cost actually was;
* how much of it is fee, slippage and Jupiter price impact;
* how stale the executed quotes were;
* how often the full size routed, and how often the fraction ladder was used;
* total PnL under a grid of cost assumptions, from zero cost to worse than the
  deployed 1.25 %/side fee.

A quote is not a fill and a modeled fee is not a paid fee. Paper fills cannot
show priority fees, Jito tips, ATA rent or transaction fees; those need
executed on-chain transactions and are deliberately absent here.

Usage::

    env PYTHONPATH=src .venv/bin/python src/execution_cost_report.py \\
        --run-dir output/paper/live_20261125_120000
    env PYTHONPATH=src .venv/bin/python src/execution_cost_report.py \\
        --run-dir output/paper/live_20261125_120000 --json
    env PYTHONPATH=src .venv/bin/python src/execution_cost_report.py \\
        --run-dir output/paper/live_20261125_120000 \\
        --artifact output/reports/execution_cost.json
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from pathlib import Path
from typing import Any

# Cost assumptions (per side). 0.0125 is the deployed fee model; slippage is a
# separate axis because the deployed lane passes --slippage 0 and relies on the
# Jupiter quote's own price impact.
DEFAULT_FEE_GRID = (0.0, 0.0025, 0.005, 0.0075, 0.01, 0.0125, 0.015, 0.02)
DEFAULT_SLIPPAGE_GRID = (0.0, 0.005, 0.01, 0.0125)


def _load_rows(run_dir: Path) -> tuple[list[dict[str, Any]], int]:
    path = run_dir / "trades.jsonl"
    if not path.exists():
        raise FileNotFoundError(f"no trades.jsonl under {run_dir}")
    rows: list[dict[str, Any]] = []
    corrupt = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            corrupt += 1
            continue
        if isinstance(row, dict):
            rows.append(row)
        else:
            corrupt += 1
    return rows, corrupt


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(float(value)) else None
    if isinstance(value, str):
        try:
            parsed = float(value)
        except ValueError:
            return None
        return parsed if math.isfinite(parsed) else None
    return None


def _json_field(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return None
    return value


def _quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * q
    lower = int(math.floor(position))
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _stats(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"n": 0, "mean": None, "median": None, "p05": None, "p95": None}
    return {
        "n": len(values),
        "mean": statistics.fmean(values),
        "median": statistics.median(values),
        "p05": _quantile(values, 0.05),
        "p95": _quantile(values, 0.95),
    }


def _trade_view(row: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize one trade row, or return None when it cannot be priced."""
    size = _number(row.get("size"))
    entry_price = _number(row.get("entry_price_sol"))
    exit_price = _number(row.get("exit_price_sol"))
    pnl = _number(row.get("pnl_sol"))
    if (
        size is None
        or size <= 0
        or entry_price is None
        or entry_price <= 0
        or pnl is None
    ):
        return None
    unsold = _number(row.get("unsold_size")) or 0.0
    sold = max(0.0, size - unsold)
    entry_value = _number(row.get("entry_value_sol"))
    entry_fee = _number(row.get("entry_fee_sol"))
    exit_value = _number(row.get("exit_value_sol"))
    exit_fee = _number(row.get("exit_fee_sol"))
    entry_impact = _number(row.get("entry_impact")) or 0.0

    entry_quote_ts = None
    raw = _json_field(row.get("entry_quote_raw"))
    if isinstance(raw, dict):
        entry_quote_ts = _number(raw.get("_quoted_at_ms"))
    entry_ts = _number(row.get("entry_ts_ms"))

    fill_evidence = _json_field(row.get("exit_fill_evidence"))
    if not isinstance(fill_evidence, list):
        fill_evidence = []
    fractions = row.get("exit_quote_fractions")
    if isinstance(fractions, str):
        fractions = _json_field(fractions)
    if not isinstance(fractions, list):
        fractions = []

    exit_quote_latencies: list[float] = []
    exit_quote_ts: list[float] = []
    for leg in fill_evidence:
        if not isinstance(leg, dict):
            continue
        quote_ts = _number(leg.get("quote_ts_ms"))
        fill_ts = _number(leg.get("fill_ts_ms"))
        if quote_ts is not None:
            exit_quote_ts.append(quote_ts)
            if fill_ts is not None and fill_ts >= quote_ts:
                exit_quote_latencies.append(fill_ts - quote_ts)

    return {
        "mint": row.get("mint"),
        "rule": row.get("rule"),
        "size": size,
        "sold": sold,
        "unsold": unsold,
        "entry_price_sol": entry_price,
        "exit_price_sol": exit_price,
        "entry_value_sol": entry_value,
        "entry_fee_sol": entry_fee,
        "exit_value_sol": exit_value,
        "exit_fee_sol": exit_fee,
        "entry_impact": entry_impact,
        "pnl_sol": pnl,
        "return_pct": _number(row.get("return_pct")),
        "exit_status": row.get("exit_status"),
        "exit_reason": row.get("exit_reason"),
        "entry_quote_price_impact_pct": _number(
            row.get("entry_quote_price_impact_pct")
        ),
        "exit_quote_price_impact_pct": _number(
            row.get("exit_quote_price_impact_pct")
        ),
        "entry_quote_slippage_bps": _number(
            row.get("entry_quote_slippage_bps")
        ),
        "exit_quote_slippage_bps": _number(
            row.get("exit_quote_slippage_bps")
        ),
        "exit_full_size": row.get("exit_full_size"),
        "exit_fractions": [f for f in fractions if isinstance(f, (int, float))],
        "entry_quote_age_ms": (
            entry_ts - entry_quote_ts
            if entry_ts is not None
            and entry_quote_ts is not None
            and entry_ts >= entry_quote_ts
            else None
        ),
        "exit_quote_latency_ms": (
            statistics.fmean(exit_quote_latencies)
            if exit_quote_latencies
            else None
        ),
        "exit_quote_ts_ms": min(exit_quote_ts) if exit_quote_ts else None,
        "exit_ts_ms": _number(row.get("exit_ts_ms")),
        "quote_guard_checks": _number(row.get("quote_guard_checks")),
        "quote_guard_unavailable": _number(
            row.get("quote_guard_unavailable")
        ),
        "quote_guard_min_ratio": _number(row.get("quote_guard_min_ratio")),
    }


def _cost_sensitivity(
    trades: list[dict[str, Any]],
    *,
    fee_grid: tuple[float, ...],
    slippage_grid: tuple[float, ...],
) -> list[dict[str, Any]]:
    """Total PnL at each (fee, slippage) pair, on full sells only.

    The quote prices are held fixed, so this isolates the cost assumption from
    the price path. Write-offs and partial sales are excluded: their unsold
    remainder has no quote and cannot be repriced without inventing one.
    """
    usable = [
        trade
        for trade in trades
        if trade["unsold"] == 0
        and trade["exit_price_sol"] is not None
        and trade["exit_price_sol"] > 0
    ]
    results: list[dict[str, Any]] = []
    for fee in fee_grid:
        for slippage in slippage_grid:
            total = 0.0
            for trade in usable:
                size = trade["size"]
                entry_value = size * trade["entry_price_sol"] * (1 + slippage)
                exit_value = size * trade["exit_price_sol"] * (1 - slippage)
                total += (
                    exit_value
                    - exit_value * fee
                    - entry_value
                    - entry_value * fee
                )
            results.append(
                {
                    "fee_per_side": fee,
                    "slippage_per_side": slippage,
                    "trades": len(usable),
                    "pnl_sol": total,
                }
            )
    return results


def summarize(
    rows: list[dict[str, Any]],
    *,
    corrupt_rows: int = 0,
    fee_grid: tuple[float, ...] = DEFAULT_FEE_GRID,
    slippage_grid: tuple[float, ...] = DEFAULT_SLIPPAGE_GRID,
) -> dict[str, Any]:
    trades = [view for row in rows if (view := _trade_view(row)) is not None]
    unusable = len(rows) - len(trades)

    total_pnl = sum(t["pnl_sol"] for t in trades)
    entry_notional = sum(t["size"] * t["entry_price_sol"] for t in trades)

    modeled_entry_slippage = []
    modeled_exit_slippage = []
    modeled_fee = []
    # Cost components are only complete for a fully sold position: a partial
    # row's ``entry_value_sol``/``entry_fee_sol`` describe the unsold remainder,
    # so including it would understate the cost. Write-offs are counted
    # separately as losses, not as execution cost.
    cost_trades = [t for t in trades if t["unsold"] == 0]
    for trade in cost_trades:
        if trade["entry_value_sol"] is not None:
            modeled_entry_slippage.append(
                max(
                    0.0,
                    trade["entry_value_sol"]
                    - trade["size"] * trade["entry_price_sol"],
                )
            )
        if trade["exit_value_sol"] is not None:
            modeled_exit_slippage.append(
                max(
                    0.0,
                    trade["sold"] * trade["exit_price_sol"]
                    - trade["exit_value_sol"],
                )
            )
        fee_total = 0.0
        if trade["entry_fee_sol"] is not None:
            fee_total += trade["entry_fee_sol"]
        if trade["exit_fee_sol"] is not None:
            fee_total += trade["exit_fee_sol"]
        modeled_fee.append(fee_total)

    total_cost = (
        sum(modeled_entry_slippage)
        + sum(modeled_exit_slippage)
        + sum(modeled_fee)
    )
    cost_notional = sum(
        t["size"] * t["entry_price_sol"] for t in cost_trades
    )
    entry_impacts = [
        t["entry_quote_price_impact_pct"]
        for t in trades
        if t["entry_quote_price_impact_pct"] is not None
    ]
    exit_impacts = [
        t["exit_quote_price_impact_pct"]
        for t in trades
        if t["exit_quote_price_impact_pct"] is not None
    ]
    entry_quote_age = [
        float(t["entry_quote_age_ms"])
        for t in trades
        if t["entry_quote_age_ms"] is not None
    ]
    exit_quote_latency = [
        t["exit_quote_latency_ms"]
        for t in trades
        if t["exit_quote_latency_ms"] is not None
    ]
    full_size = [t for t in trades if t["exit_full_size"] is True]
    ladder = [
        t
        for t in trades
        if t["exit_fractions"] and min(t["exit_fractions"]) < 1.0
    ]
    write_offs = [
        t
        for t in trades
        if t["exit_status"] in ("no_route", "near_zero_exit")
        or t["unsold"] > 0
    ]
    catastrophic = [
        t for t in trades if (t["return_pct"] or 0.0) <= -0.90
    ]
    guard_trades = [
        t for t in trades if t["quote_guard_checks"] is not None
    ]

    return {
        "trades": len(trades),
        "unusable_rows": unusable,
        "corrupt_rows": corrupt_rows,
        "total_pnl_sol": total_pnl,
        "entry_notional_sol": entry_notional,
        "modeled_cost": {
            "trades": len(cost_trades),
            "entry_slippage_sol": sum(modeled_entry_slippage),
            "exit_slippage_sol": sum(modeled_exit_slippage),
            "fee_sol": sum(modeled_fee),
            "total_sol": total_cost,
            "notional_sol": cost_notional,
            "share_of_entry_notional": (
                total_cost / cost_notional if cost_notional > 0 else None
            ),
        },
        "entry_price_impact_pct": _stats(entry_impacts),
        "exit_price_impact_pct": _stats(exit_impacts),
        "entry_quote_age_ms": _stats(entry_quote_age),
        "exit_quote_latency_ms": _stats(exit_quote_latency),
        "exit_full_size_share": (
            len(full_size) / len(trades) if trades else None
        ),
        "exit_fraction_ladder_share": (
            len(ladder) / len(trades) if trades else None
        ),
        "write_off_share": len(write_offs) / len(trades) if trades else None,
        "catastrophic_share": (
            len(catastrophic) / len(trades) if trades else None
        ),
        "quote_guard": {
            "trades": len(guard_trades),
            "checks": sum(
                int(t["quote_guard_checks"] or 0) for t in guard_trades
            ),
            "unavailable": sum(
                int(t["quote_guard_unavailable"] or 0) for t in guard_trades
            ),
            "min_ratio": _stats(
                [
                    t["quote_guard_min_ratio"]
                    for t in guard_trades
                    if t["quote_guard_min_ratio"] is not None
                ]
            ),
        },
        "cost_sensitivity": _cost_sensitivity(
            trades, fee_grid=fee_grid, slippage_grid=slippage_grid
        ),
    }


def format_report(result: dict[str, Any], run_dir: Path) -> str:
    def pct(value: float | None) -> str:
        return "n/a" if value is None else f"{value * 100:.2f}%"

    def sol(value: float | None) -> str:
        return "n/a" if value is None else f"{value:+.4f}"

    def stat(block: dict[str, Any], key: str) -> str:
        value = block.get(key)
        return "n/a" if value is None else f"{value:.2f}"

    lines = [
        f"# Execution cost report: {run_dir}",
        "",
        f"- trades: **{result['trades']}** "
        f"({result['unusable_rows']} unusable, "
        f"{result['corrupt_rows']} corrupt rows)",
        f"- entry notional: **{result['entry_notional_sol']:.4f} SOL**",
        f"- realized PnL: **{sol(result['total_pnl_sol'])} SOL**",
        "",
        "## Modeled round-trip cost",
        "",
        f"Computed on the **{result['modeled_cost']['trades']} fully sold** "
        "trades; partial exits and write-offs are excluded (their unsold "
        "remainder has no quote).",
        "",
        "| component | SOL |",
        "| --- | ---: |",
        f"| entry slippage | {result['modeled_cost']['entry_slippage_sol']:+.4f} |",
        f"| exit slippage | {result['modeled_cost']['exit_slippage_sol']:+.4f} |",
        f"| fee (both sides) | {result['modeled_cost']['fee_sol']:+.4f} |",
        f"| **total** | **{result['modeled_cost']['total_sol']:+.4f}** |",
        "",
        "Cost as a share of entry notional: "
        f"**{pct(result['modeled_cost']['share_of_entry_notional'])}**.",
        "This is the modeled cost the broker applied. It is not a paid fee: "
        "priority fees, Jito tips, ATA rent and transaction fees are only "
        "observable with executed on-chain transactions.",
        "",
        "## Quote quality",
        "",
        "| measure | n | mean | median | p05 | p95 |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for label, key in (
        ("entry price impact (%)", "entry_price_impact_pct"),
        ("exit price impact (%)", "exit_price_impact_pct"),
        ("entry quote age (ms)", "entry_quote_age_ms"),
        ("exit quote latency (ms)", "exit_quote_latency_ms"),
    ):
        block = result[key]
        lines.append(
            f"| {label} | {block['n']} | {stat(block, 'mean')} | "
            f"{stat(block, 'median')} | {stat(block, 'p05')} | "
            f"{stat(block, 'p95')} |"
        )
    lines += [
        "",
        f"- full-size exit quotes: **{pct(result['exit_full_size_share'])}**",
        f"- fraction-ladder exits: **{pct(result['exit_fraction_ladder_share'])}**",
        f"- write-offs / near-zero exits: **{pct(result['write_off_share'])}**",
        f"- catastrophic exits (<= -90 %): **{pct(result['catastrophic_share'])}**",
        "",
        "## PnL under cost assumptions",
        "",
        "Quote prices are held fixed; only fee and slippage vary. Write-offs "
        "and partial sales are excluded because their unsold remainder has no "
        "quote.",
        "",
    ]
    grid = result["cost_sensitivity"]
    if not grid:
        lines.append("_No fully sold trades to reprice._")
    else:
        fees = sorted({row["fee_per_side"] for row in grid})
        slips = sorted({row["slippage_per_side"] for row in grid})
        header = "| fee / side | " + " | ".join(
            f"slip {s * 100:.2f}%" for s in slips
        ) + " |"
        lines.append(header)
        lines.append("| ---: | " + " | ".join("---:" for _ in slips) + " |")
        by_key = {
            (row["fee_per_side"], row["slippage_per_side"]): row["pnl_sol"]
            for row in grid
        }
        for fee in fees:
            cells = " | ".join(
                f"{by_key[(fee, slip)]:+.3f}" for slip in slips
            )
            lines.append(f"| {fee * 100:.2f}% | {cells} |")
    if result["quote_guard"]["trades"]:
        guard = result["quote_guard"]
        min_ratio = guard["min_ratio"]["median"]
        lines += [
            "",
            "## Executable-exit guard",
            "",
            f"- positions probed: {guard['trades']}",
            f"- probes: {guard['checks']}, unavailable routes: "
            f"{guard['unavailable']}",
            "- median lowest exit-value ratio: "
            + ("n/a" if min_ratio is None else f"{min_ratio:.3f}"),
        ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument(
        "--artifact",
        type=Path,
        default=None,
        help="write the full result as JSON to this path",
    )
    args = parser.parse_args(argv)

    rows, corrupt = _load_rows(args.run_dir)
    result = summarize(rows, corrupt_rows=corrupt)
    result["run_dir"] = str(args.run_dir)

    if args.artifact is not None:
        args.artifact.parent.mkdir(parents=True, exist_ok=True)
        args.artifact.write_text(
            json.dumps(result, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(format_report(result, args.run_dir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
