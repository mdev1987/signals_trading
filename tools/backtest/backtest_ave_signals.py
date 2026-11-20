#!/usr/bin/env python3
"""Standalone backtest of the AveSignalMonitor Telegram signals.

No VectorBT, no portfolio engine, no simulator: this script only measures how
the signal mints moved after each message.

Execution convention (same as the rest of the project, so numbers stay
comparable):

* entry decision  = signal timestamp + ``--latency-ms`` (default 5 s);
* entry price     = close of the latest **real** 1-minute bar whose close time
                    is at or before the entry decision;
* exit decision   = entry decision + horizon;
* exit price      = close of the latest real bar at or before the exit decision;
* ``is_filled == 1`` bars are ignored (stale forward-filled rows).

Returns are reported both gross and net of ``--fee-rate`` on each side.
The per-signal table also records maximum favourable/adverse excursion over the
holding window using real bar highs/lows (sampled on the 1-minute grid).

Usage (from the repository root)::

    python tools/backtest/backtest_ave_signals.py
    python tools/backtest/backtest_ave_signals.py --days 2026-11-08
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

REPO_ROOT = Path(__file__).resolve().parents[2]
SIGNALS_ROOT = Path("output/backtest/signals")
DAYS_ROOT = Path("output/backtest/days")
DEFAULT_OUTPUT = Path("output/backtest/ave_signals")
MINUTE_MS = 60_000

DEFAULT_DAYS = (
    "2026-08-09",
    "2026-10-03",
    "2026-10-04",
    "2026-10-05",
    "2026-11-08",
)
DEFAULT_HORIZONS = (1, 3, 5, 15, 30, 60)


@dataclass(frozen=True, slots=True)
class MintBars:
    close_ts: np.ndarray
    close_px: np.ndarray
    high_px: np.ndarray
    low_px: np.ndarray


def load_signals(
    repo_root: Path, days: list[str], channel: str
) -> pd.DataFrame:
    frames = []
    for day in days:
        path = repo_root / SIGNALS_ROOT / day / "strategy_signals.csv"
        if not path.exists():
            raise FileNotFoundError(f"Missing signals file: {path}")
        frame = pd.read_csv(path)
        frame["day"] = day
        frames.append(frame)
    signals = pd.concat(frames, ignore_index=True)
    wanted = channel.lower().lstrip("@")
    usernames = (
        signals["source_username"].astype(str).str.lower().str.lstrip("@")
    )
    signals = signals[usernames == wanted].copy()
    signals["signal_timestamp_ms"] = signals["signal_timestamp_ms"].astype("int64")
    signals = signals.sort_values(
        ["day", "signal_timestamp_ms", "signal_id"], kind="stable"
    ).reset_index(drop=True)
    return signals


def load_bars(repo_root: Path, day: str, mints: list[str]) -> dict[str, MintBars]:
    path = repo_root / DAYS_ROOT / day / "ohlcv" / "ohlcv_1m.parquet"
    if not path.exists():
        raise FileNotFoundError(f"Missing OHLCV parquet: {path}")
    available = set(pq.ParquetFile(path).schema_arrow.names)
    columns = [
        name
        for name in ("mint", "timestamp_ms", "high", "low", "close", "is_filled")
        if name in available
    ]
    frame = pq.read_table(
        path, columns=columns, filters=[("mint", "in", sorted(set(mints)))]
    ).to_pandas()
    if frame.empty:
        return {}

    real = (frame["is_filled"].fillna(1).astype("int8") == 0) & np.isfinite(
        frame["close"]
    )
    frame = frame[real].copy()
    frame["close_ts"] = frame["timestamp_ms"].astype("int64") + MINUTE_MS
    bars: dict[str, MintBars] = {}
    for mint, group in frame.groupby("mint", sort=True):
        group = group.sort_values("close_ts", kind="stable")
        bars[str(mint)] = MintBars(
            close_ts=group["close_ts"].to_numpy(dtype="int64"),
            close_px=group["close"].to_numpy(dtype="float64"),
            high_px=group["high"].to_numpy(dtype="float64"),
            low_px=group["low"].to_numpy(dtype="float64"),
        )
    return bars


def load_flagged_mints(repo_root: Path, day: str) -> set[str]:
    """Mints with at least one candle-range anomaly flag on ``day``.

    The flag files are produced by the dataset builder. They are optional: a
    missing file simply means "nothing flagged".
    """
    path = (
        repo_root
        / DAYS_ROOT
        / day
        / "ohlcv"
        / "flagged_candle_range_anomaly_1m.csv"
    )
    if not path.exists():
        return set()
    frame = pd.read_csv(path, usecols=["mint"])
    return set(frame["mint"].dropna().astype(str))


def _last_at_or_before(bars: MintBars, timestamp_ms: int) -> tuple[int, float] | None:
    index = int(np.searchsorted(bars.close_ts, timestamp_ms, side="right")) - 1
    if index < 0:
        return None
    return int(bars.close_ts[index]), float(bars.close_px[index])


def _excursions(
    bars: MintBars, start_ts: int, end_ts: int
) -> tuple[float | None, float | None]:
    left = int(np.searchsorted(bars.close_ts, start_ts, side="right"))
    right = int(np.searchsorted(bars.close_ts, end_ts, side="right"))
    if right <= left:
        return None, None
    window = slice(left, right)
    high = float(np.nanmax(bars.high_px[window]))
    low = float(np.nanmin(bars.low_px[window]))
    if not np.isfinite(high) or not np.isfinite(low):
        return None, None
    return high, low


def backtest(
    signals: pd.DataFrame,
    bars_by_day: dict[str, dict[str, MintBars]],
    *,
    horizons: tuple[int, ...],
    latency_ms: int,
    fee_rate: float,
    stake_sol: float,
    max_price_age_ms: int,
    flagged_mints_by_day: dict[str, set[str]] | None = None,
) -> pd.DataFrame:
    flagged_mints_by_day = flagged_mints_by_day or {}
    rows: list[dict[str, object]] = []
    for signal in signals.itertuples(index=False):
        day = str(signal.day)
        mint = str(signal.mint)
        bars = bars_by_day.get(day, {}).get(mint)
        signal_ts = int(signal.signal_timestamp_ms)
        decision_ts = signal_ts + latency_ms
        base = {
            "day": day,
            "signal_id": signal.signal_id,
            "signal_timestamp_ms": signal_ts,
            "signal_timestamp_utc": signal.signal_timestamp_utc,
            "mint": mint,
            "source_username": signal.source_username,
            "signal_type": signal.signal_type,
            "entry_decision_timestamp_ms": decision_ts,
            "flagged_mint": mint in flagged_mints_by_day.get(day, set()),
        }

        if bars is None or bars.close_ts.size == 0:
            for horizon in horizons:
                rows.append(
                    {
                        **base,
                        "horizon_minutes": horizon,
                        "status": "no_bars",
                    }
                )
            continue

        entry = _last_at_or_before(bars, decision_ts)
        if entry is None:
            for horizon in horizons:
                rows.append(
                    {**base, "horizon_minutes": horizon, "status": "no_entry_price"}
                )
            continue
        entry_ts, entry_px = entry
        entry_age_ms = decision_ts - entry_ts
        base.update(
            {
                "entry_price_timestamp_ms": entry_ts,
                "entry_price_sol": entry_px,
                "entry_price_age_ms": entry_age_ms,
            }
        )
        fresh_entry = entry_age_ms <= max_price_age_ms

        for horizon in horizons:
            exit_decision_ts = decision_ts + horizon * MINUTE_MS
            exit_ = _last_at_or_before(bars, exit_decision_ts)
            if exit_ is None:
                rows.append(
                    {
                        **base,
                        "horizon_minutes": horizon,
                        "status": "no_exit_price",
                    }
                )
                continue
            exit_ts, exit_px = exit_
            if exit_ts <= entry_ts:
                rows.append(
                    {
                        **base,
                        "horizon_minutes": horizon,
                        "status": "no_move_window",
                    }
                )
                continue

            gross = exit_px / entry_px - 1.0
            net = (exit_px * (1.0 - fee_rate)) / (
                entry_px * (1.0 + fee_rate)
            ) - 1.0
            high, low = _excursions(bars, entry_ts, exit_ts)
            rows.append(
                {
                    **base,
                    "horizon_minutes": horizon,
                    "status": "closed",
                    "entry_fresh": bool(fresh_entry),
                    "exit_decision_timestamp_ms": exit_decision_ts,
                    "exit_price_timestamp_ms": exit_ts,
                    "exit_price_sol": exit_px,
                    "exit_price_age_ms": exit_decision_ts - exit_ts,
                    "gross_return": gross,
                    "net_return": net,
                    "net_pnl_sol": stake_sol * net,
                    "mfe": (high / entry_px - 1.0) if high else None,
                    "mae": (low / entry_px - 1.0) if low else None,
                }
            )
    return pd.DataFrame(rows)


def summarise(results: pd.DataFrame, horizons: tuple[int, ...]) -> pd.DataFrame:
    closed = results[results["status"] == "closed"].copy()
    rows = []
    for horizon in horizons:
        block = closed[closed["horizon_minutes"] == horizon]
        if block.empty:
            rows.append({"horizon_minutes": horizon, "trades": 0})
            continue
        returns = block["net_return"].astype("float64")
        ages = block["exit_price_age_ms"].astype("float64")
        rows.append(
            {
                "horizon_minutes": horizon,
                "trades": int(len(block)),
                "win_rate": float((returns > 0).mean()),
                "mean_net_return": float(returns.mean()),
                "median_net_return": float(returns.median()),
                "p10_net_return": float(returns.quantile(0.10)),
                "p90_net_return": float(returns.quantile(0.90)),
                "best_net_return": float(returns.max()),
                "worst_net_return": float(returns.min()),
                "total_net_pnl_sol": float(block["net_pnl_sol"].sum()),
                "mean_mfe": float(block["mfe"].mean()),
                "mean_mae": float(block["mae"].mean()),
                "mean_net_return_ex_top1pct": float(
                    returns[returns <= returns.quantile(0.99)].mean()
                ),
                "stale_exit_ratio": float((ages > 60_000).mean()),
                "median_exit_price_age_ms": float(ages.median()),
                "max_exit_price_age_ms": float(ages.max()),
            }
        )
    return pd.DataFrame(rows)


def summarise_by_day(results: pd.DataFrame, horizon: int) -> pd.DataFrame:
    closed = results[
        (results["status"] == "closed") & (results["horizon_minutes"] == horizon)
    ]
    if closed.empty:
        return pd.DataFrame()
    grouped = closed.groupby("day")["net_return"]
    summary = grouped.agg(
        trades="size",
        win_rate=lambda values: float((values > 0).mean()),
        mean_net_return="mean",
        median_net_return="median",
    )
    summary["total_net_pnl_sol"] = closed.groupby("day")["net_pnl_sol"].sum()
    return summary.reset_index()


def summarise_sensitivity(results: pd.DataFrame, horizon: int) -> pd.DataFrame:
    """Compare all closed signals with the subset on flagged mints removed."""
    closed = results[
        (results["status"] == "closed") & (results["horizon_minutes"] == horizon)
    ]
    rows = []
    for label, mask in (
        ("all", pd.Series(True, index=closed.index)),
        ("ex_flagged_mints", ~closed["flagged_mint"].astype(bool)),
    ):
        block = closed[mask]
        if block.empty:
            rows.append({"subset": label, "trades": 0})
            continue
        returns = block["net_return"].astype("float64")
        rows.append(
            {
                "subset": label,
                "trades": int(len(block)),
                "win_rate": float((returns > 0).mean()),
                "mean_net_return": float(returns.mean()),
                "median_net_return": float(returns.median()),
                "total_net_pnl_sol": float(block["net_pnl_sol"].sum()),
            }
        )
    return pd.DataFrame(rows)


def summarise_exclusions(
    results: pd.DataFrame, signals: pd.DataFrame, horizon: int
) -> pd.DataFrame:
    """Signals, closed signals and exclusions per day at one horizon."""
    block = results[results["horizon_minutes"] == horizon]
    available = signals.groupby("day").size().rename("signals")
    closed = (
        block[block["status"] == "closed"].groupby("day").size().rename("closed")
    )
    frame = pd.concat([available, closed], axis=1).fillna(0).astype("int64")
    frame["excluded"] = frame["signals"] - frame["closed"]
    return frame.reset_index()


def _fmt_pct(value: float | None) -> str:
    if value is None or not np.isfinite(value):
        return "-"
    return f"{100.0 * value:+.2f} %"


def render_markdown(
    summary: pd.DataFrame,
    by_day: pd.DataFrame,
    sensitivity: pd.DataFrame,
    exclusions: pd.DataFrame,
    signals: pd.DataFrame,
    results: pd.DataFrame,
    horizon: int,
    config: dict[str, object],
) -> str:
    lines = [
        "# AveSignalMonitor signal backtest",
        "",
        "Standalone OHLCV evaluation of the Telegram signals. No VectorBT, no "
        "portfolio engine, no AMM simulation.",
        "",
        "## Configuration",
        "",
        f"- Channel: `{config['channel']}`",
        f"- Signals: {len(signals)}",
        f"- Entry: signal timestamp + {config['latency_ms']} ms latency, latest "
        "real 1-minute close at or before the decision.",
        "- Exit: entry decision + horizon, latest real close at or before the "
        "exit decision.",
        f"- Fee: {config['fee_rate']} per side, stake {config['stake_sol']} SOL.",
        "- Stale bars (`is_filled == 1`) are ignored.",
        "",
        "## Results by horizon",
        "",
        "| Horizon | Trades | Win rate | Mean net | Mean ex-top 1% | "
        "Median net | P10 | P90 | Mean MFE | Mean MAE | Stale exits | "
        "Total PnL (SOL) |",
        "| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | "
        "---: | ---: | ---: |",
    ]
    for row in summary.itertuples(index=False):
        if getattr(row, "trades", 0) == 0:
            continue
        lines.append(
            f"| {row.horizon_minutes}m | {row.trades} | "
            f"{100.0 * row.win_rate:.1f} % | {_fmt_pct(row.mean_net_return)} | "
            f"{_fmt_pct(row.mean_net_return_ex_top1pct)} | "
            f"{_fmt_pct(row.median_net_return)} | "
            f"{_fmt_pct(row.p10_net_return)} | {_fmt_pct(row.p90_net_return)} | "
            f"{_fmt_pct(row.mean_mfe)} | {_fmt_pct(row.mean_mae)} | "
            f"{100.0 * row.stale_exit_ratio:.1f} % | "
            f"{row.total_net_pnl_sol:+.4f} |"
        )
    lines += [
        "",
        f"## By day ({horizon}m horizon)",
        "",
        "| Day | Trades | Win rate | Mean net | Median net | Total PnL (SOL) |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in by_day.itertuples(index=False):
        lines.append(
            f"| {row.day} | {row.trades} | {100.0 * row.win_rate:.1f} % | "
            f"{_fmt_pct(row.mean_net_return)} | "
            f"{_fmt_pct(row.median_net_return)} | {row.total_net_pnl_sol:+.4f} |"
        )
    lines += [
        "",
        f"## Flagged-candle sensitivity ({horizon}m horizon)",
        "",
        "The dataset builder flags candles whose intra-minute range is abnormal. "
        "The check below removes every signal on a mint that has at least one "
        "flagged candle that day.",
        "",
        "| Subset | Trades | Win rate | Mean net | Median net | Total PnL (SOL) |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in sensitivity.itertuples(index=False):
        if row.trades == 0:
            continue
        lines.append(
            f"| {row.subset} | {row.trades} | {100.0 * row.win_rate:.1f} % | "
            f"{_fmt_pct(row.mean_net_return)} | "
            f"{_fmt_pct(row.median_net_return)} | {row.total_net_pnl_sol:+.4f} |"
        )
    lines += [
        "",
        f"## Signal coverage ({horizon}m horizon)",
        "",
        "Signals that could not be priced are excluded, not forced closed.",
        "",
        "| Day | Signals | Closed | Excluded |",
        "| --- | ---: | ---: | ---: |",
    ]
    for row in exclusions.itertuples(index=False):
        lines.append(
            f"| {row.day} | {row.signals} | {row.closed} | {row.excluded} |"
        )
    unique_entry = results.dropna(subset=["entry_price_age_ms"]).drop_duplicates(
        "signal_id"
    )
    fresh_share = (
        float((unique_entry["entry_price_age_ms"] <= 60000).mean())
        if not unique_entry.empty
        else float("nan")
    )
    lines += [
        "",
        "## Diagnostics",
        "",
        f"- {len(signals)} alerts on {signals['mint'].nunique()} distinct mints.",
        f"- {int((results['status'] == 'no_bars').sum() // max(len(config['horizons']), 1))} "
        "alerts have no OHLCV rows; "
        f"{int((results['status'] == 'no_entry_price').sum() // max(len(config['horizons']), 1))} "
        "have no real close at or before the entry decision.",
        f"- Among priceable alerts, the entry close was at most 60 s old for "
        f"{100.0 * fresh_share:.1f} %.",
        "- Exit prices can be stale when a mint stops trading; `Stale exits` is "
        "the share of exits resting on a close more than 60 s old.",
        "",
        "## Notes",
        "",
        "- Returns are per signal, equal stake; they do not model slot or cash "
        "limits, routing, PIT eligibility or pool liquidity.",
        "- MFE/MAE use the real bar highs/lows between entry and exit (1-minute "
        "sampling).",
        "- A signal is excluded from the return statistics when no real close "
        "exists for the requested window.",
        "",
    ]
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--days", nargs="+", default=list(DEFAULT_DAYS), help="Day folders"
    )
    parser.add_argument(
        "--channel", default="AveSignalMonitor", help="Signal source username"
    )
    parser.add_argument(
        "--horizons",
        nargs="+",
        type=int,
        default=list(DEFAULT_HORIZONS),
        help="Holding horizons in minutes",
    )
    parser.add_argument("--latency-ms", type=int, default=5_000)
    parser.add_argument("--fee-rate", type=float, default=0.0125)
    parser.add_argument("--stake-sol", type=float, default=0.1)
    parser.add_argument("--max-price-age-ms", type=int, default=60_000)
    parser.add_argument(
        "--exclude-flagged-mints",
        action="store_true",
        help="Drop signals on mints with flagged candle-range anomalies",
    )
    parser.add_argument(
        "--output-dir", type=Path, default=DEFAULT_OUTPUT
    )
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    repo_root = args.repo_root.resolve()
    output_dir = (
        args.output_dir
        if args.output_dir.is_absolute()
        else repo_root / args.output_dir
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    horizons = tuple(sorted(set(args.horizons)))

    signals = load_signals(repo_root, list(args.days), args.channel)
    if signals.empty:
        raise SystemExit(f"No signals found for channel {args.channel!r}")
    bars_by_day = {
        day: load_bars(repo_root, day, signals.loc[signals.day == day, "mint"].tolist())
        for day in args.days
    }
    flagged_mints_by_day = {
        day: load_flagged_mints(repo_root, day) for day in args.days
    }
    if args.exclude_flagged_mints:
        keep = [
            row.mint not in flagged_mints_by_day.get(str(row.day), set())
            for row in signals.itertuples(index=False)
        ]
        signals = signals[keep].reset_index(drop=True)
        if signals.empty:
            raise SystemExit("All signals were dropped by --exclude-flagged-mints")
    results = backtest(
        signals,
        bars_by_day,
        horizons=horizons,
        latency_ms=args.latency_ms,
        fee_rate=args.fee_rate,
        stake_sol=args.stake_sol,
        max_price_age_ms=args.max_price_age_ms,
        flagged_mints_by_day=flagged_mints_by_day,
    )
    summary = summarise(results, horizons)
    primary = 30 if 30 in horizons else horizons[0]
    by_day = summarise_by_day(results, primary)
    sensitivity = summarise_sensitivity(results, primary)
    exclusions = summarise_exclusions(results, signals, primary)
    config = {
        "channel": args.channel,
        "days": list(args.days),
        "latency_ms": args.latency_ms,
        "fee_rate": args.fee_rate,
        "stake_sol": args.stake_sol,
        "max_price_age_ms": args.max_price_age_ms,
        "exclude_flagged_mints": bool(args.exclude_flagged_mints),
        "horizons": list(horizons),
        "generated_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "engine": "standalone_pandas_ohlcv",
        "note": (
            "Signal-quality evaluation only. Not an execution simulation and "
            "not the VectorBT portfolio baseline."
        ),
    }
    results.to_csv(output_dir / "signals_backtest.csv", index=False)
    summary.to_csv(output_dir / "summary_by_horizon.csv", index=False)
    by_day.to_csv(output_dir / "summary_by_day.csv", index=False)
    sensitivity.to_csv(output_dir / "sensitivity_flagged.csv", index=False)
    exclusions.to_csv(output_dir / "coverage_by_day.csv", index=False)
    (output_dir / "summary.json").write_text(
        json.dumps(
            {
                "config": config,
                "by_horizon": summary.to_dict(orient="records"),
                "by_day": by_day.to_dict(orient="records"),
                "signal_counts": {
                    "total": int(len(signals)),
                    "closed": {
                        str(horizon): int(
                            (
                                (results["horizon_minutes"] == horizon)
                                & (results["status"] == "closed")
                            ).sum()
                        )
                        for horizon in horizons
                    },
                    "status_counts": {
                        str(key): int(value)
                        for key, value in results["status"].value_counts().items()
                    },
                },
                "sensitivity_flagged": sensitivity.to_dict(orient="records"),
                "coverage_by_day": exclusions.to_dict(orient="records"),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (output_dir / "summary.md").write_text(
        render_markdown(
            summary,
            by_day,
            sensitivity,
            exclusions,
            signals,
            results,
            primary,
            config,
        ),
        encoding="utf-8",
    )

    print(f"signals: {len(signals)}")
    print(summary.to_string(index=False))
    print()
    print(by_day.to_string(index=False))
    print()
    print(f"outputs: {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
