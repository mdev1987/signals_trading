#!/usr/bin/env python3
"""Compare the 1-minute-close fill model with the realistic trade fill.

The 1-minute-close model (``backtest_ave_signals.py --fill-source ohlcv``) buys
the close of the last *completed* bar, which is median ~29 s stale. On tokens
that are moving up that stale price is systematically cheaper than the market,
so the model overstates the edge. This script joins the rule features to both
fill models' 5-minute returns and prints the rule variants side by side.

Usage::

    python src/compare_fill_models.py \
        --ohlcv output/backtest/ave_signals_8d_ohlcv/signals_backtest.csv \
        --event output/backtest/ave_signals_8d_event/signals_backtest.csv \
        --features output/backtest/ave_signals/patterns_5m_8d/features.csv
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
HORIZON = 5
FEE_RATE = 0.0125
STAKE_SOL = 0.1
FLOW_MIN = 9.456
TOTAL_BUY_MIN = 3.0
DIP_MAX = 2.0
SIGNERS_MAX = 147
TRADES_MAX = 611


def load_returns(path: Path, column: str) -> pd.DataFrame:
    frame = pd.read_csv(path)
    frame = frame[
        (frame["horizon_minutes"] == HORIZON) & (frame["status"] == "closed")
    ].copy()
    return frame[["day", "signal_id", "net_return", "entry_price_age_ms"]].rename(
        columns={
            "net_return": column,
            "entry_price_age_ms": column + "_entry_age_ms",
        }
    )


def variants(frame: pd.DataFrame) -> dict[str, pd.Series]:
    core = (frame["ev_flow_1m"] > FLOW_MIN) & (
        frame["alert_total_buy"] > TOTAL_BUY_MIN
    )
    dip_missing = frame["pre_ret_15m"].isna()
    dip_ok = dip_missing | (frame["pre_ret_15m"] <= DIP_MAX)
    crowd_ok = (
        frame["ev_signers_5m"].isna() | (frame["ev_signers_5m"] <= SIGNERS_MAX)
    ) & (frame["ev_trades_5m"].isna() | (frame["ev_trades_5m"] <= TRADES_MAX))
    age_ge_10 = frame["pre_age_min"] >= 10
    return {
        "all signals": pd.Series(True, index=frame.index),
        "core (flow+buy)": core,
        "core + dip overlay": core & dip_ok,
        "core + anti-crowd": core & crowd_ok,
        "core + dip + anti-crowd (deployed)": core & dip_ok & crowd_ok,
        "core + age>=10min": core & age_ge_10,
        "core + dip + age>=10min": core & dip_ok & age_ge_10,
        "hole: dip missing (live population)": core & dip_missing,
        "hole: dip present & <=2.0": core & ~dip_missing & (
            frame["pre_ret_15m"] <= DIP_MAX
        ),
    }


def describe(values: pd.Series) -> dict[str, float]:
    return {
        "trades": int(len(values)),
        "win_rate": float((values > 0).mean()) if len(values) else np.nan,
        "mean_net_return": float(values.mean()) if len(values) else np.nan,
        "median_net_return": float(values.median()) if len(values) else np.nan,
        "total_net_pnl_sol": float(values.sum() * STAKE_SOL) if len(values) else np.nan,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ohlcv",
        type=Path,
        default=REPO_ROOT
        / "output/backtest/ave_signals_8d_ohlcv/signals_backtest.csv",
    )
    parser.add_argument(
        "--event",
        type=Path,
        default=REPO_ROOT
        / "output/backtest/ave_signals_8d_event/signals_backtest.csv",
    )
    parser.add_argument(
        "--features",
        type=Path,
        default=REPO_ROOT
        / "output/backtest/ave_signals/patterns_5m_8d/features.csv",
    )
    parser.add_argument("--oos-only", action="store_true", default=True)
    args = parser.parse_args(argv)

    ohlcv = load_returns(args.ohlcv, "ohlcv_return")
    event = load_returns(args.event, "event_return")
    features = pd.read_csv(args.features)
    features = features[["day", "signal_id", "ev_flow_1m", "alert_total_buy",
                         "pre_ret_15m", "ev_signers_5m", "ev_trades_5m",
                         "pre_age_min", "is_train", "flagged_mint"]]
    frame = (
        features.merge(ohlcv, on=["day", "signal_id"], how="inner")
        .merge(event, on=["day", "signal_id"], how="inner")
    )
    if args.oos_only:
        frame = frame[~frame["is_train"].astype(bool)]
    print(f"joined signals: {len(frame)}  (OOS only: {args.oos_only})")
    print(
        "fill staleness: ohlcv median %.1fs | event median %.1fs"
        % (
            frame["ohlcv_return_entry_age_ms"].median() / 1000,
            frame["event_return_entry_age_ms"].median() / 1000,
        )
    )
    rows = []
    for name, mask in variants(frame).items():
        block = frame[mask]
        for model, column in (("ohlcv", "ohlcv_return"), ("event", "event_return")):
            row = {"variant": name, "fill": model}
            row.update(describe(block[column]))
            rows.append(row)
    table = pd.DataFrame(rows)
    pivot = table.pivot(index="variant", columns="fill", values=[
        "trades", "win_rate", "mean_net_return", "median_net_return",
        "total_net_pnl_sol",
    ])
    display = pd.DataFrame(
        {
            "n": table[table.fill == "ohlcv"].set_index("variant")["trades"],
            "ohlcv win": pivot[("win_rate", "ohlcv")],
            "event win": pivot[("win_rate", "event")],
            "ohlcv mean": pivot[("mean_net_return", "ohlcv")],
            "event mean": pivot[("mean_net_return", "event")],
            "ohlcv med": pivot[("median_net_return", "ohlcv")],
            "event med": pivot[("median_net_return", "event")],
            "ohlcv PnL": pivot[("total_net_pnl_sol", "ohlcv")],
            "event PnL": pivot[("total_net_pnl_sol", "event")],
        }
    )
    for column in ("ohlcv win", "event win", "ohlcv mean", "event mean",
                   "ohlcv med", "event med"):
        display[column] = (display[column] * 100).round(1)
    display["ohlcv PnL"] = display["ohlcv PnL"].round(2)
    display["event PnL"] = display["event PnL"].round(2)
    display.index.name = "variant"
    print()
    print(display.to_string())
    print()
    print("PnL assumes %.2f SOL stake and %.2f%% fee per side."
          % (STAKE_SOL, FEE_RATE * 100))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
