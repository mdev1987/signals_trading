#!/usr/bin/env python3
"""Decompose the paper-execution round-trip cost without double-counting.

The paper engine fills at the Jupiter quote's ``outAmount`` and then subtracts
its own ``--fee`` (1.25 % per side). The quote already contains:

* the platform fee (``feeBps`` / ``platformFee.feeBps``),
* the swap price impact (``priceImpact``),
* the route's pool/provider fee (inside ``outAmount``).

So those are *embedded in the fill price* and must not be subtracted again. The
quote also exposes ``otherAmountThreshold``: the worst fill the 50 bps
slippage tolerance permits. The paper engine does **not** apply that haircut,
so the recorded PnL assumes a better fill than the transaction guarantees.

This tool reports, per trade and in aggregate:

===== ============================================================
paper      quote fill + the engine's 1.25 %/side fee (what ``pnl_sol`` is)
threshold  paper with each fill moved to ``otherAmountThreshold``
live       threshold + priority/Jito/signature fees (ATA rent reported
           separately because it is recoverable, not spent)
===== ============================================================

It never changes a strategy field: it is a measurement over existing
``entries.jsonl`` / ``trades.jsonl`` records.

Examples::

    env PYTHONPATH=src .venv/bin/python src/cost_audit.py
    env PYTHONPATH=src .venv/bin/python src/cost_audit.py --json
    env PYTHONPATH=src .venv/bin/python src/cost_audit.py \\
        --run-dir output/paper/live_20261124_045122
"""

from __future__ import annotations

import argparse
import glob
import json
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

LAMPORTS_PER_SOL = 1_000_000_000
# A token account is ~0.00203928 SOL of rent. The executor does not close it, so
# the rent is locked capital after the trade, not a fee. Report it separately.
DEFAULT_ATA_RENT_SOL = 0.00203928
# oxfile.toml live settings: 2,000 priority lamports + 10,000 Jito tip per tx.
DEFAULT_LIVE_TX_LAMPORTS = 12_000
BASIS_POINTS = 10_000.0


