#!/usr/bin/env python3
"""Preflight exit-depth threshold study (the "V3" gate experiment).

The engine's preflight guard quotes a *sell of the full entry size* before it
buys, and logs the resulting ratio::

    preflight lane mint=<mint> exit_price=<sol> entry_cost=<sol> \\
        ratio=<net exit value / entry cost> min=<threshold>

The ratio is dominated by the cost model, not by pool depth. For a zero-move
trade it equals the **fee floor**::

    floor = (1 - slippage) * (1 - fee) / (1 + fee)

With the deployed ``fee = 1.25 %/side`` and ``slippage = 50 bps`` the floor is
0.9704, i.e. a perfectly healthy pool still quotes about 0.97. A raw threshold
such as 0.95 is therefore *below the floor* and can never reject anything,
while 0.98 rejects almost the whole feed. Any threshold must be read against
the floor, which is why this tool reports both the raw ratio and the
fee-floor-relative **depth margin** in basis points.

This tool joins the logged preflight quotes to the realized trades and reports
what each threshold would have accepted and rejected. It changes nothing: it
is a measurement over existing ``trades.jsonl`` and the bot log.

Because a stricter threshold can only remove trades the deployed 0.90 guard
already accepted, the counterfactual is fully observed: every rejected trade
has a known outcome. That is not true below the deployed threshold, where the
rejected trades were never entered and have no outcome.

Examples::

    env PYTHONPATH=src .venv/bin/python src/preflight_study.py
    env PYTHONPATH=src .venv/bin/python src/preflight_study.py \\
        --thresholds 0.960 0.965 0.970 0.975 0.980
    env PYTHONPATH=src .venv/bin/python src/preflight_study.py --json
"""

from __future__ import annotations

import argparse
import glob
import json
import math
import re
import statistics
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

DEFAULT_LOG = Path.home() / ".local/share/oxmgr/logs/paper-bot.log"
DEFAULT_THRESHOLDS = (0.90, 0.95, 0.98)
# Matches the deployed paper cost model; used only to anchor the depth margin.
DEFAULT_FEE = 0.0125
DEFAULT_SLIPPAGE = 0.005
CATASTROPHIC_RETURN = -0.90
JOIN_TOLERANCE_MS = 300_000

_LINE = re.compile(
    r"^(?P<ts>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d+)\s.*?"
    r"preflight lane mint=(?P<mint>\S+) exit_price=(?P<price>\S+) "
    r"entry_cost=(?P<cost>\S+) ratio=(?P<ratio>\S+) min=(?P<minimum>\S+)"
)


@dataclass(frozen=True, slots=True)
class PreflightQuote:
    """One logged preflight attempt, accepted or rejected by the gate."""

    timestamp_ms: int
    mint: str
    ratio: float
    minimum: float
    exit_price_sol: float | None
    entry_cost_sol: float | None


@dataclass(frozen=True, slots=True)
class JoinedTrade:
    """A preflight quote and the trade it guarded."""

    ratio: float
    entry_ts_ms: int
    mint: str
    return_pct: float
    pnl_sol: float
    exit_value_sol: float | None
    pnl_at_threshold_sol: float | None


