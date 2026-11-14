#!/usr/bin/env python3
"""Build a clean, aligned VectorBT dataset from a raw OHLCV timeframe CSV.

Requirements:
    pip install pandas numpy

Examples:
    python build_vbt_dataset.py ohlcv_clean/ohlcv_1m.csv -o vbt_dataset_1m
    python build_vbt_dataset.py ohlcv_clean/ohlcv_5s.csv -o vbt_dataset_5s --min-trades 30

Output files:
    universe.csv             Full-window descriptive metrics plus point-in-time eligibility summary.
    universe_timeline.csv    Per-mint decision history with cumulative metrics available at each bar.
    universe_transitions.csv Eligibility entry and exit events with their decision-time metrics.
    wide/<name>.csv          Aligned Spot, directional Execution, freshness, and universe masks.
    ohlcv_clean_<tf>.csv     Long-format data with price-validity and point-in-time eligibility flags.
    dataset_report.json      Configuration, point-in-time universe policy, coverage, and validation.

Point-in-time universe rules:
    * Universe metrics are cumulative and use completed bars only.
    * Eligibility is shifted forward by at least one full bar; a mint is never tradable in the bar that first makes it qualify.
    * All mints are retained in aligned frames; strategies must apply universe_eligible at every decision and entry timestamp.
    * Full-window fields in universe.csv are descriptive only and never determine historical eligibility.

Data integrity rules:
    * Spot OHLC and BUY/SELL Execution OHLC are stored separately.
    * Execution OHLC uses valid observed component prices and is never forward-filled.
    * Filled candles carry only the previous Spot mark; --max-stale-bars controls mark validity.
    * The default --max-stale-bars=0 means filled prices are not valid marks.
    * Prices before a token's first trade remain NaN.
    * candle_range_anomaly_flag is an audit warning; it does not remove or alter data.
    * Historical fills from other traders are not guaranteed fills for hypothetical orders.
      Order size, pool depth, fees, price impact, and slippage must still be modeled.
"""

import argparse
import json
import os
import re
import sys

import numpy as np
import pandas as pd

DATASET_BUILDER_VERSION = "v7.0"
REQUIRED = ["mint", "timestamp_ms", "open", "high", "low", "close", "trades", "is_filled"]
OPTIONAL_NUM = [
    "vwap_sol", "volume_token", "volume_sol", "jump_flag", "pool_changed",
    "non_wsol_trades", "zero_quote_trades", "routed_trades", "bundle_cand_trades",
    "spot_open", "spot_high", "spot_low", "spot_close",
    "buy_execution_open", "buy_execution_high", "buy_execution_low", "buy_execution_close",
    "sell_execution_open", "sell_execution_high", "sell_execution_low", "sell_execution_close",
    "buy_execution_components", "sell_execution_components", "execution_valid_events",
    "execution_invalid_events", "execution_component_count", "execution_coverage",
    "execution_ohlc_complete", "candle_range_ratio", "candle_range_anomaly_flag",
]
EXECUTION_PRICE_COLUMNS = [
    f"{side}_execution_{field}"
    for side in ("buy", "sell")
    for field in ("open", "high", "low", "close")
]
FRAMES = [
    "open", "high", "low", "close",
    "spot_open", "spot_high", "spot_low", "spot_close",
    "spot_valid_open", "spot_valid_high", "spot_valid_low", "spot_valid_close",
    *EXECUTION_PRICE_COLUMNS,
    "execution_ohlc_complete", "execution_coverage", "execution_valid",
    "buy_execution_valid", "sell_execution_valid",
    "spot_mark_valid", "candle_range_ratio", "candle_range_anomaly_flag",
    "vwap_sol", "volume_token", "volume_sol", "trades",
    "real_bar", "stale_bars", "age_bars", "jump_flag", "universe_eligible",
]


def infer_bar_ms(path, override):
    if override:
        return int(override * 1000)
    m = re.search(r"(\d+)([smh])", os.path.basename(path).lower())
    if not m:
        sys.exit("Could not infer the bar interval from the input filename; provide --bar-seconds.")
    return int(m.group(1)) * {"s": 1000, "m": 60_000, "h": 3_600_000}[m.group(2)]


def load(path):
    df = pd.read_csv(path, low_memory=False)
    miss = [c for c in REQUIRED if c not in df.columns]
    if miss:
        sys.exit(f"Missing required columns: {miss}")

    original_columns = set(df.columns)
    has_execution = all(c in original_columns for c in EXECUTION_PRICE_COLUMNS) and (
        "execution_ohlc_complete" in original_columns
    )
    df.attrs["has_directional_execution_ohlc"] = has_execution

    for c in OPTIONAL_NUM:
        if c in df.columns:
            continue
        if c.startswith("spot_"):
            df[c] = df[c.replace("spot_", "")]
        elif c in ("vwap_sol", "volume_token", "volume_sol", "execution_coverage", "candle_range_ratio") or c in EXECUTION_PRICE_COLUMNS:
            df[c] = np.nan
        else:
            df[c] = 0

    if "pool_id" not in df.columns:
        df["pool_id"] = ""
    return df


