#!/usr/bin/env python3
"""Compare the Telegram signal channels on realistic 5-minute outcomes.

``alert_outcomes.py`` prices every channel/broadcast alert with the last real
trade at or before the decision timestamp. This tool turns that per-alert table
into the comparison the strategy actually needs:

* base rate per channel (a curated buy signal vs a launch scanner);
* which parsed scanner fields separate winners from losers;
* what happens when two channels alert on the same mint;
* how much of the mean is one lucky trade.

Usage::

    python src/compare_channels.py --days 2026-11-20
    python src/compare_channels.py            # every priced day
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from paper_bot.signals import normalize_dex

ROOT = Path("output/backtest/signals_channels")
OUTPUT = Path("output/backtest/channel_comparison")
STAKE_SOL = 0.10
MIN_BUCKET = 100


def available(days: list[str] | None, tag: str = "") -> list[str]:
    suffix = f"_{tag}" if tag else ""
    if days:
        return days
    found = []
    for child in sorted(ROOT.iterdir()):
        if child.is_dir() and (child / f"alerts_priced{suffix}.csv").exists():
            found.append(child.name)
    return found


def load(
    days: list[str],
    horizon: int,
    tag: str = "",
    max_entry_delay_ms: int = 60_000,
    max_entry_age_ms: int = 2_000,
    fill_mode: str | None = None,
) -> pd.DataFrame:
    suffix = f"_{tag}" if tag else ""
    frames = []
    for day in days:
        path = ROOT / day / f"alerts_priced{suffix}.csv"
        if not path.exists():
            continue
        frame = pd.read_csv(path)
        frame["day"] = day
        if fill_mode is not None:
            if "entry_fill_mode" not in frame:
                # Silently ignoring the requested convention would mix fill
                # modes and present it as one comparison.
                raise ValueError(
                    f"{path} has no 'entry_fill_mode' column; cannot apply "
                    f"--fill-mode {fill_mode!r}"
                )
            # The pricing tool records the convention it used; never mix
            # last-before and first-after rows in one comparison.
            frame = frame[frame["entry_fill_mode"] == fill_mode]
        frame["return"] = frame[f"net_{horizon}m_return"]
        keep = (
            frame["entry_px"].notna()
            & frame["entry_ts"].notna()
            & frame["return"].notna()
            & np.isfinite(frame["return"])
        )
        if "entry_delay_ms" in frame and max_entry_delay_ms > 0:
            # A first-after fill that arrived minutes later is not a trade the
            # deployed 60 s window could have taken.
            keep &= frame["entry_delay_ms"].fillna(0) <= max_entry_delay_ms
        if "entry_price_age_ms" in frame and max_entry_age_ms > 0:
            # A last-before fill older than the tolerance is a stale mark.
            keep &= frame["entry_price_age_ms"].fillna(0) <= max_entry_age_ms
        frame = frame[keep].copy()
        frames.append(frame)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


AVE_OUTCOMES = Path("output/backtest/ave_signals_8d_event/signals_backtest.csv")


def load_ave(days: list[str], horizon: int) -> pd.DataFrame:
    """AVE outcomes from its own event-fill backtest (same fill convention)."""
    if not AVE_OUTCOMES.exists():
        return pd.DataFrame()
    frame = pd.read_csv(AVE_OUTCOMES)
    frame = frame[
        (frame["horizon_minutes"] == horizon)
        & (frame["status"] == "closed")
        & frame["day"].isin(days)
    ].copy()
    if frame.empty:
        return pd.DataFrame()
    frame["channel"] = "@AveSignalMonitor"
    frame["return"] = frame["net_return"]
    frame["alert_ts_ms"] = frame["signal_timestamp_ms"]
    return frame[
        [
            "day",
            "channel",
            "signal_id",
            "mint",
            "alert_ts_ms",
            "entry_ts",
            "entry_px",
            "entry_price_age_ms",
            "return",
        ]
    ]


def describe(values: pd.Series) -> dict[str, float | int]:
    if values.empty:
        return {"n": 0}
    ordered = values.sort_values()
    ex_top = ordered.iloc[:-1] if len(ordered) > 1 else ordered
    # Single bad prints (a 1e-11 SOL trade followed by a 6 SOL one) otherwise
    # dominate the mean; cap the contribution of any one trade at +1000 %.
    capped = values.clip(lower=-0.999, upper=9.99)
    return {
        "n": int(len(values)),
        "win_rate": float((values > 0).mean()),
        "mean": float(values.mean()),
        "mean_capped": float(capped.mean()),
        "median": float(values.median()),
        "p10": float(values.quantile(0.10)),
        "p90": float(values.quantile(0.90)),
        "pnl_sol": float(values.sum() * STAKE_SOL),
        "pnl_capped": float(capped.sum() * STAKE_SOL),
        "mean_ex_top": float(ex_top.mean()) if len(ex_top) else np.nan,
    }


def table(rows: list[dict]) -> pd.DataFrame:
    frame = pd.DataFrame(rows)
    for column in (
        "win_rate",
        "mean",
        "mean_capped",
        "median",
        "p10",
        "p90",
        "mean_ex_top",
    ):
        if column in frame:
            frame[column] = (frame[column] * 100).round(1)
    for column in ("pnl_sol", "pnl_capped"):
        if column in frame:
            frame[column] = frame[column].round(2)
    return frame


def deployed_scanner_rule(frame: pd.DataFrame) -> pd.DataFrame:
    """Filter to the deployed lane: AVE, first card, Pumpfunamm, 20-100k.

    ``alert_outcomes.py`` prices every alert; the strategy itself trades only
    the first card per mint and only inside the band. Keeping this in the
    comparison tool makes the headline table reproducible with one flag.
    """
    if frame.empty:
        return frame
    block = frame[frame["channel"] == "@AveSolanaTokenScanner"].copy()
    if block.empty:
        return block
    block = block.sort_values(["mint", "alert_ts_ms"]).drop_duplicates(
        "mint", keep="first"
    )
    dex = block["dex"].map(normalize_dex)
    liquidity = pd.to_numeric(block["liquidity_usd"], errors="coerce")
    return block[
        (dex == normalize_dex("Pumpfunamm"))
        & (liquidity >= 20_000)
        & (liquidity < 100_000)
    ].copy()


def channel_table(frame: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for channel, block in frame.groupby("channel"):
        row = {"channel": channel, "priced": len(block)}
        row.update(describe(block["return"]))
        # Negative = the fill used a print older than the decision (a stale
        # mark); positive = the bot waited for a print after it.
        if "entry_delay_ms" in block:
            lag = block["entry_delay_ms"]
        else:
            lag = -block["entry_price_age_ms"]
        row["entry_lag_s_median"] = round(float(lag.median()) / 1000, 1)
        rows.append(row)
    return table(rows).sort_values("n", ascending=False)


def quantile_buckets(
    frame: pd.DataFrame, column: str, *, buckets: int = 4
) -> pd.DataFrame:
    block = frame[frame[column].notna()].copy()
    if block[column].nunique() < buckets:
        return pd.DataFrame()
    try:
        block["bucket"] = pd.qcut(
            block[column], buckets, duplicates="drop", labels=False
        )
    except ValueError:
        return pd.DataFrame()
    rows = []
    for index, group in block.groupby("bucket", sort=True):
        rows.append(
            {
                "bucket": int(index) + 1,
                "range": f"{group[column].min():,.4g} – {group[column].max():,.4g}",
                **describe(group["return"]),
            }
        )
    return table(rows)


def categorical_buckets(
    frame: pd.DataFrame, column: str, *, min_n: int = MIN_BUCKET
) -> pd.DataFrame:
    rows = []
    for channel, block in frame.groupby("channel"):
        if column not in block:
            continue
        for value, group in block.groupby(column):
            if len(group) < min_n:
                continue
            row = {"channel": channel, "value": value, "alerts": len(group)}
            row.update(describe(group["return"]))
            rows.append(row)
    if not rows:
        return pd.DataFrame()
    return table(rows)


def feature_buckets(frame: pd.DataFrame, features: list[str]) -> dict[str, pd.DataFrame]:
    output = {}
    for channel, block in frame.groupby("channel"):
        for feature in features:
            if feature not in block or block[feature].notna().sum() < MIN_BUCKET:
                continue
            result = quantile_buckets(block, feature)
            if not result.empty:
                output[f"{channel} · {feature}"] = result
    return output


def overlap_table(frame: pd.DataFrame) -> pd.DataFrame:
    ordered = frame.sort_values("alert_ts_ms")
    grouped = ordered.groupby("mint")
    mints = pd.DataFrame(
        {
            "channels": grouped["channel"].apply(lambda values: sorted(set(values))),
            "n_channels": grouped["channel"].apply(lambda values: len(set(values))),
            # The actionable trade is the earliest alert on that mint.
            "return_": grouped["return"].first(),
            "first_alert": grouped["alert_ts_ms"].min(),
            "last_alert": grouped["alert_ts_ms"].max(),
        }
    )
    mints["lag_min"] = (mints["last_alert"] - mints["first_alert"]) / 60_000
    rows = []
    for count, block in mints.groupby("n_channels"):
        row = {"channels": f"{count} channel(s)", "mints": len(block)}
        row.update(describe(block["return_"]))
        row["median_lag_min"] = round(float(block["lag_min"].median()), 1)
        rows.append(row)
    rows.append(
        {
            "channels": "total",
            "mints": len(mints),
            **describe(mints["return_"]),
        }
    )
    return table(rows)


def channel_pairs(frame: pd.DataFrame) -> pd.DataFrame:
    by_mint = frame.groupby("mint")["channel"].apply(lambda values: tuple(sorted(set(values))))
    rows = []
    for channels, group in by_mint.groupby(by_mint):
        if len(channels) < 2:
            continue
        block = frame[frame["mint"].isin(group.index)]
        rows.append(
            {
                "pair": " + ".join(channels),
                "mints": int(len(group)),
                **describe(block.groupby("mint")["return"].mean()),
            }
        )
    return table(rows).sort_values("mints", ascending=False)


def best_filters(frame: pd.DataFrame) -> pd.DataFrame:
    """Greedy one-condition filters on the scanner features, for orientation only."""
    candidates = {
        "@AveSolanaTokenScanner": {
            "liquidity_usd": (5_000, 25_000, 100_000),
            "mcap_usd": (10_000, 25_000, 50_000, 100_000),
            "insiders": (10, 50, 100),
            "insider_holdings_pct": (0.5, 1.0, 5.0),
            "snipes": (0, 3, 10),
            "holders": (10, 30, 100),
            "top_holder_share": (20, 40, 60),
        },
        "@DRBTSolanaPF": {
            "dev_age_min": (60, 1440, 43200),
            "dev_balance_sol": (0.5, 2, 10),
            "dev_tx": (10, 100, 1000),
        },
    }
    rows = []
    for channel, features in candidates.items():
        block = frame[frame["channel"] == channel]
        if block.empty:
            continue
        base = describe(block["return"])
        rows.append(
            {
                "channel": channel,
                "filter": "all alerts",
                "n": base["n"],
                "win_rate": base["win_rate"],
                "mean": base["mean"],
                "median": base["median"],
            }
        )
        for feature, cuts in features.items():
            if feature not in block:
                continue
            values = block[feature]
            for cut in cuts:
                for direction, mask in (
                    (f"{feature} <= {cut}", values <= cut),
                    (f"{feature} > {cut}", values > cut),
                ):
                    selected = block[mask]
                    if len(selected) < MIN_BUCKET:
                        continue
                    stats = describe(selected["return"])
                    rows.append(
                        {
                            "channel": channel,
                            "filter": direction,
                            "n": stats["n"],
                            "win_rate": stats["win_rate"],
                            "mean": stats["mean"],
                            "median": stats["median"],
                        }
                    )
    result = pd.DataFrame(rows)
    for column in ("win_rate", "mean", "median"):
        result[column] = (result[column] * 100).round(1)
    return result.sort_values(["channel", "win_rate"], ascending=[True, False])


def markdown_table(frame: pd.DataFrame) -> str:
    if frame.empty:
        return "_no rows_"
    columns = list(frame.columns)
    lines = [
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    for row in frame.itertuples(index=False):
        lines.append(
            "| "
            + " | ".join(
                f"{value:.1f}" if isinstance(value, float) else str(value)
                for value in row
            )
            + " |"
        )
    return "\n".join(lines)


def write_report(
    days: list[str],
    horizon: int,
    frame: pd.DataFrame,
    channels: pd.DataFrame,
    overlap: pd.DataFrame,
    pairs: pd.DataFrame,
    buckets: dict[str, pd.DataFrame],
    dex_table: pd.DataFrame,
    greedy: pd.DataFrame,
    out_dir: Path,
    latency_ms: int,
    fill_mode: str = "last-before",
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    if fill_mode == "first-after":
        entry_note = (
            "Entry = the first real trade at or after the decision (the "
            "deployed trade-mode convention)."
        )
    else:
        entry_note = (
            "Entry = the last real trade at or before the decision, with "
            "prints older than the entry-age filter dropped."
        )
    lines = [
        f"# Channel comparison, realistic {horizon}-minute fills",
        "",
        f"Days: {', '.join(days)}. First alert + {latency_ms / 1000:g} s. "
        f"{entry_note}",
        "Exit = last print at/before the horizon; 1.25 % fee per side.",
        "",
        "## Base rates",
        "",
        markdown_table(channels),
        "",
        "`mean_ex_top` removes the single best trade; if it is far below `mean`,",
        "the channel's average is one outlier, not a repeatable edge.",
        "",
        "## Cross-channel overlap (per mint, mean return)",
        "",
        markdown_table(overlap),
        "",
        "## Which pairs of channels fire on the same mint",
        "",
        markdown_table(pairs),
        "",
        "## Feature buckets",
        "",
    ]
    for name, block in buckets.items():
        lines += [f"### {name}", "", markdown_table(block), ""]
    if not dex_table.empty:
        lines += ["### Channel by DEX", "", markdown_table(dex_table), ""]
    lines += [
        "## Single-condition filters (orientation only)",
        "",
        "Selected *after* looking at the same day, so treat as hypotheses. A",
        f"bucket needs at least {MIN_BUCKET} alerts to appear.",
        "",
        markdown_table(greedy),
        "",
    ]
    path = out_dir / "report.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", nargs="*", default=None)
    parser.add_argument("--horizon", type=int, default=5)
    parser.add_argument(
        "--latency-ms",
        type=int,
        default=5_000,
        help="Entry delay used when the alerts were priced (label only).",
    )
    parser.add_argument(
        "--tag", default="", help="Read alerts_priced_<tag>.csv instead of the default."
    )
    parser.add_argument(
        "--fill-mode",
        choices=("last-before", "first-after"),
        default="last-before",
        help=(
            "Entry convention recorded by alert_outcomes.py. Rows priced with "
            "the other convention are dropped (the column is written by newer "
            "priced files; older files keep every row)."
        ),
    )
    parser.add_argument(
        "--max-entry-delay-ms",
        type=int,
        default=60_000,
        help=(
            "Drop first-after fills later than this after the decision "
            "(matches the deployed entry window); 0 disables."
        ),
    )
    parser.add_argument(
        "--max-entry-age-ms",
        type=int,
        default=2_000,
        help="Drop last-before fills whose print is older than this; 0 disables.",
    )
    parser.add_argument(
        "--scanner-rule",
        action="store_true",
        help=(
            "Keep only the deployed lane: first @AveSolanaTokenScanner card "
            "per mint, dex == Pumpfunamm, 20,000 <= liquidity_usd < 100,000."
        ),
    )
    args = parser.parse_args(argv)

    days = available(args.days, args.tag)
    frame = load(
        days,
        args.horizon,
        args.tag,
        args.max_entry_delay_ms,
        args.max_entry_age_ms,
        args.fill_mode,
    )
    if frame.empty:
        raise SystemExit("No priced alerts found; run alert_outcomes.py first.")
    if args.scanner_rule:
        frame = deployed_scanner_rule(frame)
        if frame.empty:
            raise SystemExit("No alerts match the deployed scanner rule.")
    if not args.scanner_rule and "@AveSignalMonitor" not in set(frame["channel"]):
        ave = load_ave(days, args.horizon)
        if not ave.empty:
            frame = pd.concat([frame, ave], ignore_index=True)

    channels = channel_table(frame)
    if args.scanner_rule:
        # One lane by construction: overlap and pair tables need two channels.
        overlap = pd.DataFrame(columns=["mint", "channels"])
        pairs = pd.DataFrame(columns=["pair", "mints"])
    else:
        overlap = overlap_table(frame)
        pairs = channel_pairs(frame)
    features = [
        "liquidity_usd",
        "mcap_usd",
        "insiders",
        "insider_holdings_pct",
        "snipes",
        "rushers",
        "holders",
        "top_holder_share",
        "pair_sol",
        "dev_age_min",
        "dev_balance_sol",
        "dev_tx",
    ]
    buckets = feature_buckets(frame, features)
    dex_table = categorical_buckets(frame, "dex")
    greedy = best_filters(frame)

    out_dir = OUTPUT if not args.tag else OUTPUT.parent / f"channel_comparison_{args.tag}"
    out_dir.mkdir(parents=True, exist_ok=True)
    channels.to_csv(out_dir / "channels.csv", index=False)
    overlap.to_csv(out_dir / "overlap.csv", index=False)
    pairs.to_csv(out_dir / "pairs.csv", index=False)
    greedy.to_csv(out_dir / "greedy_filters.csv", index=False)
    if not dex_table.empty:
        dex_table.to_csv(out_dir / "by_dex.csv", index=False)
    for name, block in buckets.items():
        safe = (
            name.replace("@", "").replace(" · ", "_").replace(" ", "_")
            .replace("/", "_")
        )
        block.to_csv(out_dir / f"bucket_{safe}.csv", index=False)
    path = write_report(
        days, args.horizon, frame, channels, overlap, pairs, buckets, dex_table,
        greedy, out_dir, args.latency_ms, args.fill_mode,
    )

    summary = {
        "days": days,
        "horizon": args.horizon,
        "alerts": int(len(frame)),
        "channels": channels.to_dict("records"),
        "report": str(path),
    }
    (out_dir / "summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
