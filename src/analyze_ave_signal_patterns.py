#!/usr/bin/env python3
"""Search for PIT-safe patterns that separate profitable Ave signals.

The script joins three sources, all cut off at the entry decision timestamp
(``signal_timestamp_ms + latency``):

* the Telegram alert text (max pump, wallet kind/count, MC, buys, inflow);
* the validated 1-minute candles (pre-signal return, volume, range, age);
* the on-chain events parquet (market cap, liquidity, wallet flow, signers).

It then runs a deliberately small, falsifiable analysis:

1. univariate AUC / Spearman on the **train** days (Oct 3-5);
2. the same statistic on the **holdout** days (Aug 9 + Nov 8), so a pattern only
   counts if it survives out of sample;
3. train-fitted quintile tables, applied unchanged to the holdout;
4. a regularised logistic model and a depth-3 regression tree, both fitted on
   train only, with holdout decile / leaf economics.

No VectorBT, no look-ahead. The output is descriptive, not a trading system.

Usage (from the repository root)::

    python src/analyze_ave_signal_patterns.py
    python src/analyze_ave_signal_patterns.py --horizon 5
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

REPO_ROOT = Path(__file__).resolve().parents[1]
SIGNALS_ROOT = Path("output/backtest/signals")
DAYS_ROOT = Path("output/backtest/days")
BACKTEST_ROOT = Path("output/backtest/ave_signals")
MESSAGES_ROOT = Path("messages")
MINUTE_MS = 60_000

DEFAULT_DAYS = (
    "2026-08-09",
    "2026-10-03",
    "2026-10-04",
    "2026-10-05",
    "2026-11-08",
)
DEFAULT_TRAIN = ("2026-10-03", "2026-10-04", "2026-10-05")

ALERT_FEATURES = (
    "alert_max_pump",
    "alert_already_pumped",
    "alert_wallet_count",
    "alert_smart_wallet",
    "alert_mc",
    "alert_total_buy",
    "alert_inflow_n",
    "alert_inflow_sum",
    "alert_inflow_max",
    "alert_inflow_top_share",
    "alert_inflow_hhi",
    "alert_buy_per_wallet",
    "alert_buy_per_mc",
)
BAR_FEATURES = (
    "pre_ret_1m",
    "pre_ret_3m",
    "pre_ret_5m",
    "pre_ret_15m",
    "pre_ret_30m",
    "pre_vol_1m",
    "pre_vol_5m",
    "pre_vol_15m",
    "pre_vol_30m",
    "pre_trades_5m",
    "pre_trades_15m",
    "pre_buy_ratio_5m",
    "pre_buy_ratio_15m",
    "pre_range_5m",
    "pre_range_15m",
    "pre_dd_from_high_5m",
    "pre_dd_from_high_15m",
    "pre_age_min",
    "pre_log_price",
)
EVENT_FEATURES = (
    "ev_mc_sol",
    "ev_liq_sol",
    "ev_liq_over_mc",
    "ev_age_min",
    "ev_last_event_age_ms",
    "ev_flow_1m",
    "ev_flow_5m",
    "ev_flow_15m",
    "ev_buy_ratio_5m",
    "ev_buy_ratio_15m",
    "ev_signers_5m",
    "ev_signers_15m",
    "ev_top_signer_share_5m",
    "ev_signer_hhi_5m",
    "ev_trades_5m",
)
CONTEXT_FEATURES = (
    "ctx_hour",
    "ctx_minute",
    "ctx_signals_5m",
    "ctx_signals_15m",
    "ctx_gap_prev_min",
)


@dataclass(frozen=True, slots=True)
class Split:
    train: np.ndarray
    holdout: np.ndarray


# --------------------------------------------------------------------------- #
# Alert text
# --------------------------------------------------------------------------- #


def parse_alert(text: object) -> dict[str, float]:
    """Parse the fields the channel prints at signal time."""
    value = str(text)
    features: dict[str, float] = {}

    match = re.search(r"Max Pump:\s*(<?)\s*([\d.]+)x", value)
    if match:
        pumped = 0.0 if match.group(1) else float(match.group(2))
        features["alert_max_pump"] = pumped
        features["alert_already_pumped"] = 1.0 if pumped >= 2 else 0.0

    match = re.search(r"(\d+)\s+(KOL|Smart) Wallet Buy", value)
    if match:
        features["alert_wallet_count"] = float(match.group(1))
        features["alert_smart_wallet"] = 1.0 if match.group(2) == "Smart" else 0.0

    match = re.search(r"Current MC:\s*([\d.]+)\s*([KM]?)", value)
    if match:
        scale = {"K": 1e3, "M": 1e6}.get(match.group(2), 1.0)
        features["alert_mc"] = float(match.group(1)) * scale

    match = re.search(r"Total Buy\s*([\d.,]+)\s*SOL", value)
    if match:
        features["alert_total_buy"] = float(
            match.group(1).replace(",", "")
        )

    # Inflow lines sit below the "Inflow" header. They carry the wallet buys
    # and must not include the summary "Total Buy ... SOL" line, which would
    # otherwise be counted twice.
    inflow_lines: list[tuple[float, str]] = []
    in_inflow = False
    for line in value.splitlines():
        stripped = line.strip()
        if "Inflow" in stripped:
            in_inflow = True
            continue
        if not in_inflow or "Total Buy" in stripped:
            continue
        match = re.search(
            r"Buy\s+([\d.,{}]+)\s*([A-Za-z][A-Za-z0-9.]*)", stripped
        )
        if not match:
            continue
        amount = match.group(1).replace(",", "").replace("{", "").replace("}", "")
        try:
            inflow_lines.append((float(amount), match.group(2).upper()))
        except ValueError:
            continue

    if inflow_lines:
        amounts = [amount for amount, _ in inflow_lines]
        total = float(sum(amounts))
        features["alert_inflow_n"] = float(len(amounts))
        features["alert_inflow_max"] = float(max(amounts))
        features["alert_inflow_non_sol"] = (
            1.0 if any(unit != "SOL" for _, unit in inflow_lines) else 0.0
        )
        if total > 0:
            features["alert_inflow_top_share"] = float(max(amounts)) / total
            features["alert_inflow_hhi"] = float(
                sum((item / total) ** 2 for item in amounts)
            )
        # The SOL-denominated wallet buys are the only ones comparable with
        # the on-chain flow features. Token-unit messages are left missing.
        sol_buys = [amount for amount, unit in inflow_lines if unit == "SOL"]
        if sol_buys:
            features["alert_inflow_sum"] = float(sum(sol_buys))

    wallet_count = features.get("alert_wallet_count", float("nan"))
    total_buy = features.get("alert_total_buy", float("nan"))
    if np.isfinite(wallet_count) and wallet_count > 0 and np.isfinite(total_buy):
        features["alert_buy_per_wallet"] = total_buy / wallet_count
    alert_mc = features.get("alert_mc", float("nan"))
    if np.isfinite(alert_mc) and alert_mc > 0 and np.isfinite(total_buy):
        features["alert_buy_per_mc"] = total_buy / alert_mc

    return features


def load_signals(repo_root: Path, days: list[str]) -> pd.DataFrame:
    frames = []
    for day in days:
        path = repo_root / SIGNALS_ROOT / day / "strategy_signals.csv"
        signals = pd.read_csv(path)
        signals["day"] = day
        messages_path = (
            repo_root / MESSAGES_ROOT / day / "@avesignalmonitor-messages.csv"
        )
        if messages_path.exists():
            messages = pd.read_csv(messages_path)
            lookup = messages.set_index("MessageId")["Message"].to_dict()
            signals["text"] = signals["source_message_id"].map(lookup)
        else:
            signals["text"] = None
        frames.append(signals)
    combined = pd.concat(frames, ignore_index=True)
    combined["signal_timestamp_ms"] = combined["signal_timestamp_ms"].astype("int64")
    return combined.sort_values(
        ["day", "signal_timestamp_ms", "signal_id"], kind="stable"
    ).reset_index(drop=True)


# --------------------------------------------------------------------------- #
# Features
# --------------------------------------------------------------------------- #


def _finite(value: float) -> float:
    return float(value) if np.isfinite(value) else float("nan")


def bar_features(
    bars: pd.DataFrame, decision_ts: int, entry_px: float
) -> dict[str, float]:
    """Features from real bars that closed at or before the decision."""
    if bars.empty:
        return {}
    close_ts = bars["close_ts"].to_numpy(dtype="int64")
    right = int(np.searchsorted(close_ts, decision_ts, side="right"))
    if right <= 0:
        return {}
    history = bars.iloc[:right]
    features: dict[str, float] = {
        "pre_age_min": _finite((decision_ts - close_ts[0]) / MINUTE_MS),
        "pre_log_price": _finite(np.log(entry_px)) if entry_px > 0 else float("nan"),
    }

    for window in (1, 3, 5, 15, 30):
        start_ts = decision_ts - window * MINUTE_MS
        window_rows = history[history["close_ts"] > start_ts]
        past = history[history["close_ts"] <= start_ts]
        if not past.empty:
            base_px = float(past["close"].iloc[-1])
            if base_px > 0:
                features[f"pre_ret_{window}m"] = _finite(entry_px / base_px - 1.0)
        if not window_rows.empty:
            features[f"pre_vol_{window}m"] = _finite(
                window_rows["volume_sol"].sum()
            )
            if window in (5, 15):
                trades = float(window_rows["trades"].sum())
                buys = float(window_rows["buys"].sum())
                features[f"pre_trades_{window}m"] = trades
                features[f"pre_buy_ratio_{window}m"] = (
                    _finite(buys / trades) if trades > 0 else float("nan")
                )
                high = float(window_rows["high"].max())
                low = float(window_rows["low"].min())
                features[f"pre_range_{window}m"] = (
                    _finite((high - low) / entry_px) if entry_px > 0 else float("nan")
                )
                features[f"pre_dd_from_high_{window}m"] = (
                    _finite(entry_px / high - 1.0) if high > 0 else float("nan")
                )
    return features


def load_bar_features(
    repo_root: Path, day: str, signals: pd.DataFrame, outcomes: pd.DataFrame
) -> pd.DataFrame:
    path = repo_root / DAYS_ROOT / day / "ohlcv" / "ohlcv_1m.parquet"
    if signals.empty:
        return pd.DataFrame()
    columns = [
        "mint",
        "timestamp_ms",
        "high",
        "low",
        "close",
        "volume_sol",
        "trades",
        "buys",
        "is_filled",
    ]
    frame = pq.read_table(
        path,
        columns=columns,
        filters=[("mint", "in", sorted(signals["mint"].unique()))],
    ).to_pandas()
    real = (frame["is_filled"].fillna(1).astype("int8") == 0) & np.isfinite(
        frame["close"]
    )
    frame = frame[real].copy()
    frame["close_ts"] = frame["timestamp_ms"].astype("int64") + MINUTE_MS
    grouped = {
        str(mint): group.sort_values("close_ts", kind="stable").reset_index(drop=True)
        for mint, group in frame.groupby("mint", sort=False)
    }

    rows = []
    for row in signals.itertuples(index=False):
        outcome = outcomes.loc[outcomes["signal_id"] == row.signal_id]
        if outcome.empty:
            continue
        entry = outcome.iloc[0]
        features = bar_features(
            grouped.get(str(row.mint), pd.DataFrame()),
            int(entry["entry_decision_timestamp_ms"]),
            float(entry["entry_price_sol"]),
        )
        rows.append({"signal_id": row.signal_id, **features})
    return pd.DataFrame(rows)


def _flow_features(events: pd.DataFrame, decision_ts: int) -> dict[str, float]:
    if events.empty:
        return {}
    timestamps = events["timestamp_ms"].to_numpy(dtype="int64")
    right = int(np.searchsorted(timestamps, decision_ts, side="right"))
    if right <= 0:
        return {}
    history = events.iloc[:right]
    last = history.iloc[-1]
    features: dict[str, float] = {
        "ev_mc_sol": _finite(last["market_cap_sol"]),
        "ev_liq_sol": _finite(last["quote_in_pool"]),
        "ev_age_min": _finite(
            (decision_ts - int(timestamps[0])) / MINUTE_MS
        ),
        "ev_last_event_age_ms": _finite(
            decision_ts - int(last["timestamp_ms"])
        ),
    }
    if features["ev_liq_sol"] > 0 and features["ev_mc_sol"] > 0:
        features["ev_liq_over_mc"] = _finite(
            features["ev_liq_sol"] / features["ev_mc_sol"]
        )

    signed = np.where(
        history["action"].astype(str).str.lower() == "buy",
        history["quote_amount"].astype("float64"),
        -history["quote_amount"].astype("float64"),
    )
    quote = history["quote_amount"].astype("float64").to_numpy()
    is_buy = (history["action"].astype(str).str.lower() == "buy").to_numpy()

    for window in (1, 5, 15):
        start_ts = decision_ts - window * MINUTE_MS
        mask = timestamps[:right] > start_ts
        if not mask.any():
            continue
        features[f"ev_flow_{window}m"] = _finite(signed[mask].sum())
        if window in (5, 15):
            total = float(quote[mask].sum())
            features[f"ev_buy_ratio_{window}m"] = (
                _finite(float(quote[mask][is_buy[mask]].sum()) / total)
                if total > 0
                else float("nan")
            )
            features[f"ev_signers_{window}m"] = float(
                history.loc[mask, "tx_signer"].nunique()
            )
            features[f"ev_trades_{window}m"] = float(mask.sum())
            buy_quote = pd.Series(quote[mask][is_buy[mask]])
            if not buy_quote.empty and buy_quote.sum() > 0:
                shares = buy_quote / buy_quote.sum()
                features[f"ev_top_signer_share_{window}m"] = float(shares.max())
                features[f"ev_signer_hhi_{window}m"] = float((shares**2).sum())
    return features


def load_event_features(
    repo_root: Path, day: str, signals: pd.DataFrame, outcomes: pd.DataFrame
) -> pd.DataFrame:
    path = repo_root / DAYS_ROOT / day / "parquet" / "events.parquet"
    if signals.empty:
        return pd.DataFrame()
    columns = [
        "timestamp_ms",
        "mint",
        "action",
        "quote_amount",
        "tx_signer",
        "market_cap_sol",
        "quote_in_pool",
    ]
    frame = pq.read_table(
        path,
        columns=columns,
        filters=[("mint", "in", sorted(signals["mint"].unique()))],
    ).to_pandas()
    grouped = {
        str(mint): group.sort_values("timestamp_ms", kind="stable").reset_index(
            drop=True
        )
        for mint, group in frame.groupby("mint", sort=False)
    }

    rows = []
    for row in signals.itertuples(index=False):
        outcome = outcomes.loc[outcomes["signal_id"] == row.signal_id]
        if outcome.empty:
            continue
        features = _flow_features(
            grouped.get(str(row.mint), pd.DataFrame()),
            int(outcome.iloc[0]["entry_decision_timestamp_ms"]),
        )
        rows.append({"signal_id": row.signal_id, **features})
    return pd.DataFrame(rows)


def context_features(signals: pd.DataFrame) -> pd.DataFrame:
    ordered = signals.sort_values(
        ["day", "signal_timestamp_ms"], kind="stable"
    ).reset_index(drop=True)
    timestamps = ordered["signal_timestamp_ms"].to_numpy(dtype="int64")
    days = ordered["day"].to_numpy()
    hours = pd.to_datetime(ordered["signal_timestamp_utc"]).dt.hour.to_numpy()
    minutes = pd.to_datetime(ordered["signal_timestamp_utc"]).dt.minute.to_numpy()

    rows = []
    for index in range(len(ordered)):
        day = days[index]
        earlier = np.where(days[:index] == day)[0]
        same_day = timestamps[earlier]
        deltas = timestamps[index] - same_day
        rows.append(
            {
                "signal_id": ordered["signal_id"].iloc[index],
                "ctx_hour": float(hours[index]),
                "ctx_minute": float(minutes[index]),
                "ctx_signals_5m": float((deltas > 0).sum() - (deltas > 5 * MINUTE_MS).sum()),
                "ctx_signals_15m": float((deltas > 0).sum() - (deltas > 15 * MINUTE_MS).sum()),
                "ctx_gap_prev_min": float(deltas[0] / MINUTE_MS) if deltas.size else float("nan"),
            }
        )
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- #
# Statistics
# --------------------------------------------------------------------------- #


def auc(feature: pd.Series, target: pd.Series) -> tuple[float, int]:
    values = pd.to_numeric(feature, errors="coerce")
    label = pd.to_numeric(target, errors="coerce")
    ok = values.notna() & np.isfinite(values) & label.notna()
    values = values[ok]
    label = (label[ok] > 0).astype(int)
    if label.nunique() < 2:
        return float("nan"), 0
    ranks = values.rank()
    positives = int(label.sum())
    negatives = len(label) - positives
    return float(
        (ranks[label == 1].mean() - (positives + 1) / 2) / negatives
    ), int(len(label))


def spearman(feature: pd.Series, target: pd.Series) -> float:
    values = pd.to_numeric(feature, errors="coerce")
    label = pd.to_numeric(target, errors="coerce")
    ok = values.notna() & np.isfinite(values) & label.notna()
    if ok.sum() < 3:
        return float("nan")
    ranked_values = values[ok].rank().to_numpy(dtype="float64")
    ranked_label = label[ok].rank().to_numpy(dtype="float64")
    if ranked_values.std() == 0 or ranked_label.std() == 0:
        return float("nan")
    return float(np.corrcoef(ranked_values, ranked_label)[0, 1])


def univariate(features: pd.DataFrame, columns: list[str], train: np.ndarray) -> pd.DataFrame:
    rows = []
    target = features["net_return"]
    for column in columns:
        if column not in features:
            continue
        train_auc, train_n = auc(features.loc[train, column], target[train])
        hold_auc, hold_n = auc(
            features.loc[~train, column], target[~train]
        )
        correlation = spearman(features.loc[train, column], target[train])
        rows.append(
            {
                "feature": column,
                "train_n": train_n,
                "train_auc": train_auc,
                "holdout_auc": hold_auc,
                "train_spearman": correlation,
                "auc_gap": abs(train_auc - hold_auc)
                if np.isfinite(train_auc) and np.isfinite(hold_auc)
                else np.nan,
            }
        )
    return pd.DataFrame(rows).sort_values("train_auc", ascending=False)


def quintiles(
    features: pd.DataFrame,
    column: str,
    train: np.ndarray,
    bins: int = 5,
) -> pd.DataFrame:
    values = pd.to_numeric(features[column], errors="coerce")
    train_values = values[train]
    edges = np.unique(
        np.quantile(train_values.dropna(), np.linspace(0, 1, bins + 1))
    )
    if len(edges) < 3:
        return pd.DataFrame()
    bucket = pd.cut(values, bins=edges, include_lowest=True, duplicates="drop")
    rows = []
    for label, block in features.groupby(bucket, observed=True):
        rows.append(
            {
                "bucket": str(label),
                "train_n": int(train[block.index].sum()),
                "train_win_rate": float(
                    (block.loc[train[block.index], "net_return"] > 0).mean()
                )
                if train[block.index].any()
                else np.nan,
                "train_mean_return": float(
                    block.loc[train[block.index], "net_return"].mean()
                )
                if train[block.index].any()
                else np.nan,
                "holdout_n": int((~train[block.index]).sum()),
                "holdout_win_rate": float(
                    (block.loc[~train[block.index], "net_return"] > 0).mean()
                )
                if (~train[block.index]).any()
                else np.nan,
                "holdout_mean_return": float(
                    block.loc[~train[block.index], "net_return"].mean()
                )
                if (~train[block.index]).any()
                else np.nan,
            }
        )
    return pd.DataFrame(rows)


def prepare_matrix(
    features: pd.DataFrame, columns: list[str], train: np.ndarray
) -> tuple[np.ndarray, np.ndarray, np.ndarray, dict[str, tuple[float, float]]]:
    matrix = features[columns].astype("float64").copy()
    matrix = matrix.replace([np.inf, -np.inf], np.nan)
    medians = matrix.iloc[train].median()
    matrix = matrix.fillna(medians).fillna(0.0)
    means = matrix.iloc[train].mean()
    stds = matrix.iloc[train].std(ddof=0).replace(0.0, 1.0)
    matrix = (matrix - means) / stds
    return (
        matrix.to_numpy(),
        features["net_return"].to_numpy(dtype="float64"),
        (features["net_return"] > 0).to_numpy(dtype="int64"),
        {column: (float(means[column]), float(stds[column])) for column in columns},
    )


def fit_logistic(
    design: np.ndarray, label: np.ndarray, l2: float = 1.0, iterations: int = 50
) -> np.ndarray:
    augmented = np.column_stack([np.ones(len(design)), design])
    beta = np.zeros(augmented.shape[1])
    penalty = np.ones_like(beta)
    penalty[0] = 0.0
    for _ in range(iterations):
        probability = 1.0 / (1.0 + np.exp(-np.clip(augmented @ beta, -30, 30)))
        weights = np.clip(probability * (1.0 - probability), 1e-9, None)
        gradient = augmented.T @ (probability - label) + l2 * penalty * beta
        hessian = (
            augmented.T @ (augmented * weights[:, None])
            + l2 * np.diag(penalty)
        )
        step = np.linalg.solve(hessian, gradient)
        beta -= step
        if np.max(np.abs(step)) < 1e-8:
            break
    return beta


def logistic_report(
    features: pd.DataFrame,
    columns: list[str],
    train: np.ndarray,
    l2: float,
) -> tuple[pd.DataFrame, dict[str, object]]:
    design, returns, labels, scaling = prepare_matrix(features, columns, train)
    beta = fit_logistic(design[train], labels[train], l2=l2)
    probability = 1.0 / (
        1.0 + np.exp(-np.clip(np.column_stack([np.ones(len(design)), design]) @ beta, -30, 30))
    )
    holdout = ~train
    hold_auc, hold_n = auc(pd.Series(probability[holdout]), pd.Series(labels[holdout]))
    train_auc, train_n = auc(pd.Series(probability[train]), pd.Series(labels[train]))

    edges = np.unique(np.quantile(probability[train], np.linspace(0, 1, 11)))
    bucket = pd.cut(probability[holdout], bins=edges, include_lowest=True)
    deciles = (
        pd.DataFrame(
            {
                "bucket": bucket,
                "net_return": returns[holdout],
            }
        )
        .groupby("bucket", observed=True)["net_return"]
        .agg(trades="size", mean_return="mean", median_return="median")
        .reset_index()
    )
    deciles["win_rate"] = (
        pd.DataFrame(
            {
                "bucket": bucket,
                "win": (returns[holdout] > 0).astype(int),
            }
        )
        .groupby("bucket", observed=True)["win"]
        .mean()
        .to_numpy()
    )
    deciles["bucket"] = deciles["bucket"].astype(str)
    weights = pd.DataFrame(
        {
            "feature": columns,
            "coef": beta[1:],
        }
    ).sort_values("coef", key=np.abs, ascending=False)
    summary = {
        "train_auc": train_auc,
        "train_n": train_n,
        "holdout_auc": hold_auc,
        "holdout_n": hold_n,
        "l2": l2,
        "scaling": scaling,
    }
    return weights, {"summary": summary, "holdout_deciles": deciles}


# --------------------------------------------------------------------------- #
# Tree
# --------------------------------------------------------------------------- #


@dataclass
class Node:
    feature: str | None = None
    threshold: float | None = None
    left: "Node | None" = None
    right: "Node | None" = None
    value: float = 0.0
    count: int = 0
    indices: np.ndarray | None = None


def _best_split(
    matrix: np.ndarray,
    returns: np.ndarray,
    columns: list[str],
    indices: np.ndarray,
    minimum_leaf: int,
) -> tuple[int, float, float, np.ndarray, np.ndarray] | None:
    parent_mean = returns[indices].mean()
    parent_sse = float(((returns[indices] - parent_mean) ** 2).sum())
    best = None
    for feature_index, _ in enumerate(columns):
        values = matrix[indices, feature_index]
        candidates = np.unique(np.quantile(values, np.linspace(0.1, 0.9, 9)))
        for threshold in candidates:
            left = indices[values <= threshold]
            right = indices[values > threshold]
            if len(left) < minimum_leaf or len(right) < minimum_leaf:
                continue
            sse = float(((returns[left] - returns[left].mean()) ** 2).sum()) + float(
                ((returns[right] - returns[right].mean()) ** 2).sum()
            )
            gain = parent_sse - sse
            if best is None or gain > best[0]:
                best = (gain, feature_index, float(threshold), left, right)
    return best


def build_tree(
    matrix: np.ndarray,
    returns: np.ndarray,
    columns: list[str],
    indices: np.ndarray,
    depth: int,
    minimum_leaf: int,
    max_depth: int,
) -> Node:
    node = Node(
        value=float(returns[indices].mean()),
        count=int(len(indices)),
        indices=indices,
    )
    if depth >= max_depth or len(indices) < 2 * minimum_leaf:
        return node
    split = _best_split(matrix, returns, columns, indices, minimum_leaf)
    if split is None:
        return node
    _, feature_index, threshold, left, right = split
    node.feature = columns[feature_index]
    node.threshold = threshold
    node.left = build_tree(
        matrix, returns, columns, left, depth + 1, minimum_leaf, max_depth
    )
    node.right = build_tree(
        matrix, returns, columns, right, depth + 1, minimum_leaf, max_depth
    )
    return node


def tree_leaves(node: Node) -> list[Node]:
    if node.feature is None or node.left is None or node.right is None:
        return [node]
    return tree_leaves(node.left) + tree_leaves(node.right)


def describe_tree(node: Node, depth: int = 0) -> list[str]:
    indent = "  " * depth
    if node.feature is None:
        return [
            f"{indent}leaf: n={node.count}, train_mean={100 * node.value:+.1f}%"
        ]
    return [
        f"{indent}{node.feature} <= {node.threshold:.4g}",
        *describe_tree(node.left, depth + 1),
        f"{indent}{node.feature} > {node.threshold:.4g}",
        *describe_tree(node.right, depth + 1),
    ]


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #


def _fmt_pct(value: float | None) -> str:
    if value is None or not np.isfinite(value):
        return "-"
    return f"{100.0 * value:+.1f} %"


def render_report(
    features: pd.DataFrame,
    stats: pd.DataFrame,
    tables: dict[str, pd.DataFrame],
    weights: pd.DataFrame,
    logistic: dict[str, object],
    tree_lines: list[str],
    tree_leaves_table: pd.DataFrame,
    split: Split,
    horizon: int,
) -> str:
    lines = [
        "# AveSignalMonitor: what separates profitable signals?",
        "",
        f"All features are cut off at the {horizon}-minute entry decision. "
        "Patterns are selected on train days and must survive the holdout.",
        "",
        f"- Train: 2026-10-03 .. 2026-10-05 ({int(split.train.sum())} priced signals)",
        f"- Holdout: 2026-08-09 + 2026-11-08 ({int(split.holdout.sum())} priced signals)",
        f"- Base win rate: {100.0 * (features['net_return'] > 0).mean():.1f} %; "
        f"mean {_fmt_pct(features['net_return'].mean())}",
        "",
        "## Univariate screen",
        "",
        "| Feature | Train AUC | Holdout AUC | Gap | Train Spearman |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for row in stats.head(15).itertuples(index=False):
        lines.append(
            f"| {row.feature} | {row.train_auc:.3f} | {row.holdout_auc:.3f} | "
            f"{row.auc_gap:.3f} | {row.train_spearman:+.3f} |"
        )
    lines += ["", "## Quintiles (train edges applied to holdout)", ""]
    for column, table in tables.items():
        lines += [
            f"### {column}",
            "",
            "| Bucket | Train n | Train win | Train mean | Holdout n | "
            "Holdout win | Holdout mean |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
        for row in table.itertuples(index=False):
            lines.append(
                f"| {row.bucket} | {row.train_n} | "
                f"{100.0 * row.train_win_rate:.0f} % | "
                f"{_fmt_pct(row.train_mean_return)} | {row.holdout_n} | "
                f"{100.0 * row.holdout_win_rate:.0f} % | "
                f"{_fmt_pct(row.holdout_mean_return)} |"
            )
        lines.append("")
    summary = logistic["summary"]
    lines += [
        "## Logistic model",
        "",
        f"Train AUC {summary['train_auc']:.3f}, holdout AUC "
        f"{summary['holdout_auc']:.3f}.",
        "",
        "| Feature | Coefficient |",
        "| --- | ---: |",
    ]
    for row in weights.head(12).itertuples(index=False):
        lines.append(f"| {row.feature} | {row.coef:+.3f} |")
    lines += [
        "",
        "### Holdout deciles by predicted probability (low to high)",
        "",
        "| Decile | Trades | Win rate | Mean return | Median return |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for index, row in enumerate(logistic["holdout_deciles"].itertuples(index=False)):
        lines.append(
            f"| {index + 1} | {row.trades} | {100.0 * row.win_rate:.0f} % | "
            f"{_fmt_pct(row.mean_return)} | {_fmt_pct(row.median_return)} |"
        )
    lines += [
        "",
        "## Depth-3 regression tree (train only)",
        "",
        "```",
        *tree_lines,
        "```",
        "",
        "| Leaf | Train n | Train mean | Holdout n | Holdout mean | Holdout win |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in tree_leaves_table.itertuples(index=False):
        lines.append(
            f"| {row.leaf} | {row.train_n} | {_fmt_pct(row.train_mean)} | "
            f"{row.holdout_n} | {_fmt_pct(row.holdout_mean)} | "
            f"{100.0 * row.holdout_win:.0f} % |"
        )
    lines += [
        "",
        "## Caveats",
        "",
        "- This is exploratory. Many features were screened, so the holdout is "
        "the only defence against selection noise; treat any single small "
        "bucket as provisional.",
        "- Alerts are one-per-mint in this dataset, but several alerts can share "
        "the same day regime. The holdout spans two different days, not a "
        "random sample.",
        "- The target is net return at the chosen horizon after 1.25 % per-side "
        "fees. A pattern in 30-minute outcomes need not survive other horizons.",
        "- No slot, cash, routing, liquidity-impact or PIT constraints are "
        "modelled. This does not replace the execution simulator.",
        "",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", nargs="+", default=list(DEFAULT_DAYS))
    parser.add_argument("--train-days", nargs="+", default=list(DEFAULT_TRAIN))
    parser.add_argument("--horizon", type=int, default=30)
    parser.add_argument("--min-leaf", type=int, default=40)
    parser.add_argument("--tree-depth", type=int, default=3)
    parser.add_argument("--l2", type=float, default=4.0)
    parser.add_argument(
        "--output-dir", type=Path, default=BACKTEST_ROOT / "patterns"
    )
    parser.add_argument(
        "--outcomes-csv",
        type=Path,
        default=BACKTEST_ROOT / "signals_backtest.csv",
        help=(
            "Per-signal outcomes to model. Point this at "
            "ave_signals_8d_event/signals_backtest.csv to score patterns with "
            "realistic trade fills instead of 1-minute closes."
        ),
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

    outcomes_path = (
        args.outcomes_csv
        if args.outcomes_csv.is_absolute()
        else repo_root / args.outcomes_csv
    )
    outcomes = pd.read_csv(outcomes_path)
    outcomes = outcomes[
        (outcomes["status"] == "closed")
        & (outcomes["horizon_minutes"] == args.horizon)
    ].copy()
    signals = load_signals(repo_root, list(args.days))
    signals = signals[
        signals["signal_id"].isin(set(outcomes["signal_id"]))
    ].reset_index(drop=True)

    alert = pd.DataFrame(
        [parse_alert(text) for text in signals["text"]]
    )
    signals = pd.concat([signals, alert], axis=1)
    context = context_features(signals)
    bar = pd.concat(
        [
            load_bar_features(
                repo_root,
                day,
                signals[signals["day"] == day],
                outcomes,
            )
            for day in args.days
        ],
        ignore_index=True,
    )
    events = pd.concat(
        [
            load_event_features(
                repo_root,
                day,
                signals[signals["day"] == day],
                outcomes,
            )
            for day in args.days
        ],
        ignore_index=True,
    )

    features = (
        signals[
            [
                "signal_id",
                "day",
                "signal_timestamp_utc",
                "mint",
                "alert_max_pump",
                "alert_already_pumped",
                "alert_wallet_count",
                "alert_smart_wallet",
                "alert_mc",
                "alert_total_buy",
                "alert_inflow_n",
                "alert_inflow_sum",
                "alert_inflow_max",
                "alert_inflow_top_share",
                "alert_inflow_hhi",
                "alert_inflow_non_sol",
                "alert_buy_per_wallet",
                "alert_buy_per_mc",
            ]
        ]
        .merge(bar, on="signal_id", how="left")
        .merge(events, on="signal_id", how="left")
        .merge(context, on="signal_id", how="left")
        .merge(
            outcomes[
                [
                    "signal_id",
                    "net_return",
                    "gross_return",
                    "mfe",
                    "mae",
                    "entry_price_sol",
                    "entry_price_age_ms",
                    "exit_price_age_ms",
                    "flagged_mint",
                ]
            ],
            on="signal_id",
            how="inner",
        )
    )
    features["is_train"] = features["day"].isin(set(args.train_days))
    features["win"] = (features["net_return"] > 0).astype(int)
    features["big_win"] = (features["net_return"] >= 1.0).astype(int)
    features["rug"] = (features["net_return"] <= -0.9).astype(int)
    split = Split(
        train=features["is_train"].to_numpy(),
        holdout=(~features["is_train"]).to_numpy(),
    )
    features.to_csv(output_dir / "features.csv", index=False)

    columns = [
        *ALERT_FEATURES,
        *BAR_FEATURES,
        *EVENT_FEATURES,
        *CONTEXT_FEATURES,
    ]
    columns = [column for column in columns if column in features.columns]
    stats = univariate(features, columns, split.train)
    stats.to_csv(output_dir / "univariate.csv", index=False)

    focus = [
        column
        for column in stats["feature"].tolist()
        if abs(stats.loc[stats["feature"] == column, "train_auc"].iloc[0] - 0.5)
        > 0.04
    ][:8]
    tables = {}
    for column in focus:
        table = quintiles(features, column, split.train)
        if not table.empty:
            tables[column] = table
    for column, table in tables.items():
        table.to_csv(output_dir / f"quintiles_{column}.csv", index=False)

    eligible = [
        column
        for column in stats["feature"].tolist()
        if stats.loc[stats["feature"] == column, "train_n"].iloc[0] >= 200
    ]
    weights, logistic = logistic_report(
        features, eligible, split.train, args.l2
    )
    weights.to_csv(output_dir / "logistic_weights.csv", index=False)
    logistic["holdout_deciles"].to_csv(
        output_dir / "logistic_holdout_deciles.csv", index=False
    )

    _, returns, _, _ = prepare_matrix(features, eligible, split.train)
    raw = features[eligible].astype("float64").replace([np.inf, -np.inf], np.nan)
    raw = raw.fillna(raw.iloc[split.train].median()).fillna(0.0)
    tree_matrix = raw.to_numpy()
    tree = build_tree(
        tree_matrix,
        returns,
        eligible,
        np.arange(len(features)),
        0,
        args.min_leaf,
        args.tree_depth,
    )
    tree_lines = describe_tree(tree)
    leaves = tree_leaves(tree)
    leaf_rows = []
    for index, leaf in enumerate(leaves):
        if leaf.indices is None:
            continue
        in_train = split.train[leaf.indices]
        in_holdout = split.holdout[leaf.indices]
        leaf_rows.append(
            {
                "leaf": str(index + 1),
                "train_n": int(in_train.sum()),
                "train_mean": float(returns[leaf.indices][in_train].mean())
                if in_train.any()
                else np.nan,
                "holdout_n": int(in_holdout.sum()),
                "holdout_mean": float(returns[leaf.indices][in_holdout].mean())
                if in_holdout.any()
                else np.nan,
                "holdout_win": float((returns[leaf.indices][in_holdout] > 0).mean())
                if in_holdout.any()
                else np.nan,
                "_indices": leaf.indices,
            }
        )
    leaf_table = pd.DataFrame(leaf_rows)
    leaf_table.drop(columns=["_indices"]).to_csv(
        output_dir / "tree_leaves.csv", index=False
    )

    lines = [f"holdout base mean {_fmt_pct(returns[split.holdout].mean())}"]
    lines += tree_lines

    (output_dir / "summary.json").write_text(
        json.dumps(
            {
                "config": {
                    "days": list(args.days),
                    "train_days": list(args.train_days),
                    "horizon": args.horizon,
                    "min_leaf": args.min_leaf,
                    "tree_depth": args.tree_depth,
                    "l2": args.l2,
                },
                "base_rates": {
                    "n": int(len(features)),
                    "win": float(features["win"].mean()),
                    "big_win": float(features["big_win"].mean()),
                    "rug": float(features["rug"].mean()),
                    "mean_return": float(features["net_return"].mean()),
                },
                "logistic": {
                    **logistic["summary"],
                    "holdout_deciles": logistic["holdout_deciles"].to_dict(
                        orient="records"
                    ),
                },
                "tree": {
                    "text": tree_lines,
                    "leaves": leaf_table.drop(columns=["_indices"]).to_dict(
                        orient="records"
                    ),
                },
            },
            indent=2,
            default=float,
        )
        + "\n",
        encoding="utf-8",
    )
    (output_dir / "report.md").write_text(
        render_report(
            features,
            stats,
            tables,
            weights,
            logistic,
            lines,
            leaf_table.drop(columns=["_indices"]),
            split,
            args.horizon,
        ),
        encoding="utf-8",
    )

    print(f"features: {len(features)} | train {int(split.train.sum())} | "
          f"holdout {int(split.holdout.sum())}")
    print(f"base win rate {100 * features['win'].mean():.1f} % | "
          f"mean {100 * features['net_return'].mean():+.1f} %")
    print()
    print(stats.head(12).to_string(index=False))
    print()
    print(
        f"logistic train AUC {logistic['summary']['train_auc']:.3f} | "
        f"holdout AUC {logistic['summary']['holdout_auc']:.3f}"
    )
    print()
    print("\n".join(lines))
    print()
    print(f"outputs: {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
