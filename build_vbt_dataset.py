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

DATASET_BUILDER_VERSION = "v7.2.0"
WSOL_MINT = "So11111111111111111111111111111111111111112"
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

    synthetic_zero = set()
    for c in OPTIONAL_NUM:
        if c in df.columns:
            continue
        if c.startswith("spot_"):
            df[c] = df[c.replace("spot_", "")]
        elif c in ("vwap_sol", "volume_token", "volume_sol", "execution_coverage", "candle_range_ratio") or c in EXECUTION_PRICE_COLUMNS:
            df[c] = np.nan
        else:
            df[c] = 0
            synthetic_zero.add(c)

    if "pool_id" not in df.columns:
        df["pool_id"] = ""

    if "pool_changed" in synthetic_zero:
        # A missing source flag must not be reported as "no pool changes".
        # Rebuild it per mint from the selected pool identity so the pool
        # timeline and its validation agree with a legacy input.
        key_column = "pool_key" if "pool_key" in original_columns else "pool_id"
        ordered = df.sort_values(["mint", "timestamp_ms"], kind="stable")
        keys = ordered[key_column].fillna("").astype(str).str.strip()
        previous = keys.groupby(ordered["mint"].astype(str), sort=False).shift(1)
        changed = (previous.notna() & keys.ne(previous)).astype("int8")
        df["pool_changed"] = changed.reindex(df.index).astype("int8")

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


# ----------------------------------------------------------------------------- pool selection timeline
def _first_available_text(frame, candidates, default=""):
    """Return the first existing metadata column as normalized text."""
    out = pd.Series(default, index=frame.index, dtype="object")
    for name in candidates:
        if name not in frame.columns:
            continue
        values = frame[name].fillna("").astype(str).str.strip()
        out = out.where(out.ne(""), values)
    return out.replace({"nan": default, "None": default, "<NA>": default})


def _first_available_numeric(frame, candidates):
    """Return the first existing numeric column, falling back to NaN."""
    out = pd.Series(np.nan, index=frame.index, dtype="float64")
    for name in candidates:
        if name not in frame.columns:
            continue
        values = pd.to_numeric(frame[name], errors="coerce")
        out = out.where(out.notna(), values)
    return out


def _derive_reserve_model(pool_type, virtual_tokens, virtual_quote):
    """Map a selected pool to the reserve model the execution simulator uses.

    Pump bonding-curve events carry effective virtual reserves directly. Pump AMM
    pools use exactly one virtual reserve style, so the two styles are mutually
    exclusive and can be identified from the per-bar reserve snapshot.

    Returns ``(reserve_model, unambiguous)`` where ``unambiguous`` is False when
    both virtual components are non-zero and the price identity cannot be chosen.
    """
    model = pd.Series("", index=pool_type.index, dtype="object")
    unambiguous = pd.Series(True, index=pool_type.index, dtype="bool")

    pump = pool_type.eq("pump")
    amm = pool_type.eq("pump-amm")

    vt_active = virtual_tokens.notna() & virtual_tokens.ne(0.0)
    vq_present = virtual_quote.notna()
    vq_active = vq_present & virtual_quote.ne(0.0)

    ambiguous = amm & vt_active & vq_active
    model = model.mask(ambiguous, "ambiguous")
    unambiguous = unambiguous & ~ambiguous

    model = model.mask(amm & ~ambiguous & vt_active, "amm_virtual_tokens")
    model = model.mask(amm & ~ambiguous & ~vt_active & vq_present, "amm_virtual_quote")
    model = model.mask(amm & ~ambiguous & ~vt_active & ~vq_present, "unknown")
    model = model.mask(pump, "bonding_curve")
    return model, unambiguous


