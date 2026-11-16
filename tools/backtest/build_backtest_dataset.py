#!/usr/bin/env python3
"""Build the filtered replay and multi-hour OHLCV backtest dataset.

Runs the stage end to end:

1. Stream the hourly archives and keep only the target mint universe
   (``filter_replay.py``).
2. Build OHLCV candles from the reduced replay with the unchanged
   ``raw_to_clean_ohlcv.py`` pipeline.
3. Verify coverage, signal alignment, and (optionally) equivalence against an
   earlier single-period dataset (``verify_backtest_dataset.py``).
4. Write a consolidated ``dataset_report.json``.

Example:
    python tools/backtest/build_backtest_dataset.py \\
        --replay-dir 2026-11-08-replay \\
        --timeframes 1m,5m
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "tools" / "backtest") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "tools" / "backtest"))

import pandas as pd  # noqa: E402

import filter_replay  # noqa: E402
import verify_backtest_dataset  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replay-dir", type=Path, default=Path("2026-11-08-replay"))
    parser.add_argument(
        "--mints", type=Path, default=Path("output/backtest/signals/target_mints.txt")
    )
    parser.add_argument(
        "--signals",
        type=Path,
        default=Path("output/backtest/signals/strategy_signals.csv"),
    )
    parser.add_argument("--output-dir", type=Path, default=Path("output/backtest"))
    parser.add_argument("--timeframes", default="1m,5m")
    parser.add_argument("--workers", type=int, default=0, help="0 = auto")
    parser.add_argument(
        "--open-prev-close",
        action="store_true",
        help="Carry the previous close into each candle open (off by default).",
    )
    parser.add_argument(
        "--reference",
        type=Path,
        default=None,
        help="Earlier single-period OHLCV CSV for an overlaid equivalence check.",
    )
    parser.add_argument(
        "--reference-window",
        type=str,
        default=None,
        help="UTC window for the reference check, e.g. 2026-11-08T05:00/2026-11-08T06:00",
    )
    parser.add_argument("--skip-filter", action="store_true")
    parser.add_argument("--skip-ohlcv", action="store_true")
    parser.add_argument("--skip-verify", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    filtered_dir = args.output_dir / "replay_filtered"
    ohlcv_dir = args.output_dir / "ohlcv"
    filtered_file = filtered_dir / "target_mints_events.jsonl.zst"
    timeframes = [item.strip() for item in args.timeframes.split(",") if item.strip()]

    timings: dict[str, float] = {}
    report: dict = {
        "stage": "backtest_dataset",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "inputs": {
            "replay_dir": str(args.replay_dir),
            "mints": str(args.mints),
            "signals": str(args.signals),
        },
        "output_dir": str(args.output_dir),
        "timeframes": timeframes,
        "open_prev_close": bool(args.open_prev_close),
    }

    if not args.skip_filter:
        started = time.time()
        filter_args = [
            "--replay-dir",
            str(args.replay_dir),
            "--mints",
            str(args.mints),
            "--output-dir",
            str(filtered_dir),
        ]
        if args.workers > 0:
            filter_args += ["--workers", str(args.workers)]
        exit_code = filter_replay.main(filter_args)
        timings["filter_seconds"] = round(time.time() - started, 1)
        if exit_code != 0:
            print("[build] filter step failed", file=sys.stderr)
            return exit_code

    filter_report_path = filtered_dir / "replay_filter_report.json"
    if not filter_report_path.exists():
        raise SystemExit(f"Missing filter report: {filter_report_path}")
    report["filter"] = json.loads(filter_report_path.read_text())

    if not args.skip_ohlcv:
        started = time.time()
        command = [
            sys.executable,
            str(REPO_ROOT / "raw_to_clean_ohlcv.py"),
            str(filtered_file),
            "-o",
            str(ohlcv_dir),
            "-t",
            ",".join(timeframes),
            "--fill",
        ]
        if args.open_prev_close:
            command.append("--open-prev-close")
        result = subprocess.run(command, cwd=REPO_ROOT, check=False)
        timings["ohlcv_seconds"] = round(time.time() - started, 1)
        if result.returncode != 0:
            print("[build] OHLCV step failed", file=sys.stderr)
            return result.returncode

    cleaning_report_path = ohlcv_dir / "cleaning_report.json"
    report["ohlcv"] = (
        json.loads(cleaning_report_path.read_text())
        if cleaning_report_path.exists()
        else None
    )

    if args.skip_verify:
        report["passed"] = bool(report["filter"].get("passed"))
    else:
        signals = pd.read_csv(args.signals)
        reference_window = None
        if args.reference is not None:
            if args.reference_window is None:
                raise SystemExit("--reference requires --reference-window")
            reference_window = verify_backtest_dataset._parse_window(
                args.reference_window
            )

        started = time.time()
        verification = {}
        for timeframe in timeframes:
            path = ohlcv_dir / f"ohlcv_{timeframe}.csv"
            if not path.exists():
                raise SystemExit(f"Missing OHLCV output: {path}")
            frame = pd.read_csv(path, low_memory=False)
            result = verify_backtest_dataset.verify_dataset(
                frame,
                signals,
                timeframe_ms=verify_backtest_dataset.timeframe_ms_from_label(timeframe),
                reference_path=args.reference if timeframe == timeframes[0] else None,
                reference_window=reference_window
                if timeframe == timeframes[0]
                else None,
            )
            (ohlcv_dir / f"dataset_verification_{timeframe}.json").write_text(
                json.dumps(result, indent=2), encoding="utf-8"
            )
            verification[timeframe] = result
        timings["verification_seconds"] = round(time.time() - started, 1)
        report["verification"] = verification
        report["passed"] = bool(report["filter"].get("passed")) and all(
            result["passed"] for result in verification.values()
        )

    report["timings_seconds"] = timings
    report_path = args.output_dir / "dataset_report.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(
        json.dumps(
            {
                "passed": report["passed"],
                "filter_events": report["filter"]["total_events_kept"],
                "mints_with_events": report["filter"]["mints_with_events"],
                "timings_seconds": timings,
                "report": str(report_path),
            },
            indent=2,
        )
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