@dataclass(frozen=True)
class QuoteCosts:
    """Costs visible in one Jupiter quote and whether they are applied."""

    platform_fee_bps: float
    price_impact_bps: float
    slippage_bps: float
    threshold_slippage_bps: float
    priority_fee_sol: float
    rent_sol: float
    signature_sol: float


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_quote_costs(raw: Any) -> QuoteCosts:
    """Extract one quote's cost fields.

    ``feeBps`` and ``platformFee.feeBps`` are the same charge; prefer the
    explicit platform value and fall back, never sum them. ``priceImpact`` is
    already a percentage (``priceImpactPct`` is the same value as a fraction).
    """
    if not raw:
        return QuoteCosts(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    quote = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
    if not isinstance(quote, dict):
        return QuoteCosts(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

    platform = quote.get("platformFee")
    platform_bps = _float(platform.get("feeBps")) if isinstance(platform, dict) else 0.0
    if platform_bps <= 0:
        platform_bps = _float(quote.get("feeBps"))

    impact = quote.get("priceImpact")
    if impact is None:
        impact = _float(quote.get("priceImpactPct")) * 100.0
    # A negative priceImpact is a cost; report it as a positive number.
    price_impact_bps = -_float(impact) * 100.0

    slippage_bps = _float(quote.get("slippageBps"))
    out_amount = _float(quote.get("outAmount"))
    threshold = _float(quote.get("otherAmountThreshold"))
    threshold_slippage_bps = (
        max(0.0, (1.0 - threshold / out_amount) * BASIS_POINTS)
        if out_amount > 0 and threshold > 0
        else slippage_bps
    )

    return QuoteCosts(
        platform_fee_bps=platform_bps,
        price_impact_bps=price_impact_bps,
        slippage_bps=slippage_bps,
        threshold_slippage_bps=threshold_slippage_bps,
        priority_fee_sol=_float(quote.get("prioritizationFeeLamports")) / LAMPORTS_PER_SOL,
        rent_sol=_float(quote.get("rentFeeLamports")) / LAMPORTS_PER_SOL,
        signature_sol=_float(quote.get("signatureFeeLamports")) / LAMPORTS_PER_SOL,
    )


def decompose_trade(
    trade: dict[str, Any],
    *,
    reference_deviation_pct: float | None = None,
    live_tx_lamports: int = DEFAULT_LIVE_TX_LAMPORTS,
    ata_rent_sol: float = DEFAULT_ATA_RENT_SOL,
) -> dict[str, Any]:
    """One trade's cost components and PnL under the three conventions.

    The quote-embedded costs are reported but never subtracted from ``pnl_sol``
    again: the fill price already contains them. Only the costs the paper fill
    did not apply (slippage tolerance, live transaction fees) move the PnL.
    """
    notional = _float(trade.get("entry_value_sol"))
    entry = parse_quote_costs(trade.get("entry_quote_raw"))
    exit_ = parse_quote_costs(trade.get("exit_quote_raw"))
    entry_fee = _float(trade.get("entry_fee_sol"))
    exit_fee = _float(trade.get("exit_fee_sol"))
    pnl = _float(trade.get("pnl_sol"))

    quote_embedded_bps = (
        entry.platform_fee_bps
        + exit_.platform_fee_bps
        + entry.price_impact_bps
        + exit_.price_impact_bps
    )
    broker_fee_bps = (
        (entry_fee + exit_fee) / notional * BASIS_POINTS if notional > 0 else 0.0
    )
    live_tx_sol = (
        entry.priority_fee_sol
        + exit_.priority_fee_sol
        + entry.rent_sol
        + exit_.rent_sol
        + entry.signature_sol
        + exit_.signature_sol
    )
    live_tx_bps = live_tx_sol / notional * BASIS_POINTS if notional > 0 else 0.0
    quote_slip_bps = entry.threshold_slippage_bps + exit_.threshold_slippage_bps

    paper_cost_bps = quote_embedded_bps + broker_fee_bps + live_tx_bps
    threshold_cost_bps = paper_cost_bps + quote_slip_bps

    slip_sol = notional * quote_slip_bps / BASIS_POINTS
    # Use the configured live fee when the paper quote reports zero lamports.
    configured_live_sol = (
        2 * live_tx_lamports / LAMPORTS_PER_SOL if live_tx_sol == 0 else live_tx_sol
    )

    return {
        "mint": trade.get("mint"),
        "entry_ts_ms": trade.get("entry_ts_ms"),
        # Only rows with both quotes can be decomposed; the aggregate must not
        # average the missing ones in as zeros.
        "has_quotes": bool(trade.get("entry_quote_raw"))
        and bool(trade.get("exit_quote_raw")),
        "notional_sol": notional,
        "entry_value_sol": notional,
        "exit_value_sol": _float(trade.get("exit_value_sol")),
        "return_pct": _float(trade.get("return_pct")),
        "quote_embedded_bps": quote_embedded_bps,
        "platform_fee_bps": entry.platform_fee_bps + exit_.platform_fee_bps,
        "price_impact_bps": entry.price_impact_bps + exit_.price_impact_bps,
        "broker_fee_bps": broker_fee_bps,
        "broker_fee_sol": entry_fee + exit_fee,
        "gross_sol": pnl + entry_fee + exit_fee,
        "quote_slippage_bps": quote_slip_bps,
        "live_tx_bps": live_tx_bps,
        "threshold_cost_bps": threshold_cost_bps,
        "reference_deviation_bps": (
            reference_deviation_pct * 100.0
            if reference_deviation_pct is not None
            else None
        ),
        "pnl_paper_sol": pnl,
        "pnl_threshold_sol": pnl - slip_sol,
        "pnl_live_sol": pnl - slip_sol - configured_live_sol,
        "ata_rent_locked_sol": ata_rent_sol,
    }


def _median(values: Iterable[float]) -> float | None:
    values = [value for value in values if value is not None]
    return statistics.median(values) if values else None


# Per-side fee assumptions for the sensitivity table. 1.25 % is the deployed
# conservative buffer; the explicit Jupiter platform fee is 0.1 %. The true
# all-in swap fee must be measured on a funded fill, never assumed downward.
FEE_SENSITIVITY = (0.0125, 0.0100, 0.0075, 0.0050, 0.0030)


def fee_sensitivity(
    rows: list[dict[str, Any]], fees: Iterable[float] = FEE_SENSITIVITY
) -> list[dict[str, float]]:
    """Net PnL if the recorded fills were charged a different per-side fee.

    The quoted fill already contains the pool fee and price impact, so this
    isolates the broker's percentage overlay. It is a sensitivity, not an
    estimate of the real fee.
    """
    gross = sum(row["gross_sol"] for row in rows)
    traded = sum(row["entry_value_sol"] + row["exit_value_sol"] for row in rows)
    return [
        {"fee_per_side": fee, "pnl_sol": gross - traded * fee}
        for fee in fees
    ]


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate cost components and the three PnL conventions.

    Quote-embedded components are averaged only over trades that recorded both
    quotes. Averaging missing quotes in as zero halves the apparent cost and
    makes the fee look smaller than it is.
    """
    if not rows:
        return {"n": 0}
    notional = sum(row["notional_sol"] for row in rows)
    paper = sum(row["pnl_paper_sol"] for row in rows)
    quoted = [row for row in rows if row["has_quotes"]]
    quoted_paper = sum(row["pnl_paper_sol"] for row in quoted)
    quoted_threshold = sum(row["pnl_threshold_sol"] for row in quoted)
    quoted_live = sum(row["pnl_live_sol"] for row in quoted)
    summary: dict[str, Any] = {
        "n": len(rows),
        "notional_sol": notional,
        "quoted_n": len(quoted),
        "quote_coverage": len(quoted) / len(rows),
        "broker_fee_bps_mean": statistics.mean(row["broker_fee_bps"] for row in rows),
        "reference_deviation_bps_median": _median(
            row["reference_deviation_bps"] for row in rows
        ),
        "pnl_paper_sol": paper,
        "quoted_pnl_paper_sol": quoted_paper,
        "quoted_pnl_threshold_sol": quoted_threshold,
        "quoted_pnl_live_sol": quoted_live,
        "threshold_delta_sol": quoted_threshold - quoted_paper,
        "live_delta_sol": quoted_live - quoted_threshold,
        "one_point_cost_sol": notional * 0.01,
        "ata_rent_locked_sol": sum(row["ata_rent_locked_sol"] for row in rows),
        # Gross edge must be read separately from the fee overlay: the quote
        # fill already contains the pool fee and price impact, so the broker
        # percentage is the only *assumed* cost on top of it.
        "broker_fee_sol_total": sum(row["broker_fee_sol"] for row in rows),
        "fee_sensitivity": fee_sensitivity(rows),
    }
    if quoted:
        summary.update(
            {
                "quote_embedded_bps_mean": statistics.mean(
                    row["quote_embedded_bps"] for row in quoted
                ),
                "platform_fee_bps_mean": statistics.mean(
                    row["platform_fee_bps"] for row in quoted
                ),
                "price_impact_bps_mean": statistics.mean(
                    row["price_impact_bps"] for row in quoted
                ),
                "quote_embedded_bps_median": _median(
                    row["quote_embedded_bps"] for row in quoted
                ),
                "price_impact_bps_median": _median(
                    row["price_impact_bps"] for row in quoted
                ),
                "quote_slippage_bps_median": _median(
                    row["quote_slippage_bps"] for row in quoted
                ),
                "threshold_cost_bps_mean": statistics.mean(
                    row["threshold_cost_bps"] for row in quoted
                ),
            }
        )
        normal = [row for row in quoted if row["return_pct"] > -0.9]
        summary.update(
            {
                "normal_n": len(normal),
                "normal_price_impact_bps_median": _median(
                    row["price_impact_bps"] for row in normal
                ),
                "normal_quote_embedded_bps_median": _median(
                    row["quote_embedded_bps"] for row in normal
                ),
            }
        )
    else:
        summary.update(
            {
                "quote_embedded_bps_mean": None,
                "quote_embedded_bps_median": None,
                "price_impact_bps_median": None,
                "platform_fee_bps_mean": None,
                "price_impact_bps_mean": None,
                "quote_slippage_bps_median": None,
                "threshold_cost_bps_mean": None,
                "normal_n": 0,
                "normal_price_impact_bps_median": None,
                "normal_quote_embedded_bps_median": None,
            }
        )
    return summary


def load_rows(paths: list[Path], *, ata_rent_sol: float, live_tx_lamports: int) -> list[dict]:
    """Join trades with their entry on (mint, message_id) for the deviation."""
    rows = []
    for run_dir in paths:
        entries: dict[tuple[str, Any], dict] = {}
        entry_path = run_dir / "entries.jsonl"
        if entry_path.exists():
            for line in entry_path.open():
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                entries[(entry.get("mint"), entry.get("message_id"))] = entry
        trade_path = run_dir / "trades.jsonl"
        if not trade_path.exists():
            continue
        for line in trade_path.open():
            try:
                trade = json.loads(line)
            except json.JSONDecodeError:
                continue
            entry = entries.get((trade.get("mint"), trade.get("message_id")))
            deviation = (
                _float(entry.get("entry_reference_deviation_pct"))
                if entry and entry.get("entry_reference_deviation_pct") is not None
                else None
            )
            row = decompose_trade(
                trade,
                reference_deviation_pct=deviation,
                ata_rent_sol=ata_rent_sol,
                live_tx_lamports=live_tx_lamports,
            )
            row["run_dir"] = run_dir.name
            rows.append(row)
    return rows


def _fmt_bps(value: float | None) -> str:
    return "       -" if value is None else f"{value:8.2f}"


def format_report(summary: dict[str, Any]) -> str:
    if not summary["n"]:
        return "no trades"
    lines = [
        f"trades              : {summary['n']}",
        f"notional            : {summary['notional_sol']:.3f} SOL",
        f"quote coverage      : {summary['quoted_n']}/{summary['n']} "
        f"({summary['quote_coverage'] * 100:.1f} %) have both quotes",
        "",
        "cost components (bps of entry notional, mean per trade with quotes)",
        f"  quote platform fee : {_fmt_bps(summary['platform_fee_bps_mean'])}",
        f"  quote price impact : {_fmt_bps(summary['price_impact_bps_mean'])}"
        f"  (median {_fmt_bps(summary['price_impact_bps_median']).strip()})",
        f"  quote embedded     : {_fmt_bps(summary['quote_embedded_bps_mean'])}"
        "  (already in the fill price)",
        f"  broker fee 1.25%/s : {summary['broker_fee_bps_mean']:8.2f}  (applied on top)",
        f"  slippage tolerance : {_fmt_bps(summary['quote_slippage_bps_median'])}"
        "  (median, NOT applied by paper)",
        f"  total drag vs mid  : "
        f"{(summary['quote_embedded_bps_mean'] or 0) + summary['broker_fee_bps_mean']:8.2f}"
        "  (round trip; embedded part already in PnL)",
        f"  total @ threshold  : {_fmt_bps(summary['threshold_cost_bps_mean'])}",
        f"  non-tail only      : impact median "
        f"{_fmt_bps(summary['normal_price_impact_bps_median']).strip()} bps, "
        f"embedded median "
        f"{_fmt_bps(summary['normal_quote_embedded_bps_median']).strip()} bps "
        f"(n={summary['normal_n']})",
        "",
        "PnL under fill conventions (SOL)",
        f"  paper (recorded)   : {summary['pnl_paper_sol']:+10.4f}  (n={summary['n']})",
        f"  paper, quoted only : {summary['quoted_pnl_paper_sol']:+10.4f}  "
        f"(n={summary['quoted_n']})",
        f"  threshold fill     : {summary['quoted_pnl_threshold_sol']:+10.4f}  "
        f"({summary['threshold_delta_sol']:+.4f})",
        f"  live tx fees       : {summary['quoted_pnl_live_sol']:+10.4f}  "
        f"({summary['live_delta_sol']:+.4f})",
        f"  1 pp cost at this n: {summary['one_point_cost_sol']:.4f} SOL",
        "",
        "edge decomposition (SOL, recorded fills)",
        f"  gross of broker fee: "
        f"{summary['pnl_paper_sol'] + summary['broker_fee_sol_total']:+10.4f}",
        f"  broker fee overlay : {-summary['broker_fee_sol_total']:+10.4f}",
        f"  recorded net       : {summary['pnl_paper_sol']:+10.4f}",
        "",
        "fee sensitivity (SOL, quoted fills held fixed; sensitivity, not an estimate)",
    ]
    for band in summary.get("fee_sensitivity", []):
        lines.append(
            f"  {band['fee_per_side'] * 100:5.2f} %/side : {band['pnl_sol']:+9.4f}"
        )
    lines.append(
        f"  ATA rent locked    : {summary['ata_rent_locked_sol']:.4f} SOL "
        "(recoverable on close)"
    )
    if summary["reference_deviation_bps_median"] is not None:
        lines.append(
            f"  realized vs reference: {summary['reference_deviation_bps_median']:+.2f} bps (median)"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run-dir",
        type=Path,
        action="append",
        default=None,
        help="A paper run directory. Repeatable; default: every output/paper/live_*.",
    )
    parser.add_argument("--json", action="store_true", help="Print the per-trade rows as JSON.")
    parser.add_argument(
        "--ata-rent-sol", type=float, default=DEFAULT_ATA_RENT_SOL
    )
    parser.add_argument(
        "--live-tx-lamports", type=int, default=DEFAULT_LIVE_TX_LAMPORTS
    )
    args = parser.parse_args(argv)

    if args.run_dir:
        paths = sorted(args.run_dir)
    else:
        paths = sorted(Path(path) for path in glob.glob("output/paper/live_*"))

    rows = load_rows(
        paths,
        ata_rent_sol=args.ata_rent_sol,
        live_tx_lamports=args.live_tx_lamports,
    )
    if args.json:
        print(json.dumps(rows, indent=2, default=str))
        return 0
    print(format_report(summarize(rows)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
