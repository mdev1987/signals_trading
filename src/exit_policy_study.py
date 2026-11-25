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
    env PYTHONPATH=src .venv/bin/python src/exit_policy_study.py \
        --artifact output/backtest/exit_study/artifact_fresh60s.json

The artifact is the compact research record: every trade and policy with its
entry/exit evidence, the summary metrics and a mint-cluster percentile
bootstrap (seed and resample count recorded), so the numbers quoted in
``docs/EXIT_POLICY_STUDY.md`` can be regenerated from the same inputs.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import json
import statistics
from pathlib import Path

from alert_outcomes import iter_target_trades
from forward_gates import DEFAULT_RESAMPLES, DEFAULT_SEED, cluster_bootstrap

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


def _print_at(
    times: list[int],
    prices: list[float],
    when: int,
    fresh_ms: int,
) -> tuple[int, float] | None:
    """The first print at/after ``when`` inside the freshness window."""
    idx = bisect.bisect_left(times, when)
    if idx < len(times) and times[idx] - when <= fresh_ms:
        return times[idx], prices[idx]
    return None


def close_at(
    times: list[int],
    prices: list[float],
    when: int,
    fresh_ms: int,
) -> float | None:
    """Price of the first print at/after ``when`` inside the freshness window."""
    found = _print_at(times, prices, when, fresh_ms)
    return None if found is None else found[1]


def _leg(
    reason: str,
    ts_ms: int | None,
    exit_px: float | None,
    entry_px: float,
    fee: float,
    weight: float = 1.0,
) -> dict:
    """One priced leg of a policy outcome.

    ``fresh`` is the study's executable-exit test: a scheduled leg without a
    print inside the freshness window is a -100 % write-off, not a stale mark.
    A trigger leg fires on a print that is in the series by construction.
    """
    return {
        "reason": reason,
        "exit_ts_ms": ts_ms,
        "exit_price_sol": exit_px,
        "fresh": exit_px is not None,
        "net": -1.0 if exit_px is None else net(entry_px, exit_px, fee),
        "weight": weight,
    }


def _scheduled(
    reason: str,
    times: list[int],
    prices: list[float],
    when: int,
    fresh_ms: int,
    entry_px: float,
    fee: float,
) -> dict:
    found = _print_at(times, prices, when, fresh_ms)
    if found is None:
        return _leg(f"{reason}_no_fresh_print", None, None, entry_px, fee)
    return _leg(reason, found[0], found[1], entry_px, fee)


