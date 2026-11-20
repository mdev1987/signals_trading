#!/usr/bin/env python3
"""Reconcile candidate orders with the Pool Selection Timeline and execution fills.

The output is an order-level audit for every BUY and SELL candidate. It reports
the routing metadata from the timeline, the simulator status, and a concrete
reason for every non-fill. All non-fill statuses, not only ``unsupported_pool``,
must be included when reconciling candidate counts.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd

from execution_simulator import run_orders

AUDIT_COLUMNS = [
    "order_id",
    "side",
    "timestamp_ms",
    "mint",
    "pool_id",
    "pool",
    "quote_mint",
    "reserve_model",
    "execution_supported",
    "pool_selection_status",
    "amount",
    "status",
    "reason",
    "bar_start_timestamp_ms",
    "bar_end_timestamp_ms",
    "available_at_timestamp_ms",
]

CANDIDATE_COLUMNS = {
    "mint",
    "bar_start_timestamp_ms",
    "bar_end_timestamp_ms",
    "pool_id",
    "pool",
    "quote_mint",
    "buy_candidate",
    "sell_candidate",
}

TIMELINE_COLUMNS = {
    "mint",
    "timestamp_ms",
    "bar_end_timestamp_ms",
    "available_at_timestamp_ms",
    "pool_id",
    "pool",
    "quote_mint",
    "reserve_model",
    "execution_supported",
    "pool_selection_status",
}

RESERVE_MODEL_REASONS = {
    "missing_amm_reserves",
    "ambiguous_virtual_reserves",
    "missing_bonding_curve_reserves",
}


def _require_columns(frame: pd.DataFrame, required: set[str], label: str) -> None:
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"{label} is missing required columns: {missing}")


def load_candidate_inputs(
    candidates_path: Path,
    timeline_path: Path,
) -> pd.DataFrame:
    """Join candidate rows to the timeline and validate routing metadata."""
    candidates = pd.read_csv(candidates_path)
    timeline = pd.read_csv(timeline_path)
    _require_columns(candidates, CANDIDATE_COLUMNS, "Candidate CSV")
    _require_columns(timeline, TIMELINE_COLUMNS, "Pool Timeline CSV")

    candidates = candidates.copy()
    candidates["order_id"] = "c" + candidates.index.astype(str)

    right = timeline[
        [
            "mint",
            "timestamp_ms",
            "bar_end_timestamp_ms",
            "available_at_timestamp_ms",
            "pool_id",
            "pool",
            "quote_mint",
            "reserve_model",
            "execution_supported",
            "pool_selection_status",
        ]
    ].rename(
        columns={
            "timestamp_ms": "bar_start_timestamp_ms",
            "bar_end_timestamp_ms": "timeline_bar_end_timestamp_ms",
            "available_at_timestamp_ms": "timeline_available_at_timestamp_ms",
            "pool_id": "timeline_pool_id",
            "pool": "timeline_pool",
            "quote_mint": "timeline_quote_mint",
        }
    )

    joined = candidates.merge(
        right,
        on=["mint", "bar_start_timestamp_ms"],
        how="left",
        validate="one_to_one",
        indicator=True,
    )
    missing_timeline = int((joined["_merge"] != "both").sum())
    if missing_timeline:
        raise ValueError(f"{missing_timeline} candidate rows have no Pool Timeline row")

    if "order_timestamp_ms" in joined.columns:
        bad_order_time = int(
            (joined["order_timestamp_ms"] != joined["bar_end_timestamp_ms"]).sum()
        )
        if bad_order_time:
            raise ValueError(
                f"{bad_order_time} candidate rows have order_timestamp_ms != "
                "bar_end_timestamp_ms"
            )

    bad_timeline_bar_end = int(
        (
            joined["timeline_bar_end_timestamp_ms"]
            != joined["bar_end_timestamp_ms"]
        ).sum()
    )
    if bad_timeline_bar_end:
        raise ValueError(
            f"{bad_timeline_bar_end} timeline rows disagree on the candle close"
        )

    bad_available = int(
        (
            joined["timeline_available_at_timestamp_ms"]
            != joined["bar_end_timestamp_ms"]
        ).sum()
    )
    if bad_available:
        raise ValueError(
            f"{bad_available} timeline rows were not available at the candle close"
        )

    for candidate_column, timeline_column in (
        ("pool_id", "timeline_pool_id"),
        ("pool", "timeline_pool"),
        ("quote_mint", "timeline_quote_mint"),
    ):
        mismatch = int(
            (
                joined[candidate_column].astype(str)
                != joined[timeline_column].astype(str)
            ).sum()
        )
        if mismatch:
            raise ValueError(
                f"{mismatch} candidate/timeline rows disagree on {candidate_column}"
            )

    joined = joined.drop(
        columns=[
            "_merge",
            "timeline_pool_id",
            "timeline_pool",
            "timeline_quote_mint",
        ]
    )
    joined = joined.rename(
        columns={"timeline_available_at_timestamp_ms": "available_at_timestamp_ms"}
    )
    return joined


def build_orders(
    joined: pd.DataFrame,
    *,
    side: str,
    flag: str,
    amount: float,
) -> pd.DataFrame:
    selected = joined[joined[flag] == 1]
    orders = selected[
        ["order_id", "bar_end_timestamp_ms", "mint", "pool_id"]
    ].rename(columns={"bar_end_timestamp_ms": "timestamp_ms"})
    orders["side"] = side
    orders["amount"] = amount
    return orders.sort_values("timestamp_ms", kind="stable").reset_index(drop=True)


def audit_side(
    joined: pd.DataFrame,
    *,
    events_path: Path,
    work_dir: Path,
    side: str,
    flag: str,
    amount: float,
    max_state_age_ms: int | None,
    max_price_impact_bps: float | None,
) -> pd.DataFrame:
    selected = joined[joined[flag] == 1].copy()
    orders = build_orders(joined, side=side, flag=flag, amount=amount)

    orders_path = work_dir / f"candidate_orders_{side}.csv"
    fills_path = work_dir / f"candidate_fills_{side}.csv"
    orders.to_csv(orders_path, index=False)

    run_orders(
        events_path,
        orders_path,
        fills_path,
        max_state_age_ms=max_state_age_ms,
        max_price_impact_bps=max_price_impact_bps,
    )
    fills = pd.read_csv(fills_path)

    metadata = selected[
        [
            "order_id",
            "quote_mint",
            "pool",
            "reserve_model",
            "bar_start_timestamp_ms",
            "bar_end_timestamp_ms",
            "available_at_timestamp_ms",
            "execution_supported",
            "pool_selection_status",
        ]
    ].rename(
        columns={
            "pool": "timeline_pool",
            "reserve_model": "timeline_reserve_model",
        }
    )

    audit = fills.merge(metadata, on="order_id", how="left", validate="one_to_one")
    if len(audit) != len(selected):
        raise ValueError(
            f"{side} audit lost rows: {len(selected)} candidates, {len(audit)} fills"
        )
    if audit["execution_supported"].isna().any():
        raise ValueError(f"{side} audit has candidates without timeline metadata")

    # The timeline is the routing authority. The simulator can leave pool /
    # reserve_model empty when a rejected order has no usable state.
    audit["pool"] = audit["timeline_pool"]
    audit["reserve_model"] = audit["timeline_reserve_model"]
    return audit.drop(columns=["timeline_pool", "timeline_reserve_model"])


def summarize_side(audit: pd.DataFrame, side: str) -> dict[str, Any]:
    rows = audit[audit["side"] == side]
    supported = rows[rows["execution_supported"] == 1]
    filled = rows[rows["status"] == "filled"]
    unsupported = rows[rows["execution_supported"] == 0]
    supported_not_filled = supported[supported["status"] != "filled"]

    status_reason = rows.groupby(["status", "reason"], dropna=False).size()
    status_reason_counts = {
        f"{status}|{'' if pd.isna(reason) else reason}": int(count)
        for (status, reason), count in status_reason.items()
    }

    filled_on_unsupported = int(
        ((rows["status"] == "filled") & (rows["execution_supported"] == 0)).sum()
    )
    reserve_model_rejects_on_supported = int(
        (
            supported["reason"].isin(RESERVE_MODEL_REASONS)
            & (supported["status"] != "filled")
        ).sum()
    )

    accounted_total = len(filled) + len(unsupported) + len(supported_not_filled)
    reconciled = accounted_total == len(rows)
    routing_invariant = (
        filled_on_unsupported == 0 and reserve_model_rejects_on_supported == 0
    )

    return {
        "candidates": int(len(rows)),
        "execution_supported": int(len(supported)),
        "execution_supported_not_filled": int(len(supported_not_filled)),
        "execution_unsupported": int(len(unsupported)),
        "filled": int(len(filled)),
        "filled_on_unsupported": filled_on_unsupported,
        "reserve_model_rejects_on_supported": reserve_model_rejects_on_supported,
        "status_reason_counts": status_reason_counts,
        "reconciliation": {
            "filled": int(len(filled)),
            "execution_unsupported": int(len(unsupported)),
            "supported_not_filled": int(len(supported_not_filled)),
            "total": int(len(rows)),
            "accounted_total": int(accounted_total),
            "reconciled": reconciled,
        },
        "routing_invariant": routing_invariant,
    }


def build_audit(
    *,
    candidates_path: Path,
    timeline_path: Path,
    events_path: Path,
    output_dir: Path,
    buy_amount_sol: float = 0.0001,
    sell_amount_tokens: float = 100.0,
    max_state_age_ms: int | None = 60_000,
    max_price_impact_bps: float | None = None,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    joined = load_candidate_inputs(candidates_path, timeline_path)

    frames = []
    for side, flag, amount in (
        ("buy", "buy_candidate", buy_amount_sol),
        ("sell", "sell_candidate", sell_amount_tokens),
    ):
        frames.append(
            audit_side(
                joined,
                events_path=events_path,
                work_dir=output_dir,
                side=side,
                flag=flag,
                amount=amount,
                max_state_age_ms=max_state_age_ms,
                max_price_impact_bps=max_price_impact_bps,
            )
        )

    audit = pd.concat(frames, ignore_index=True)
    audit = audit[AUDIT_COLUMNS].sort_values(
        ["side", "timestamp_ms", "order_id"]
    ).reset_index(drop=True)

    audit_path = output_dir / "candidate_execution_audit.csv"
    audit.to_csv(audit_path, index=False)

    insufficient_liquidity = audit[
        (audit["side"] == "sell") & (audit["status"] == "insufficient_liquidity")
    ]
    insufficient_liquidity_path = (
        output_dir / "sell_insufficient_liquidity.csv"
    )
    insufficient_liquidity.to_csv(insufficient_liquidity_path, index=False)

    buy_summary = summarize_side(audit, "buy")
    sell_summary = summarize_side(audit, "sell")
    checks_passed = all(
        side_summary["reconciliation"]["reconciled"]
        and side_summary["routing_invariant"]
        for side_summary in (buy_summary, sell_summary)
    )
    summary = {
        "candidates_csv": str(candidates_path),
        "pool_timeline_csv": str(timeline_path),
        "events": str(events_path),
        "order_sizing": {
            "buy_sol": buy_amount_sol,
            "sell_tokens": sell_amount_tokens,
        },
        "max_state_age_ms": max_state_age_ms,
        "max_price_impact_bps": max_price_impact_bps,
        "simulation_note": (
            "The BUY and SELL candidate sets are replayed in separate simulator "
            "runs, matching the original candidate integration. A production "
            "backtest should replay both sides together in order-time order."
        ),
        "checks_passed": checks_passed,
        "buy": buy_summary,
        "sell": sell_summary,
        "audit_csv": str(audit_path),
        "sell_insufficient_liquidity_csv": str(insufficient_liquidity_path),
    }
    summary_path = output_dir / "candidate_execution_audit_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    summary["summary_json"] = str(summary_path)
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Emit a per-order audit for candidate BUY/SELL orders and reconcile "
            "fills, unsupported rows, and all order-time rejects."
        )
    )
    parser.add_argument(
        "--candidates",
        type=Path,
        default=Path("pool_compatibility_candidates_v7_1_1.csv"),
        help="Candidate CSV with buy_candidate / sell_candidate flags.",
    )
    parser.add_argument(
        "--timeline",
        type=Path,
        default=Path("output/vbt_dataset_pit_v7_2_1m/pool_selection_timeline.csv"),
        help="Pool Selection Timeline CSV.",
    )
    parser.add_argument(
        "--events",
        type=Path,
        default=Path("05.jsonl.zst"),
        help="Raw PumpAPI JSONL or Zstandard JSONL file.",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        default=Path("output/execution_audit"),
        help="Directory for the audit CSV, fills, and summary JSON.",
    )
    parser.add_argument(
        "--buy-amount-sol",
        type=float,
        default=0.0001,
        help="Gross SOL budget for each BUY candidate.",
    )
    parser.add_argument(
        "--sell-amount-tokens",
        type=float,
        default=100.0,
        help="Token amount offered for each SELL candidate.",
    )
    parser.add_argument(
        "--max-state-age-ms",
        type=int,
        default=60_000,
        help="Reject snapshots older than this value; use 0 to disable.",
    )
    parser.add_argument(
        "--max-price-impact-bps",
        type=float,
        default=None,
        help="Optional maximum curve price impact in basis points.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    max_state_age_ms = None if args.max_state_age_ms == 0 else args.max_state_age_ms
    if max_state_age_ms is not None and max_state_age_ms < 0:
        raise SystemExit("--max-state-age-ms must be >= 0")
    if args.buy_amount_sol <= 0:
        raise SystemExit("--buy-amount-sol must be > 0")
    if args.sell_amount_tokens <= 0:
        raise SystemExit("--sell-amount-tokens must be > 0")
    if args.max_price_impact_bps is not None and args.max_price_impact_bps < 0:
        raise SystemExit("--max-price-impact-bps must be >= 0")

    summary = build_audit(
        candidates_path=args.candidates,
        timeline_path=args.timeline,
        events_path=args.events,
        output_dir=args.output_dir,
        buy_amount_sol=args.buy_amount_sol,
        sell_amount_tokens=args.sell_amount_tokens,
        max_state_age_ms=max_state_age_ms,
        max_price_impact_bps=args.max_price_impact_bps,
    )
    print(json.dumps(summary, indent=2))
    return 0 if summary["checks_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
