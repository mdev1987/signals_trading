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

WSOL_MINT = "So11111111111111111111111111111111111111112"


def classify_wsol_routability(
    events_path: Path, signals: pd.DataFrame, missing_mints: set[str] | None = None
) -> pd.DataFrame:
    """Mark each signal routable when its mint trades WSOL at or after the signal.

    Dataset-level routability is deliberately narrow: it only answers "can this
    filtered stream produce SOL-denominated bars at and following this signal?".
    Pool model support (bonding curve, PumpSwap virtual reserves, vault AMMs) is
    a simulator concern and is checked at order time, not here.
    """
    import io

    import zstandard as zstd

    absent = {str(mint) for mint in (missing_mints or ())}
    last_trade_ms: dict[str, int] = {}
    with events_path.open("rb") as source:
        reader = zstd.ZstdDecompressor().stream_reader(
            source, read_across_frames=True
        )
        for line in io.BufferedReader(reader, 1 << 20):
            if not line.strip():
                continue
            event = filter_replay._loads(line)
            if (
                isinstance(event, dict)
                and event.get("quoteMint") == WSOL_MINT
                and event.get("action") in ("buy", "sell")
            ):
                mint = str(event.get("mint"))
                raw_timestamp = event.get("timestamp", event.get("timestamp_ms"))
                try:
                    timestamp_ms = int(raw_timestamp)
                except (TypeError, ValueError):
                    continue
                if timestamp_ms > last_trade_ms.get(mint, -1):
                    last_trade_ms[mint] = timestamp_ms

    rows = []
    for row in signals.itertuples(index=False):
        mint = str(row.mint)
        signal_ms = getattr(row, "signal_timestamp_ms", None)
        if signal_ms is None:
            signal_ms = int(
                pd.Timestamp(row.signal_timestamp_utc).timestamp() * 1000
            )
        signal_ms = int(signal_ms)
        last_trade = last_trade_ms.get(mint)
        if last_trade is None:
            # Not a quote problem: the feed has no WSOL market for this mint at
            # all, usually because it launched on a venue the replay does not
            # cover.
            reason = "no_replay_events" if mint in absent else "no_wsol_quoted_trades"
        elif last_trade < signal_ms:
            # The market for this mint went quiet before the signal arrived.
            # The dataset layer cannot know the entry latency or staleness
            # limit, so it conservatively requires a WSOL trade at or after the
            # signal; otherwise no bar can follow the signal.
            reason = "no_wsol_trades_after_signal"
        else:
            reason = ""
        routable = not reason
        rows.append(
            {
                "signal_id": row.signal_id,
                "signal_timestamp_utc": row.signal_timestamp_utc,
                "mint": mint,
                "dataset_routable": int(routable),
                "reason": reason,
            }
        )
    return pd.DataFrame(rows)


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
    parser.add_argument(
        "--normalize-legacy",
        action="store_true",
        help=(
            "Backfill priceSol/marketCapSol for WSOL events in pre-2026-11 "
            "archives, which omit the SOL-denominated fields."
        ),
    )
    parser.add_argument(
        "--allow-missing-mints",
        type=int,
        default=0,
        help=(
            "Tolerate up to this many signal mints with no replay events "
            "(venues the feed does not cover). They are routed out of the "
            "dataset with reason 'no_replay_events'."
        ),
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
        if args.normalize_legacy:
            filter_args.append("--normalize-legacy")
        if args.allow_missing_mints > 0:
            filter_args += ["--allow-missing-mints", str(args.allow_missing_mints)]
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
        excluded: set[str] = set()
        if not filtered_file.exists():
            raise SystemExit(f"Missing filtered replay: {filtered_file}")
        missing_mints = set(report["filter"].get("mints_missing") or ())
        routing = classify_wsol_routability(
            filtered_file, signals, missing_mints=missing_mints
        )
        routing_path = args.output_dir / "signal_routing.csv"
        routing.to_csv(routing_path, index=False)
        excluded = set(
            routing.loc[routing["dataset_routable"] == 0, "mint"].astype(str)
        )
        reason_counts = (
            routing.loc[routing["dataset_routable"] == 0, "reason"]
            .value_counts()
            .to_dict()
        )
        report["routing"] = {
            "file": str(routing_path),
            "signals": int(len(routing)),
            "dataset_routable": int(routing["dataset_routable"].sum()),
            "excluded": len(excluded),
            "excluded_by_reason": {str(k): int(v) for k, v in reason_counts.items()},
            "excluded_mints": sorted(excluded),
        }
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
                exclude_mints=excluded,
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
