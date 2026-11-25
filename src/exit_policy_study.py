#!/usr/bin/env python3
"""Compare exit policies on the scanner lane, out of sample.

Entries are taken unchanged from the canon lane pricing (first BUY print at
card + 5 s, ``Pumpfunamm``, 20-100k launch liquidity). Only the exit policy
varies, so any difference belongs to the exit.

An exit is executable only if a trade print exists within ``fresh_ms`` of the
moment the policy wants to sell. If the pool stopped printing, the leg is
written off at -100 % (that is what a market sell into a dead pool returns).
Fees are ``--fee`` per side on every leg, and each trade's return is capped at
+999 %, the same cap the research tables use.

This is a pre-registered comparison: the policy list, the executable-exit rule,
the fee and the metric set are fixed in ``docs/EXIT_POLICY_STUDY.md`` before the
run. It does not tune the frozen strategy and its result must not be pooled with
the frozen paper sample.

Usage::

    env PYTHONPATH=src .venv/bin/python src/exit_policy_study.py
    env PYTHONPATH=src .venv/bin/python src/exit_policy_study.py \
        --days 2026-11-21 2026-11-22 --fresh-ms 5000
"""

from __future__ import annotations

import argparse
import bisect
import csv
import json
import statistics
from pathlib import Path

from alert_outcomes import iter_target_trades

DEFAULT_FEE = 0.0125
DEFAULT_NOTIONAL_SOL = 0.1
DEFAULT_FRESH_MS = 60_000
MAX_HOLD_MS = 900_000
HORIZON_MS = 300_000
LANE_DAYS = ("2026-11-21", "2026-11-22")
CHANNEL_ROOT = Path("output/backtest/signals_channels")
REPLAY_ROOT = Path("/mnt/HDD/PumpAPI_Replay_Zst")
CACHE_ROOT = Path("output/backtest/exit_study")

# Declared in the study doc before the run. Strings encode the parameters so a
# policy cannot silently change between runs.
POLICIES = (
    "base5",  # deployed: sell 100 % at entry + 5 min
    "hold15",  # naive extension
    "trail25",
    "trail35",
    "trail50",
    "trail25_5",
    "trail35_5",
    "trail50_5",
    "stop50_5",
    "stop70_5",
    "tp50all",
    "tp100all",
    "tp150all",
    "tp50h5",
    "tp75h5",
    "tp100h5",
    "tp50ht30",
    "dyn5",
    "dyn10",
    "ladder",
    "oracle5",
    "oracle15",
)