# ----------------------------------------------------------------------------- universe
def mint_table(real, a):
    g = real.groupby("mint")
    m = g.agg(trades=("trades", "sum"), real_bars=("trades", "size"),
              volume_sol=("volume_sol", "sum"), volume_token=("volume_token", "sum"),
              first_ts=("timestamp_ms", "min"), last_ts=("timestamp_ms", "max"),
              median_close=("close", "median"), price_min=("low", "min"), price_max=("high", "max"),
              non_wsol=("non_wsol_trades", "sum"), zero_quote=("zero_quote_trades", "sum"),
              routed=("routed_trades", "sum"), jump_bars=("jump_flag", "sum"),
              pool_changes=("pool_changed", "sum"), n_pools=("pool_id", "nunique"))
    m["range_ratio"] = m["price_max"] / m["price_min"]
    m["non_wsol_frac"] = m["non_wsol"] / m["trades"]
    m["zero_quote_frac"] = m["zero_quote"] / m["trades"]
    m["routed_frac"] = m["routed"] / m["trades"]

    rules = {
        "few_trades": m["trades"] < a.min_trades,
        "few_bars": m["real_bars"] < a.min_bars,
        "low_volume": m["volume_sol"] < a.min_volume_sol,
        "price_out_of_range": ~m["median_close"].between(a.min_price, a.max_price),
        "pegged_or_flat": (m["range_ratio"] < a.pegged_ratio) & (m["trades"] >= a.pegged_min_trades),
        "non_wsol_dominant": m["non_wsol_frac"] > a.max_non_wsol_frac,
        "dust_dominant": m["zero_quote_frac"] > a.max_zero_quote_frac,
    }
    reasons = pd.Series("", index=m.index)
    for name, mask in rules.items():
        reasons = reasons.where(~mask, reasons + name + ";")
    m["exclude_reasons"] = reasons
    m["included"] = (reasons == "") | a.keep_all
    m["first_utc"] = pd.to_datetime(m["first_ts"], unit="ms").dt.strftime("%Y-%m-%d %H:%M:%S")
    m["last_utc"] = pd.to_datetime(m["last_ts"], unit="ms").dt.strftime("%Y-%m-%d %H:%M:%S")
    return m, {k: int(v.sum()) for k, v in rules.items()}


def build_point_in_time_universe(real, grid, a, delay_bars=1):
    """Build an eligibility mask using only completed bars available before each decision bar."""
    if delay_bars < 1:
        raise ValueError("delay_bars must be >= 1 to prevent same-bar look-ahead")

    cols = pd.Index(sorted(real["mint"].unique()), name="mint")
    index = pd.Index(grid, name="timestamp_ms")

    def pivot(column):
        if column not in real.columns:
            return pd.DataFrame(np.nan, index=index, columns=cols)
        return real.pivot(index="timestamp_ms", columns="mint", values=column).reindex(
            index=index, columns=cols
        )

    close = pivot("close")
    high = pivot("high")
    low = pivot("low")
    real_bar = close.notna()

    trades = pivot("trades").fillna(0.0)
    volume_sol = pivot("volume_sol").fillna(0.0)
    non_wsol = pivot("non_wsol_trades").fillna(0.0)
    zero_quote = pivot("zero_quote_trades").fillna(0.0)

    cumulative = {
        "cumulative_trades": trades.cumsum(),
        "cumulative_real_bars": real_bar.astype("int64").cumsum(),
        "cumulative_volume_sol": volume_sol.cumsum(),
        "cumulative_non_wsol_trades": non_wsol.cumsum(),
        "cumulative_zero_quote_trades": zero_quote.cumsum(),
        "median_close": close.expanding(min_periods=1).median(),
        "cumulative_price_min": low.expanding(min_periods=1).min(),
        "cumulative_price_max": high.expanding(min_periods=1).max(),
    }
    cumulative["range_ratio"] = (
        cumulative["cumulative_price_max"] / cumulative["cumulative_price_min"]
    )
    cumulative["non_wsol_frac"] = (
        cumulative["cumulative_non_wsol_trades"] / cumulative["cumulative_trades"].replace(0, np.nan)
    )
    cumulative["zero_quote_frac"] = (
        cumulative["cumulative_zero_quote_trades"] / cumulative["cumulative_trades"].replace(0, np.nan)
    )

    # Shift every metric by complete bars so decisions at bar t only see data through t-delay.
    known = {name: values.shift(delay_bars) for name, values in cumulative.items()}
    known_real_bars = known["cumulative_real_bars"].fillna(0)
    known_trades = known["cumulative_trades"].fillna(0)
    known_volume = known["cumulative_volume_sol"].fillna(0)

    rules = {
        "few_trades": known_trades < a.min_trades,
        "few_bars": known_real_bars < a.min_bars,
        "low_volume": known_volume < a.min_volume_sol,
        "price_out_of_range": (
            known["median_close"].isna()
            | ~(
                known["median_close"].ge(a.min_price)
                & known["median_close"].le(a.max_price)
            )
        ),
        "pegged_or_flat": (
            known["range_ratio"].notna()
            & known["range_ratio"].lt(a.pegged_ratio)
            & known_trades.ge(a.pegged_min_trades)
        ),
        "non_wsol_dominant": known["non_wsol_frac"].gt(a.max_non_wsol_frac),
        "dust_dominant": known["zero_quote_frac"].gt(a.max_zero_quote_frac),
    }

    reasons = pd.DataFrame("", index=index, columns=cols, dtype=object)
    for name, mask in rules.items():
        mask = mask.fillna(False)
        reasons = reasons.where(~mask, reasons + name + ";")

    history_available = known_real_bars.gt(0)
    if a.keep_all:
        eligible = history_available
        reasons = reasons.where(~eligible, "")
    else:
        eligible = reasons.eq("") & history_available
        reasons = reasons.where(~history_available, reasons)
        reasons = reasons.mask(~history_available, reasons + "insufficient_prior_history;")

    eligible = eligible.fillna(False).astype("int8")
    known["exclude_reasons"] = reasons
    return {"eligible": eligible, "metrics": known, "exclude_reasons": reasons}