def build_pool_selection_timeline(source, bar_ms):
    """Expose the selected pool for each real candle only after that candle closes.

    Input timestamps identify candle starts. The pool decision is based on all
    events in that completed bucket, so the returned selection is not available
    until timestamp_ms + bar_ms. Filled rows are excluded by construction.
    """
    if bar_ms <= 0:
        raise ValueError("bar_ms must be > 0")
    required = {"mint", "timestamp_ms"}
    missing = sorted(required - set(source.columns))
    if missing:
        raise ValueError(f"Pool timeline source is missing columns: {missing}")

    data = source.copy()
    if "is_filled" in data.columns:
        filled = pd.to_numeric(data["is_filled"], errors="coerce").fillna(1).ne(0)
        data = data.loc[~filled].copy()
    if data.empty:
        return pd.DataFrame(columns=[
            "mint", "timestamp_ms", "bar_start_timestamp_ms", "bar_end_timestamp_ms",
            "available_at_timestamp_ms", "bar_start_utc", "bar_end_utc", "available_at_utc",
            "pool_id", "pool_key", "pool", "quote_mint", "pool_changed",
            "pool_id_valid", "quote_mint_conflict_flag", "pool_type_conflict_flag",
            "pool_selection_status", "reserve_model", "execution_supported",
            "pool_count", "pool_excluded_trades",
            "selected_pool_trade_share", "selected_pool_volume_share",
            "pool_switch_discontinuity_flag",
        ])

    data["timestamp_ms"] = pd.to_numeric(data["timestamp_ms"], errors="coerce")
    data = data.loc[data["timestamp_ms"].notna()].copy()
    data["timestamp_ms"] = data["timestamp_ms"].astype("int64")
    data = data.sort_values(["mint", "timestamp_ms"], kind="stable").reset_index(drop=True)
    if data.duplicated(["mint", "timestamp_ms"]).any():
        raise ValueError("Pool timeline source contains duplicate mint/timestamp rows")

    def norm_text(name):
        if name not in data.columns:
            return pd.Series("", index=data.index, dtype="object")
        return data[name].fillna("").astype(str).str.strip().replace({"nan": "", "None": "", "<NA>": ""})

    pool_id = norm_text("pool_id")
    pool_key = norm_text("pool_key")
    pool_type = _first_available_text(data, ("pool", "pool_type", "close_pool", "open_pool"))
    quote_mint = _first_available_text(data, ("quote_mint", "close_quote_mint", "open_quote_mint"))
    open_quote = _first_available_text(data, ("open_quote_mint",))
    close_quote = _first_available_text(data, ("close_quote_mint",))
    open_pool = _first_available_text(data, ("open_pool",))
    close_pool = _first_available_text(data, ("close_pool",))

    # pool_key is the identity used by raw_to_clean_ohlcv to separate pools
    # when pool_id is absent. Keep it distinct from the executable pool_id.
    fallback_key = pool_id.where(pool_id.ne(""), "__missing_pool_id__:" + pool_type.where(pool_type.ne(""), "unknown"))
    pool_key = pool_key.where(pool_key.ne(""), fallback_key)

    grouped_key = pool_key.groupby(data["mint"], sort=False)
    previous_key = grouped_key.shift(1)
    computed_changed = (previous_key.notna() & pool_key.ne(previous_key)).astype("int8")
    if "pool_changed" in data.columns:
        source_changed = pd.to_numeric(data["pool_changed"], errors="coerce")
        pool_changed = source_changed.where(source_changed.notna(), computed_changed).fillna(0).astype("int8")
    else:
        pool_changed = computed_changed

    quote_conflict = (open_quote.ne("") & close_quote.ne("") & open_quote.ne(close_quote)).astype("int8")
    pool_conflict = (open_pool.ne("") & close_pool.ne("") & open_pool.ne(close_pool)).astype("int8")
    pool_id_valid = pool_id.ne("").astype("int8")

    status = np.select(
        [
            pool_id_valid.eq(0), quote_conflict.eq(1), pool_conflict.eq(1),
            quote_mint.eq(""), pool_type.eq(""),
        ],
        [
            "missing_pool_id", "quote_mint_conflict", "pool_type_conflict",
            "missing_quote_mint", "missing_pool_type",
        ],
        default="ready",
    )

    virtual_tokens = _first_available_numeric(
        data, ("close_virtual_tokens_in_pool", "open_virtual_tokens_in_pool", "virtual_tokens_in_pool")
    )
    virtual_quote = _first_available_numeric(
        data, ("close_virtual_quote_in_pool", "open_virtual_quote_in_pool", "virtual_quote_in_pool")
    )
    reserve_model, unambiguous = _derive_reserve_model(pool_type, virtual_tokens, virtual_quote)

    tokens_in_pool = _first_available_numeric(
        data, ("close_tokens_in_pool", "open_tokens_in_pool", "tokens_in_pool")
    )
    quote_in_pool = _first_available_numeric(
        data, ("close_quote_in_pool", "open_quote_in_pool", "quote_in_pool")
    )
    effective_quote = quote_in_pool.where(reserve_model.ne("amm_virtual_quote"), quote_in_pool + virtual_quote.fillna(0.0))
    amm_reserves_ok = (
        tokens_in_pool.gt(0)
        & effective_quote.gt(0)
    )
    execution_supported = (
        pool_id_valid.eq(1)
        & quote_mint.eq(WSOL_MINT)
        & unambiguous
        & (
            (reserve_model.eq("bonding_curve"))
            | (reserve_model.isin(["amm_virtual_tokens", "amm_virtual_quote"]) & amm_reserves_ok)
        )
    ).astype("int8")

    start = data["timestamp_ms"].to_numpy(dtype="int64")
    end = start + int(bar_ms)
    timeline = pd.DataFrame({
        "mint": data["mint"].astype(str).to_numpy(),
        "timestamp_ms": start,
        "bar_start_timestamp_ms": start,
        "bar_end_timestamp_ms": end,
        "available_at_timestamp_ms": end,
        "bar_start_utc": pd.to_datetime(start, unit="ms", utc=True).strftime("%Y-%m-%d %H:%M:%S"),
        "bar_end_utc": pd.to_datetime(end, unit="ms", utc=True).strftime("%Y-%m-%d %H:%M:%S"),
        "available_at_utc": pd.to_datetime(end, unit="ms", utc=True).strftime("%Y-%m-%d %H:%M:%S"),
        "pool_id": pool_id.to_numpy(),
        "pool_key": pool_key.to_numpy(),
        "pool": pool_type.to_numpy(),
        "quote_mint": quote_mint.to_numpy(),
        "pool_changed": pool_changed.to_numpy(dtype="int8"),
        "pool_id_valid": pool_id_valid.to_numpy(dtype="int8"),
        "quote_mint_conflict_flag": quote_conflict.to_numpy(dtype="int8"),
        "pool_type_conflict_flag": pool_conflict.to_numpy(dtype="int8"),
        "pool_selection_status": status,
        "reserve_model": reserve_model.to_numpy(),
        "execution_supported": execution_supported.to_numpy(dtype="int8"),
    })

    for column in (
        "pool_count", "pool_excluded_trades", "selected_pool_trade_share",
        "selected_pool_volume_share", "pool_switch_discontinuity_flag",
    ):
        if column in data.columns:
            timeline[column] = pd.to_numeric(data[column], errors="coerce").to_numpy()
        else:
            timeline[column] = np.nan

    return timeline.sort_values(["mint", "timestamp_ms"], kind="stable").reset_index(drop=True)


