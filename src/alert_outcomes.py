#!/usr/bin/env python3
"""Price scanner-channel alerts against the raw replay trade stream.

The replay dataset shipped for the AVE study only contains the 309 mints that
AveSignalMonitor alerted on, so the scanner feeds cannot be priced from
``output/backtest/days/<day>/parquet/events.parquet``. This tool reads the raw
hourly archives (``/mnt/HDD/PumpAPI_Replay_Zst/<day>/*.jsonl.zst``) once,
keeps only trades for alerted mints, and records a real trade print as the
fill — the same realistic fill convention as
``backtest_ave_signals.py --fill-source event``. Two entry conventions are
available: ``last-before`` (the last print at/before the decision, with stale
prints filtered) and ``first-after`` (the first print at/after it, which is
what the deployed trade-mode bot does).

Input is the per-day ``channel_features.csv`` written by
``prepare_channel_signals.py`` (one row per channel/mint alert). Output is
``alerts_priced.csv`` with, for each horizon:

    entry_ts / entry_px / entry_price_age_ms / entry_delay_ms
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
    min_quote_sol: float = 0.01,
    action: str = "any",
):
    """Yield (mint, timestamp, price, side) for target mints only.

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
            action_match = ACTION_BYTES_RE.search(line)
            side = action_match.group(1).decode() if action_match else ""
            if action != "any" and side != action:
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
                side,
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


def select_entry(
    target: dict,
    timestamp: int,
    price: float,
    previous: tuple[int, float] | tuple[int, float, str] | None,
    mode: str,
    *,
    eligible: bool = True,
    side: str | None = None,
) -> None:
    """Record the entry fill for one target, if this trade decides it.

    ``last-before`` uses the most recent print at or before the decision and is
    the conservative mark for a fast mover; ``first-after`` uses the first
    print at or after it, which is what the deployed trade-mode bot buys.

    ``eligible`` is False for a print the live engine would not buy on (a SELL
    when the bot is waiting for a BUY); such a print neither fills the entry
    nor advances ``entry_checked``.
    """
    if not eligible or target["entry_checked"]:
        return
    if mode == "first-after":
        if timestamp >= target["entry_target"]:
            target["entry_checked"] = True
            target["entry_ts"], target["entry_px"] = timestamp, price
            target["entry_side"] = side
        return
    if timestamp > target["entry_target"]:
        target["entry_checked"] = True
        if previous is not None and previous[0] <= target["entry_target"]:
            target["entry_ts"], target["entry_px"] = previous[0], previous[1]
            target["entry_side"] = previous[2] if len(previous) > 2 else None