def attach_point_in_time_summary(meta, universe, grid):
    """Attach point-in-time eligibility summaries without using them to filter historical rows."""
    eligible = universe["eligible"].astype(bool)
    previous = eligible.shift(1, fill_value=False)
    entered = eligible & ~previous
    exited = ~eligible & previous

    meta["included_at_window_end"] = meta["included"].astype(bool)
    meta["window_end_exclude_reasons"] = meta["exclude_reasons"]
    meta = meta.drop(columns=["included", "exclude_reasons"])
    meta["ever_eligible"] = eligible.any(axis=0)
    meta["eligible_at_window_end"] = eligible.iloc[-1].astype(bool)
    meta["eligible_bar_count"] = eligible.sum(axis=0).astype("int64")
    meta["eligibility_entry_count"] = entered.sum(axis=0).astype("int64")
    meta["eligibility_exit_count"] = exited.sum(axis=0).astype("int64")
    meta["universe_mode"] = "point_in_time"
    meta["window_metrics_scope"] = "full_input_window_descriptive_only"

    first_eligible = {}
    last_eligible = {}
    for mint in eligible.columns:
        positions = np.flatnonzero(eligible[mint].to_numpy())
        first_eligible[mint] = int(grid[positions[0]]) if len(positions) else np.nan
        last_eligible[mint] = int(grid[positions[-1]]) if len(positions) else np.nan
    meta["first_eligible_ts"] = pd.Series(first_eligible)
    meta["last_eligible_ts"] = pd.Series(last_eligible)
    meta["first_eligible_utc"] = pd.to_datetime(meta["first_eligible_ts"], unit="ms").dt.strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    meta["last_eligible_utc"] = pd.to_datetime(meta["last_eligible_ts"], unit="ms").dt.strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    return meta