def _parse_float(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    return parsed if math.isfinite(parsed) else None


def _log_timestamp_ms(text: str) -> int:
    stamp = datetime.strptime(text, "%Y-%m-%d %H:%M:%S,%f").replace(tzinfo=UTC)
    return int(stamp.timestamp() * 1000)


def parse_preflight_log(path: Path) -> list[PreflightQuote]:
    """Parse every evaluable preflight line, including gate rejections.

    ``ratio=invalid`` lines are skipped: the engine fails closed on them, but
    they carry no ratio to evaluate.
    """
    quotes: list[PreflightQuote] = []
    with path.open("r", errors="replace") as handle:
        for line in handle:
            match = _LINE.match(line)
            if not match:
                continue
            ratio = _parse_float(match.group("ratio"))
            if ratio is None:
                continue
            quotes.append(
                PreflightQuote(
                    timestamp_ms=_log_timestamp_ms(match.group("ts")),
                    mint=match.group("mint"),
                    ratio=ratio,
                    minimum=_parse_float(match.group("minimum")) or 0.0,
                    exit_price_sol=_parse_float(match.group("price")),
                    entry_cost_sol=_parse_float(match.group("cost")),
                )
            )
    return quotes


def load_funnel(run_dirs: Iterable[Path]) -> dict[str, Any]:
    """Count signals, accepted entries and skip reasons across the runs.

    This is the denominator the threshold table needs: a gate can only be read
    against the number of signals it was choosing from, not just the trades it
    happened to open.
    """
    signals = 0
    accepted = 0
    skip_reasons: dict[str, int] = {}
    for run_dir in run_dirs:
        signal_path = run_dir / "signals.jsonl"
        if signal_path.exists():
            with signal_path.open() as handle:
                for line in handle:
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    signals += 1
                    if (row.get("decision") or {}).get("allowed"):
                        accepted += 1
        skip_path = run_dir / "skips.jsonl"
        if skip_path.exists():
            with skip_path.open() as handle:
                for line in handle:
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    reason = str(row.get("reason"))
                    skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
    return {
        "signals_received": signals,
        "signals_accepted": accepted,
        "skip_reasons": skip_reasons,
    }


def load_trades(run_dirs: Iterable[Path]) -> list[dict[str, Any]]:
    trades: list[dict[str, Any]] = []
    for run_dir in run_dirs:
        path = run_dir / "trades.jsonl"
        if not path.exists():
            continue
        with path.open() as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("entry_ts_ms") and row.get("mint"):
                    trades.append(row)
    return trades


def _exit_threshold_scaled_pnl(trade: dict[str, Any]) -> float | None:
    """PnL if the exit filled at ``otherAmountThreshold`` instead of ``outAmount``.

    The paper engine fills at the quote's optimistic ``outAmount`` and never
    applies the 50 bps tolerance. This scales the gross exit value down to the
    worst amount the transaction actually guarantees.
    """
    raw = trade.get("exit_quote_raw")
    if not raw:
        return None
    try:
        quote = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        return None
    if not isinstance(quote, dict):
        return None
    out = _parse_float(str(quote.get("outAmount")))
    threshold = _parse_float(str(quote.get("otherAmountThreshold")))
    if not out or not threshold or out <= 0 or threshold <= 0:
        return None
    exit_value = _parse_float(str(trade.get("exit_value_sol")))
    pnl = _parse_float(str(trade.get("pnl_sol")))
    fee = _parse_float(str(trade.get("fee")))
    if exit_value is None or pnl is None:
        return None
    scale = threshold / out
    # The exit fee is proportional to the gross exit value, so it scales too.
    if fee is None:
        fee = DEFAULT_FEE
    adjusted_exit = exit_value * scale * (1.0 - fee)
    original_net_exit = exit_value * (1.0 - fee)
    return pnl - (original_net_exit - adjusted_exit)


def join_quotes(
    quotes: list[PreflightQuote],
    trades: list[dict[str, Any]],
    *,
    tolerance_ms: int = JOIN_TOLERANCE_MS,
) -> tuple[list[JoinedTrade], int, int]:
    """Pair each preflight quote with its trade by mint and nearest entry time.

    Greedy over the closest pairs, one-to-one, so re-used mints (a token that
    appears in more than one run) cannot silently share a quote.
    """
    by_mint: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    for index, trade in enumerate(trades):
        by_mint.setdefault(str(trade["mint"]), []).append((index, trade))

    candidates: list[tuple[int, int, int, int]] = []
    for quote_index, quote in enumerate(quotes):
        for trade_index, trade in by_mint.get(quote.mint, ()):
            delta = abs(int(trade["entry_ts_ms"]) - quote.timestamp_ms)
            if delta <= tolerance_ms:
                candidates.append((delta, quote_index, trade_index, quote_index))
    candidates.sort()

    used_quotes: set[int] = set()
    used_trades: set[int] = set()
    joined: list[JoinedTrade] = []
    for _delta, quote_index, trade_index, _ in candidates:
        if quote_index in used_quotes or trade_index in used_trades:
            continue
        quote = quotes[quote_index]
        trade = trades[trade_index]
        used_quotes.add(quote_index)
        used_trades.add(trade_index)
        joined.append(
            JoinedTrade(
                ratio=quote.ratio,
                entry_ts_ms=int(trade["entry_ts_ms"]),
                mint=str(trade["mint"]),
                return_pct=_parse_float(str(trade.get("return_pct"))) or 0.0,
                pnl_sol=_parse_float(str(trade.get("pnl_sol"))) or 0.0,
                exit_value_sol=_parse_float(str(trade.get("exit_value_sol"))),
                pnl_at_threshold_sol=_exit_threshold_scaled_pnl(trade),
            )
        )
    joined.sort(key=lambda item: item.entry_ts_ms)
    unmatched_quotes = len(quotes) - len(used_quotes)
    unmatched_trades = len(trades) - len(used_trades)
    return joined, unmatched_quotes, unmatched_trades


def fee_floor_ratio(fee: float, slippage: float) -> float:
    """The ratio of a zero-move trade under the live fee/slippage model."""
    if not 0 <= fee < 1 or not 0 <= slippage < 1:
        raise ValueError("fee and slippage must be in [0, 1)")
    return (1.0 - slippage) * (1.0 - fee) / (1.0 + fee)


def depth_margin_bps(ratio: float, floor: float) -> float:
    """Ratio expressed relative to the fee floor, in basis points.

    ``0`` means the pool quotes exactly the cost-model loss; ``+50`` means the
    preflight sale would recover 0.5 % more than the entry cost (a real price
    gain at entry time); ``-50`` means a 0.5 % quoted drawdown.
    """
    if floor <= 0:
        raise ValueError("floor must be positive")
    return (ratio / floor - 1.0) * 10_000.0


def max_drawdown_sol(pnls: list[float]) -> float:
    peak = 0.0
    equity = 0.0
    worst = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        worst = min(worst, equity - peak)
    return worst


def _profit_factor(pnls: list[float]) -> float | None:
    gross_profit = sum(value for value in pnls if value > 0)
    gross_loss = sum(value for value in pnls if value < 0)
    if gross_loss == 0:
        return None
    return gross_profit / abs(gross_loss)


def evaluate(
    joined: list[JoinedTrade],
    thresholds: Iterable[float],
    *,
    baseline_ratio: float = 0.90,
) -> list[dict[str, Any]]:
    """What each threshold would have accepted, rejected, and kept out."""
    rows: list[dict[str, Any]] = []
    for threshold in sorted(set(thresholds)):
        accepted = [item for item in joined if item.ratio >= threshold]
        rejected = [item for item in joined if item.ratio < threshold]
        returns = [item.return_pct for item in accepted]
        pnls = [item.pnl_sol for item in accepted]
        catastrophic = [item for item in accepted if item.return_pct <= CATASTROPHIC_RETURN]
        rejected_catastrophic = [
            item for item in rejected if item.return_pct <= CATASTROPHIC_RETURN
        ]
        rejected_profitable = [item for item in rejected if item.return_pct > 0]
        threshold_pnls = [
            item.pnl_at_threshold_sol
            for item in accepted
            if item.pnl_at_threshold_sol is not None
        ]
        rows.append(
            {
                "threshold": threshold,
                "accepted": len(accepted),
                "rejected": len(rejected),
                "accepted_fraction": (
                    len(accepted) / len(joined) if joined else None
                ),
                "catastrophic": len(catastrophic),
                "catastrophic_rate": (
                    len(catastrophic) / len(accepted) if accepted else None
                ),
                "near_zero_loss_sol": -sum(item.pnl_sol for item in catastrophic),
                "pnl_sol": sum(pnls),
                "profit_factor": _profit_factor(pnls),
                "max_drawdown_sol": max_drawdown_sol(pnls),
                "win_rate": (
                    sum(1 for value in returns if value > 0) / len(returns)
                    if returns
                    else None
                ),
                "rejected_catastrophic": len(rejected_catastrophic),
                "rejected_profitable": len(rejected_profitable),
                "rejected_pnl_sol": sum(item.pnl_sol for item in rejected),
                "pnl_at_threshold_fill_sol": (
                    sum(threshold_pnls) if threshold_pnls else None
                ),
                "threshold_fill_n": len(threshold_pnls),
                "is_deployed_gate": abs(threshold - baseline_ratio) < 1e-12,
            }
        )
    return rows


def summarize_ratios(joined: list[JoinedTrade], floor: float) -> dict[str, Any]:
    values = sorted(item.ratio for item in joined)
    if not values:
        return {}
    return {
        "n": len(values),
        "min": values[0],
        "p10": values[int(0.10 * (len(values) - 1))],
        "median": statistics.median(values),
        "p90": values[int(0.90 * (len(values) - 1))],
        "max": values[-1],
        "floor": floor,
        "min_depth_bps": depth_margin_bps(values[0], floor),
        "median_depth_bps": depth_margin_bps(statistics.median(values), floor),
        "max_depth_bps": depth_margin_bps(values[-1], floor),
    }


def _fmt_pct(value: float | None) -> str:
    return "     -" if value is None else f"{value * 100:5.1f}%"


def _fmt_sol(value: float | None) -> str:
    return "       -" if value is None else f"{value:+8.3f}"


def format_table(
    rows: list[dict[str, Any]],
    *,
    floor: float,
    stats: dict[str, Any],
) -> str:
    lines = [
        f"preflight quotes joined : {stats.get('n', 0)}",
        f"fee floor               : {floor:.4f} "
        f"({stats.get('median_depth_bps', 0.0):+.0f} bps at the median quote)",
        f"observed ratio          : min {stats.get('min', 0):.4f} "
        f"median {stats.get('median', 0):.4f} max {stats.get('max', 0):.4f}",
        "",
        "threshold  depth  accepted  cat  cat%   near0_SOL    PnL_SOL    PF   maxDD"
        "  rej_cat  rej_win  rej_PnL  PnL@thr",
    ]
    for row in rows:
        name = f"{row['threshold']:.3f}"
        if row["is_deployed_gate"]:
            name += "*"
        lines.append(
            f"{name:>9} {depth_margin_bps(row['threshold'], floor):+6.0f} "
            f"{row['accepted']:8d} {row['catastrophic']:4d} "
            f"{_fmt_pct(row['catastrophic_rate'])} "
            f"{row['near_zero_loss_sol']:10.3f} {row['pnl_sol']:+10.3f} "
            f"{(row['profit_factor'] or 0):5.2f} {row['max_drawdown_sol']:+8.3f} "
            f"{row['rejected_catastrophic']:8d} {row['rejected_profitable']:8d} "
            f"{row['rejected_pnl_sol']:+8.3f} "
            f"{_fmt_sol(row['pnl_at_threshold_fill_sol'])}"
        )
    lines.append("")
    lines.append(
        "* deployed threshold. depth = ratio vs fee floor in bps "
        "(0 = zero-move; +50 = 0.5 % quoted gain at entry)."
    )
    lines.append(
        "PnL@thr prices every accepted exit at otherAmountThreshold "
        "(the worst fill the 50 bps tolerance guarantees)."
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--log",
        type=Path,
        action="append",
        default=None,
        help=f"Bot log to parse. Repeatable; default {DEFAULT_LOG}.",
    )
    parser.add_argument(
        "--run-dir",
        type=Path,
        action="append",
        default=None,
        help="Paper run directory. Repeatable; default every output/paper/live_*.",
    )
    parser.add_argument(
        "--thresholds",
        type=float,
        nargs="+",
        default=list(DEFAULT_THRESHOLDS),
    )
    parser.add_argument("--baseline", type=float, default=0.90)
    parser.add_argument("--fee", type=float, default=DEFAULT_FEE)
    parser.add_argument("--slippage", type=float, default=DEFAULT_SLIPPAGE)
    parser.add_argument("--tolerance-ms", type=int, default=JOIN_TOLERANCE_MS)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    logs = args.log or [DEFAULT_LOG]
    quotes: list[PreflightQuote] = []
    for path in logs:
        if path.exists():
            quotes.extend(parse_preflight_log(path))

    run_dirs = args.run_dir or sorted(
        Path(path) for path in glob.glob("output/paper/live_*")
    )
    trades = load_trades(run_dirs)
    funnel = load_funnel(run_dirs)
    joined, unmatched_quotes, _unmatched_trades = join_quotes(
        quotes, trades, tolerance_ms=args.tolerance_ms
    )
    floor = fee_floor_ratio(args.fee, args.slippage)
    rows = evaluate(joined, args.thresholds, baseline_ratio=args.baseline)
    stats = summarize_ratios(joined, floor)

    if args.json:
        print(
            json.dumps(
                {
                    "fee_floor": floor,
                    "ratio_stats": stats,
                    "quotes": len(quotes),
                    "trades": len(trades),
                    "funnel": funnel,
                    "joined": len(joined),
                    "unmatched_quotes": unmatched_quotes,
                    "thresholds": rows,
                },
                indent=2,
            )
        )
        return 0

    print(
        f"log quotes={len(quotes)} trades={len(trades)} joined={len(joined)} "
        f"unmatched_quotes={unmatched_quotes}"
    )
    if funnel["signals_received"]:
        skips = funnel["skip_reasons"]
        print(
            f"funnel: signals={funnel['signals_received']} "
            f"accepted={funnel['signals_accepted']} "
            f"preflight_econ={skips.get('entry_no_exit_economics', 0)} "
            f"preflight_route={skips.get('entry_no_exit_route', 0)}"
        )
    if not joined:
        print("no preflight quotes matched a trade")
        return 0
    print(format_table(rows, floor=floor, stats=stats))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