def lane_rows(day: str) -> list[dict]:
    """Deployed-lane rows that actually got an entry on ``day``."""
    path = CHANNEL_ROOT / day / "alerts_priced_canon.csv"
    with path.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    out = []
    for row in rows:
        if row.get("channel") != "@AveSolanaTokenScanner":
            continue
        if row.get("dex") != "Pumpfunamm":
            continue
        try:
            liquidity = float(row["liquidity_usd"])
            entry_ts = float(row["entry_ts"])
            entry_px = float(row["entry_px"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (20_000.0 <= liquidity < 100_000.0):
            continue
        out.append(
            {"mint": row["mint"], "entry_ts": int(entry_ts), "entry_px": entry_px}
        )
    return out


def collect_prints(
    day: str,
    rows: list[dict],
    *,
    cache_dir: Path | None = CACHE_ROOT,
) -> dict[str, list[tuple[int, float]]]:
    """Every lane-mint print from its entry to the 15-minute cap."""
    cache = None if cache_dir is None else cache_dir / f"{day}.json"
    if cache is not None and cache.exists():
        raw = json.loads(cache.read_text())
        return {mint: [(int(ts), float(px)) for ts, px in values] for mint, values in raw.items()}
    by_mint = {row["mint"]: row for row in rows}
    prints: dict[str, list[tuple[int, float]]] = {mint: [] for mint in by_mint}
    targets = {mint.encode() for mint in by_mint}
    for archive in sorted((REPLAY_ROOT / day).glob("*.jsonl.zst")):
        for mint, ts, px, _side in iter_target_trades(archive, targets, 0.01, "any"):
            row = by_mint.get(mint)
            if row is None:
                continue
            end = row["entry_ts"] + MAX_HOLD_MS + 60_000
            if row["entry_ts"] <= ts <= end:
                prints[mint].append((ts, px))
    for values in prints.values():
        values.sort()
    if cache is not None:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(prints))
    return prints


def net(entry_px: float, exit_px: float, fee: float = DEFAULT_FEE) -> float:
    """Net return of one leg, capped at +999 % like the research tables."""
    ret = (exit_px * (1 - fee)) / (entry_px * (1 + fee)) - 1.0
    return min(ret, 9.99)


def close_at(
    times: list[int],
    prices: list[float],
    when: int,
    fresh_ms: int,
) -> float | None:
    """Price of the first print at/after ``when`` inside the freshness window."""
    idx = bisect.bisect_left(times, when)
    if idx < len(times) and times[idx] - when <= fresh_ms:
        return prices[idx]
    return None


def simulate(
    row: dict,
    series: list[tuple[int, float]],
    policy: str,
    fresh_ms: int,
    fee: float = DEFAULT_FEE,
) -> float:
    """Return the policy's net return for one entry. -1.0 means unexecutable."""
    entry_ts, entry_px = row["entry_ts"], row["entry_px"]
    times = [ts for ts, _ in series]
    prices = [px for _, px in series]
    horizon = entry_ts + HORIZON_MS
    last = entry_ts + MAX_HOLD_MS

    def at(when: int) -> float:
        px = close_at(times, prices, when, fresh_ms)
        return -1.0 if px is None else net(entry_px, px, fee)

    if policy == "base5":
        return at(horizon)
    if policy == "hold15":
        return at(last)
    if policy.startswith("trail"):
        body = policy[5:]
        trail = int(body.split("_")[0]) / 100.0
        # The plain form trails until the 15-minute cap; the ``_5`` form keeps
        # the deployed 5-minute deadline and only adds the stop.
        deadline = horizon if body.endswith("_5") else last
        peak = 0.0
        start = bisect.bisect_left(times, entry_ts + 60_000)
        for idx in range(start, len(times)):
            ts, px = series[idx]
            if ts > deadline:
                break
            peak = max(peak, px)
            if px <= peak * (1 - trail):
                return net(entry_px, px, fee)
        return at(deadline)
    if policy.startswith("stop"):
        # Absolute stop from entry, no activation, deployed 5-minute deadline.
        threshold = int(policy[4:].split("_")[0]) / 100.0
        floor = entry_px * (1 - threshold)
        for ts, px in series:
            if ts > horizon:
                break
            if px <= floor:
                return net(entry_px, px, fee)
        return at(horizon)
    if policy in ("tp50all", "tp100all", "tp150all"):
        tp = {"tp50all": 0.5, "tp100all": 1.0, "tp150all": 1.5}[policy]
        target = entry_px * (1 + tp)
        for ts, px in series:
            if ts > horizon:
                break
            if px >= target:
                return net(entry_px, px, fee)
        return at(horizon)
    if policy.startswith("tp"):  # tp50h5, tp75h5, tp100h5, tp50ht30
        tp = {"50": 0.5, "75": 0.75, "100": 1.0}[policy[2:5].rstrip("h")]
        rest = policy.split("h")[1]
        target = entry_px * (1 + tp)
        leg_px = None
        for ts, px in series:
            if ts > horizon:
                break
            if px >= target:
                leg_px = px
                break
        if rest == "5":
            tail = at(horizon)
            if leg_px is None:
                return tail
            return 0.5 * net(entry_px, leg_px, fee) + 0.5 * tail
        peak = 0.0
        tail = at(last)
        start = bisect.bisect_left(times, entry_ts + 60_000)
        for idx in range(start, len(times)):
            ts, px = series[idx]
            if ts > last:
                break
            peak = max(peak, px)
            if px <= peak * 0.70:
                tail = net(entry_px, px, fee)
                break
        if leg_px is None:
            return tail
        return 0.5 * net(entry_px, leg_px, fee) + 0.5 * tail
    if policy.startswith("dyn"):
        floor = int(policy[3:]) / 100.0
        when = horizon
        while when <= last:
            px = close_at(times, prices, when, fresh_ms)
            if px is None:
                return -1.0
            if px < entry_px * (1 + floor):
                return net(entry_px, px, fee)
            when += 30_000
        return at(last)
    if policy == "ladder":
        legs = (120_000, 300_000, 600_000)
        return sum(at(entry_ts + delta) for delta in legs) / len(legs)
    if policy in ("oracle5", "oracle15"):
        end = horizon if policy == "oracle5" else last
        best = max((px for ts, px in series if entry_ts < ts <= end), default=None)
        return -1.0 if best is None else net(entry_px, best, fee)
    raise ValueError(f"unknown policy: {policy}")


def summarize(returns: list[float], notional_sol: float) -> dict:
    wins = [value for value in returns if value > 0]
    losses = [value for value in returns if value < 0]
    gross_profit = sum(wins)
    gross_loss = sum(losses)
    return {
        "n": len(returns),
        "win": len(wins) / len(returns),
        "mean": statistics.mean(returns),
        "median": statistics.median(returns),
        "pf": gross_profit / abs(gross_loss) if gross_loss else float("inf"),
        "catastrophic": sum(1 for value in returns if value <= -0.9) / len(returns),
        "pnl_sol": sum(returns) * notional_sol,
    }


def run_day(day: str, fresh_ms: int, fee: float) -> dict[str, list[float]]:
    rows = lane_rows(day)
    prints = collect_prints(day, rows)
    return {
        policy: [simulate(row, prints[row["mint"]], policy, fresh_ms, fee) for row in rows]
        for policy in POLICIES
    }


def format_table(title: str, results: dict[str, list[float]], notional: float) -> str:
    lines = [
        f"## {title}",
        f"{'policy':12} {'n':>4} {'win':>7} {'mean':>8} {'median':>8} "
        f"{'PF':>6} {'cat':>6} {'SOL':>8}",
    ]
    for policy in POLICIES:
        row = summarize(results[policy], notional)
        lines.append(
            f"{policy:12} {row['n']:4d} {row['win']*100:6.2f}% "
            f"{row['mean']*100:+7.2f}% {row['median']*100:+7.2f}% "
            f"{row['pf']:6.2f} {row['catastrophic']*100:5.1f}% {row['pnl_sol']:+8.2f}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", nargs="+", default=list(LANE_DAYS))
    parser.add_argument("--fresh-ms", type=int, default=DEFAULT_FRESH_MS)
    parser.add_argument("--fee", type=float, default=DEFAULT_FEE)
    parser.add_argument("--notional-sol", type=float, default=DEFAULT_NOTIONAL_SOL)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    per_day = {}
    for day in args.days:
        per_day[day] = run_day(day, args.fresh_ms, args.fee)
        print(format_table(day, per_day[day], args.notional_sol))
        print()
    combined = {
        policy: [value for day in args.days for value in per_day[day][policy]]
        for policy in POLICIES
    }
    print(format_table(f"combined fresh_ms={args.fresh_ms}", combined, args.notional_sol))
    if args.out is not None:
        args.out.write_text(json.dumps(combined, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