def universe_audit_tables(universe, alive, grid, delay_bars, bar_ms):
    """Create row-level decision history and eligibility transition audit tables."""
    eligible = universe["eligible"]
    metrics = universe["metrics"]
    reasons = universe["exclude_reasons"]
    cols = eligible.columns
    mi, ti = np.nonzero(alive.reindex(index=grid, columns=cols).to_numpy().T)
    timestamps = np.asarray(grid, dtype="int64")[ti]
    timeline = pd.DataFrame({
        "mint": cols.to_numpy()[mi],
        "timestamp_ms": timestamps,
        "datetime_utc": pd.to_datetime(timestamps, unit="ms").strftime("%Y-%m-%d %H:%M:%S"),
        "decision_cutoff_timestamp_ms": timestamps - delay_bars * bar_ms,
        "universe_eligible": eligible.to_numpy()[ti, mi].astype("int8"),
        "exclude_reasons": reasons.to_numpy()[ti, mi],
    })
    for name, frame in metrics.items():
        if name == "exclude_reasons":
            continue
        timeline[name] = frame.to_numpy()[ti, mi]

    previous = eligible.shift(1, fill_value=0).astype("int8")
    entered = eligible.eq(1) & previous.eq(0)
    exited = eligible.eq(0) & previous.eq(1)
    entry_i, entry_j = np.nonzero(entered.to_numpy())
    exit_i, exit_j = np.nonzero(exited.to_numpy())

    transition_rows = []
    for event_type, rows, cols_idx in (
        ("entered", entry_i, entry_j),
        ("exited", exit_i, exit_j),
    ):
        for row, col in zip(rows, cols_idx):
            transition_rows.append({
                "mint": cols[col],
                "timestamp_ms": int(grid[row]),
                "datetime_utc": pd.to_datetime(int(grid[row]), unit="ms").strftime("%Y-%m-%d %H:%M:%S"),
                "decision_cutoff_timestamp_ms": int(grid[row] - delay_bars * bar_ms),
                "transition": event_type,
                "exclude_reasons": reasons.iat[row, col],
                "cumulative_trades": metrics["cumulative_trades"].iat[row, col],
                "cumulative_real_bars": metrics["cumulative_real_bars"].iat[row, col],
                "cumulative_volume_sol": metrics["cumulative_volume_sol"].iat[row, col],
                "median_close": metrics["median_close"].iat[row, col],
                "range_ratio": metrics["range_ratio"].iat[row, col],
                "non_wsol_frac": metrics["non_wsol_frac"].iat[row, col],
                "zero_quote_frac": metrics["zero_quote_frac"].iat[row, col],
            })
    transitions = pd.DataFrame(transition_rows)
    if transitions.empty:
        transitions = pd.DataFrame(columns=[
            "mint", "timestamp_ms", "datetime_utc", "decision_cutoff_timestamp_ms",
            "transition", "exclude_reasons", "cumulative_trades", "cumulative_real_bars",
            "cumulative_volume_sol", "median_close", "range_ratio", "non_wsol_frac", "zero_quote_frac",
        ])
    else:
        transitions = transitions.sort_values(["timestamp_ms", "mint"], kind="stable").reset_index(drop=True)
    return timeline, transitions