def validate_pool_selection_timeline(timeline, source, bar_ms):
    """Validate uniqueness, timing, and metadata consistency of the pool timeline."""
    source_real = source.copy()
    if "is_filled" in source_real.columns:
        source_real = source_real.loc[
            pd.to_numeric(source_real["is_filled"], errors="coerce").fillna(1).eq(0)
        ]
    if "timestamp_ms" in source_real.columns:
        source_real = source_real.loc[pd.to_numeric(source_real["timestamp_ms"], errors="coerce").notna()]

    duplicate_keys = int(timeline.duplicated(["mint", "timestamp_ms"]).sum()) if len(timeline) else 0
    expected_keys = set(zip(source_real["mint"].astype(str), source_real["timestamp_ms"].astype("int64")))
    actual_keys = (
        set(zip(timeline["mint"].astype(str), timeline["timestamp_ms"].astype("int64")))
        if len(timeline)
        else set()
    )
    # Symmetric difference keeps an added candle and a dropped candle from
    # cancelling out, which an absolute row-count delta would hide.
    row_count_mismatch = len(expected_keys.symmetric_difference(actual_keys))

    filled_rows = 0
    if "is_filled" in source.columns and len(timeline):
        filled_source = source.loc[
            pd.to_numeric(source["is_filled"], errors="coerce").fillna(1).ne(0)
        ]
        filled_keys = set(
            zip(filled_source["mint"].astype(str), filled_source["timestamp_ms"].astype("int64"))
        )
        filled_rows = len(filled_keys.intersection(actual_keys))
    bar_end_expected = pd.to_numeric(timeline.get("timestamp_ms", pd.Series(dtype=float)), errors="coerce") + bar_ms
    bar_end_observed = pd.to_numeric(timeline.get("bar_end_timestamp_ms", pd.Series(dtype=float)), errors="coerce")
    available_observed = pd.to_numeric(timeline.get("available_at_timestamp_ms", pd.Series(dtype=float)), errors="coerce")
    timestamp_alignment_errors = int((pd.to_numeric(timeline.get("timestamp_ms", pd.Series(dtype=float)), errors="coerce") % bar_ms != 0).sum()) if len(timeline) else 0
    bar_end_mismatch = int((~np.isclose(bar_end_expected, bar_end_observed, rtol=0, atol=0, equal_nan=False)).sum()) if len(timeline) else 0
    available_before_bar_end = int((available_observed < bar_end_observed).sum()) if len(timeline) else 0
    source_changed_mismatch = 0
    if "pool_changed" in source_real.columns and len(timeline):
        ordered_source = source_real.sort_values(["mint", "timestamp_ms"], kind="stable").copy()
        source_pool_id = _first_available_text(ordered_source, ("pool_id",))
        source_pool_key = _first_available_text(ordered_source, ("pool_key",))
        source_pool_type = _first_available_text(ordered_source, ("pool", "pool_type"))
        fallback_key = source_pool_id.where(
            source_pool_id.ne(""),
            "__missing_pool_id__:" + source_pool_type.where(source_pool_type.ne(""), "unknown"),
        )
        source_pool_key = source_pool_key.where(source_pool_key.ne(""), fallback_key)
        prior_key = source_pool_key.groupby(ordered_source["mint"], sort=False).shift(1)
        expected_changed = (prior_key.notna() & source_pool_key.ne(prior_key)).astype("int8").reset_index(drop=True)
        source_changed = pd.to_numeric(ordered_source["pool_changed"], errors="coerce").reset_index(drop=True)
        comparable = source_changed.notna()
        source_changed_mismatch = int(
            (source_changed[comparable].astype("int64") != expected_changed[comparable]).sum()
        )
    quote_conflict_rows = int(pd.to_numeric(timeline.get("quote_mint_conflict_flag", 0), errors="coerce").fillna(0).sum()) if len(timeline) else 0
    pool_type_conflict_rows = int(pd.to_numeric(timeline.get("pool_type_conflict_flag", 0), errors="coerce").fillna(0).sum()) if len(timeline) else 0
    missing_pool_id_rows = int(pd.to_numeric(timeline.get("pool_id_valid", 0), errors="coerce").fillna(0).eq(0).sum()) if len(timeline) else 0
    missing_quote_mint_rows = int(timeline.get("quote_mint", pd.Series(dtype=str)).fillna("").astype(str).str.strip().eq("").sum()) if len(timeline) else 0
    missing_pool_type_rows = int(timeline.get("pool", pd.Series(dtype=str)).fillna("").astype(str).str.strip().eq("").sum()) if len(timeline) else 0

    checks = {
        "duplicate_mint_timestamp": duplicate_keys,
        "row_count_mismatch": row_count_mismatch,
        "timestamp_alignment_errors": timestamp_alignment_errors,
        "bar_end_mismatch": bar_end_mismatch,
        "available_before_bar_end": available_before_bar_end,
        "pool_changed_source_mismatch": source_changed_mismatch,
        "quote_mint_conflict_rows": quote_conflict_rows,
        "pool_type_conflict_rows": pool_type_conflict_rows,
        "filled_rows_included": filled_rows,
        "missing_pool_id_rows": missing_pool_id_rows,
        "missing_quote_mint_rows": missing_quote_mint_rows,
        "missing_pool_type_rows": missing_pool_type_rows,
    }
    # Selected pool metadata is required for execution routing: the simulator
    # needs a pool identifier, a protocol (pump vs pump-amm), and a quote mint.
    structural = (
        "duplicate_mint_timestamp", "row_count_mismatch", "timestamp_alignment_errors",
        "bar_end_mismatch", "available_before_bar_end", "pool_changed_source_mismatch",
        "quote_mint_conflict_rows", "pool_type_conflict_rows", "filled_rows_included",
        "missing_pool_id_rows", "missing_quote_mint_rows", "missing_pool_type_rows",
    )
    checks["passed"] = all(checks[name] == 0 for name in structural)
    return checks


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
    o, h, lo, c = fr["open"], fr["high"], fr["low"], fr["close"]
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
        "nan_ohlc_in_alive": int(np.isnan(np.stack([o.values, h.values, lo.values, c.values]))[:, a].sum()),
        "non_positive_price": int(((np.stack([o.values, h.values, lo.values, c.values]) <= 0) & a).sum()),
        "high_lt_open_close": int(((h.values < np.maximum(o.values, c.values)) & a).sum()),
        "low_gt_open_close": int(((lo.values > np.minimum(o.values, c.values)) & a).sum()),
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

    # Pool selection is bucket-close information. Keep it in a separate timeline
    # so it cannot be mistaken for metadata available at the candle open.
    pool_timeline = build_pool_selection_timeline(real_all, bar_ms)
    pool_timeline.to_csv(
        os.path.join(a.outdir, "pool_selection_timeline.csv"),
        index=False, float_format="%.12g",
    )
    pool_timeline_validation = validate_pool_selection_timeline(pool_timeline, real_all, bar_ms)

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
        "pool_selection_timeline": {
            "file": "pool_selection_timeline.csv",
            "rows": int(len(pool_timeline)),
            "mints": int(pool_timeline["mint"].nunique()) if len(pool_timeline) else 0,
            "pool_ids_missing_rows": int(pool_timeline_validation["missing_pool_id_rows"]),
            "quote_mint_missing_rows": int(pool_timeline_validation["missing_quote_mint_rows"]),
            "pool_type_missing_rows": int(pool_timeline_validation["missing_pool_type_rows"]),
            "distinct_pool_ids": int(pool_timeline.loc[pool_timeline["pool_id"].astype(str).ne(""), "pool_id"].nunique()) if len(pool_timeline) else 0,
            "pool_switch_rows": int(pd.to_numeric(pool_timeline.get("pool_changed", 0), errors="coerce").fillna(0).sum()) if len(pool_timeline) else 0,
            "execution_supported_rows": int(pd.to_numeric(pool_timeline.get("execution_supported", 0), errors="coerce").fillna(0).sum()) if len(pool_timeline) else 0,
            "reserve_model_counts": (
                {str(k): int(v) for k, v in pool_timeline.get("reserve_model", pd.Series(dtype=str)).fillna("").astype(str).value_counts().items()}
                if len(pool_timeline) else {}
            ),
            "execution_support_policy": "execution_supported means the selected pool uses a WSOL quote and a reserve model the execution simulator implements (pump bonding curve, or pump-amm with one unambiguous virtual reserve style)",
            "availability_policy": "pool selection uses completed events within the candle bucket; available_at_timestamp_ms equals bar_start_timestamp_ms + bar_ms; never use this selection before available_at_timestamp_ms",
            "filled_rows_policy": "filled bars are excluded; carried pool identifiers are not treated as fresh pool selections",
            "validation": pool_timeline_validation,
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
    summary = {k: report[k] for k in (
        "mints_total", "mints_included", "mints_eligible_at_window_end",
        "universe_eligible_rows", "eligible_buy_execution_rows",
        "eligible_sell_execution_rows", "real_bars_included",
        "filled_bars_included", "validation",
    )}
    summary["pool_selection_validation"] = pool_timeline_validation
    print(json.dumps(summary, ensure_ascii=False, indent=1), file=sys.stderr)
    if not chk["passed"]:
        sys.exit("Dataset validation failed; inspect dataset_report.json.")
    if not pool_timeline_validation["passed"]:
        sys.exit("Pool selection timeline validation failed; inspect pool_selection_timeline.csv and dataset_report.json.")


if __name__ == "__main__":
    main()
