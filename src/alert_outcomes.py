#!/usr/bin/env python3
"""Price scanner-channel alerts against the raw replay trade stream.

The replay dataset shipped for the AVE study only contains the 309 mints that
AveSignalMonitor alerted on, so the scanner feeds cannot be priced from
``output/backtest/days/<day>/parquet/events.parquet``. This tool reads the raw
hourly archives (``/mnt/HDD/PumpAPI_Replay_Zst/<day>/*.jsonl.zst``) once,
keeps only trades for alerted mints, and records the last real trade at or
before each decision timestamp — the same realistic fill convention as
``backtest_ave_signals.py --fill-source event``.

Input is the per-day ``channel_features.csv`` written by
``prepare_channel_signals.py`` (one row per channel/mint alert). Output is
``alerts_priced.csv`` with, for each horizon:

    entry_ts / entry_px / entry_price_age_ms
    exit_<h>m_ts / exit_<h>m_px / exit_<h>m_age_ms
    net_<h>m_return              (after --fee-rate per side)
    first_trade_ts / alert_to_first_trade_ms

Usage::

    python src/alert_outcomes.py --day 2026-11-20
    python src/alert_outcomes.py --day 2026-11-20 --horizons 1 3 5 15 30
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import pandas as pd


REPLAY_ROOT = Path("/mnt/HDD/PumpAPI_Replay_Zst")
CHANNEL_ROOT = Path("output/backtest/signals_channels")

# Raw archives are compact JSON: trade events put the top-level ``mint`` before
# ``priceSol``/``timestamp``. Extracting those three scalars with C-level regex
# avoids building a Python dict for every one of the tens of millions of events
# in a day, which is by far the dominant cost of a full-archive pass.
MINT_BYTES_RE = re.compile(rb'"mint":"([^"]{20,64})"')
PRICE_BYTES_RE = re.compile(rb'"priceSol":([0-9eE.+-]+)')
TIMESTAMP_BYTES_RE = re.compile(rb'"timestamp":(\d{10,16})')
QUOTE_BYTES_RE = re.compile(rb'"quoteAmount":([0-9eE.+-]+)')
ACTION_BYTES_RE = re.compile(rb'"action":"([a-z]+)"')


def iter_target_trades(
    path: Path,
    targets: set[bytes],
    min_quote_sol: float = 0.0,
    action: str = "any",
):
    """Yield (mint, timestamp, price) for target mints only.

    Trade events carry the top-level mint before the price fields, so the first
    ``"mint"`` match is the event mint. Transfer events may carry a nested mint
    but no ``priceSol``, so they are dropped by the price check.

    ``min_quote_sol`` drops dust prints. The replay feed derives ``priceSol``
    from the swap's own token/quote amounts, and sub-0.001 SOL bot trades
    occasionally produce impossible prices (1e-13 followed by 1e-4). On a pool
    with hundreds of SOL of liquidity those prints are not the executable mid,
    so a small floor removes the feed errors without removing real trades.
    """
    import io

    import zstandard

    with path.open("rb") as handle:
        reader = zstandard.ZstdDecompressor().stream_reader(handle)
        buffered = io.BufferedReader(reader, buffer_size=1 << 20)
        for line in buffered:
            if b'"mint":"' not in line:
                continue
            mint_match = MINT_BYTES_RE.search(line)
            if mint_match is None or mint_match.group(1) not in targets:
                continue
            price_match = PRICE_BYTES_RE.search(line)
            if price_match is None:
                continue
            stamp_match = TIMESTAMP_BYTES_RE.search(line)
            if stamp_match is None:
                continue
            if action != "any":
                action_match = ACTION_BYTES_RE.search(line)
                if action_match is None or action_match.group(1).decode() != action:
                    continue
            if min_quote_sol > 0:
                quote_match = QUOTE_BYTES_RE.search(line)
                if quote_match is None:
                    continue
                try:
                    if float(quote_match.group(1)) < min_quote_sol:
                        continue
                except ValueError:
                    continue
            try:
                price = float(price_match.group(1))
            except ValueError:
                continue
            if price <= 0:
                continue
            yield (
                mint_match.group(1).decode(),
                int(stamp_match.group(1)),
                price,
            )


def load_targets(day: str, latency_ms: int, horizons: list[int]) -> list[dict]:
    path = CHANNEL_ROOT / day / "channel_features.csv"
    if not path.exists():
        raise SystemExit(f"Missing alert features: {path}")
    frame = pd.read_csv(path)
    targets: list[dict] = []
    for row in frame.to_dict("records"):
        alert_ts = int(row["alert_ts_ms"])
        entry_target = alert_ts + latency_ms
        target = {
            "row": row,
            "alert_ts": alert_ts,
            "entry_target": entry_target,
            "entry_ts": None,
            "entry_px": None,
            "first_trade_ts": None,
            "entry_checked": False,
        }
        for horizon in horizons:
            target[f"exit_{horizon}_target"] = entry_target + horizon * 60_000
            target[f"exit_{horizon}_ts"] = None
            target[f"exit_{horizon}_px"] = None
        targets.append(target)
    return targets


def stream_day(
    day: str,
    targets: list[dict],
    horizons: list[int],
    *,
    replay_root: Path = REPLAY_ROOT,
    min_quote_sol: float = 0.0,
    action: str = "any",
    verbose: bool = True,
) -> int:
    by_mint: dict[str, list[dict]] = {}
    for target in targets:
        by_mint.setdefault(str(target["row"]["mint"]), []).append(target)
    last: dict[str, tuple[int, float]] = {}
    seen_mints: set[str] = set()

    archives = sorted((replay_root / day).glob("*.jsonl.zst"))
    if not archives:
        raise SystemExit(f"No replay archives under {replay_root / day}")

    target_bytes = {mint.encode() for mint in by_mint}
    total = 0
    for archive in archives:
        kept = 0
        for mint, timestamp, price in iter_target_trades(
            archive, target_bytes, min_quote_sol, action
        ):
            kept += 1
            previous = last.get(mint)
            if mint not in seen_mints:
                seen_mints.add(mint)
                for target in by_mint[mint]:
                    target["first_trade_ts"] = timestamp
            for target in by_mint[mint]:
                if not target["entry_checked"] and timestamp > target["entry_target"]:
                    target["entry_checked"] = True
                    if previous is not None and previous[0] <= target["entry_target"]:
                        target["entry_ts"], target["entry_px"] = previous
                for horizon in horizons:
                    key = f"exit_{horizon}"
                    if (
                        target.get(key + "_px") is None
                        and target.get(key + "_target") is not None
                        and timestamp > target[key + "_target"]
                        and previous is not None
                        and previous[0] <= target[key + "_target"]
                    ):
                        target[key + "_ts"], target[key + "_px"] = previous
            last[mint] = (timestamp, price)
        total += kept
        if verbose:
            print(
                f"  {archive.name}: {kept} kept trades "
                f"({len(seen_mints)}/{len(by_mint)} mints seen)",
                flush=True,
            )

    # Fill targets whose decision timestamp is after the last trade of the day.
    for mint, mint_targets in by_mint.items():
        previous = last.get(mint)
        if previous is None:
            continue
        timestamp, price = previous
        for target in mint_targets:
            if target["entry_px"] is None and timestamp <= target["entry_target"]:
                target["entry_ts"], target["entry_px"] = timestamp, price
            for horizon in horizons:
                key = f"exit_{horizon}"
                if (
                    target.get(key + "_px") is None
                    and timestamp <= target[key + "_target"]
                ):
                    target[key + "_ts"], target[key + "_px"] = timestamp, price
    return total


def build_frame(
    targets: list[dict],
    horizons: list[int],
    latency_ms: int,
    fee_rate: float,
) -> pd.DataFrame:
    rows = []
    for target in targets:
        row = dict(target["row"])
        alert_ts = target["alert_ts"]
        row["latency_ms"] = latency_ms
        row["entry_ts"] = target["entry_ts"]
        row["entry_px"] = target["entry_px"]
        row["entry_price_age_ms"] = (
            None if target["entry_ts"] is None else target["entry_target"] - target["entry_ts"]
        )
        row["first_trade_ts"] = target["first_trade_ts"]
        row["alert_to_first_trade_ms"] = (
            None
            if target["first_trade_ts"] is None
            else target["first_trade_ts"] - alert_ts
        )
        for horizon in horizons:
            key = f"exit_{horizon}"
            row[f"{key}_ts"] = target.get(f"{key}_ts")
            row[f"{key}_px"] = target.get(f"{key}_px")
            age = (
                None
                if target.get(f"{key}_ts") is None
                else target[f"{key}_target"] - target[f"{key}_ts"]
            )
            row[f"{key}_age_ms"] = age
            entry_px = target["entry_px"]
            exit_px = target.get(f"{key}_px")
            if entry_px and exit_px:
                row[f"net_{horizon}m_return"] = (
                    (exit_px * (1 - fee_rate)) / (entry_px * (1 + fee_rate)) - 1.0
                )
            else:
                row[f"net_{horizon}m_return"] = None
        rows.append(row)
    return pd.DataFrame(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--day", required=True)
    parser.add_argument("--horizons", nargs="+", type=int, default=[1, 3, 5, 15, 30])
    parser.add_argument("--latency-ms", type=int, default=5_000)
    parser.add_argument("--fee-rate", type=float, default=0.0125)
    parser.add_argument("--replay-root", type=Path, default=REPLAY_ROOT)
    parser.add_argument(
        "--min-quote-sol",
        type=float,
        default=0.0,
        help=(
            "Ignore trades whose quoteAmount is below this many SOL. Set a "
            "small floor (e.g. 0.01) to drop sub-cent dust prints that corrupt "
            "priceSol with impossible values."
        ),
    )
    parser.add_argument(
        "--action",
        choices=("any", "buy", "sell"),
        default="any",
        help="Price only on prints of this side (e.g. 'sell' to model what a seller receives).",
    )
    parser.add_argument(
        "--tag",
        default="",
        help="Suffix for the output files, e.g. 'q01' -> alerts_priced_q01.csv.",
    )
    parser.add_argument(
        "--out", type=Path, default=None, help="Default: <day>/alerts_priced<tag>.csv"
    )
    args = parser.parse_args(argv)

    horizons = sorted(set(args.horizons))
    targets = load_targets(args.day, args.latency_ms, horizons)
    print(f"{args.day}: {len(targets)} alerts over {len({t['row']['mint'] for t in targets})} mints")
    stream_day(
        args.day,
        targets,
        horizons,
        replay_root=args.replay_root,
        min_quote_sol=args.min_quote_sol,
        action=args.action,
    )
    frame = build_frame(targets, horizons, args.latency_ms, args.fee_rate)

    suffix = f"_{args.tag}" if args.tag else ""
    out = args.out or CHANNEL_ROOT / args.day / f"alerts_priced{suffix}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(out, index=False)

    priced = frame["entry_px"].notna()
    summary = {
        "day": args.day,
        "alerts": int(len(frame)),
        "mints": int(frame["mint"].nunique()),
        "with_entry_price": int(priced.sum()),
        "coverage": float(priced.mean()) if len(frame) else 0.0,
    }
    for horizon in horizons:
        column = f"net_{horizon}m_return"
        block = frame[priced & frame[column].notna()]
        summary[f"{horizon}m"] = {
            "n": int(len(block)),
            "win_rate": float((block[column] > 0).mean()) if len(block) else None,
            "mean": float(block[column].mean()) if len(block) else None,
            "median": float(block[column].median()) if len(block) else None,
        }
    print(json.dumps(summary, indent=2))
    (CHANNEL_ROOT / args.day / f"outcomes_report{suffix}.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