# ----------------------------------------------------------------------------- wide frames
def build_wide(real, grid, last_ts, tail_fill, keep_first_open,
               max_stale_bars=0, candle_range_ratio=10.0, universe_eligible=None):
    """Build aligned spot marks and directional observed-execution OHLC frames."""
    if max_stale_bars < 0:
        raise ValueError("max_stale_bars must be >= 0")
    if not np.isfinite(candle_range_ratio) or candle_range_ratio <= 1:
        raise ValueError("candle_range_ratio must be a finite number greater than 1")

    cols = pd.Index(sorted(real["mint"].unique()), name="mint")

    def piv(c):
        source = c if c in real.columns else c.replace("spot_", "")
        return real.pivot(index="timestamp_ms", columns="mint", values=source).reindex(index=grid, columns=cols)

    spot_close_w = piv("spot_close")
    real_m = spot_close_w.notna()
    spot_close_f = spot_close_w.ffill()
    alive = spot_close_f.notna()
    if not tail_fill:
        alive &= pd.DataFrame(
            grid[:, None] <= last_ts.reindex(cols).values[None, :], index=grid, columns=cols
        )
        spot_close_f = spot_close_f.where(alive)

    spot_open_w, spot_high_w, spot_low_w = piv("spot_open"), piv("spot_high"), piv("spot_low")
    spot_open_f = spot_open_w.where(real_m, spot_close_f)
    spot_high_f = spot_high_w.where(real_m, spot_close_f)
    spot_low_f = spot_low_w.where(real_m, spot_close_f)

    # Backward-compatible OHLC: optional open adjustment is kept separate from spot_open.
    if keep_first_open:
        open_f = spot_open_w.where(real_m, spot_close_f)
    else:
        prev = spot_close_f.shift(1)
        open_f = prev.where(real_m & prev.notna(), spot_open_w).where(real_m, spot_close_f)
    high_f = np.maximum(spot_high_w, open_f).where(real_m, spot_close_f)
    low_f = np.minimum(spot_low_w, open_f).where(real_m, spot_close_f)
    close_f = spot_close_f

    pos = np.arange(len(grid))[:, None]
    last_pos = pd.DataFrame(
        np.where(real_m.values, pos, np.nan), index=grid, columns=cols
    ).ffill()
    first_pos = real_m.values.argmax(axis=0)[None, :]
    stale_bars = (pos - last_pos).astype("float64")
    stale_bars = pd.DataFrame(stale_bars, index=grid, columns=cols).where(alive)
    age_bars = pd.DataFrame(pos - first_pos, index=grid, columns=cols).where(alive)
    spot_mark_valid = alive & stale_bars.le(max_stale_bars)

    if universe_eligible is None:
        # Default helper behavior: a mint can only be considered after a prior real bar exists.
        universe_eligible = real_m.shift(1, fill_value=False).cummax()
    else:
        universe_eligible = universe_eligible.reindex(index=grid, columns=cols).fillna(0).astype("int8")

    # Directional execution OHLC is observed on real bars only. Never forward-fill it.
    exec_frames = {}
    for col in EXECUTION_PRICE_COLUMNS:
        exec_frames[col] = piv(col)
    execution_complete_source = piv("execution_ohlc_complete").eq(1) & real_m
    buy_components = piv("buy_execution_components").fillna(0.0)
    sell_components = piv("sell_execution_components").fillna(0.0)

    buy_valid = execution_complete_source & buy_components.gt(0) & exec_frames["buy_execution_close"].notna()
    sell_valid = execution_complete_source & sell_components.gt(0) & exec_frames["sell_execution_close"].notna()
    execution_valid = buy_valid | sell_valid

    out = {
        "open": open_f, "high": high_f, "low": low_f, "close": close_f,
        "spot_open": spot_open_f, "spot_high": spot_high_f,
        "spot_low": spot_low_f, "spot_close": spot_close_f,
        "spot_valid_open": spot_open_f.where(spot_mark_valid),
        "spot_valid_high": spot_high_f.where(spot_mark_valid),
        "spot_valid_low": spot_low_f.where(spot_mark_valid),
        "spot_valid_close": spot_close_f.where(spot_mark_valid),
        "execution_ohlc_complete": execution_complete_source.astype("int8"),
        "execution_coverage": piv("execution_coverage").where(real_m).fillna(0.0),
        "execution_valid": execution_valid.astype("int8"),
        "buy_execution_valid": buy_valid.astype("int8"),
        "sell_execution_valid": sell_valid.astype("int8"),
        "spot_mark_valid": spot_mark_valid.astype("int8"),
        "universe_eligible": universe_eligible.astype("int8"),
        "stale_bars": stale_bars,
        "age_bars": age_bars,
    }
    for col, frame in exec_frames.items():
        side = col.split("_", 1)[0]
        valid_mask = buy_valid if side == "buy" else sell_valid
        # Incomplete bars remain in source OHLCV/audit, but are masked in the evaluation dataset.
        out[col] = frame.where(valid_mask)

    range_source = piv("candle_range_ratio")
    with np.errstate(divide="ignore", invalid="ignore"):
        calculated_range = spot_high_w / spot_low_w
    range_ratio = range_source.where(range_source.notna(), calculated_range)
    range_ratio = range_ratio.where(real_m, 1.0).where(alive)
    out["candle_range_ratio"] = range_ratio
    out["candle_range_anomaly_flag"] = (
        real_m & range_ratio.ge(candle_range_ratio)
    ).fillna(False).astype("int8")

    out["vwap_sol"] = piv("vwap_sol")
    for v in ("volume_token", "volume_sol", "trades", "jump_flag"):
        out[v] = piv(v).where(real_m, 0.0).where(alive)

    out["real_bar"] = real_m.astype("int8")
    out["stale_bars"] = out["stale_bars"].where(alive)
    out["age_bars"] = out["age_bars"].where(alive)
    return {k: out[k] for k in FRAMES}, alive