def stream_day(
    day: str,
    targets: list[dict],
    horizons: list[int],
    *,
    replay_root: Path = REPLAY_ROOT,
    min_quote_sol: float = 0.01,
    action: str = "any",
    entry_fill_mode: str = "last-before",
    entry_side: str = "any",
    verbose: bool = True,
) -> int:
    by_mint: dict[str, list[dict]] = {}
    for target in targets:
        by_mint.setdefault(str(target["row"]["mint"]), []).append(target)
    last: dict[str, tuple[int, float]] = {}
    # Last print the live engine would buy on (BUY when ``entry_side`` is buy).
    last_entry: dict[str, tuple[int, float, str]] = {}
    seen_mints: set[str] = set()

    archives = sorted((replay_root / day).glob("*.jsonl.zst"))
    if not archives:
        raise SystemExit(f"No replay archives under {replay_root / day}")

    target_bytes = {mint.encode() for mint in by_mint}
    total = 0
    for archive in archives:
        kept = 0
        for mint, timestamp, price, side in iter_target_trades(
            archive, target_bytes, min_quote_sol, action
        ):
            kept += 1
            previous = last.get(mint)
            side_ok = entry_side == "any" or side == entry_side
            entry_previous = last_entry.get(mint) if side_ok else None
            if mint not in seen_mints:
                seen_mints.add(mint)
                for target in by_mint[mint]:
                    target["first_trade_ts"] = timestamp
            for target in by_mint[mint]:
                select_entry(
                    target,
                    timestamp,
                    price,
                    entry_previous,
                    entry_fill_mode,
                    eligible=side_ok,
                    side=side,
                )
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
            if side_ok:
                last_entry[mint] = (timestamp, price, side)
        total += kept
        if verbose:
            print(
                f"  {archive.name}: {kept} kept trades "
                f"({len(seen_mints)}/{len(by_mint)} mints seen)",
                flush=True,
            )

    # Fill targets whose decision timestamp is after the last trade of the day.
    # ``first-after`` has nothing to buy once the tape stops, so it stays
    # unpriced there; ``last-before`` falls back to the final eligible print.
    # The fallback uses ``last_entry`` (already filtered to ``entry_side``), not
    # the unfiltered ``last`` print: with ``--entry-side buy`` falling back to a
    # closing SELL would price a buy the deployed engine could never take.
    for mint, mint_targets in by_mint.items():
        previous = last.get(mint)
        if previous is None:
            continue
        timestamp, price = previous
        entry_fill = last_entry.get(mint)
        for target in mint_targets:
            if (
                entry_fill_mode == "last-before"
                and target["entry_px"] is None
                and entry_fill is not None
                and entry_fill[0] <= target["entry_target"]
            ):
                target["entry_ts"], target["entry_px"] = entry_fill[:2]
                target["entry_side"] = entry_fill[2] if len(entry_fill) > 2 else None
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
    *,
    max_entry_age_ms: int = 0,
    max_entry_delay_ms: int = 60_000,
    entry_fill_mode: str = "last-before",
) -> pd.DataFrame:
    rows = []
    for target in targets:
        row = dict(target["row"])
        alert_ts = target["alert_ts"]
        row["latency_ms"] = latency_ms
        row["entry_fill_mode"] = entry_fill_mode
        row["entry_ts"] = target["entry_ts"]
        row["entry_px"] = target["entry_px"]
        # Negative for ``first-after``: the fill happens after the decision.
        # ``entry_price_age_ms`` is the age of the print (stale side);
        # ``entry_delay_ms`` is the signed lag of the fill itself.
        row["entry_price_age_ms"] = (
            None if target["entry_ts"] is None else target["entry_target"] - target["entry_ts"]
        )
        row["entry_delay_ms"] = (
            None if target["entry_ts"] is None else target["entry_ts"] - target["entry_target"]
        )
        row["entry_side"] = target.get("entry_side")
        too_stale = (
            max_entry_age_ms > 0
            and row["entry_price_age_ms"] is not None
            and row["entry_price_age_ms"] > max_entry_age_ms
        )
        too_late = (
            max_entry_delay_ms > 0
            and row["entry_delay_ms"] is not None
            and row["entry_delay_ms"] > max_entry_delay_ms
        )
        if too_stale or too_late:
            # Neither print is a fill the deployed bot could have taken: a
            # stale mark on one side, a missed entry window on the other.
            # Drop the entry so the row is excluded from returns.
            row["entry_ts"] = None
            row["entry_px"] = None
            row["entry_side"] = None
            row["entry_price_age_ms"] = None
            row["entry_delay_ms"] = None
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
        if row["entry_px"] is None:
            # The entry was rejected as stale; its returns are not observations.
            for horizon in horizons:
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
        default=0.01,
        help=(
            "Ignore trades whose quoteAmount is below this many SOL. The "
            "default 0.01 drops sub-cent dust prints whose priceSol is an "
            "impossible feed artifact rather than an executable price."
        ),
    )
    parser.add_argument(
        "--action",
        choices=("any", "buy", "sell"),
        default="any",
        help="Price only on prints of this side (e.g. 'sell' to model what a seller receives).",
    )
    parser.add_argument(
        "--entry-fill-mode",
        choices=("last-before", "first-after"),
        default="last-before",
        help=(
            "last-before: the last print at/before the decision (conservative, "
            "but stale on a fast mover). first-after: the first print at/after "
            "the decision, which is what the deployed trade-mode bot does."
        ),
    )
    parser.add_argument(
        "--entry-side",
        choices=("any", "buy", "sell"),
        default="buy",
        help=(
            "Side of the print that may fill the entry. 'buy' matches the "
            "deployed engine, which waits for the first BUY print; 'any' is "
            "the looser research convention."
        ),
    )
    parser.add_argument(
        "--max-entry-age-ms",
        type=int,
        default=2_000,
        help=(
            "Drop the entry when the print it would use is older than this. "
            "0 disables the filter. Ignored for first-after, whose prints are "
            "always at or after the decision."
        ),
    )
    parser.add_argument(
        "--max-entry-delay-ms",
        type=int,
        default=60_000,
        help=(
            "Drop an entry that would fill later than this after the decision "
            "(the deployed bot gives up at 60 s). Applies to first-after; "
            "last-before fills are covered by --max-entry-age-ms."
        ),
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

    if (
        args.action != "any"
        and args.entry_side != "any"
        and args.action != args.entry_side
    ):
        # Every print is filtered by --action, then the entry needs a print of
        # --entry-side. Disjoint sides silently yield zero priced entries.
        parser.error(
            f"--action {args.action!r} and --entry-side {args.entry_side!r} "
            "select disjoint prints; set --entry-side to the same side or "
            "'any'"
        )

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
        entry_fill_mode=args.entry_fill_mode,
        entry_side=args.entry_side,
    )
    frame = build_frame(
        targets,
        horizons,
        args.latency_ms,
        args.fee_rate,
        max_entry_age_ms=args.max_entry_age_ms,
        max_entry_delay_ms=args.max_entry_delay_ms,
        entry_fill_mode=args.entry_fill_mode,
    )

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
        "entry_fill_mode": args.entry_fill_mode,
        "max_entry_age_ms": args.max_entry_age_ms,
        "max_entry_delay_ms": args.max_entry_delay_ms,
        "min_quote_sol": args.min_quote_sol,
        "action": args.action,
        "entry_side": args.entry_side,
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
