#!/usr/bin/env python3
"""Validate the filtered replay and multi-hour OHLCV backtest dataset.

Checks the properties the backtest depends on:

* every signaled mint is present in the OHLCV dataset
* ``(mint, timestamp_ms)`` is unique and aligned to the timeframe grid
* every signal has at least one bar at or after its timestamp
* the real/filled bar split is reported per pool

With ``--reference`` it also re-derives the OHLCV for an overlapping window from
an earlier single-period dataset and verifies that only the columns which
legitimately depend on dataset scope differ:

* ``*event_index``  - sequential counter over the input row set
* ``age_bars``, ``prev_close*`` - depend on how much history precedes the window
* ``routed_trades`` - counts events per transaction signature across the whole
  loaded universe, so it changes when the mint universe is filtered

Any other mismatch fails the verification.

Example:
    python src/verify_backtest_dataset.py \\
        --ohlcv output/backtest/ohlcv/ohlcv_1m.csv \\
        --signals output/backtest/signals/strategy_signals.csv \\
        --reference output/ohlcv_clean_v7_1_1/ohlcv_1m.csv \\
        --reference-window 2026-11-08T05:00/2026-11-08T06:00 \\
        --output output/backtest/ohlcv/dataset_verification.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

# Columns whose values legitimately depend on the dataset scope or on how much
# history precedes the comparison window.
SCOPE_DEPENDENT_PREFIXES = ("prev_close",)
SCOPE_DEPENDENT_COLUMNS = {
    "age_bars",
    "routed_trades",
    # Derived from prev_close / prev_close_ts_ms, so they change when a bar has
    # no predecessor inside the loaded window.
    "price_gap_elapsed_ms",
    "price_discontinuity_ratio",
}
SCOPE_DEPENDENT_SUFFIXES = ("event_index",)


def timeframe_ms_from_label(label: str) -> int:
    """Convert ``1m`` / ``5m`` / ``15s`` / ``1h`` into milliseconds."""
    units = {"s": 1_000, "m": 60_000, "h": 3_600_000}
    text = label.strip().lower()
    if len(text) < 2 or text[-1] not in units or not text[:-1].isdigit():
        raise ValueError(f"Invalid timeframe: {label!r}")
    return int(text[:-1]) * units[text[-1]]


def _parse_window(value: str) -> tuple[pd.Timestamp, pd.Timestamp]:
    start_text, end_text = value.split("/")

    def to_utc(text: str) -> pd.Timestamp:
        stamp = pd.Timestamp(text)
        if stamp.tzinfo is None:
            return stamp.tz_localize("UTC")
        return stamp.tz_convert("UTC")

    start = to_utc(start_text)
    end = to_utc(end_text)
    if end <= start:
        raise argparse.ArgumentTypeError(f"Invalid window: {value}")
    return start, end


def _is_scope_dependent(column: str) -> bool:
    if column in SCOPE_DEPENDENT_COLUMNS:
        return True
    if column.startswith(SCOPE_DEPENDENT_PREFIXES):
        return True
    return column.endswith(SCOPE_DEPENDENT_SUFFIXES)


def _values_equal(left: pd.Series, right: pd.Series) -> int:
    """Return the number of rows that differ, comparing strings and numbers safely."""
    if (
        left.dtype == object
        or right.dtype == object
        or pd.api.types.is_string_dtype(left)
        or pd.api.types.is_string_dtype(right)
    ):
        mismatch = ~(
            (left.astype(str) == right.astype(str)) | (left.isna() & right.isna())
        )
        return int(mismatch.sum())
    left_numeric = pd.to_numeric(left, errors="coerce").astype("float64")
    right_numeric = pd.to_numeric(right, errors="coerce").astype("float64")
    mismatch = ~np.isclose(
        left_numeric, right_numeric, rtol=0.0, atol=0.0, equal_nan=True
    )
    return int(mismatch.sum())


def compare_with_reference(
    ohlcv: pd.DataFrame,
    reference_path: Path,
    window: tuple[pd.Timestamp, pd.Timestamp],
    targets: set[str],
) -> dict:
    reference_columns = pd.read_csv(reference_path, nrows=0).columns.tolist()
    missing_columns = sorted(set(reference_columns) - set(ohlcv.columns))
    if missing_columns:
        raise SystemExit(
            "Reference has columns the current dataset does not emit: "
            f"{missing_columns}"
        )

    reference = pd.read_csv(reference_path, usecols=reference_columns, low_memory=False)
    start, end = window

    def slice_real(frame: pd.DataFrame) -> pd.DataFrame:
        timestamps = pd.to_datetime(frame["datetime_utc"], utc=True)
        mask = (
            (timestamps >= start)
            & (timestamps < end)
            & frame["mint"].isin(targets)
            & (frame["is_filled"] == 0)
        )
        return frame.loc[mask].sort_values(["mint", "timestamp_ms"]).reset_index(drop=True)

    current = slice_real(ohlcv)
    previous = slice_real(reference)
    keys = ["mint", "timestamp_ms"]

    stats = {
        "reference": str(reference_path),
        "window_utc": [start.isoformat(), end.isoformat()],
        "current_real_bars": int(len(current)),
        "reference_real_bars": int(len(previous)),
        "same_keys": bool(current[keys].equals(previous[keys])),
        "compared_rows": 0,
        "compared_columns": 0,
        "scope_dependent_columns": {},
        "mismatched_columns": {},
        "unexplained_columns": {},
    }
    if not stats["same_keys"]:
        stats["unexplained_columns"] = {"__keys__": "bar keys differ"}
        stats["passed"] = False
        return stats

    merged = current.merge(previous, on=keys, suffixes=("__current", "__reference"))
    stats["compared_rows"] = int(len(merged))

    for column in reference_columns:
        if column in keys:
            continue
        stats["compared_columns"] += 1
        differing = _values_equal(
            merged[f"{column}__current"], merged[f"{column}__reference"]
        )
        if differing == 0:
            continue
        if _is_scope_dependent(column):
            stats["scope_dependent_columns"][column] = differing
        else:
            stats["mismatched_columns"][column] = differing

    stats["unexplained_columns"] = dict(stats["mismatched_columns"])
    stats["passed"] = not stats["unexplained_columns"]
    return stats


def verify_dataset(
    ohlcv: pd.DataFrame,
    signals: pd.DataFrame,
    timeframe_ms: int,
    reference_path: Path | None = None,
    reference_window: tuple[pd.Timestamp, pd.Timestamp] | None = None,
    exclude_mints: set[str] | None = None,
) -> dict:
    all_targets = set(signals["mint"].astype(str))
    excluded = set(exclude_mints or ())
    targets = all_targets - excluded
    mints = set(ohlcv["mint"].astype(str))

    duplicate_keys = int(ohlcv.duplicated(["mint", "timestamp_ms"]).sum())
    unaligned = int((ohlcv["timestamp_ms"] % timeframe_ms != 0).sum())
    negative_or_zero = int(
        ((ohlcv["high"] < ohlcv["low"]) | (ohlcv["close"] <= 0)).sum()
    )

    timestamps = pd.to_datetime(ohlcv["datetime_utc"], utc=True)
    real = ohlcv.loc[ohlcv["is_filled"] == 0]
    filled = ohlcv.loc[ohlcv["is_filled"] == 1]

    coverage = []
    for row in signals.itertuples(index=False):
        if row.mint in excluded:
            continue
        signal_time = pd.Timestamp(row.signal_timestamp_utc)
        mint_bars = ohlcv.loc[ohlcv["mint"] == row.mint]
        after = mint_bars.loc[
            pd.to_datetime(mint_bars["datetime_utc"], utc=True) >= signal_time
        ]
        coverage.append(
            {
                "signal_id": row.signal_id,
                "mint": row.mint,
                "signal_timestamp_utc": row.signal_timestamp_utc,
                "bars_after_signal": int(len(after)),
                "last_bar_utc": after["datetime_utc"].max() if len(after) else None,
            }
        )
    coverage_frame = pd.DataFrame(coverage)

    report = {
        "timeframe_ms": timeframe_ms,
        "rows": int(len(ohlcv)),
        "mints": int(ohlcv["mint"].nunique()),
        "signaled_mints": len(targets),
        "excluded_mints": sorted(excluded & all_targets),
        "missing_signaled_mints": sorted(targets - mints),
        "untracked_mints": sorted(mints - all_targets),
        "duplicate_mint_timestamp_keys": duplicate_keys,
        "misaligned_timestamps": unaligned,
        "invalid_price_rows": negative_or_zero,
        "window_utc": {
            "first": str(timestamps.min()),
            "last": str(timestamps.max()),
        },
        "real_bars": int(len(real)),
        "filled_bars": int(len(filled)),
        "real_bars_by_pool": {
            str(k): int(v) for k, v in real["pool"].value_counts().items()
        },
        "signals_without_bars": int((coverage_frame["bars_after_signal"] == 0).sum()),
        "bars_after_signal": {
            "min": int(coverage_frame["bars_after_signal"].min()),
            "median": float(coverage_frame["bars_after_signal"].median()),
            "max": int(coverage_frame["bars_after_signal"].max()),
        } if len(coverage_frame) else {"min": 0, "median": 0.0, "max": 0},
        "signals_with_less_than_30m": int(
            (coverage_frame["bars_after_signal"] < 30).sum()
        ),
    }

    checks = {
        "all_signaled_mints_present": not report["missing_signaled_mints"],
        "no_untracked_mints": not report["untracked_mints"],
        "unique_mint_timestamp": duplicate_keys == 0,
        "aligned_timestamps": unaligned == 0,
        "valid_price_rows": negative_or_zero == 0,
        "every_signal_has_a_bar": report["signals_without_bars"] == 0,
    }

    if reference_path is not None and reference_window is not None:
        comparison = compare_with_reference(
            ohlcv, reference_path, reference_window, targets
        )
        report["reference_comparison"] = comparison
        checks["reference_equivalent"] = bool(comparison["passed"])

    report["checks"] = checks
    report["passed"] = all(checks.values())

    lowest = coverage_frame.nsmallest(5, "bars_after_signal")
    report["signals_with_fewest_bars"] = lowest.to_dict(orient="records")
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ohlcv", type=Path, required=True)
    parser.add_argument("--signals", type=Path, required=True)
    parser.add_argument(
        "--timeframe-ms",
        type=int,
        default=0,
        help="Bar length in milliseconds (default: infer from the bar timestamps).",
    )
    parser.add_argument(
        "--reference",
        type=Path,
        default=None,
        help="Earlier single-period OHLCV CSV for an overlap equivalence check.",
    )
    parser.add_argument(
        "--reference-window",
        type=str,
        default=None,
        help="UTC window as 2026-11-08T05:00/2026-11-08T06:00.",
    )
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument(
        "--exclude-mints-file",
        type=Path,
        default=None,
        help=(
            "Newline-separated mints whose signals cannot be served by this "
            "dataset (for example, no WSOL-quoted events). They are reported "
            "but not required to have bars."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    ohlcv = pd.read_csv(args.ohlcv, low_memory=False)
    signals = pd.read_csv(args.signals)

    timeframe_ms = args.timeframe_ms
    if timeframe_ms <= 0:
        ordered = ohlcv.sort_values(["mint", "timestamp_ms"])
        deltas = ordered.groupby("mint")["timestamp_ms"].diff().dropna()
        timeframe_ms = int(deltas.median())

    reference_window = None
    if args.reference is not None:
        if args.reference_window is None:
            raise SystemExit("--reference requires --reference-window")
        reference_window = _parse_window(args.reference_window)

    report = verify_dataset(
        ohlcv,
        signals,
        timeframe_ms,
        reference_path=args.reference,
        reference_window=reference_window,
        exclude_mints=(
            {
                line.strip()
                for line in args.exclude_mints_file.read_text().splitlines()
                if line.strip()
            }
            if args.exclude_mints_file is not None
            else None
        ),
    )

    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