def validate(fr, alive, max_stale_bars=0, candle_range_ratio=10.0):
    """Validate spot marks, freshness masks, directional execution OHLC, and anomaly flags."""
    o, h, l, c = fr["open"], fr["high"], fr["low"], fr["close"]
    real = fr["real_bar"] == 1
    a = alive.values
    spot_o, spot_h, spot_l, spot_c = (
        fr["spot_open"], fr["spot_high"], fr["spot_low"], fr["spot_close"]
    )

    def side_errors(side):
        valid = fr[f"{side}_execution_valid"] == 1
        eo = fr[f"{side}_execution_open"]
        eh = fr[f"{side}_execution_high"]
        el = fr[f"{side}_execution_low"]
        ec = fr[f"{side}_execution_close"]
        bad = (
            eo.isna() | eh.isna() | el.isna() | ec.isna()
            | (eo <= 0) | (eh <= 0) | (el <= 0) | (ec <= 0)
            | (eh < np.maximum(eo, ec)) | (el > np.minimum(eo, ec))
        )
        return int((bad & valid).values.sum())

    expected_mark_valid = alive & fr["stale_bars"].le(max_stale_bars)
    expected_range = spot_h / spot_l
    expected_range_flag = (real & expected_range.ge(candle_range_ratio)).fillna(False).astype("int8")
    execution_any_values = np.zeros(real.shape, dtype=bool)
    for execution_column in EXECUTION_PRICE_COLUMNS:
        execution_any_values |= fr[execution_column].notna().values
    execution_any = pd.DataFrame(execution_any_values, index=real.index, columns=real.columns)
    universe_mask = fr["universe_eligible"]
    prior_real_seen = real.shift(1, fill_value=False).cummax()
    chk = {
        "universe_mask_non_binary": int((~universe_mask.isin([0, 1])).values.sum()),
        "universe_eligible_before_prior_real_bar": int((universe_mask.astype(bool) & ~prior_real_seen).values.sum()),
        "nan_ohlc_in_alive": int(np.isnan(np.stack([o.values, h.values, l.values, c.values]))[:, a].sum()),
        "non_positive_price": int(((np.stack([o.values, h.values, l.values, c.values]) <= 0) & a).sum()),
        "high_lt_open_close": int(((h.values < np.maximum(o.values, c.values)) & a).sum()),
        "low_gt_open_close": int(((l.values > np.minimum(o.values, c.values)) & a).sum()),
        "negative_volume": int(((fr["volume_token"].values < 0) | (fr["volume_sol"].values < 0)).sum()),
        "filled_spot_bar_not_flat": int((((spot_o != spot_c) | (spot_h != spot_c) | (spot_l != spot_c)) & alive & ~real).values.sum()),
        "real_bar_stale_not_zero": int(((fr["stale_bars"] != 0) & real).values.sum()),
        "negative_stale_or_age": int(((fr["stale_bars"] < 0) | (fr["age_bars"] < 0)).values.sum()),
        "spot_mark_validity_mismatch": int((fr["spot_mark_valid"].astype(bool) != expected_mark_valid).values.sum()),
        "spot_valid_price_mask_mismatch": int((
            sum(
                (fr[f"spot_valid_{field}"].notna() != expected_mark_valid).values.sum()
                for field in ("open", "high", "low", "close")
            )
            + sum(
                np.count_nonzero(
                    expected_mark_valid.values
                    & ~np.isclose(
                        fr[f"spot_valid_{field}"].values, fr[f"spot_{field}"].values,
                        rtol=1e-9, atol=1e-12, equal_nan=True,
                    )
                )
                for field in ("open", "high", "low", "close")
            )
        )),
        "real_bars_missing_trades": int(((fr["trades"] < 1) & real).values.sum()),
        "grid_irregular": int(len(np.unique(np.diff(o.index.values))) > 1),
        "invalid_spot_ohlc": int((
            spot_o.isna() | spot_h.isna() | spot_l.isna() | spot_c.isna()
            | (spot_o <= 0) | (spot_h <= 0) | (spot_l <= 0) | (spot_c <= 0)
            | (spot_h < np.maximum(spot_o, spot_c))
            | (spot_l > np.minimum(spot_o, spot_c))
        ).values[a].sum()),
        "candle_range_ratio_mismatch": int((
            alive & ~np.isclose(fr["candle_range_ratio"], expected_range.where(real, 1.0),
                               rtol=1e-9, atol=1e-12, equal_nan=True)
        ).values.sum()),
        "candle_range_flag_mismatch": int((
            fr["candle_range_anomaly_flag"] != expected_range_flag
        ).values.sum()),
        "execution_prices_on_filled_bars": int((execution_any & ~real).values.sum()),
        "execution_complete_on_filled_bars": int(((fr["execution_ohlc_complete"] == 1) & ~real).values.sum()),
        "execution_validity_mismatch": int((
            ((fr["execution_valid"] == 1) & (~real | (fr["execution_ohlc_complete"] != 1)))
            | ((fr["buy_execution_valid"] == 1) & (~real | (fr["execution_ohlc_complete"] != 1)))
            | ((fr["sell_execution_valid"] == 1) & (~real | (fr["execution_ohlc_complete"] != 1)))
        ).values.sum()),
        "invalid_buy_execution_ohlc": side_errors("buy"),
        "invalid_sell_execution_ohlc": side_errors("sell"),
    }
    chk["passed"] = all(v == 0 for v in chk.values())
    return chk


