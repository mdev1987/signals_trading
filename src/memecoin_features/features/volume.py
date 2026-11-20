"""Volume-quality features: activity, imbalance, relative volume, consistency.

All rolling windows are trailing and include the current bucket; the *baseline*
for relative volume and z-scores uses only the buckets before the current one.
Nothing here reads a later bucket.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

TRADE_ACTIONS = ("buy", "sell")


def _safe_ratio(numerator: pd.Series, denominator: pd.Series) -> pd.Series:
    denominator = denominator.replace(0.0, np.nan)
    return numerator / denominator


def compute_volume_features(
    base: pd.DataFrame,
    trades: pd.DataFrame,
    windows: tuple[int, ...],
    bar_ms: int,
) -> pd.DataFrame:
    """Return volume features aligned to ``base`` (sorted by mint/bucket)."""

    keys = ["mint", "bucket_start_ms"]
    out = pd.DataFrame(index=base.index)

    if trades.empty:
        zero_columns = [
            "trade_count",
            "buy_count",
            "sell_count",
            "volume_sol",
            "buy_volume_sol",
            "sell_volume_sol",
            "active_1m_buckets",
        ]
        for name in zero_columns:
            out[name] = 0.0
        out["buy_sell_imbalance"] = 0.0
        out["avg_trade_sol"] = 0.0
        for window in windows:
            out[f"volume_sol_{window}b"] = 0.0
            out[f"buy_volume_sol_{window}b"] = 0.0
            out[f"sell_volume_sol_{window}b"] = 0.0
            out[f"buy_sell_imbalance_{window}b"] = 0.0
            out[f"relative_volume_{window}b"] = np.nan
            out[f"volume_zscore_{window}b"] = np.nan
            out[f"volume_consistency_{window}b"] = np.nan
        return out

    trades = trades.copy()
    trades["is_buy"] = trades["action"] == "buy"
    trades["buy_quote"] = trades["quote_amount"].where(trades["is_buy"], 0.0)
    trades["sell_quote"] = trades["quote_amount"].where(~trades["is_buy"], 0.0)
    trades["minute_start_ms"] = (trades["timestamp_ms"] // 60_000) * 60_000

    aggregated = (
        trades.groupby(keys, sort=False)
        .agg(
            trade_count=("signature", "size"),
            buy_count=("is_buy", "sum"),
            sell_count=("is_buy", lambda values: int((~values).sum())),
            buy_volume_sol=("buy_quote", "sum"),
            sell_volume_sol=("sell_quote", "sum"),
            active_1m_buckets=("minute_start_ms", "nunique"),
        )
        .reset_index()
    )
    aggregated["volume_sol"] = (
        aggregated["buy_volume_sol"] + aggregated["sell_volume_sol"]
    )
    aggregated["buy_sell_imbalance"] = _safe_ratio(
        aggregated["buy_volume_sol"] - aggregated["sell_volume_sol"],
        aggregated["volume_sol"],
    )
    aggregated["avg_trade_sol"] = _safe_ratio(
        aggregated["volume_sol"], aggregated["trade_count"]
    )

    merged = base[keys].merge(aggregated, how="left", on=keys)
    fill_zero = [
        "trade_count",
        "buy_count",
        "sell_count",
        "buy_volume_sol",
        "sell_volume_sol",
        "volume_sol",
        "active_1m_buckets",
        "buy_sell_imbalance",
        "avg_trade_sol",
    ]
    merged[fill_zero] = merged[fill_zero].fillna(0.0)
    for name in (
        "trade_count",
        "buy_count",
        "sell_count",
        "active_1m_buckets",
    ):
        merged[name] = merged[name].astype("int64")

    merged = merged.sort_values(keys, kind="stable")
    grouped = merged.groupby("mint", sort=False)

    out = pd.DataFrame(index=merged.index)
    for name in (
        "trade_count",
        "buy_count",
        "sell_count",
        "volume_sol",
        "buy_volume_sol",
        "sell_volume_sol",
        "active_1m_buckets",
        "buy_sell_imbalance",
        "avg_trade_sol",
    ):
        out[name] = merged[name].to_numpy()

    for window in windows:
        volume = merged["volume_sol"]

        volume_sum = grouped["volume_sol"].transform(
            lambda values, w=window: values.rolling(w, min_periods=1).sum()
        )
        buy_sum = grouped["buy_volume_sol"].transform(
            lambda values, w=window: values.rolling(w, min_periods=1).sum()
        )
        sell_sum = grouped["sell_volume_sol"].transform(
            lambda values, w=window: values.rolling(w, min_periods=1).sum()
        )
        previous_mean = grouped["volume_sol"].transform(
            lambda values, w=window: values.shift(1)
            .rolling(w, min_periods=w)
            .mean()
        )
        previous_std = grouped["volume_sol"].transform(
            lambda values, w=window: values.shift(1)
            .rolling(w, min_periods=w)
            .std()
        )
        previous_active = grouped["volume_sol"].transform(
            lambda values, w=window: (values > 0.0)
            .astype("float64")
            .shift(1)
            .rolling(w, min_periods=w)
            .mean()
        )

        out[f"volume_sol_{window}b"] = volume_sum.to_numpy()
        out[f"buy_volume_sol_{window}b"] = buy_sum.to_numpy()
        out[f"sell_volume_sol_{window}b"] = sell_sum.to_numpy()
        out[f"buy_sell_imbalance_{window}b"] = _safe_ratio(
            buy_sum - sell_sum, buy_sum + sell_sum
        ).to_numpy()
        out[f"relative_volume_{window}b"] = _safe_ratio(
            volume, previous_mean
        ).to_numpy()
        out[f"volume_zscore_{window}b"] = _safe_ratio(
            volume - previous_mean, previous_std
        ).to_numpy()
        out[f"volume_consistency_{window}b"] = previous_active.to_numpy()

    out.index = base.index
    return out