def simulate_trade(
    row: dict,
    series: list[tuple[int, float]],
    policy: str,
    fresh_ms: int,
    fee: float = DEFAULT_FEE,
) -> dict:
    """Return the policy outcome for one entry, with per-leg evidence.

    The arithmetic is the pre-registered simulator's: scheduled exits need a
    print within ``fresh_ms`` or the leg is written off, trigger exits sell at
    the triggering print, and every leg is net of ``fee`` per side. The extra
    fields exist so the study can emit a reproducible per-trade artifact; they
    do not change any exit decision.
    """
    entry_ts, entry_px = row["entry_ts"], row["entry_px"]
    times = [ts for ts, _ in series]
    prices = [px for _, px in series]
    horizon = entry_ts + HORIZON_MS
    last = entry_ts + MAX_HOLD_MS

    def scheduled(reason: str, when: int) -> dict:
        return _scheduled(reason, times, prices, when, fresh_ms, entry_px, fee)

    if policy == "base5":
        legs = [scheduled("horizon", horizon)]
    elif policy == "hold15":
        legs = [scheduled("hold15", last)]
    elif policy.startswith("trail"):
        body = policy[5:]
        trail = int(body.split("_")[0]) / 100.0
        # The plain form trails until the 15-minute cap; the ``_5`` form keeps
        # the deployed 5-minute deadline and only adds the stop.
        deadline = horizon if body.endswith("_5") else last
        peak = 0.0
        start = bisect.bisect_left(times, entry_ts + 60_000)
        trigger: dict | None = None
        for idx in range(start, len(times)):
            ts, px = series[idx]
            if ts > deadline:
                break
            peak = max(peak, px)
            if px <= peak * (1 - trail):
                trigger = _leg("trailing_stop", ts, px, entry_px, fee)
                break
        legs = [
            trigger
            if trigger is not None
            else scheduled("cap15" if deadline == last else "horizon", deadline)
        ]
    elif policy.startswith("stop"):
        # Absolute stop from entry, no activation, deployed 5-minute deadline.
        threshold = int(policy[4:].split("_")[0]) / 100.0
        floor = entry_px * (1 - threshold)
        trigger = None
        for ts, px in series:
            if ts > horizon:
                break
            if px <= floor:
                trigger = _leg("stop_loss", ts, px, entry_px, fee)
                break
        legs = [trigger if trigger is not None else scheduled("horizon", horizon)]
    elif policy in ("tp50all", "tp100all", "tp150all"):
        tp = {"tp50all": 0.5, "tp100all": 1.0, "tp150all": 1.5}[policy]
        target = entry_px * (1 + tp)
        trigger = None
        for ts, px in series:
            if ts > horizon:
                break
            if px >= target:
                trigger = _leg("take_profit", ts, px, entry_px, fee)
                break
        legs = [trigger if trigger is not None else scheduled("horizon", horizon)]
    elif policy.startswith("tp"):  # tp50h5, tp75h5, tp100h5, tp50ht30
        tp = {"50": 0.5, "75": 0.75, "100": 1.0}[policy[2:5].rstrip("h")]
        rest = policy.split("h")[1]
        target = entry_px * (1 + tp)
        leg = None
        for ts, px in series:
            if ts > horizon:
                break
            if px >= target:
                leg = _leg("take_profit", ts, px, entry_px, fee, weight=0.5)
                break
        if rest == "5":
            tail = scheduled("horizon", horizon)
        else:
            peak = 0.0
            tail = scheduled("hold15", last)
            start = bisect.bisect_left(times, entry_ts + 60_000)
            for idx in range(start, len(times)):
                ts, px = series[idx]
                if ts > last:
                    break
                peak = max(peak, px)
                if px <= peak * 0.70:
                    tail = _leg("trailing_stop", ts, px, entry_px, fee)
                    break
        if leg is None:
            legs = [tail]
        else:
            tail["weight"] = 0.5
            legs = [leg, tail]
    elif policy.startswith("dyn"):
        floor = int(policy[3:]) / 100.0
        when = horizon
        legs = []
        while when <= last:
            leg = scheduled("dynamic_tick", when)
            if not leg["fresh"]:
                # The pre-registered rule writes the whole trade off as soon
                # as a scheduled check has no executable print.
                legs = [leg]
                break
            if leg["exit_price_sol"] < entry_px * (1 + floor):
                legs = [_leg("dynamic_floor", leg["exit_ts_ms"], leg["exit_price_sol"], entry_px, fee)]
                break
            when += 30_000
        else:
            legs = [scheduled("hold15", last)]
    elif policy == "ladder":
        legs = [
            scheduled(f"ladder_{delta // 1000}s", entry_ts + delta)
            for delta in (120_000, 300_000, 600_000)
        ]
    elif policy in ("oracle5", "oracle15"):
        end = horizon if policy == "oracle5" else last
        best_px: float | None = None
        best_ts: int | None = None
        for ts, px in series:
            if entry_ts < ts <= end and (best_px is None or px > best_px):
                best_px, best_ts = px, ts
        legs = [_leg("oracle_best", best_ts, best_px, entry_px, fee)]
    else:
        raise ValueError(f"unknown policy: {policy}")

    if policy == "ladder":
        total = sum(leg["net"] for leg in legs) / len(legs)
    else:
        total = sum(leg["net"] * leg["weight"] for leg in legs)
    final = legs[-1]
    return {
        "mint": row["mint"],
        "entry_ts_ms": entry_ts,
        "entry_price_sol": entry_px,
        "policy": policy,
        "fresh_ms": fresh_ms,
        "exit_ts_ms": final["exit_ts_ms"],
        "exit_price_sol": final["exit_price_sol"],
        "exit_reason": "+".join(dict.fromkeys(leg["reason"] for leg in legs)),
        "fresh": all(leg["fresh"] for leg in legs),
        "net": total,
        "legs": legs,
    }


def simulate(
    row: dict,
    series: list[tuple[int, float]],
    policy: str,
    fresh_ms: int,
    fee: float = DEFAULT_FEE,
) -> float:
    """Return the policy's net return for one entry. -1.0 means unexecutable."""
    return simulate_trade(row, series, policy, fresh_ms, fee)["net"]


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


def run_day_details(
    day: str, fresh_ms: int, fee: float
) -> dict[str, list[dict]]:
    """Per-trade outcomes with the evidence each exit was priced from."""
    rows = lane_rows(day)
    prints = collect_prints(day, rows)
    return {
        policy: [
            simulate_trade(row, prints[row["mint"]], policy, fresh_ms, fee)
            for row in rows
        ]
        for policy in POLICIES
    }


def run_day(day: str, fresh_ms: int, fee: float) -> dict[str, list[float]]:
    details = run_day_details(day, fresh_ms, fee)
    return {
        policy: [trade["net"] for trade in details[policy]]
        for policy in POLICIES
    }