# ----------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="long OHLCV CSV -> clean aligned VectorBT dataset")
    ap.add_argument("input")
    ap.add_argument("-o", "--outdir", default="vbt_dataset")
    ap.add_argument("--bar-seconds", type=float, help="Override the bar interval when it cannot be inferred from the filename")
    ap.add_argument("--min-trades", type=int, default=20)
    ap.add_argument("--min-bars", type=int, default=3)
    ap.add_argument("--min-volume-sol", type=float, default=0.5)
    ap.add_argument("--min-price", type=float, default=1e-11, help="Minimum median price in SOL")
    ap.add_argument("--max-price", type=float, default=0.05, help="Maximum median price in SOL; excludes many non-memecoin assets")
    ap.add_argument("--pegged-ratio", type=float, default=1.02, help="Treat max/min below this ratio as flat or stablecoin-like")
    ap.add_argument("--pegged-min-trades", type=int, default=50)
    ap.add_argument("--max-non-wsol-frac", type=float, default=0.5)
    ap.add_argument("--max-zero-quote-frac", type=float, default=0.2)
    ap.add_argument(
        "--keep-all", action="store_true",
        help="Bypass point-in-time filters; each mint becomes eligible one bar after its first real observation",
    )
    ap.add_argument("--no-tail-fill", action="store_true", help="Leave values as NaN after the last observed trade")
    ap.add_argument("--keep-first-trade-open", action="store_true", help="Use the first observed trade price as the candle open")
    ap.add_argument(
        "--max-stale-bars", type=int, default=0,
        help="Maximum age of a carried spot mark that remains valid; default 0 means real candles only",
    )
    ap.add_argument(
        "--candle-range-ratio", type=float, default=10.0,
        help="Flag spot candles where high/low reaches this ratio; audit only (default: 10)",
    )
    ap.add_argument(
        "--universe-delay-bars", type=int, default=1,
        help="Number of completed bars required before a newly qualified mint becomes eligible; must be >= 1",
    )
    ap.add_argument(
        "--require-execution-ohlc", action="store_true",
        help="Fail unless eligible bars contain both observed BUY and SELL execution OHLC",
    )
    a = ap.parse_args()
    if a.max_stale_bars < 0:
        ap.error("--max-stale-bars must be >= 0")
    if a.universe_delay_bars < 1:
        ap.error("--universe-delay-bars must be >= 1 to prevent same-bar look-ahead")
    if not np.isfinite(a.candle_range_ratio) or a.candle_range_ratio <= 1:
        ap.error("--candle-range-ratio must be a finite number greater than 1")

    bar_ms = infer_bar_ms(a.input, a.bar_seconds)
    df = load(a.input)
    source_has_execution = bool(df.attrs.get("has_directional_execution_ohlc", False))
    if a.require_execution_ohlc and not source_has_execution:
        sys.exit("Input is missing directional execution OHLC; rerun raw_to_clean_ohlcv v6 first.")
    real_all = df[df["is_filled"] == 0].copy()
    if real_all.empty:
        sys.exit("The input contains no real candles (is_filled=0).")

    meta, window_end_rule_counts = mint_table(real_all, a)
    t0, t1 = int(real_all["timestamp_ms"].min()), int(real_all["timestamp_ms"].max())
    grid = np.arange(t0, t1 + bar_ms, bar_ms, dtype="int64")

    # Build the point-in-time mask from every mint. Full-window summary metrics never filter history.
    universe = build_point_in_time_universe(
        real_all, grid, a, delay_bars=a.universe_delay_bars
    )
    meta = attach_point_in_time_summary(meta, universe, grid)
    real = real_all
    fr, alive = build_wide(
        real, grid, meta["last_ts"], not a.no_tail_fill, a.keep_first_trade_open,
        max_stale_bars=a.max_stale_bars,
        candle_range_ratio=a.candle_range_ratio,
        universe_eligible=universe["eligible"],
    )

    eligible_mask = fr["universe_eligible"].astype(bool)
    eligible_buy = (fr["buy_execution_valid"] == 1) & eligible_mask
    eligible_sell = (fr["sell_execution_valid"] == 1) & eligible_mask
    if a.require_execution_ohlc and (int(eligible_buy.values.sum()) == 0 or int(eligible_sell.values.sum()) == 0):
        sys.exit(
            "No eligible BUY or SELL execution bars are available; inspect universe_eligible, "
            "buy_execution_valid, and sell_execution_valid."
        )
    chk = validate(
        fr, alive,
        max_stale_bars=a.max_stale_bars,
        candle_range_ratio=a.candle_range_ratio,
    )

    os.makedirs(os.path.join(a.outdir, "wide"), exist_ok=True)
    idx = pd.Index(pd.to_datetime(grid, unit="ms").strftime("%Y-%m-%d %H:%M:%S"), name="datetime_utc")
    for name, frame in fr.items():
        w = frame.copy()
        w.index = idx
        w.to_csv(os.path.join(a.outdir, "wide", f"{name}.csv"), float_format="%.12g")

    cols = fr["close"].columns
    mi, ti = np.nonzero(alive.values.T)  # Preserve mint-major row ordering.
    long = pd.DataFrame({"mint": cols.values[mi], "datetime_utc": idx.values[ti], "timestamp_ms": grid[ti]})
    for name, frame in fr.items():
        long[name] = frame.values[ti, mi]
    tf_tag = re.search(r"(\d+[smh])", os.path.basename(a.input).lower())
    long.to_csv(os.path.join(a.outdir, f"ohlcv_clean_{tf_tag.group(1) if tf_tag else 'tf'}.csv"),
                index=False, float_format="%.12g")

    meta.reset_index().to_csv(os.path.join(a.outdir, "universe.csv"), index=False, float_format="%.10g")
    universe_timeline, universe_transitions = universe_audit_tables(
        universe, alive, grid, a.universe_delay_bars, bar_ms
    )
    universe_timeline.to_csv(
        os.path.join(a.outdir, "universe_timeline.csv"), index=False, float_format="%.12g"
    )
    universe_transitions.to_csv(
        os.path.join(a.outdir, "universe_transitions.csv"), index=False, float_format="%.12g"
    )

    report = {
        "builder_version": DATASET_BUILDER_VERSION,
        "input": a.input, "bar_ms": bar_ms, "grid_bars": int(len(grid)),
        "time_range_utc": [idx[0], idx[-1]],
        "config": {k: v for k, v in vars(a).items() if k not in ("input", "outdir")},
        "universe_policy": {
            "mode": "point_in_time",
            "static_full_window_filter_used_for_historical_selection": False,
            "delay_bars": int(a.universe_delay_bars),
            "eligibility_rule": "cumulative metrics are evaluated using completed bars only; the resulting eligibility is shifted forward by the configured number of full bars",
            "first_observation": "a mint is never eligible in its first observed bar; with the default one-bar delay, eligibility can begin no earlier than the following bar",
            "backtest_requirement": "strategies must apply wide/universe_eligible.csv (or the ohlcv_clean timeframe column) at every decision and entry timestamp",
            "universe_csv_scope": "window-wide summary fields are descriptive only and are not used to filter historical rows",
            "mint_columns": "all mints found in the input are retained; future mints have no observations and remain ineligible before their first trade and qualification",
            "timeline_files": ["universe_timeline.csv", "universe_transitions.csv"],
        },
        "price_model": {
            "spot_ohlc": "pool spot-price OHLC; legacy open/high/low/close frames remain for compatibility",
            "execution_ohlc": "directional observed BUY and SELL component-price OHLC; only complete real bars are exposed in evaluation frames",
            "execution_limitation": "historical observed execution prices are not hypothetical order fills; model order size, pool depth, fees and slippage before interpreting results as executable PnL",
            "filled_prices": "carried spot values remain visible as marks; spot_mark_valid enforces --max-stale-bars; execution OHLC is never filled",
            "candle_range_anomaly": f"spot high/low >= {a.candle_range_ratio}; audit flag only, no automatic drop",
        },
        "source_has_directional_execution_ohlc": source_has_execution,
        "spot_mark_valid_rows": int(fr["spot_mark_valid"].values.sum()),
        "stale_spot_rows_invalidated": int((alive & (fr["spot_mark_valid"] == 0)).values.sum()),
        "execution_valid_rows": int(fr["execution_valid"].values.sum()),
        "buy_execution_valid_rows": int(fr["buy_execution_valid"].values.sum()),
        "sell_execution_valid_rows": int(fr["sell_execution_valid"].values.sum()),
        "candle_range_anomaly_rows": int(fr["candle_range_anomaly_flag"].values.sum()),
        "max_stale_bars": int(a.max_stale_bars),
        "universe_delay_bars": int(a.universe_delay_bars),
        "mints_total": int(len(meta)),
        "mints_included": int(meta["ever_eligible"].sum()),
        "mints_ever_eligible": int(meta["ever_eligible"].sum()),
        "mints_eligible_at_window_end": int(meta["eligible_at_window_end"].sum()),
        "universe_eligible_rows": int(fr["universe_eligible"].values.sum()),
        "universe_entry_transitions": int((universe_transitions["transition"] == "entered").sum()),
        "universe_exit_transitions": int((universe_transitions["transition"] == "exited").sum()),
        "eligible_buy_execution_rows": int(eligible_buy.values.sum()),
        "eligible_sell_execution_rows": int(eligible_sell.values.sum()),
        "window_end_rule_hits_non_exclusive": window_end_rule_counts,
        "rows_long": int(len(long)), "real_bars_included": int((fr["real_bar"] == 1).values.sum()),
        "filled_bars_included": int(((fr["real_bar"] == 0) & alive).values.sum()),
        "validation": chk,
    }
    with open(os.path.join(a.outdir, "dataset_report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(json.dumps({k: report[k] for k in ("mints_total", "mints_included", "mints_eligible_at_window_end",
                                              "universe_eligible_rows", "eligible_buy_execution_rows",
                                              "eligible_sell_execution_rows", "real_bars_included",
                                              "filled_bars_included", "validation")},
                     ensure_ascii=False, indent=1), file=sys.stderr)
    if not chk["passed"]:
        sys.exit("Dataset validation failed; inspect dataset_report.json.")


if __name__ == "__main__":
    main()