def build_artifact(
    per_day: dict[str, dict[str, list[dict]]],
    *,
    days: list[str],
    fresh_ms: int,
    fee: float,
    notional_sol: float,
    resamples: int = DEFAULT_RESAMPLES,
    seed: int = DEFAULT_SEED,
) -> dict:
    """Compact, mint-keyed study record: trades, summary, bootstrap.

    This is the artifact ``docs/EXIT_POLICY_STUDY.md`` is checked against. The
    bootstrap is the same mint-cluster implementation the forward gate uses
    (``forward_gates.cluster_bootstrap``): clusters are mints, the statistic is
    total net SOL, the interval is the 5th-95th percentile, and the seed and
    resample count are recorded so the numbers can be regenerated exactly.
    """
    policies: dict[str, dict] = {}
    for policy in POLICIES:
        trades: list[dict] = []
        for day in days:
            for trade in per_day[day][policy]:
                row = {
                    "mint": trade["mint"],
                    "entry_ts_ms": trade["entry_ts_ms"],
                    "entry_price_sol": trade["entry_price_sol"],
                    "exit_ts_ms": trade["exit_ts_ms"],
                    "exit_price_sol": trade["exit_price_sol"],
                    "exit_reason": trade["exit_reason"],
                    "fresh": trade["fresh"],
                    "net": trade["net"],
                    "net_sol": trade["net"] * notional_sol,
                }
                if len(trade["legs"]) > 1:
                    row["legs"] = [
                        {
                            "reason": leg["reason"],
                            "exit_ts_ms": leg["exit_ts_ms"],
                            "exit_price_sol": leg["exit_price_sol"],
                            "fresh": leg["fresh"],
                            "net": leg["net"],
                            "weight": leg["weight"],
                        }
                        for leg in trade["legs"]
                    ]
                trades.append(row)
        returns = [trade["net"] for trade in trades]
        pnls = [trade["net"] * notional_sol for trade in trades]
        mints = [trade["mint"] for trade in trades]
        policies[policy] = {
            "summary": summarize(returns, notional_sol),
            "bootstrap": {
                "method": "percentile_over_mint_clusters",
                "cluster_key": "mint",
                "statistic": "total_net_sol",
                "interval": "5th-95th percentile (linear interpolation)",
                "resamples": resamples,
                "seed": seed,
                **(cluster_bootstrap(pnls, mints, resamples=resamples, seed=seed) or {}),
            },
            "trades": trades,
        }
    return {
        "study": {
            "script": "src/exit_policy_study.py",
            "days": list(days),
            "fresh_ms": fresh_ms,
            "fee": fee,
            "notional_sol": notional_sol,
            "horizon_ms": HORIZON_MS,
            "max_hold_ms": MAX_HOLD_MS,
            "policies": list(POLICIES),
            "entry_rule": (
                "first BUY print at card + 5 s, dex == Pumpfunamm, "
                "20k-100k USD launch liquidity"
            ),
            "note": (
                "The 21-22 Nov days are selection data for the exit policy. "
                "TP50's independent forward sample is the paper lane, not this "
                "table, and the two must never be pooled."
            ),
            "bootstrap": {
                "method": "percentile_over_mint_clusters",
                "cluster_key": "mint",
                "statistic": "total_net_sol",
                "quantile_lower": 0.05,
                "quantile_upper": 0.95,
                "resamples": resamples,
                "seed": seed,
            },
        },
        "policies": policies,
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
    parser.add_argument(
        "--artifact",
        type=Path,
        default=None,
        help=(
            "write the compact mint-keyed study record (per-trade evidence, "
            "summary and mint-cluster bootstrap) to this JSON file"
        ),
    )
    parser.add_argument(
        "--bootstrap-seed", type=int, default=DEFAULT_SEED
    )
    parser.add_argument(
        "--bootstrap-resamples", type=int, default=DEFAULT_RESAMPLES
    )
    args = parser.parse_args(argv)

    details: dict[str, dict[str, list[dict]]] = {}
    for day in args.days:
        details[day] = run_day_details(day, args.fresh_ms, args.fee)
        returns = {
            policy: [trade["net"] for trade in details[day][policy]]
            for policy in POLICIES
        }
        print(format_table(day, returns, args.notional_sol))
        print()
    combined = {
        policy: [
            trade["net"]
            for day in args.days
            for trade in details[day][policy]
        ]
        for policy in POLICIES
    }
    print(
        format_table(
            f"combined fresh_ms={args.fresh_ms}", combined, args.notional_sol
        )
    )
    if args.out is not None:
        args.out.write_text(json.dumps(combined, indent=2))
    if args.artifact is not None:
        artifact = build_artifact(
            details,
            days=args.days,
            fresh_ms=args.fresh_ms,
            fee=args.fee,
            notional_sol=args.notional_sol,
            resamples=args.bootstrap_resamples,
            seed=args.bootstrap_seed,
        )
        args.artifact.parent.mkdir(parents=True, exist_ok=True)
        args.artifact.write_text(
            json.dumps(artifact, sort_keys=True, separators=(",", ":"))
        )
        tp50 = artifact["policies"]["tp50all"]["bootstrap"]
        print()
        print(
            "tp50all mint-cluster bootstrap: "
            f"seed={tp50['seed']} resamples={int(tp50['resamples'])} "
            f"clusters={int(tp50['clusters'])} "
            f"p05={tp50['lower_sol']:+.2f} SOL "
            f"p95={tp50['upper_sol']:+.2f} SOL "
            f"P(>0)={tp50['probability_positive']:.2f}"
        )
        print(f"artifact: {args.artifact}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
