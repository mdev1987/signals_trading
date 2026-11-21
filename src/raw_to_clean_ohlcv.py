#!/usr/bin/env python3
"""Convert PumpAPI Zstandard JSONL replay data into auditable OHLCV CSV files.

Install:
    pip install zstandard pandas numpy
    pip install orjson  # Optional faster JSON parsing

Examples:
    python raw_to_clean_ohlcv.py 05.jsonl.zst -o ohlcv_clean -t 1s,5s,15s,1m --fill
    python raw_to_clean_ohlcv.py "replay/*.jsonl.zst" -t 5s --min-trades 30 --open-prev-close

Outputs:
    <outdir>/ohlcv_<tf>.csv              Long-format OHLCV data
    <outdir>/cleaning_report.json        Cleaning and validation statistics
    <outdir>/dropped_invalid.csv         Events excluded from price construction
    <outdir>/flagged_zero_quote.csv      Events with quoteAmount equal to zero
    <outdir>/flagged_invalid_quote.csv   Events excluded from quote-volume metrics
    <outdir>/flagged_price_discontinuity_<tf>.csv  Large gaps between observed candles
    <outdir>/flagged_price_consistency_<tf>.csv    Pool spot vs reconstructed execution-price discrepancies
    <outdir>/flagged_execution_audit_<tf>.csv      Breakdown, netting, reserve, and balance diagnostics
    <outdir>/flagged_pool_depletion_<tf>.csv        Low-reserve price-impact candidates
    <outdir>/flagged_breakdown_net_mismatch_<tf>.csv Top-level net vs component reconciliation mismatches

Data rules:
  * priceSol is treated as the pool spot price after the event.
  * Each mint/time bucket uses one selected pool; pool prices are never mixed.
  * Pool selection uses valid SOL quote volume within the current bucket only.
  * Quote volume and VWAP exclude missing, non-positive, or unconvertible quote values.
  * VWAP and volume use gross component-level amounts for complete breakdowns.
  * Net event amounts are never used as execution prices for multi-breakdown events.
  * Pool spot vs execution price is classified separately from reserve-depletion candidates.
  * Invalid quote events remain visible in event counts and audit output.
  * Pool changes, price jumps, route candidates, and bundle candidates are flagged,
    not silently removed, unless --drop-jumps is explicitly supplied.
  * Cross-candle price discontinuities are audit flags; they never rewrite OHLC data.
  * priceSol is compared with quote-implied execution price as an audit metric only;
    a discrepancy is not, by itself, proof of invalid source data.

Memory: a busy day can hold several million events and every column costs
memory, so large inputs are split by mint (``--batch-events``, default 1.5M
events) and merged afterwards. Mints are independent apart from the signature
multiplicity, which one streaming pass resolves globally, so the output stays
identical to a single-pass run while peak memory stays bounded.
"""

import argparse
import gc
import glob
import io
import json
import os
import sys

import numpy as np
import pandas as pd
try:
    import orjson

    def loads(b):
        return orjson.loads(b)
except ImportError:
    def loads(b):
        return json.loads(b)

WSOL = "So11111111111111111111111111111111111111112"
POOL_SHARE_ROUNDOFF_TOLERANCE = 1e-12

ROW_COLS = [
    "event_index", "signature", "action", "mint", "pool_id", "pool", "quote_mint",
    "timestamp_ms", "token_amount", "quote_amount", "price_sol", "quote_price_sol",
    "n_breakdown", "block", "breakdown_json", "tokens_in_pool", "quote_in_pool",
    "virtual_tokens_in_pool", "virtual_quote_in_pool", "burned_liquidity", "pool_created_by", "decimals", "supply",
    "participant_quote_balance_delta_sol", "participant_balance_available",
    "participant_balance_checked",
]

COUNT_COLS = [
    "trades", "buys", "sells", "all_pool_trades", "pool_count", "pool_excluded_trades",
    "selected_pool_missing_id_trades", "valid_quote_trades", "invalid_quote_trades", "vwap_trades",
    "vwap_components", "valid_quote_components", "invalid_quote_components",
    "breakdown_buy_count", "breakdown_sell_count", "breakdown_netting_flag",
    "breakdown_net_reconciles_event", "breakdown_net_direction_matches_event",
    "breakdown_net_mismatch_flag", "breakdown_net_zero_flag",
    "pool_depletion_flag", "price_impact_candidate_flag", "data_anomaly_candidate_flag",
    "price_gap_unclassified_flag", "pool_volume_share_roundoff_clipped",
    "pool_changed", "jump_flag", "bundle_cand_trades", "routed_trades",
    "non_wsol_trades", "zero_quote_trades", "price_discontinuity_flag",
    "pool_switch_discontinuity_flag", "price_comparison_trades",
    "price_consistency_flagged_trades", "open_price_consistency_flag",
    "close_price_consistency_flag", "participant_balance_checked",
    "participant_balance_available", "execution_valid_events",
    "execution_invalid_events", "execution_component_count",
    "buy_execution_components", "sell_execution_components",
    "execution_ohlc_complete",
]
VOL_COLS = [
    "volume_token", "volume_sol", "buy_volume_token", "sell_volume_token",
    "buy_volume_sol", "sell_volume_sol", "gross_buy_quote_amount", "gross_sell_quote_amount",
]
RATIO_COLS = [
    "selected_pool_trade_share", "selected_pool_volume_share", "pool_side_reserve_ratio",
    "execution_coverage",
]

# Event details that identify the exact records behind each candle's Open and Close.
EVENT_AUDIT_FIELDS = {
    "event_index": "event_index",
    "signature": "signature",
    "action": "action",
    "block": "block",
    "token_amount": "token_amount",
    "quote_amount": "quote_amount",
    "quote_mint": "quote_mint",
    "quote_price_sol": "quote_price_sol",
    "quote_volume_sol": "vol_sol",
    "quote_valid": "quote_valid",
    "n_breakdown": "n_breakdown",
    "breakdown_source": "breakdown_source",
    "breakdown_json": "breakdown_json",
    "breakdown_valid": "breakdown_valid",
    "breakdown_netting_flag": "breakdown_netting_flag",
    "breakdown_net_reconciles_event": "breakdown_net_reconciles_event",
    "breakdown_net_direction_matches_event": "breakdown_net_direction_matches_event",
    "breakdown_net_mismatch_flag": "breakdown_net_mismatch_flag",
    "top_level_net_implied_price_sol": "top_level_net_implied_price_sol",
    "gross_buy_token_amount": "gross_buy_token_amount",
    "gross_sell_token_amount": "gross_sell_token_amount",
    "gross_buy_quote_sol": "gross_buy_quote_sol",
    "gross_sell_quote_sol": "gross_sell_quote_sol",
    "gross_token_amount": "gross_token_amount",
    "gross_quote_volume_sol": "gross_quote_volume_sol",
    "breakdown_buy_execution_price_sol": "breakdown_buy_execution_price_sol",
    "breakdown_sell_execution_price_sol": "breakdown_sell_execution_price_sol",
    "breakdown_buy_execution_open": "breakdown_buy_execution_open",
    "breakdown_buy_execution_high": "breakdown_buy_execution_high",
    "breakdown_buy_execution_low": "breakdown_buy_execution_low",
    "breakdown_buy_execution_close": "breakdown_buy_execution_close",
    "breakdown_sell_execution_open": "breakdown_sell_execution_open",
    "breakdown_sell_execution_high": "breakdown_sell_execution_high",
    "breakdown_sell_execution_low": "breakdown_sell_execution_low",
    "breakdown_sell_execution_close": "breakdown_sell_execution_close",
    "breakdown_gross_execution_vwap_sol": "breakdown_gross_execution_vwap_sol",
    "breakdown_net_token_amount": "breakdown_net_token_amount",
    "breakdown_net_quote_sol": "breakdown_net_quote_sol",
    "price_sol": "price_sol",
    "implied_price_sol": "implied_price_sol",
    "execution_price_sol": "execution_price_sol",
    "execution_price_source": "execution_price_source",
    "price_consistency_ratio": "price_consistency_ratio",
    "pool_spot_vs_execution_ratio": "pool_spot_vs_execution_ratio",
    "pool_spot_vs_buy_execution_ratio": "pool_spot_vs_buy_execution_ratio",
    "pool_spot_vs_sell_execution_ratio": "pool_spot_vs_sell_execution_ratio",
    "effective_tokens_in_pool": "effective_tokens_in_pool",
    "pool_depletion_flag": "pool_depletion_flag",
    "pool_side_reserve_ratio": "pool_side_reserve_ratio",
    "price_impact_candidate_flag": "price_impact_candidate_flag",
    "data_anomaly_candidate_flag": "data_anomaly_candidate_flag",
    "price_gap_unclassified_flag": "price_gap_unclassified_flag",
    "tokens_in_pool": "tokens_in_pool",
    "quote_in_pool": "quote_in_pool",
    "virtual_tokens_in_pool": "virtual_tokens_in_pool",
    "virtual_quote_in_pool": "virtual_quote_in_pool",
    "burned_liquidity": "burned_liquidity",
    "pool_created_by": "pool_created_by",
    "participant_quote_balance_delta_sol": "participant_quote_balance_delta_sol",
    "participant_balance_available": "participant_balance_available",
    "participant_balance_checked": "participant_balance_checked",
    "participant_quote_balance_delta_residual_sol": "participant_quote_balance_delta_residual_sol",
    "price_consistency_flag": "price_consistency_flag",
}
OPEN_EVENT_COLS = [f"open_{name}" for name in EVENT_AUDIT_FIELDS]
CLOSE_EVENT_COLS = [f"close_{name}" for name in EVENT_AUDIT_FIELDS]
PREV_CLOSE_EVENT_COLS = [f"prev_close_{name}" for name in EVENT_AUDIT_FIELDS]

FINAL_COLS = (
    [
        "mint", "datetime_utc", "timestamp_ms", "open", "high", "low", "close",
        "vwap_sol", "vwap_gross_execution_sol", "vwap_single_breakdown_sol", "vwap_trade_coverage",
        "vwap_sol_volume", "vwap_token_volume", "observed_open", "prev_close",
        "prev_close_ts_ms", "price_gap_elapsed_ms", "price_discontinuity_ratio",
        "spot_open", "spot_high", "spot_low", "spot_close",
        "candle_range_ratio", "candle_range_anomaly_flag", "stale_bars", "age_bars",
        "buy_execution_open", "buy_execution_high", "buy_execution_low", "buy_execution_close",
        "sell_execution_open", "sell_execution_high", "sell_execution_low", "sell_execution_close",
        "buy_execution_components", "sell_execution_components",
        "execution_valid_events", "execution_invalid_events", "execution_component_count",
        "execution_coverage", "execution_ohlc_complete",
    ]
    + OPEN_EVENT_COLS
    + CLOSE_EVENT_COLS
    + PREV_CLOSE_EVENT_COLS
    + VOL_COLS
    + [
        "trades", "buys", "sells", "open_ts_ms", "close_ts_ms", "pool_id", "pool_key",
        "pool", "quote_mint",
        "all_pool_trades", "pool_count", "pool_excluded_trades", "selected_pool_missing_id_trades",
        "selected_pool_trade_share", "selected_pool_volume_share", "pool_volume_share_roundoff_clipped",
        "valid_quote_trades", "invalid_quote_trades", "valid_quote_components",
        "invalid_quote_components", "vwap_trades", "vwap_components",
        "breakdown_buy_count", "breakdown_sell_count", "breakdown_netting_flag",
        "breakdown_net_reconciles_event", "breakdown_net_direction_matches_event",
    "breakdown_net_mismatch_flag", "breakdown_net_zero_flag",
        "pool_changed", "jump_flag", "bundle_cand_trades", "routed_trades",
        "non_wsol_trades", "zero_quote_trades", "price_discontinuity_flag",
        "pool_switch_discontinuity_flag", "pool_side_reserve_ratio", "pool_depletion_flag",
        "price_impact_candidate_flag", "data_anomaly_candidate_flag",
        "price_gap_unclassified_flag", "is_filled",
        "price_comparison_trades", "price_consistency_flagged_trades",
        "participant_balance_checked", "participant_balance_available",
    ]
)



def parse_tf(tf: str) -> int:
    units = {"s": 1000, "m": 60_000, "h": 3_600_000}
    tf = tf.strip().lower()
    if len(tf) < 2 or tf[-1] not in units or not tf[:-1].isdigit():
        sys.exit(f"Invalid timeframe: {tf!r}. Examples: 1s, 5s, 1m.")
    return int(tf[:-1]) * units[tf[-1]]


# ----------------------------------------------------------------------------- read raw
def _finite_float(value):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return np.nan
    return out if np.isfinite(out) else np.nan


def _participant_quote_balance_delta_sol(event):
    """Estimate the net quote-token balance change across listed traders.

    For WSOL events, use the WSOL token balance when it changes; otherwise use
    the native SOL balance as a fallback. This is diagnostic only because fees,
    rent, and other transaction instructions can affect wallet balances.
    """
    quote_mint = str(event.get("quoteMint") or "").strip()
    pre = event.get("preBalances") or {}
    post = event.get("postBalances") or {}
    if not isinstance(pre, dict) or not isinstance(post, dict):
        return np.nan, 0

    traders = set(pre) | set(post)
    total_native = 0.0
    found = False
    for trader in traders:
        pre_row = pre.get(trader) or {}
        post_row = post.get(trader) or {}
        if not isinstance(pre_row, dict) or not isinstance(post_row, dict):
            continue

        if quote_mint and quote_mint in pre_row and quote_mint in post_row:
            delta = _finite_float(post_row.get(quote_mint)) - _finite_float(pre_row.get(quote_mint))
            if np.isfinite(delta) and (abs(delta) > 1e-18 or quote_mint != WSOL):
                total_native += delta
                found = True
                continue

        if quote_mint == WSOL:
            pre_sol = _finite_float(pre_row.get("sol"))
            post_sol = _finite_float(post_row.get("sol"))
            if np.isfinite(pre_sol) and np.isfinite(post_sol):
                total_native += post_sol - pre_sol
                found = True

    if not found:
        return np.nan, 0

    if quote_mint == WSOL:
        return total_native, 1
    quote_price_sol = _finite_float(event.get("quotePriceSol"))
    if not np.isfinite(quote_price_sol) or quote_price_sol <= 0:
        return np.nan, 0
    return total_native * quote_price_sol, 1


BALANCE_AUDIT_TRIGGER_RATIO = 2.0


def _needs_participant_balance_audit(event, breakdown_count):
    """Return whether expensive participant balance maps are worth inspecting.

    Multi-breakdown events need balance reconciliation regardless of their net
    price ratio. Single/fallback events are inspected only when their top-level
    price differs materially from the quote-implied execution price.
    """
    if breakdown_count > 1:
        return True

    token_amount = _finite_float(event.get("tokenAmount"))
    quote_amount = _finite_float(event.get("quoteAmount"))
    spot_price = _finite_float(event.get("priceSol"))
    quote_factor = _quote_conversion_factor(
        event.get("quoteMint"), event.get("quotePriceSol")
    )
    if not (
        np.isfinite(token_amount) and token_amount > 0
        and np.isfinite(quote_amount) and quote_amount > 0
        and np.isfinite(quote_factor) and quote_factor > 0
        and np.isfinite(spot_price) and spot_price > 0
    ):
        return False

    execution_price = quote_amount * quote_factor / token_amount
    if not np.isfinite(execution_price) or execution_price <= 0:
        return False
    ratio = max(spot_price / execution_price, execution_price / spot_price)
    return bool(ratio >= BALANCE_AUDIT_TRIGGER_RATIO)


_EVENT_HEAD_BYTES = 260

# Fields with a handful of distinct values. Interning them keeps the per-row
# Python overhead small on multi-million-event days.
_INTERNED_FIELDS = frozenset(
    {"action", "mint", "pool_id", "pool", "quote_mint"}
)

# ``clean_trades`` coerces these to float64 immediately; allocating them as
# float64 up front avoids one boxed Python float per event.
_FLOAT_FIELDS = frozenset(
    {
        "timestamp_ms", "token_amount", "quote_amount", "price_sol",
        "quote_price_sol", "tokens_in_pool", "quote_in_pool",
        "virtual_tokens_in_pool", "virtual_quote_in_pool", "decimals", "supply",
        "participant_quote_balance_delta_sol", "participant_balance_available",
        "participant_balance_checked",
    }
)


def _intern(value):
    return sys.intern(value) if isinstance(value, str) and value else value


def _iter_jsonl(path):
    """Yield lines from a Zstandard JSONL file without materialising them."""
    try:
        import zstandard as zstd
    except ImportError as exc:
        raise SystemExit("Missing dependency: install zstandard (pip install zstandard).") from exc

    with open(path, "rb") as fh:
        reader = zstd.ZstdDecompressor().stream_reader(fh, read_across_frames=True)
        for line in io.BufferedReader(reader, 1 << 22):
            yield line


def _is_trade_line(line):
    head = line[:_EVENT_HEAD_BYTES]
    return b'"action":"buy"' in head or b'"action":"sell"' in head


def _count_event_lines(path):
    """Count BUY/SELL lines cheaply so the frame can be preallocated.

    This is a streaming decompression pass with no JSON parsing. It lets the
    reader build one object column per field instead of a list of tuples, which
    is what used to push multi-million-event days over the memory limit.
    """
    return sum(1 for line in _iter_jsonl(path) if _is_trade_line(line))


def _parse_event(line, index, stats):
    """Parse one line into a ROW_COLS tuple, or return None to skip it."""
    if not _is_trade_line(line):
        return None
    try:
        e = loads(line)
    except ValueError:
        stats["bad_json"] += 1
        return None
    bd = e.get("breakdown")
    bd = bd if isinstance(bd, list) else []
    # Store detailed JSON only when aggregation matters. A single component
    # is represented exactly by top-level fields in this source format.
    bd_json = json.dumps(bd, separators=(",", ":"), ensure_ascii=False) if len(bd) > 1 else ""
    balance_checked = int(_needs_participant_balance_audit(e, len(bd)))
    if balance_checked:
        balance_delta_sol, balance_available = _participant_quote_balance_delta_sol(e)
    else:
        balance_delta_sol, balance_available = np.nan, 0
    return (
        index, e.get("signature"), _intern(e.get("action")), _intern(e.get("mint")),
        _intern(e.get("poolId")),
        _intern(e.get("pool")), _intern(e.get("quoteMint")), e.get("timestamp"), e.get("tokenAmount"),
        e.get("quoteAmount"), e.get("priceSol"), e.get("quotePriceSol"),
        len(bd), e.get("block"), bd_json, e.get("tokensInPool"), e.get("quoteInPool"),
        e.get("virtualTokensInPool"), e.get("virtualQuoteInPool"),
        e.get("burnedLiquidity"), e.get("poolCreatedBy"),
        e.get("decimals"), e.get("supply"), balance_delta_sol,
        balance_available, balance_checked,
    )


def read_file(path, start_idx, stats):
    """Read BUY/SELL JSONL events, retaining multi-breakdown detail and reserves."""
    rows, idx = [], start_idx
    for line in _iter_jsonl(path):
        stats["lines_scanned"] += 1
        row = _parse_event(line, idx + 1, stats)
        if row is None:
            continue
        idx += 1
        rows.append(row)
    return rows, idx


def _allocate_event_columns(capacity):
    columns = {name: np.empty(capacity, dtype=object) for name in ROW_COLS}
    for name in _FLOAT_FIELDS:
        columns[name] = np.empty(capacity, dtype="float64")
    # Never-null integer fields can stay numeric, matching pandas inference.
    columns["event_index"] = np.empty(capacity, dtype="int64")
    columns["n_breakdown"] = np.empty(capacity, dtype="int64")
    # ``block`` is an integer identifier, but the source may omit it.
    block = np.empty(capacity, dtype="int64")
    block_missing = np.zeros(capacity, dtype=bool)
    return columns, block, block_missing


def _store_event_row(columns, block, block_missing, position, row):
    for name, value in zip(ROW_COLS, row):
        if name in _FLOAT_FIELDS:
            if value is None:
                columns[name][position] = np.nan
            else:
                try:
                    columns[name][position] = value
                except (TypeError, ValueError):
                    # Mirrors pd.to_numeric(..., errors="coerce").
                    columns[name][position] = np.nan
        elif name == "block":
            if value is None:
                block_missing[position] = True
            else:
                try:
                    block[position] = int(value)
                except (TypeError, ValueError):
                    block_missing[position] = True
        else:
            columns[name][position] = value


def _finalise_event_frame(columns, block, block_missing, filled, capacity):
    if filled < capacity:
        columns = {name: column[:filled] for name, column in columns.items()}
        block = block[:filled]
        block_missing = block_missing[:filled]
    if block_missing.any():
        columns["block"] = np.where(block_missing, np.nan, block.astype("float64"))
    else:
        columns["block"] = block
    frame = pd.DataFrame(columns, columns=ROW_COLS)
    del columns
    return frame


def read_frame(path, start_idx, stats):
    """Stream BUY/SELL events into a column-oriented frame.

    Same rows and dtypes as ``pd.DataFrame(read_file(...))`` but without the
    intermediate list of Python tuples, which dominates peak memory on busy
    days (5M+ events).

    Numeric fields are allocated with the dtype ``clean_trades`` converts them
    to anyway, so a busy day does not hold one boxed Python float per value and
    does not pay for an object-to-float conversion of every column.
    """
    capacity = _count_event_lines(path)
    columns, block, block_missing = _allocate_event_columns(capacity)

    filled = 0
    for line in _iter_jsonl(path):
        stats["lines_scanned"] += 1
        row = _parse_event(line, start_idx + filled + 1, stats)
        if row is None:
            continue
        _store_event_row(columns, block, block_missing, filled, row)
        filled += 1

    frame = _finalise_event_frame(columns, block, block_missing, filled, capacity)
    return frame, start_idx + filled


def _extract_mint_bytes(line):
    marker = b'"mint":"'
    start = line.find(marker)
    if start < 0:
        return None
    start += len(marker)
    end = line.find(b'"', start)
    if end < 0:
        return None
    return line[start:end].decode("utf-8", "replace")


def read_frame_subset(path, stats, mints, index_lookup):
    """Read only the events of ``mints``, keeping their global event indices.

    Used by the batched path: the file is streamed once per batch, but only the
    batch's rows are ever materialised. ``index_lookup`` maps a mint to the
    global ``event_index`` values assigned by :func:`_scan_events`, so audit
    identifiers stay identical to a single-pass run.
    """
    mints = set(mints)
    capacity = int(sum(len(index_lookup[mint]) for mint in mints))
    columns, block, block_missing = _allocate_event_columns(capacity)
    cursors = dict.fromkeys(mints, 0)

    filled = 0
    for line in _iter_jsonl(path):
        if not _is_trade_line(line):
            continue
        mint = _extract_mint_bytes(line)
        if mint is None or mint not in mints:
            continue
        position = cursors[mint]
        row = _parse_event(line, int(index_lookup[mint][position]), stats)
        if row is None:
            continue
        cursors[mint] = position + 1
        _store_event_row(columns, block, block_missing, filled, row)
        filled += 1

    frame = _finalise_event_frame(columns, block, block_missing, filled, capacity)
    return frame, filled


def _scan_events(path):
    """One streaming pass: global event indices per mint and signature multiplicity.

    Returns the compact arrays the batched path needs, plus line and parse
    counters. Nothing is retained per event apart from one int64 index and one
    int32 signature count, so this pass is cheap in memory.
    """
    per_mint: dict[str, list[int]] = {}
    signatures: list[str] = []
    lines_scanned = 0
    bad_json = 0
    index = 0
    for line in _iter_jsonl(path):
        lines_scanned += 1
        if not _is_trade_line(line):
            continue
        try:
            event = loads(line)
        except ValueError:
            bad_json += 1
            continue
        index += 1
        mint = event.get("mint")
        key = mint if isinstance(mint, str) else ""
        per_mint.setdefault(key, []).append(index)
        signature = event.get("signature")
        signatures.append(signature if isinstance(signature, str) else "")

    if signatures:
        _, inverse, counts = np.unique(
            np.asarray(signatures, dtype=object),
            return_inverse=True,
            return_counts=True,
        )
        signature_events = counts[inverse].astype("int32")
        del _, inverse, counts
    else:
        signature_events = np.zeros(0, dtype="int32")
    del signatures

    return {
        "per_mint": {
            mint: np.asarray(indices, dtype="int64")
            for mint, indices in per_mint.items()
        },
        "signature_events": signature_events,
        "lines_scanned": lines_scanned,
        "bad_json": bad_json,
        "events": index,
    }


def _pack_batches(per_mint, limit):
    """First-fit pack mints into batches of at most ``limit`` events."""
    batches: list[list[str]] = []
    sizes: list[int] = []
    for mint, indices in sorted(per_mint.items(), key=lambda item: -len(item[1])):
        for position, size in enumerate(sizes):
            if size + len(indices) <= limit:
                batches[position].append(mint)
                sizes[position] += len(indices)
                break
        else:
            batches.append([mint])
            sizes.append(len(indices))
    return batches


_RATIO_KEYS = frozenset(
    {"comparison_coverage", "median_ratio", "p90_ratio", "p99_ratio"}
)
_MAX_KEYS = frozenset({"max_ratio"})


def _merge_cleaning_stats(target, source):
    """Accumulate per-batch ``clean_trades`` counters into one stats dict.

    Counts add up; ratio summaries are weighted by ``comparison_events`` when
    both sides carry it, and ``max_ratio`` takes the maximum. Per-timeframe
    validation is recomputed on the merged bars, so it stays exact.
    """
    weight_old = target.get("comparison_events")
    weight_new = source.get("comparison_events")
    for key, value in source.items():
        if key not in target:
            target[key] = value
            continue
        current = target[key]
        if isinstance(value, dict) and isinstance(current, dict):
            _merge_cleaning_stats(current, value)
        elif (
            isinstance(value, list)
            and isinstance(current, list)
            and value
            and current
        ):
            target[key] = [min(current[0], value[0]), max(current[-1], value[-1])]
        elif isinstance(value, bool) or not isinstance(value, (int, float)):
            target[key] = value
        elif key in _MAX_KEYS:
            target[key] = max(current, value)
        elif key in _RATIO_KEYS and weight_old and weight_new:
            total = weight_old + weight_new
            target[key] = (current * weight_old + value * weight_new) / total
        else:
            target[key] = current + value


# ----------------------------------------------------------------------------- clean
def _quote_conversion_factor(quote_mint, quote_price_sol):
    quote_mint = str(quote_mint or "").strip()
    if quote_mint == WSOL:
        return 1.0
    qps = _finite_float(quote_price_sol)
    if quote_mint and np.isfinite(qps) and qps > 0:
        return qps
    return np.nan


def _parse_multi_breakdown(row):
    """Parse a multi-event breakdown into gross BUY/SELL component totals."""
    result = {
        "breakdown_detail_present": 0,
        "breakdown_detail_valid": 0,
        "breakdown_valid_components": 0,
        "breakdown_invalid_components": 0,
        "breakdown_buy_count": 0,
        "breakdown_sell_count": 0,
        "gross_buy_token_amount": 0.0,
        "gross_sell_token_amount": 0.0,
        "gross_buy_quote_amount": 0.0,
        "gross_sell_quote_amount": 0.0,
        "gross_buy_quote_sol": 0.0,
        "gross_sell_quote_sol": 0.0,
        "breakdown_buy_execution_open": np.nan,
        "breakdown_buy_execution_high": np.nan,
        "breakdown_buy_execution_low": np.nan,
        "breakdown_buy_execution_close": np.nan,
        "breakdown_sell_execution_open": np.nan,
        "breakdown_sell_execution_high": np.nan,
        "breakdown_sell_execution_low": np.nan,
        "breakdown_sell_execution_close": np.nan,
    }
    raw = row.get("breakdown_json", "")
    try:
        components = json.loads(raw) if isinstance(raw, str) and raw else None
    except (TypeError, ValueError, json.JSONDecodeError):
        components = None
    if not isinstance(components, list) or len(components) != int(row.get("n_breakdown", 0)):
        result["breakdown_invalid_components"] = int(row.get("n_breakdown", 0))
        return result

    result["breakdown_detail_present"] = 1
    factor = _quote_conversion_factor(row.get("quote_mint"), row.get("quote_price_sol"))
    buy_prices, sell_prices = [], []
    for component in components:
        if not isinstance(component, dict):
            result["breakdown_invalid_components"] += 1
            continue
        action = str(component.get("action") or "").lower()
        token_amount = _finite_float(component.get("tokenAmount"))
        quote_amount = _finite_float(component.get("quoteAmount"))
        quote_sol = quote_amount * factor if np.isfinite(factor) else np.nan
        valid = (
            action in ("buy", "sell")
            and np.isfinite(token_amount) and token_amount > 0
            and np.isfinite(quote_amount) and quote_amount > 0
            and np.isfinite(quote_sol) and quote_sol > 0
        )
        if not valid:
            result["breakdown_invalid_components"] += 1
            continue
        result["breakdown_valid_components"] += 1
        component_price = quote_sol / token_amount
        if action == "buy":
            result["breakdown_buy_count"] += 1
            result["gross_buy_token_amount"] += token_amount
            result["gross_buy_quote_amount"] += quote_amount
            result["gross_buy_quote_sol"] += quote_sol
            buy_prices.append(component_price)
        else:
            result["breakdown_sell_count"] += 1
            result["gross_sell_token_amount"] += token_amount
            result["gross_sell_quote_amount"] += quote_amount
            result["gross_sell_quote_sol"] += quote_sol
            sell_prices.append(component_price)

    result["breakdown_detail_valid"] = int(
        result["breakdown_invalid_components"] == 0
        and result["breakdown_valid_components"] == len(components)
        and result["breakdown_valid_components"] > 0
    )
    # Directional OHLC uses the individual component prices in breakdown-array order.
    # These are observed component execution prices, not hypothetical fills for a new order.
    if result["breakdown_detail_valid"]:
        for side, prices in (("buy", buy_prices), ("sell", sell_prices)):
            if prices:
                result[f"breakdown_{side}_execution_open"] = prices[0]
                result[f"breakdown_{side}_execution_high"] = max(prices)
                result[f"breakdown_{side}_execution_low"] = min(prices)
                result[f"breakdown_{side}_execution_close"] = prices[-1]
    return result


_STRING_CACHE: dict[str, str] = {}
_STRING_CACHE_LIMIT = 50_000
_STRING_MISSING = object()


def _normalise_strings(series, default=""):
    """``fillna(default).astype(str).str.strip()`` with interned values.

    The plain chain allocates a fresh string per row. On multi-million-event
    days that costs hundreds of MB for columns with a handful of distinct
    values (mint, pool id, quote mint, ...), so normalise each distinct value
    once and share it.
    """
    cache = _STRING_CACHE

    def clean(value):
        if value is None or (isinstance(value, float) and np.isnan(value)):
            return default
        text = value if isinstance(value, str) else str(value)
        cached = cache.get(text, _STRING_MISSING)
        if cached is not _STRING_MISSING:
            return cached
        stripped = sys.intern(text.strip())
        if len(cache) < _STRING_CACHE_LIMIT:
            cache[text] = stripped
        return stripped

    return series.map(clean)


def clean_trades(df, args, st, copy_frame=True):
    """Normalize raw events and reconstruct gross trade flow from breakdowns."""
    if copy_frame:
        df = df.copy()
    for c in (
        "timestamp_ms", "token_amount", "quote_amount", "price_sol", "quote_price_sol",
        "tokens_in_pool", "quote_in_pool", "virtual_tokens_in_pool", "virtual_quote_in_pool",
        "decimals", "supply",
        "participant_quote_balance_delta_sol", "participant_balance_checked",
    ):
        if c not in df:
            df[c] = np.nan
        df[c] = pd.to_numeric(df[c], errors="coerce").astype("float64")
    if "n_breakdown" not in df:
        df["n_breakdown"] = 0
    df["n_breakdown"] = pd.to_numeric(df["n_breakdown"], errors="coerce").fillna(0).astype("int64")
    for c, default in (
        ("breakdown_json", ""), ("burned_liquidity", ""), ("pool_created_by", ""),
        ("pool", ""), ("pool_id", ""), ("quote_mint", ""),
    ):
        if c not in df:
            df[c] = default
        df[c] = _normalise_strings(df[c], default)
    if "participant_balance_available" not in df:
        df["participant_balance_available"] = 0
    df["participant_balance_available"] = pd.to_numeric(
        df["participant_balance_available"], errors="coerce"
    ).fillna(0).astype("int8")
    if "participant_balance_checked" not in df:
        df["participant_balance_checked"] = df["participant_balance_available"]
    df["participant_balance_checked"] = pd.to_numeric(
        df["participant_balance_checked"], errors="coerce"
    ).fillna(0).astype("int8")

    df["pool_id_missing"] = df["pool_id"].eq("").astype("int8")
    pool_fallback = df["pool"].where(df["pool"].ne(""), "unknown")
    df["pool_key"] = df["pool_id"].where(
        df["pool_id"].ne(""), "__missing_pool_id__:" + pool_fallback
    )
    df["action"] = df["action"].astype(str).str.lower()

    # Parse only aggregated events. A one-component event is represented by its
    # top-level amounts; zero-breakdown events use an explicitly labelled fallback.
    multi_summaries = {}
    multi_idx = df.index[df["n_breakdown"] > 1]
    for idx in multi_idx:
        multi_summaries[idx] = _parse_multi_breakdown(df.loc[idx])
    summary_cols = [
        "breakdown_detail_present", "breakdown_detail_valid", "breakdown_valid_components",
        "breakdown_invalid_components", "breakdown_buy_count", "breakdown_sell_count",
        "gross_buy_token_amount", "gross_sell_token_amount", "gross_buy_quote_amount",
        "gross_sell_quote_amount", "gross_buy_quote_sol", "gross_sell_quote_sol",
        "breakdown_buy_execution_open", "breakdown_buy_execution_high",
        "breakdown_buy_execution_low", "breakdown_buy_execution_close",
        "breakdown_sell_execution_open", "breakdown_sell_execution_high",
        "breakdown_sell_execution_low", "breakdown_sell_execution_close",
    ]
    for col in summary_cols:
        df[col] = (0.0 if col.startswith("gross_") else
                   np.nan if "execution_" in col else 0)
    for idx, summary in multi_summaries.items():
        for col, value in summary.items():
            df.at[idx, col] = value

    quote_factor = np.where(
        df["quote_mint"].eq(WSOL), 1.0,
        df["quote_price_sol"].where(
            df["quote_mint"].ne("") & np.isfinite(df["quote_price_sol"])
            & (df["quote_price_sol"] > 0), np.nan,
        ),
    )
    quote_factor = pd.Series(quote_factor, index=df.index, dtype="float64")
    top_quote_sol = df["quote_amount"] * quote_factor
    top_token_valid = np.isfinite(df["token_amount"]) & (df["token_amount"] > 0)
    top_quote_valid = (
        np.isfinite(df["quote_amount"]) & (df["quote_amount"] > 0)
        & np.isfinite(quote_factor) & (quote_factor > 0)
        & np.isfinite(top_quote_sol) & (top_quote_sol > 0)
    )
    df["top_level_quote_valid"] = top_quote_valid.astype("int8")

    # One component or no breakdown: the top-level record is usable as a single
    # execution. Multi-breakdown rows must be completely reconstructed; their net
    # top-level amounts are not treated as gross trade volume or execution price.
    single_or_fallback = df["n_breakdown"] <= 1
    df.loc[single_or_fallback & (df["action"] == "buy"), "breakdown_buy_count"] = 1
    df.loc[single_or_fallback & (df["action"] == "sell"), "breakdown_sell_count"] = 1
    df.loc[single_or_fallback & (df["action"] == "buy"), "gross_buy_token_amount"] = df.loc[
        single_or_fallback & (df["action"] == "buy"), "token_amount"
    ]
    df.loc[single_or_fallback & (df["action"] == "sell"), "gross_sell_token_amount"] = df.loc[
        single_or_fallback & (df["action"] == "sell"), "token_amount"
    ]
    df.loc[single_or_fallback & (df["action"] == "buy"), "gross_buy_quote_amount"] = df.loc[
        single_or_fallback & (df["action"] == "buy"), "quote_amount"
    ]
    df.loc[single_or_fallback & (df["action"] == "sell"), "gross_sell_quote_amount"] = df.loc[
        single_or_fallback & (df["action"] == "sell"), "quote_amount"
    ]
    df.loc[single_or_fallback & (df["action"] == "buy"), "gross_buy_quote_sol"] = top_quote_sol.loc[
        single_or_fallback & (df["action"] == "buy")
    ]
    df.loc[single_or_fallback & (df["action"] == "sell"), "gross_sell_quote_sol"] = top_quote_sol.loc[
        single_or_fallback & (df["action"] == "sell")
    ]

    multi_valid = (df["n_breakdown"] > 1) & df["breakdown_detail_valid"].eq(1)
    single_valid = single_or_fallback & top_token_valid & top_quote_valid
    event_volume_valid = multi_valid | single_valid
    df["breakdown_source"] = np.select(
        [multi_valid, (df["n_breakdown"] == 1), (df["n_breakdown"] == 0)],
        ["explicit_breakdown", "top_level_single", "top_level_fallback"],
        default="invalid_breakdown",
    )
    df["breakdown_valid"] = np.where(multi_valid, 1, np.where(single_valid & (df["n_breakdown"] == 1), 1, 0)).astype("int8")
    df["volume_eligible"] = event_volume_valid.astype("int8")

    # For a multi-component event, the valid component summaries are the source
    # of gross volume. Net top-level amount is retained only for reconciliation.
    gross_token = df["gross_buy_token_amount"] + df["gross_sell_token_amount"]
    gross_quote_sol = df["gross_buy_quote_sol"] + df["gross_sell_quote_sol"]
    df["gross_token_amount"] = np.where(multi_valid, gross_token, np.where(single_valid, df["token_amount"], np.nan))
    df["gross_quote_volume_sol"] = np.where(multi_valid, gross_quote_sol, np.where(single_valid, top_quote_sol, np.nan))
    df["breakdown_net_token_amount"] = df["gross_buy_token_amount"] - df["gross_sell_token_amount"]
    df["breakdown_net_quote_sol"] = df["gross_buy_quote_sol"] - df["gross_sell_quote_sol"]
    df["breakdown_net_quote_amount"] = df["gross_buy_quote_amount"] - df["gross_sell_quote_amount"]
    df["breakdown_buy_execution_price_sol"] = (
        df["gross_buy_quote_sol"] / df["gross_buy_token_amount"].replace(0, np.nan)
    )
    df["breakdown_sell_execution_price_sol"] = (
        df["gross_sell_quote_sol"] / df["gross_sell_token_amount"].replace(0, np.nan)
    )
    # Single/no-breakdown events have one observed component, so their directional
    # component OHLC is the same execution price. Incomplete events remain NaN.
    single_execution_price = (top_quote_sol / df["token_amount"]).where(single_valid)
    single_buy_mask = single_valid & df["action"].eq("buy")
    single_sell_mask = single_valid & df["action"].eq("sell")
    for suffix in ("open", "high", "low", "close"):
        df.loc[single_buy_mask, f"breakdown_buy_execution_{suffix}"] = single_execution_price.loc[single_buy_mask]
        df.loc[single_sell_mask, f"breakdown_sell_execution_{suffix}"] = single_execution_price.loc[single_sell_mask]
    df["breakdown_gross_execution_vwap_sol"] = (
        df["gross_quote_volume_sol"] / df["gross_token_amount"]
    ).where(event_volume_valid)
    df["execution_price_sol"] = df["breakdown_gross_execution_vwap_sol"]
    df["execution_price_source"] = np.select(
        [multi_valid, (df["n_breakdown"] == 1) & single_valid, (df["n_breakdown"] == 0) & single_valid],
        ["breakdown_gross_vwap", "top_level_single", "top_level_fallback"],
        default="unavailable",
    )
    df["implied_price_sol"] = df["execution_price_sol"]  # v5 semantic: component-aware execution price
    df["top_level_net_implied_price_sol"] = (top_quote_sol / df["token_amount"]).where(
        top_quote_valid & top_token_valid
    )
    df["execution_component_count"] = np.where(
        multi_valid, df["breakdown_valid_components"], np.where(single_valid, 1, 0)
    ).astype("int64")
    df["execution_buy_component_count"] = np.where(
        event_volume_valid, df["breakdown_buy_count"], 0
    ).astype("int64")
    df["execution_sell_component_count"] = np.where(
        event_volume_valid, df["breakdown_sell_count"], 0
    ).astype("int64")
    df["valid_quote_components"] = np.where(
        df["n_breakdown"] > 1,
        df["breakdown_valid_components"],
        np.where(single_valid, 1, 0),
    ).astype("int64")
    df["invalid_quote_components"] = np.where(
        df["n_breakdown"] > 1,
        df["breakdown_invalid_components"],
        np.where(single_valid, 0, 1),
    ).astype("int64")

    # Top-level event amounts should match the signed net of component rows.
    # This explicitly distinguishes a valid netted transaction from a malformed aggregate.
    df["breakdown_netting_flag"] = (
        (df["breakdown_buy_count"] > 0) & (df["breakdown_sell_count"] > 0)
    ).astype("int8")
    df["breakdown_net_zero_flag"] = (
        multi_valid & np.isclose(df["breakdown_net_token_amount"], 0.0, rtol=0.0, atol=1e-12)
        & np.isclose(df["breakdown_net_quote_sol"], 0.0, rtol=0.0, atol=1e-12)
    ).astype("int8")
    tolerance = float(getattr(args, "breakdown_reconciliation_tolerance", 0.02))
    def _amount_matches(left, right, atol):
        if not (np.isfinite(left) and np.isfinite(right)):
            return False
        return bool(np.isclose(abs(left), abs(right), rtol=tolerance, atol=atol))
    reconciles = np.zeros(len(df), dtype="int8")
    direction_matches = np.zeros(len(df), dtype="int8")
    mismatch = np.zeros(len(df), dtype="int8")
    for idx in df.index[multi_valid]:
        row = df.loc[idx]
        net_token = row["breakdown_net_token_amount"]
        net_quote = row["breakdown_net_quote_sol"]
        top_token = row["token_amount"]
        top_quote = top_quote_sol.loc[idx]
        token_ok = _amount_matches(top_token, net_token, 1e-12)
        quote_ok = _amount_matches(top_quote, net_quote, 1e-12)
        # PumpAPI's top-level action is not guaranteed to encode the sign of
        # the net across several traders. Keep this as a separate diagnostic;
        # amount reconciliation itself is based on absolute net amounts.
        if abs(net_token) <= 1e-12:
            direction_ok = True
        else:
            expected_action = "buy" if net_token > 0 else "sell"
            direction_ok = row["action"] == expected_action
        reconciles[df.index.get_loc(idx)] = int(token_ok and quote_ok)
        direction_matches[df.index.get_loc(idx)] = int(direction_ok)
        mismatch[df.index.get_loc(idx)] = int(not (token_ok and quote_ok))
    df["breakdown_net_reconciles_event"] = reconciles
    df["breakdown_net_direction_matches_event"] = direction_matches
    df["breakdown_net_mismatch_flag"] = mismatch

    # Participant quote balance movement is diagnostic. It can include network fees,
    # rent, and unrelated instructions, so it is never used to validate/drop an event.
    df["participant_quote_balance_delta_residual_sol"] = np.where(
        df["participant_balance_available"] == 1,
        df["participant_quote_balance_delta_sol"] + df["breakdown_net_quote_sol"],
        np.nan,
    )

    timestamp_finite = np.isfinite(df["timestamp_ms"])
    masks = {
        "no_signature": df["signature"].isna() | df["signature"].astype(str).str.strip().eq(""),
        "no_mint": df["mint"].isna() | df["mint"].astype(str).str.strip().eq(""),
        "bad_timestamp": (~timestamp_finite) | (df["timestamp_ms"] % 1 != 0),
        "bad_token_amount": ~(top_token_valid | multi_valid),
        "bad_price_sol": ~(np.isfinite(df["price_sol"]) & (df["price_sol"] > 0)),
        "bad_action": ~df["action"].isin(["buy", "sell"]),
    }
    bad = pd.Series(False, index=df.index)
    reason = pd.Series("", index=df.index, dtype="object")
    for name, mask in masks.items():
        bad |= mask
        reason = reason.where(~mask, reason + name + ";")
    st["dropped_invalid_price_amount_ts"] = int(bad.sum())
    st["dropped_reasons"] = {k: int(m.sum()) for k, m in masks.items()}

    dcols = [
        "event_index", "signature", "action", "mint", "pool_id", "quote_mint",
        "timestamp_ms", "token_amount", "quote_amount", "price_sol", "n_breakdown",
        "breakdown_json", "breakdown_invalid_components",
    ]
    dropped = df.loc[bad, [c for c in dcols if c in df.columns]].assign(reason=reason[bad])
    kept_mints = set(df.loc[~bad, "mint"].dropna())
    st["mints_lost_entirely_to_invalid"] = int(len(set(dropped["mint"].dropna()) - kept_mints))
    df = df.loc[~bad].copy()

    n_before = len(df)
    df = df.drop_duplicates(
        subset=["signature", "mint", "action", "pool_id", "timestamp_ms", "token_amount", "quote_amount"]
    )
    st["dropped_duplicates"] = int(n_before - len(df))

    if args.wsol_only:
        m = df["quote_mint"] == WSOL
        st["dropped_non_wsol_quote"] = int((~m).sum())
        df = df.loc[m].copy()

    df["timestamp_ms"] = df["timestamp_ms"].astype("int64")
    df = df.sort_values(["mint", "timestamp_ms", "event_index"], kind="mergesort").reset_index(drop=True)

    if "signature_events" in df.columns:
        # Batched runs supply the global signature multiplicity, computed in
        # one streaming pass over the whole day.
        signature_events = df["signature_events"].astype("int64")
        del df["signature_events"]
    else:
        signature_events = df.groupby("signature")["signature"].transform("size")
    df["routed"] = (signature_events > 1).astype("int8")
    if args.min_trades > 1:
        mint_trade_count = df.groupby("mint")["mint"].transform("size")
        st["dropped_mints_below_min_trades"] = int(
            df.loc[mint_trade_count < args.min_trades, "mint"].nunique()
        )
        df = df.loc[mint_trade_count >= args.min_trades].reset_index(drop=True)

    pool_groups = df.groupby(["mint", "pool_key"], sort=False, dropna=False)
    pool_price_ratio = df["price_sol"] / pool_groups["price_sol"].shift(1)
    df["jump"] = (
        (pool_price_ratio > args.jump_ratio) | (pool_price_ratio < 1.0 / args.jump_ratio)
    ).fillna(False).astype("int8")
    mint_groups = df.groupby("mint", sort=False)
    gap_prev = mint_groups["timestamp_ms"].diff()
    gap_next = -mint_groups["timestamp_ms"].diff(-1)
    df["bundle_cand"] = ((gap_prev <= args.bundle_ms) | (gap_next <= args.bundle_ms)).fillna(False).astype("int8")
    st["flagged_jumps"] = int(df["jump"].sum())
    st["flagged_bundle_candidates"] = int(df["bundle_cand"].sum())
    if args.drop_jumps:
        st["dropped_jumps"] = int(df["jump"].sum())
        df = df.loc[df["jump"] == 0].reset_index(drop=True)

    # Event-level quote validity is now component-aware for multi-breakdown events.
    df["quote_valid"] = df["volume_eligible"].astype("int8")
    df["vol_sol"] = df["gross_quote_volume_sol"].where(df["quote_valid"] == 1)
    df["valid_token_amount"] = df["gross_token_amount"].where(df["quote_valid"] == 1, 0.0)
    df["buy_tok"] = df["gross_buy_token_amount"].where(df["quote_valid"] == 1, 0.0)
    df["sell_tok"] = df["gross_sell_token_amount"].where(df["quote_valid"] == 1, 0.0)
    df["buy_sol"] = df["gross_buy_quote_sol"].where(df["quote_valid"] == 1, 0.0)
    df["sell_sol"] = df["gross_sell_quote_sol"].where(df["quote_valid"] == 1, 0.0)
    df["vwap_valid"] = df["quote_valid"].astype("int8")
    df["vwap_components"] = df["execution_component_count"]
    df["vsol_s"] = df["vol_sol"].where(df["vwap_valid"] == 1, 0.0)
    df["vtok_s"] = df["valid_token_amount"].where(df["vwap_valid"] == 1, 0.0)
    df["invalid_quote"] = (df["quote_valid"] == 0).astype("int8")
    df["zero_quote"] = (df["quote_amount"] == 0).astype("int8")
    df["non_wsol"] = (~df["quote_mint"].eq(WSOL) & df["quote_mint"].ne("")).astype("int8")
    df["is_buy"] = df["action"].eq("buy").astype("int8")
    df["is_sell"] = df["action"].eq("sell").astype("int8")

    # Compare post-event pool spot against each direction's component VWAP.
    # Taking the worst available side avoids BUY/SELL prices cancelling in a
    # single gross VWAP when their execution prices differ materially.
    def _symmetric_ratio(left, right):
        with np.errstate(divide="ignore", invalid="ignore"):
            return np.maximum(left / right, right / left)

    df["pool_spot_vs_buy_execution_ratio"] = _symmetric_ratio(
        df["price_sol"], df["breakdown_buy_execution_price_sol"]
    )
    df["pool_spot_vs_sell_execution_ratio"] = _symmetric_ratio(
        df["price_sol"], df["breakdown_sell_execution_price_sol"]
    )
    side_ratios = pd.concat(
        [df["pool_spot_vs_buy_execution_ratio"], df["pool_spot_vs_sell_execution_ratio"]],
        axis=1,
    )
    df["pool_spot_vs_execution_ratio"] = side_ratios.max(axis=1, skipna=True)
    # A top-level fallback has one directional execution price; the side ratios
    # above are populated for it by the single/no-breakdown component summaries.
    df["price_consistency_ratio"] = df["pool_spot_vs_execution_ratio"]
    with np.errstate(divide="ignore", invalid="ignore"):
        raw_to_execution = df["price_sol"] / df["execution_price_sol"]
    comparable = (
        (df["quote_valid"] == 1)
        & np.isfinite(df["price_sol"]) & (df["price_sol"] > 0)
        & np.isfinite(df["execution_price_sol"]) & (df["execution_price_sol"] > 0)
        & np.isfinite(df["pool_spot_vs_execution_ratio"])
    )
    df["pool_spot_vs_execution_ratio"] = df["pool_spot_vs_execution_ratio"].where(comparable)
    df["price_consistency_ratio"] = df["pool_spot_vs_execution_ratio"]
    df["raw_to_implied_ratio"] = pd.Series(raw_to_execution, index=df.index).where(comparable)
    df["price_comparable"] = comparable.astype("int8")
    consistency_threshold = float(getattr(args, "price_consistency_ratio", 5.0))
    df["price_consistency_flag"] = (
        df["pool_spot_vs_execution_ratio"] >= consistency_threshold
    ).fillna(False).astype("int8")

    # Direction-specific post-event reserve sufficiency. The low reserve ratio
    # is evidence of possible depletion, not proof that a swap was impossible.
    net_action = np.where(df["breakdown_net_token_amount"] > 0, "buy",
                          np.where(df["breakdown_net_token_amount"] < 0, "sell", df["action"]))
    virtual_tokens = df["virtual_tokens_in_pool"].where(
        np.isfinite(df["virtual_tokens_in_pool"]), 0.0
    )
    df["effective_tokens_in_pool"] = df["tokens_in_pool"] + virtual_tokens
    buy_reserve_ratio = df["effective_tokens_in_pool"] / df["breakdown_net_token_amount"].abs().replace(0, np.nan)
    sell_reserve_ratio = df["quote_in_pool"] / df["breakdown_net_quote_amount"].abs().replace(0, np.nan)
    df["pool_side_reserve_ratio"] = np.where(
        net_action == "buy", buy_reserve_ratio, sell_reserve_ratio
    )
    df["pool_side_reserve_ratio"] = pd.to_numeric(df["pool_side_reserve_ratio"], errors="coerce")
    df.loc[df["quote_valid"] == 0, "pool_side_reserve_ratio"] = np.nan
    depletion_threshold = float(getattr(args, "pool_depletion_ratio", 0.05))
    df["pool_depletion_flag"] = (
        np.isfinite(df["pool_side_reserve_ratio"])
        & (df["pool_side_reserve_ratio"] <= depletion_threshold)
        & (df["pool_side_reserve_ratio"] >= 0)
    ).astype("int8")
    df["price_impact_candidate_flag"] = (
        (df["price_consistency_flag"] == 1) & (df["pool_depletion_flag"] == 1)
    ).astype("int8")
    reserve_evidence_available = (
        np.isfinite(df["pool_side_reserve_ratio"])
        & (df["pool_side_reserve_ratio"] >= 0)
    )
    df["data_anomaly_candidate_flag"] = (
        (df["price_consistency_flag"] == 1)
        & reserve_evidence_available
        & (df["pool_depletion_flag"] == 0)
    ).astype("int8")
    df["price_gap_unclassified_flag"] = (
        (df["price_consistency_flag"] == 1)
        & ~reserve_evidence_available
    ).astype("int8")
    top_ratio = np.maximum(
        (df["price_sol"] / df["top_level_net_implied_price_sol"]),
        (df["top_level_net_implied_price_sol"] / df["price_sol"]),
    )
    df["top_level_net_price_gap_flag"] = (top_ratio >= consistency_threshold).fillna(False).astype("int8")
    df["netting_explains_price_gap_flag"] = (
        (df["breakdown_netting_flag"] == 1)
        & (df["breakdown_net_reconciles_event"] == 1)
        & (df["top_level_net_price_gap_flag"] == 1)
        & (df["price_consistency_flag"] == 0)
    ).astype("int8")

    invalid_reasons = pd.Series("", index=df.index, dtype="object")
    invalid_reasons = invalid_reasons.mask(
        (df["n_breakdown"] > 1) & (df["breakdown_detail_valid"] == 0),
        "breakdown_missing_incomplete_or_invalid;",
    )
    invalid_reasons = invalid_reasons.mask(
        (df["n_breakdown"] <= 1) & (df["top_level_quote_valid"] == 0),
        "quote_amount_or_conversion_invalid;",
    )
    df["invalid_quote_reason"] = invalid_reasons

    # Report balance residual only when PumpAPI provided participant balances.
    df["participant_quote_balance_delta_residual_sol"] = np.where(
        df["participant_balance_available"] == 1,
        df["participant_quote_balance_delta_sol"] + df["breakdown_net_quote_sol"],
        np.nan,
    )

    st["clean_rows"] = int(len(df))
    st["clean_mints"] = int(df["mint"].nunique())
    st["missing_pool_id_rows"] = int(df["pool_id_missing"].sum())
    st["valid_quote_rows"] = int(df["quote_valid"].sum())
    st["invalid_quote_rows"] = int(df["invalid_quote"].sum())
    st["valid_quote_components"] = int(df["valid_quote_components"].sum())
    st["invalid_quote_components"] = int(df["invalid_quote_components"].sum())
    st["flagged_non_wsol_quote"] = int(df["non_wsol"].sum())
    st["flagged_zero_quote"] = int(df["zero_quote"].sum())
    st["flagged_invalid_quote"] = int(df["invalid_quote"].sum())
    st["flagged_routed_candidates"] = int(df["routed"].sum())
    st["breakdown_events"] = int((df["n_breakdown"] > 0).sum())
    st["multi_breakdown_events"] = int((df["n_breakdown"] > 1).sum())
    st["valid_multi_breakdown_events"] = int(((df["n_breakdown"] > 1) & (df["breakdown_valid"] == 1)).sum())
    st["invalid_multi_breakdown_events"] = int(((df["n_breakdown"] > 1) & (df["breakdown_valid"] == 0)).sum())
    st["netted_multi_breakdown_events"] = int(((df["n_breakdown"] > 1) & (df["breakdown_netting_flag"] == 1)).sum())
    st["reconciled_multi_breakdown_events"] = int(
        ((df["n_breakdown"] > 1) & (df["breakdown_net_reconciles_event"] == 1)).sum()
    )
    st["netting_explains_price_gap_events"] = int(df["netting_explains_price_gap_flag"].sum())
    st["breakdown_net_mismatch_events"] = int(df["breakdown_net_mismatch_flag"].sum())
    st["participant_balance_diagnostics_checked"] = int(df["participant_balance_checked"].sum())
    st["participant_balance_diagnostics_available"] = int(df["participant_balance_available"].sum())
    st["participant_balance_diagnostics_skipped"] = int(
        (df["participant_balance_checked"] == 0).sum()
    )
    st["price_consistency"] = {
        "comparison_events": int(df["price_comparable"].sum()),
        "flagged_events": int(df["price_consistency_flag"].sum()),
        "comparison_coverage": float(df["price_comparable"].mean()) if len(df) else None,
        "threshold_ratio": consistency_threshold,
        "median_ratio": float(df.loc[df["price_comparable"] == 1, "price_consistency_ratio"].median()) if df["price_comparable"].any() else None,
        "p90_ratio": float(df.loc[df["price_comparable"] == 1, "price_consistency_ratio"].quantile(0.90)) if df["price_comparable"].any() else None,
        "p99_ratio": float(df.loc[df["price_comparable"] == 1, "price_consistency_ratio"].quantile(0.99)) if df["price_comparable"].any() else None,
        "max_ratio": float(df.loc[df["price_comparable"] == 1, "price_consistency_ratio"].max()) if df["price_comparable"].any() else None,
    }
    st["pool_depletion"] = {
        "threshold_ratio": depletion_threshold,
        "events_with_reserve_ratio": int(df["pool_side_reserve_ratio"].notna().sum()),
        "flagged_events": int(df["pool_depletion_flag"].sum()),
        "price_impact_candidates": int(df["price_impact_candidate_flag"].sum()),
        "data_anomaly_candidates": int(df["data_anomaly_candidate_flag"].sum()),
        "unclassified_price_gap_events": int(df["price_gap_unclassified_flag"].sum()),
    }
    if len(df):
        fmt = "%Y-%m-%d %H:%M:%S"
        st["time_range_utc"] = [
            pd.to_datetime(df["timestamp_ms"].min(), unit="ms").strftime(fmt),
            pd.to_datetime(df["timestamp_ms"].max(), unit="ms").strftime(fmt),
        ]
    return df, dropped


# ----------------------------------------------------------------------------- bars
def _normalize_pool_volume_share(share, tolerance=POOL_SHARE_ROUNDOFF_TOLERANCE):
    """Clip only tiny floating-point overshoots above one.

    A selected pool is a subset of all pools, so its volume share cannot
    mathematically exceed 1. Values just above 1 can occur because the
    numerator and denominator are accumulated in separate groupby operations.
    Material overshoots are deliberately preserved so validation still fails.
    """
    values = pd.to_numeric(share, errors="coerce").astype("float64")
    roundoff = np.isfinite(values) & (values > 1.0) & (values <= 1.0 + tolerance)
    normalized = values.mask(roundoff, 1.0)
    return normalized, roundoff.astype("int8")



def add_candle_quality_fields(bars, candle_range_ratio=10.0, timeframe_ms=None):
    """Attach explicit spot OHLC aliases, candle-range flags, and staleness metadata."""
    if not np.isfinite(candle_range_ratio) or candle_range_ratio <= 1:
        raise ValueError("candle_range_ratio must be a finite number greater than 1")
    out = bars.copy()
    for name in ("open", "high", "low", "close"):
        spot_name = f"spot_{name}"
        if spot_name not in out:
            out[spot_name] = out[name]
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = out["spot_high"] / out["spot_low"]
    ratio = ratio.where(np.isfinite(ratio) & (ratio > 0))
    out["candle_range_ratio"] = ratio
    real = out["is_filled"].eq(0) if "is_filled" in out else pd.Series(True, index=out.index)
    out["candle_range_anomaly_flag"] = (real & ratio.ge(candle_range_ratio)).fillna(False).astype("int8")
    if "stale_bars" not in out:
        out["stale_bars"] = 0
    if "age_bars" not in out:
        if timeframe_ms is not None:
            first = out.groupby("mint", sort=False)["timestamp_ms"].transform("min")
            out["age_bars"] = ((out["timestamp_ms"] - first) // timeframe_ms).astype("int64")
        else:
            out["age_bars"] = out.groupby("mint", sort=False).cumcount().astype("int64")
    return out


def build_bars(
    df,
    tf_ms,
    open_prev_close,
    discontinuity_ratio=5.0,
    return_selected=False,
    candle_range_ratio=10.0,
):
    """Build each candle from one selected pool, never mixing pool price series."""
    if not np.isfinite(discontinuity_ratio) or discontinuity_ratio <= 1:
        raise ValueError("discontinuity_ratio must be a finite number greater than 1")
    if not np.isfinite(candle_range_ratio) or candle_range_ratio <= 1:
        raise ValueError("candle_range_ratio must be a finite number greater than 1")

    d = df.assign(bucket=(df["timestamp_ms"] // tf_ms) * tf_ms)

    # Select a pool independently inside each mint/time bucket using data from that bucket only.
    # Ranking: valid SOL quote volume, valid quote trade count, trade count, then first event.
    pool_stats = (
        d.groupby(["mint", "bucket", "pool_key"], sort=False, dropna=False)
        .agg(
            pool_id=("pool_id", "last"),
            pool_volume_sol=("vol_sol", lambda values: values.sum(min_count=1)),
            pool_valid_quote_trades=("quote_valid", "sum"),
            pool_trades=("event_index", "size"),
            pool_first_ts=("timestamp_ms", "min"),
            pool_first_event=("event_index", "min"),
        )
        .reset_index()
    )
    pool_stats = pool_stats.sort_values(
        [
            "mint", "bucket", "pool_volume_sol", "pool_valid_quote_trades",
            "pool_trades", "pool_first_ts", "pool_first_event", "pool_key",
        ],
        ascending=[True, True, False, False, False, True, True, True],
        na_position="last",
        kind="mergesort",
    )
    selected_pool = pool_stats.drop_duplicates(["mint", "bucket"], keep="first").copy()

    total_bucket = (
        d.groupby(["mint", "bucket"], sort=True)
        .agg(
            all_pool_trades=("event_index", "size"),
            all_pool_volume_sol=("vol_sol", lambda values: values.sum(min_count=1)),
            pool_count=("pool_key", "nunique"),
        )
        .reset_index()
    )
    selected_pool = selected_pool.merge(total_bucket, on=["mint", "bucket"], how="left", validate="one_to_one")
    selected_pool["pool_trade_share"] = (
        selected_pool["pool_trades"] / selected_pool["all_pool_trades"]
    )
    raw_pool_volume_share = pd.Series(
        np.where(
            selected_pool["all_pool_volume_sol"] > 0,
            selected_pool["pool_volume_sol"] / selected_pool["all_pool_volume_sol"],
            np.nan,
        ),
        index=selected_pool.index,
        dtype="float64",
    )
    (
        selected_pool["pool_volume_share"],
        selected_pool["pool_volume_share_roundoff_clipped"],
    ) = _normalize_pool_volume_share(raw_pool_volume_share)

    selected = d.merge(
        selected_pool[["mint", "bucket", "pool_key"]],
        on=["mint", "bucket", "pool_key"],
        how="inner",
        validate="many_to_one",
    )
    selected = selected.sort_values(
        ["mint", "bucket", "timestamp_ms", "event_index"], kind="mergesort"
    )

    bar_agg = {
            "open": ("price_sol", "first"),
            "high": ("price_sol", "max"),
            "low": ("price_sol", "min"),
            "close": ("price_sol", "last"),
            "volume_token": ("valid_token_amount", "sum"),
            "volume_sol": ("vol_sol", "sum"),
            "buy_volume_token": ("buy_tok", "sum"),
            "sell_volume_token": ("sell_tok", "sum"),
            "buy_volume_sol": ("buy_sol", "sum"),
            "sell_volume_sol": ("sell_sol", "sum"),
            "gross_buy_quote_amount": ("gross_buy_quote_amount", "sum"),
            "gross_sell_quote_amount": ("gross_sell_quote_amount", "sum"),
            "trades": ("price_sol", "size"),
            "buys": ("is_buy", "sum"),
            "sells": ("is_sell", "sum"),
            "open_ts_ms": ("timestamp_ms", "first"),
            "close_ts_ms": ("timestamp_ms", "last"),
            "pool_id": ("pool_id", "last"),
            "pool_key": ("pool_key", "last"),
            # The selected pool's protocol and quote mint are stable metadata
            # needed by downstream execution routing. These are aggregated
            # only after selecting one pool_key for this mint/bucket.
            "pool": ("pool", "last"),
            "quote_mint": ("quote_mint", "last"),
            "selected_pool_missing_id_trades": ("pool_id_missing", "sum"),
            "valid_quote_trades": ("quote_valid", "sum"),
            "invalid_quote_trades": ("invalid_quote", "sum"),
            "vwap_trades": ("vwap_valid", "sum"),
            "vwap_components": ("vwap_components", "sum"),
            "valid_quote_components": ("valid_quote_components", "sum"),
            "invalid_quote_components": ("invalid_quote_components", "sum"),
            "breakdown_buy_count": ("breakdown_buy_count", "sum"),
            "breakdown_sell_count": ("breakdown_sell_count", "sum"),
            "breakdown_netting_flag": ("breakdown_netting_flag", "sum"),
            "breakdown_net_reconciles_event": ("breakdown_net_reconciles_event", "sum"),
            "breakdown_net_direction_matches_event": ("breakdown_net_direction_matches_event", "sum"),
            "breakdown_net_mismatch_flag": ("breakdown_net_mismatch_flag", "sum"),
            "breakdown_net_zero_flag": ("breakdown_net_zero_flag", "sum"),
            "pool_depletion_flag": ("pool_depletion_flag", "sum"),
            "price_impact_candidate_flag": ("price_impact_candidate_flag", "sum"),
            "data_anomaly_candidate_flag": ("data_anomaly_candidate_flag", "sum"),
            "price_gap_unclassified_flag": ("price_gap_unclassified_flag", "sum"),
            "pool_side_reserve_ratio": ("pool_side_reserve_ratio", "min"),
            "price_comparison_trades": ("price_comparable", "sum"),
            "price_consistency_flagged_trades": ("price_consistency_flag", "sum"),
            "participant_balance_checked": ("participant_balance_checked", "sum"),
            "participant_balance_available": ("participant_balance_available", "sum"),
            "open_price_consistency_flag": ("price_consistency_flag", "first"),
            "close_price_consistency_flag": ("price_consistency_flag", "last"),
            "jump_flag": ("jump", "max"),
            "bundle_cand_trades": ("bundle_cand", "sum"),
            "routed_trades": ("routed", "sum"),
            "non_wsol_trades": ("non_wsol", "sum"),
            "zero_quote_trades": ("zero_quote", "sum"),
            "vwap_sol_volume": ("vsol_s", "sum"),
            "vwap_token_volume": ("vtok_s", "sum"),
            "buy_execution_open": ("breakdown_buy_execution_open", "first"),
            "buy_execution_high": ("breakdown_buy_execution_high", "max"),
            "buy_execution_low": ("breakdown_buy_execution_low", "min"),
            "buy_execution_close": ("breakdown_buy_execution_close", "last"),
            "sell_execution_open": ("breakdown_sell_execution_open", "first"),
            "sell_execution_high": ("breakdown_sell_execution_high", "max"),
            "sell_execution_low": ("breakdown_sell_execution_low", "min"),
            "sell_execution_close": ("breakdown_sell_execution_close", "last"),
            "buy_execution_components": ("execution_buy_component_count", "sum"),
            "sell_execution_components": ("execution_sell_component_count", "sum"),
            "execution_valid_events": ("volume_eligible", "sum"),
            "execution_invalid_events": ("volume_eligible", lambda values: int((values == 0).sum())),
            "execution_component_count": ("execution_component_count", "sum"),
    }
    for output_name, source_name in EVENT_AUDIT_FIELDS.items():
        # GroupBy.first/last skip nulls and can silently combine fields from
        # different events. iloc preserves the exact first/last source row,
        # including NaN quote fields on an invalid-quote event.
        bar_agg[f"open_{output_name}"] = (source_name, lambda values: values.iloc[0])
        bar_agg[f"close_{output_name}"] = (source_name, lambda values: values.iloc[-1])

    bars = (
        selected.groupby(["mint", "bucket"], sort=True)
        .agg(**bar_agg)
        .reset_index()
        .rename(columns={"bucket": "timestamp_ms"})
    )

    # Preserve the observed pool-spot OHLC before any optional synthetic-open adjustment.
    bars["spot_open"] = bars["open"]
    bars["spot_high"] = bars["high"]
    bars["spot_low"] = bars["low"]
    bars["spot_close"] = bars["close"]

    # A zero-volume candle means no quote-validated event was available, not measured zero liquidity.
    no_valid_quote = bars["valid_quote_trades"] == 0
    bars.loc[no_valid_quote, VOL_COLS] = np.nan
    no_vwap_trade = bars["vwap_trades"] == 0
    bars.loc[no_vwap_trade, ["vwap_sol_volume", "vwap_token_volume"]] = np.nan
    bars["vwap_gross_execution_sol"] = (
        bars["vwap_sol_volume"] / bars["vwap_token_volume"]
    ).where(bars["vwap_token_volume"] > 0)
    # Retain both previous names as compatibility aliases; v5 now uses gross components.
    bars["vwap_single_breakdown_sol"] = bars["vwap_gross_execution_sol"]
    bars["vwap_sol"] = bars["vwap_gross_execution_sol"]
    bars["vwap_trade_coverage"] = (
        bars["vwap_trades"] / bars["valid_quote_trades"].replace(0, np.nan)
    )
    bars["execution_coverage"] = (
        bars["execution_valid_events"] / bars["trades"].replace(0, np.nan)
    )
    bars["execution_ohlc_complete"] = (bars["execution_invalid_events"] == 0).astype("int8")

    bars = bars.merge(
        selected_pool[
            ["mint", "bucket", "pool_count", "pool_trades", "all_pool_trades",
             "pool_trade_share", "pool_volume_share", "pool_volume_share_roundoff_clipped"]
        ].rename(columns={"bucket": "timestamp_ms"}),
        on=["mint", "timestamp_ms"],
        how="left",
        validate="one_to_one",
    )

    # Consolidate immediately after the merge, before adding the pool audit aliases.
    # The merge can leave many internal blocks even if a later copy would compact them.
    bars = bars.copy()
    bars = bars.assign(
        pool_excluded_trades=bars["all_pool_trades"] - bars["pool_trades"],
        selected_pool_trade_share=bars["pool_trade_share"],
        selected_pool_volume_share=bars["pool_volume_share"],
    ).drop(columns=["pool_trades", "pool_trade_share", "pool_volume_share"])

    # pool_changed means the selected pool differs from the previous observed candle for this mint.
    bars = bars.sort_values(["mint", "timestamp_ms"], kind="mergesort").reset_index(drop=True)
    mint_groups = bars.groupby("mint", sort=False)
    previous_pool = mint_groups["pool_key"].shift(1)
    bars["pool_changed"] = (
        previous_pool.notna() & bars["pool_key"].ne(previous_pool)
    ).astype("int8")

    # Compare the first observed price in this candle with the previous observed close.
    # The symmetric ratio is always >= 1 and is not used to rewrite or drop OHLC values.
    bars["observed_open"] = bars["open"]
    bars["prev_close"] = mint_groups["close"].shift(1)
    bars["prev_close_ts_ms"] = mint_groups["close_ts_ms"].shift(1)
    bars["price_gap_elapsed_ms"] = (
        bars["open_ts_ms"] - bars["prev_close_ts_ms"]
    )
    with np.errstate(divide="ignore", invalid="ignore"):
        gap_up = bars["observed_open"] / bars["prev_close"]
        gap_down = bars["prev_close"] / bars["observed_open"]
        bars["price_discontinuity_ratio"] = np.maximum(gap_up, gap_down)
    invalid_gap_ratio = ~np.isfinite(bars["price_discontinuity_ratio"])
    bars.loc[invalid_gap_ratio, "price_discontinuity_ratio"] = np.nan
    bars["price_discontinuity_flag"] = (
        bars["price_discontinuity_ratio"] >= discontinuity_ratio
    ).fillna(False).astype("int8")
    bars["pool_switch_discontinuity_flag"] = (
        (bars["pool_changed"] == 1) & (bars["price_discontinuity_flag"] == 1)
    ).astype("int8")

    # Preserve the previous observed candle's final event for raw-event traceability.
    for suffix in EVENT_AUDIT_FIELDS:
        bars[f"prev_close_{suffix}"] = mint_groups[f"close_{suffix}"].shift(1)

    if open_prev_close:
        previous_close = mint_groups["close"].shift(1)
        previous_pool_key = mint_groups["pool_key"].shift(1)
        same_pool_as_previous = bars["pool_key"].eq(previous_pool_key)
        previous_close = previous_close.where(same_pool_as_previous)
        bars["open"] = previous_close.fillna(bars["open"])
        bars["high"] = np.maximum(bars["high"], bars["open"])
        bars["low"] = np.minimum(bars["low"], bars["open"])

    # Previous-close audit fields add many columns; consolidate before adding final bar metadata.
    bars = bars.copy()
    first_timestamp = bars.groupby("mint", sort=False)["timestamp_ms"].transform("min")
    bars = bars.assign(
        is_filled=np.zeros(len(bars), dtype="int8"),
        stale_bars=np.zeros(len(bars), dtype="int64"),
        age_bars=((bars["timestamp_ms"] - first_timestamp) // tf_ms).astype("int64"),
    )
    bars = add_candle_quality_fields(bars, candle_range_ratio, tf_ms)
    if return_selected:
        return bars, selected
    return bars


def fill_gaps(bars, tf_ms, candle_range_ratio=10.0):
    """Fill gaps between the first and last observed candle using the previous close."""
    rng = bars.groupby("mint")["timestamp_ms"].agg(["min", "max"])
    lengths = ((rng["max"] - rng["min"]) // tf_ms + 1).to_numpy()
    total = int(lengths.sum())
    within = np.arange(total) - np.repeat(np.cumsum(lengths) - lengths, lengths)
    full = pd.DataFrame({
        "mint": np.repeat(rng.index.to_numpy(), lengths),
        "timestamp_ms": np.repeat(rng["min"].to_numpy(), lengths) + within * tf_ms,
    })
    full = full.merge(bars.drop(columns=["is_filled"]), on=["mint", "timestamp_ms"], how="left")
    # The merge carries many event-audit columns; consolidate once before fill operations.
    full = full.copy()

    filled = full["open"].isna()
    full["close"] = full.groupby("mint")["close"].ffill()
    for c in ("open", "high", "low"):
        full[c] = full[c].fillna(full["close"])
    full["spot_close"] = full.groupby("mint")["spot_close"].ffill()
    for c in ("spot_open", "spot_high", "spot_low"):
        full[c] = full[c].fillna(full["spot_close"])
    # Only synthetic gap candles receive zero volume. Preserve NaN on real candles
    # whose selected-pool events had no valid quote: NaN means unknown/unmeasurable,
    # while 0.0 on a real candle would falsely claim measured zero trading volume.
    for c in VOL_COLS:
        full.loc[filled, c] = 0.0
    for c in COUNT_COLS:
        full[c] = full[c].fillna(0).astype("int64")
    for c in RATIO_COLS:
        full[c] = full[c].fillna(0.0)
    full["pool_id"] = full.groupby("mint")["pool_id"].ffill()
    full["pool_key"] = full.groupby("mint")["pool_key"].ffill()
    # These values are informational on filled rows only. Downstream Pool
    # Selection Timeline must be built from real bars (is_filled == 0).
    for column in ("pool", "quote_mint"):
        if column in full.columns:
            full[column] = full.groupby("mint")[column].ffill()
    full["open_ts_ms"] = full["open_ts_ms"].astype("Int64")
    full["close_ts_ms"] = full["close_ts_ms"].astype("Int64")
    full["is_filled"] = filled.astype("int8")
    full["age_bars"] = full.groupby("mint", sort=False).cumcount().astype("int64")
    full["_stale_segment"] = full.groupby("mint", sort=False)["is_filled"].transform(
        lambda values: values.eq(0).cumsum()
    )
    full["stale_bars"] = full.groupby(["mint", "_stale_segment"], sort=False).cumcount().astype("int64")
    full = full.drop(columns=["_stale_segment"])
    # Synthetic bars carry spot marks for valuation only. They never inherit execution OHLC.
    execution_price_cols = [
        "buy_execution_open", "buy_execution_high", "buy_execution_low", "buy_execution_close",
        "sell_execution_open", "sell_execution_high", "sell_execution_low", "sell_execution_close",
    ]
    full.loc[filled, execution_price_cols] = np.nan
    full.loc[filled, "execution_ohlc_complete"] = 0
    full = add_candle_quality_fields(full, candle_range_ratio, tf_ms)
    return full


def validate(
    bars, discontinuity_ratio=5.0, price_consistency_ratio=5.0, candle_range_ratio=10.0
):
    """Validate OHLC, gross component-volume accounting, and audit classifications."""
    px = ["open", "high", "low", "close"]
    real = bars["is_filled"] == 0

    def _mismatch(left, right, mask):
        valid = mask & left.notna() & right.notna()
        if not valid.any():
            return 0
        return int((~np.isclose(left[valid], right[valid], rtol=1e-9, atol=1e-12)).sum())

    checks = {
        "duplicate_keys": int(bars.duplicated(["mint", "timestamp_ms"]).sum()),
        "nan_ohlc": int(bars[px].isna().any(axis=1).sum()),
        "non_positive_price": int((bars[px] <= 0).any(axis=1).sum()),
        "high_lt_open_close": int((bars["high"] < np.maximum(bars["open"], bars["close"])).sum()),
        "low_gt_open_close": int((bars["low"] > np.minimum(bars["open"], bars["close"])).sum()),
        "negative_volume": int((bars[VOL_COLS] < 0).any(axis=1).sum()),
        "non_finite_volume": int(
            ((~np.isfinite(bars[VOL_COLS])) & bars[VOL_COLS].notna()).any(axis=1).sum()
        ),
        "invalid_vwap": int(
            (bars["vwap_sol"].notna()
             & (~np.isfinite(bars["vwap_sol"]) | (bars["vwap_sol"] <= 0))).sum()
        ),
        "invalid_pool_share": int(
            (~bars["selected_pool_trade_share"].between(0, 1) & real).sum()
            + ((bars["selected_pool_volume_share"].notna()
                & ~bars["selected_pool_volume_share"].between(0, 1)) & real).sum()
        ),
        "invalid_pool_counts": int(
            ((bars["pool_count"] < 1) & real).sum()
            + ((bars["pool_excluded_trades"] < 0) & real).sum()
            + ((bars["selected_pool_missing_id_trades"] < 0) & real).sum()
        ),
        "pool_trade_accounting": int(
            ((bars["trades"] + bars["pool_excluded_trades"] != bars["all_pool_trades"]) & real).sum()
        ),
        "buy_sell_event_accounting": int(
            ((bars["buys"] + bars["sells"] != bars["trades"]) & real).sum()
        ),
        "quote_event_accounting": int(
            ((bars["valid_quote_trades"] + bars["invalid_quote_trades"] != bars["trades"]) & real).sum()
        ),
        "buy_sell_gross_token_volume_mismatch": _mismatch(
            bars["buy_volume_token"] + bars["sell_volume_token"],
            bars["volume_token"], real & bars["volume_token"].notna()
        ),
        "buy_sell_gross_quote_volume_mismatch": _mismatch(
            bars["buy_volume_sol"] + bars["sell_volume_sol"],
            bars["volume_sol"], real & bars["volume_sol"].notna()
        ),
        "vwap_components_exceed_valid_components": int(
            ((bars["vwap_components"] > bars["valid_quote_components"]) & real).sum()
        ),
        "invalid_vwap_components": int(
            (
                (bars["vwap_components"] > 0)
                & (
                    ~np.isfinite(bars["vwap_sol_volume"])
                    | ~np.isfinite(bars["vwap_token_volume"])
                    | (bars["vwap_sol_volume"] <= 0)
                    | (bars["vwap_token_volume"] <= 0)
                    | ~np.isclose(
                        bars["vwap_single_breakdown_sol"],
                        bars["vwap_sol_volume"] / bars["vwap_token_volume"],
                        rtol=1e-9, atol=0.0, equal_nan=False,
                    )
                ) & real
            ).sum()
            + (
                (bars["vwap_components"] == 0)
                & (bars["vwap_single_breakdown_sol"].notna()
                   | bars["vwap_sol_volume"].notna()
                   | bars["vwap_token_volume"].notna()) & real
            ).sum()
        ),
        "vwap_coverage_mismatch": int(
            (
                real
                & (
                    ((bars["valid_quote_trades"] > 0)
                     & ~np.isclose(
                         bars["vwap_trade_coverage"],
                         bars["vwap_trades"] / bars["valid_quote_trades"].replace(0, np.nan),
                         rtol=1e-9, atol=1e-12, equal_nan=False,
                     ))
                    | ((bars["valid_quote_trades"] == 0) & bars["vwap_trade_coverage"].notna())
                )
            ).sum()
        ),
        "vwap_legacy_alias_mismatch": int(
            (
                real & ~np.isclose(
                    bars["vwap_sol"], bars["vwap_single_breakdown_sol"],
                    rtol=1e-9, atol=0.0, equal_nan=True,
                )
            ).sum()
        ),
        "invalid_open_close_execution_price": int(
            (
                real & bars["open_quote_valid"].eq(1)
                & (
                    ~np.isfinite(bars["open_execution_price_sol"])
                    | (bars["open_execution_price_sol"] <= 0)
                    | ~np.isclose(
                        bars["open_execution_price_sol"],
                        bars["open_gross_quote_volume_sol"] / bars["open_gross_token_amount"],
                        rtol=1e-9, atol=0.0, equal_nan=False,
                    )
                )
            ).sum()
            + (
                real & bars["close_quote_valid"].eq(1)
                & (
                    ~np.isfinite(bars["close_execution_price_sol"])
                    | (bars["close_execution_price_sol"] <= 0)
                    | ~np.isclose(
                        bars["close_execution_price_sol"],
                        bars["close_gross_quote_volume_sol"] / bars["close_gross_token_amount"],
                        rtol=1e-9, atol=0.0, equal_nan=False,
                    )
                )
            ).sum()
        ),
        "invalid_balance_audit_flags": int(
            (
                real
                & (
                    (bars["participant_balance_checked"] < 0)
                    | (bars["participant_balance_available"] < 0)
                    | (bars["participant_balance_available"] > bars["participant_balance_checked"])
                )
            ).sum()
        ),
        "invalid_price_comparison_counts": int(
            (
                real & (
                    (bars["price_comparison_trades"] > bars["trades"])
                    | (bars["price_consistency_flagged_trades"] > bars["price_comparison_trades"])
                    | (bars["price_consistency_flagged_trades"] < 0)
                )
            ).sum()
        ),
        "invalid_pool_side_reserve_ratio": int(
            (
                real & bars["pool_side_reserve_ratio"].notna()
                & (~np.isfinite(bars["pool_side_reserve_ratio"]) | (bars["pool_side_reserve_ratio"] < 0))
            ).sum()
        ),
        "price_gap_classification_mismatch": int(
            (
                real
                & (
                    (bars["price_consistency_flagged_trades"]
                     != (bars["price_impact_candidate_flag"]
                         + bars["data_anomaly_candidate_flag"]
                         + bars["price_gap_unclassified_flag"]))
                )
            ).sum()
        ),
        "invalid_discontinuity_ratio": int(
            (
                bars["price_discontinuity_ratio"].notna()
                & (
                    ~np.isfinite(bars["price_discontinuity_ratio"])
                    | (bars["price_discontinuity_ratio"] < 1.0)
                ) & real
            ).sum()
        ),
        "discontinuity_flag_mismatch": int(
            (
                real & (
                    bars["price_discontinuity_flag"]
                    != (bars["price_discontinuity_ratio"] >= discontinuity_ratio).fillna(False).astype("int8")
                )
            ).sum()
        ),
        "pool_switch_discontinuity_flag_mismatch": int(
            (
                real & (
                    bars["pool_switch_discontinuity_flag"]
                    != ((bars["pool_changed"] == 1) & (bars["price_discontinuity_flag"] == 1)).astype("int8")
                )
            ).sum()
        ),
        "volume_present_without_valid_quote": int(
            (bars[VOL_COLS].notna().any(axis=1) & (bars["valid_quote_trades"] == 0) & real).sum()
        ),
        "invalid_spot_ohlc": int(
            (bars[["spot_open", "spot_high", "spot_low", "spot_close"]].isna().any(axis=1)).sum()
            + ((bars[["spot_open", "spot_high", "spot_low", "spot_close"]] <= 0).any(axis=1)).sum()
            + int((bars["spot_high"] < np.maximum(bars["spot_open"], bars["spot_close"])).sum())
            + int((bars["spot_low"] > np.minimum(bars["spot_open"], bars["spot_close"])).sum())
        ),
        "invalid_candle_range_ratio": int(
            (real & (
                ~np.isfinite(bars["candle_range_ratio"])
                | (bars["candle_range_ratio"] < 1.0)
                | ~np.isclose(
                    bars["candle_range_ratio"],
                    bars["spot_high"] / bars["spot_low"],
                    rtol=1e-9, atol=1e-12, equal_nan=False,
                )
            )).sum()
        ),
        "candle_range_flag_mismatch": int(
            (bars["candle_range_anomaly_flag"]
             != ((bars["candle_range_ratio"] >= candle_range_ratio) & real).astype("int8")).sum()
        ),
        "invalid_staleness_metadata": int(
            ((bars["stale_bars"] < 0) | (bars["age_bars"] < 0)).sum()
            + ((bars["stale_bars"] != 0) & real).sum()
            + ((bars["stale_bars"] == 0) & ~real).sum()
        ),
        "execution_coverage_mismatch": int(
            (~np.isclose(
                bars["execution_coverage"],
                (bars["execution_valid_events"] / bars["trades"].replace(0, np.nan)).fillna(0.0),
                rtol=1e-9, atol=1e-12, equal_nan=True,
            )).sum()
        ),
        "execution_completeness_mismatch": int(
            (bars["execution_ohlc_complete"]
             != ((bars["execution_invalid_events"] == 0) & real).astype("int8")).sum()
        ),
        "execution_component_accounting_mismatch": int(
            (bars["execution_component_count"]
             != bars["buy_execution_components"] + bars["sell_execution_components"]).sum()
        ),
        "invalid_execution_ohlc": int(
            sum(
                (
                    ((bars[f"{side}_execution_components"] > 0) & bars["execution_ohlc_complete"].eq(1))
                    & (
                        ~np.isfinite(bars[f"{side}_execution_open"])
                        | ~np.isfinite(bars[f"{side}_execution_high"])
                        | ~np.isfinite(bars[f"{side}_execution_low"])
                        | ~np.isfinite(bars[f"{side}_execution_close"])
                        | (bars[f"{side}_execution_open"] <= 0)
                        | (bars[f"{side}_execution_high"] <= 0)
                        | (bars[f"{side}_execution_low"] <= 0)
                        | (bars[f"{side}_execution_close"] <= 0)
                        | (bars[f"{side}_execution_high"] < np.maximum(
                            bars[f"{side}_execution_open"], bars[f"{side}_execution_close"]
                        ))
                        | (bars[f"{side}_execution_low"] > np.minimum(
                            bars[f"{side}_execution_open"], bars[f"{side}_execution_close"]
                        ))
                    )
                ).sum()
                for side in ("buy", "sell")
            )
        ),
        "execution_prices_on_filled_bars": int(
            (bars[
                [
                    "buy_execution_open", "buy_execution_high", "buy_execution_low", "buy_execution_close",
                    "sell_execution_open", "sell_execution_high", "sell_execution_low", "sell_execution_close",
                ]
            ].notna().any(axis=1) & ~real).sum()
        ),
    }
    checks["passed"] = all(value == 0 for key, value in checks.items() if key != "passed")
    return checks


# ----------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="raw replay .jsonl.zst -> clean OHLCV CSV (VectorBT)")
    ap.add_argument("inputs", nargs="+", help="Input files or glob patterns, ordered by time")
    ap.add_argument("-o", "--outdir", default="ohlcv_clean")
    ap.add_argument("-t", "--timeframes", default="1s,5s,15s,1m")
    ap.add_argument("--fill", action="store_true", help="Fill empty candles (is_filled=1)")
    ap.add_argument("--open-prev-close", action="store_true", help="Use the previous close as the next candle open")
    ap.add_argument("--wsol-only", action="store_true", help="Keep only Wrapped SOL quote trades")
    ap.add_argument("--min-trades", type=int, default=1)
    ap.add_argument("--jump-ratio", type=float, default=5.0)
    ap.add_argument(
        "--discontinuity-ratio",
        type=float,
        default=5.0,
        help="Flag a candle when its first trade price differs from previous observed close by this symmetric ratio (default: 5)",
    )
    ap.add_argument(
        "--price-consistency-ratio",
        type=float,
        default=5.0,
        help=(
            "Flag a selected-pool event when priceSol and quote-implied execution "
            "price differ by this symmetric ratio (default: 5; audit only)"
        ),
    )
    ap.add_argument(
        "--pool-depletion-ratio", type=float, default=0.05,
        help="Flag possible pool-side depletion when post-event reserve / net trade amount is at or below this ratio (default: 0.05; audit only)",
    )
    ap.add_argument(
        "--breakdown-reconciliation-tolerance", type=float, default=0.02,
        help="Relative tolerance for reconciling top-level event net amounts with breakdown net amounts (default: 0.02)",
    )
    ap.add_argument(
        "--candle-range-ratio", type=float, default=10.0,
        help="Flag a real candle when spot high / spot low is at least this ratio (default: 10; audit only)",
    )
    ap.add_argument("--drop-jumps", action="store_true")
    ap.add_argument("--bundle-ms", type=int, default=3)
    ap.add_argument(
        "--batch-events",
        type=int,
        default=1_500_000,
        help=(
            "Process at most this many events per in-memory batch, splitting the "
            "input by mint when a day is larger. Keeps peak memory bounded on "
            "busy days. 0 disables batching."
        ),
    )
    args = ap.parse_args()

    if not np.isfinite(args.discontinuity_ratio) or args.discontinuity_ratio <= 1:
        ap.error("--discontinuity-ratio must be a finite number greater than 1")
    if not np.isfinite(args.price_consistency_ratio) or args.price_consistency_ratio <= 1:
        ap.error("--price-consistency-ratio must be a finite number greater than 1")
    if not np.isfinite(args.pool_depletion_ratio) or not 0 < args.pool_depletion_ratio < 1:
        ap.error("--pool-depletion-ratio must be between 0 and 1")
    if not np.isfinite(args.breakdown_reconciliation_tolerance) or not 0 <= args.breakdown_reconciliation_tolerance < 1:
        ap.error("--breakdown-reconciliation-tolerance must be between 0 and 1")
    if not np.isfinite(args.candle_range_ratio) or args.candle_range_ratio <= 1:
        ap.error("--candle-range-ratio must be a finite number greater than 1")

    paths = []
    for p in args.inputs:
        m = sorted(glob.glob(p, recursive=True))
        paths += m if m else [p]
    for p in paths:
        if not os.path.isfile(p):
            sys.exit(f"Input file not found: {p}")

    os.makedirs(args.outdir, exist_ok=True)
    stats = {"lines_scanned": 0, "bad_json": 0}
    tf_list = [x.strip() for x in args.timeframes.split(",") if x.strip()]
    tf_frames: dict[str, list] = {tf: [] for tf in tf_list}
    dropped_frames: list = []
    zero_quote_frames: list = []
    invalid_quote_frames: list = []
    zq_cols = [
        "event_index", "signature", "action", "mint", "pool_id", "quote_mint",
        "timestamp_ms", "token_amount", "quote_amount", "price_sol", "n_breakdown",
        "invalid_quote_reason", "breakdown_json",
    ]
    invalid_quote_cols = [
        "event_index", "signature", "action", "mint", "pool_id", "quote_mint",
        "timestamp_ms", "token_amount", "quote_amount", "quote_price_sol",
        "price_sol", "n_breakdown", "breakdown_json", "invalid_quote_reason",
        "breakdown_invalid_components",
    ]

    # A single day of filtered replay can hold several million events. Building
    # every column for all of them at once needs more memory than a laptop has,
    # so large inputs are split by mint: mints are independent (except for the
    # signature multiplicity, which the scan resolves globally), so the merged
    # result is identical to a single-pass run.
    plan = None
    batch_mints: list[list[str]] = []
    if len(paths) == 1 and args.batch_events > 0:
        candidate = _scan_events(paths[0])
        if candidate["events"] > args.batch_events:
            plan = candidate
            batch_mints = _pack_batches(candidate["per_mint"], args.batch_events)
            stats["lines_scanned"] = candidate["lines_scanned"]
            stats["bad_json"] = candidate["bad_json"]
            print(
                f"[read] {candidate['events']:,} events -> {len(batch_mints)} "
                f"batches of <= {args.batch_events:,} events",
                file=sys.stderr,
            )
        del candidate

    if plan is not None:
        sources = [
            (paths[0], mints, plan["per_mint"], plan["signature_events"])
            for mints in batch_mints
        ]
    else:
        sources = [(paths, None, None, None)]

    idx = 0
    total_rows = 0
    for batch_no, (source_paths, mints, index_lookup, signature_events) in enumerate(
        sources, 1
    ):
        if mints is None:
            frames, idx = [], 0
            for p in source_paths:
                frame, idx = read_frame(p, idx, stats)
                frames.append(frame)
                print(f"[read] {p}: {len(frame):,} buy/sell rows", file=sys.stderr)
            df = frames[0] if len(frames) == 1 else pd.concat(frames, ignore_index=True)
            del frames
        else:
            parse_stats = {"bad_json": 0}
            df, _ = read_frame_subset(source_paths, parse_stats, mints, index_lookup)
            stats["bad_json"] += parse_stats["bad_json"]
            df["signature_events"] = signature_events[
                df["event_index"].to_numpy(dtype="int64") - 1
            ]
            print(
                f"[read] batch {batch_no}/{len(sources)}: {len(df):,} rows, "
                f"{len(mints)} mints",
                file=sys.stderr,
            )
        total_rows += len(df)

        batch_stats = stats if plan is None else {}
        df, dropped = clean_trades(df, args, batch_stats, copy_frame=False)
        if plan is not None:
            _merge_cleaning_stats(stats, batch_stats)
        if df.empty:
            print(f"[clean] batch {batch_no}: no events remain", file=sys.stderr)
            del df
            continue

        dropped_frames.append(dropped)
        zero_quote_frames.append(df.loc[df["zero_quote"] == 1, zq_cols].copy())
        invalid_quote_frames.append(
            df.loc[df["invalid_quote"] == 1, invalid_quote_cols].copy()
        )

        for tf in tf_list:
            tf_ms = parse_tf(tf)
            bars, selected_events = build_bars(
                df,
                tf_ms,
                args.open_prev_close,
                args.discontinuity_ratio,
                return_selected=True,
                candle_range_ratio=args.candle_range_ratio,
            )
            if args.fill:
                bars = fill_gaps(bars, tf_ms, args.candle_range_ratio)
            tf_frames[tf].append((bars, selected_events))
        del df
        gc.collect()

    stats["raw_buy_sell_rows"] = total_rows
    if not total_rows or not dropped_frames:
        sys.exit("No buy/sell events were found in the input files.")
    if plan is not None and len(batch_mints) > 1:
        stats["batched"] = {
            "events": total_rows,
            "batches": len(batch_mints),
            "limit": int(args.batch_events),
        }
    print(f"[clean] {stats}", file=sys.stderr)

    # Write audit files for manual inspection.
    if len(dropped_frames) == 1:
        dropped_frames[0].to_csv(
            os.path.join(args.outdir, "dropped_invalid.csv"), index=False
        )
    else:
        pd.concat(dropped_frames, ignore_index=True).to_csv(
            os.path.join(args.outdir, "dropped_invalid.csv"), index=False
        )
    pd.concat(zero_quote_frames, ignore_index=True).to_csv(
        os.path.join(args.outdir, "flagged_zero_quote.csv"), index=False
    )
    pd.concat(invalid_quote_frames, ignore_index=True).to_csv(
        os.path.join(args.outdir, "flagged_invalid_quote.csv"), index=False
    )
    del dropped_frames, zero_quote_frames, invalid_quote_frames
    gc.collect()

    report = {
        "version": "v6.2.2",
        "inputs": paths,
        "cleaning": stats,
        "execution_ohlc_policy": {
            "spot_ohlc": "open/high/low/close and spot_open/spot_high/spot_low/spot_close are pool spot prices from priceSol",
            "buy_execution_ohlc": "directional OHLC built from per-event weighted average BUY execution prices; multi-breakdown component prices are aggregated within each event in breakdown array order",
            "sell_execution_ohlc": "directional OHLC built from per-event weighted average SELL execution prices; multi-breakdown component prices are aggregated within each event in breakdown array order",
            "execution_ohlc_complete": "true only when every selected event in the candle has a complete valid execution breakdown/top-level quote; partial side OHLC remains diagnostic only",
            "limitation": "historical component execution prices are observed fills, not a simulated fill for a new order size; pool depth/slippage must still be modeled for strategy execution",
        },
        "candle_range_policy": {
            "metric": "spot_high / spot_low",
            "threshold_ratio": args.candle_range_ratio,
            "action": "audit flag only; does not modify or drop OHLC values",
        },
        "staleness_policy": {
            "stale_bars": "zero on a real candle; consecutive number of filled bars since the last real trade candle",
            "age_bars": "number of bars since the mint first appears in the output grid",
            "filled_price_usage": "carried spot price is a mark only; build_vbt_dataset controls validity using --max-stale-bars; execution OHLC is never forward-filled",
        },
        "pool_selection": {
            "policy": "highest_valid_gross_sol_quote_volume_per_mint_bucket",
            "scope": "completed current bucket only; no later buckets are used",
            "execution_note": "The selected pool is known only after the bucket closes; do not use it as if it were known at the bucket open.",
            "reserve_metadata": "open/close/prev-close tokens_in_pool, quote_in_pool, virtual_tokens_in_pool and virtual_quote_in_pool are emitted so downstream code can identify the pool reserve model; they do not change OHLCV, VWAP, or execution-price math",
        },
        "volume_policy": {
            "multi_breakdown": "use gross sums of valid BUY and SELL components; never use top-level net amounts as gross volume",
            "incomplete_breakdown": "exclude the event from quote/token volume and execution-price calculations, but retain the event and raw breakdown in audit output",
            "single_or_missing_breakdown": "use positive validated top-level quote and token amounts; source is explicitly labelled",
            "volume_token": "gross BUY token amount + gross SELL token amount",
            "volume_sol": "gross BUY quote SOL + gross SELL quote SOL",
            "buy_sell_fields": "gross direction-specific component volume; trades/buys/sells remain top-level event counts for backward compatibility",
        },
        "vwap_policy": {
            "metric": "gross component-aware execution VWAP",
            "formula": "sum(valid gross component quote volume converted to SOL) / sum(valid gross component token amount)",
            "multi_breakdown_policy": "include all components only when the complete breakdown is present and every component has valid action, positive token amount, and positive convertible quote amount",
            "fallback_policy": "single/no-breakdown events use their top-level amount and are labelled in execution_price_source",
            "vwap_sol": "alias of vwap_gross_execution_sol; retained for build_vbt_dataset.py compatibility",
        },
        "breakdown_policy": {
            "net_reconciliation": "compare top-level event amounts with absolute signed BUY-minus-SELL component net; default relative tolerance is 2%",
            "netting_flag": "set when at least one BUY and one SELL component occur in the same event",
            "top_level_action_direction": "stored as a separate diagnostic; the top-level action is not assumed to equal the sign of the multi-party BUY-minus-SELL net",
            "zero_net_policy": "keep a complete multi-breakdown event even when its top-level net token or quote amount is zero; use gross components for volume and VWAP",
            "incomplete_policy": "retain and flag; do not infer missing components from top-level net amount",
            "tolerance": args.breakdown_reconciliation_tolerance,
        },
        "price_consistency_policy": {
            "comparison": "priceSol (post-event pool spot) versus component-aware gross execution price",
            "execution_price": "gross component quote SOL divided by gross component token amount; top-level fallback only for single/no-breakdown events",
            "pool_spot_vs_execution_ratio": "symmetric max(spot/execution, execution/spot), always >= 1",
            "threshold_ratio": args.price_consistency_ratio,
            "action": "audit only; no price correction and no automatic trade deletion",
        },
        "pool_depletion_policy": {
            "threshold_ratio": args.pool_depletion_ratio,
            "buy_side": "post-event (tokensInPool + virtualTokensInPool) / absolute net token amount; if virtual reserve is absent, use tokensInPool",
            "sell_side": "post-event quoteInPool / absolute net quote amount in quote-mint units",
            "interpretation": "candidate indicator only; reserve fields may be post-event snapshots and do not independently prove execution feasibility",
            "classification": "large spot/execution gap with low relevant reserve is a pool-depletion/price-impact candidate; with sufficient reserve it is a data-anomaly candidate; without valid reserve evidence it is unclassified",
        },
        "balance_audit_policy": {
            "metric": "sum of participant quote balance deltas minus expected net quote balance change",
            "trigger": f"inspect multi-breakdown events and single/fallback events with top-level spot/execution ratio >= {BALANCE_AUDIT_TRIGGER_RATIO}",
            "warning": "diagnostic only; transaction fees, rent, native SOL/WSOL wrapping, and unrelated instructions can create residuals",
        },
        "timeframes": {},
    }
    failed = False
    for tf in tf_list:
        batch_frames = tf_frames.pop(tf)
        if not batch_frames:
            continue
        if len(batch_frames) == 1:
            bars, selected_events = batch_frames[0]
        else:
            bars = pd.concat(
                [frame for frame, _ in batch_frames], ignore_index=True
            )
            selected_events = pd.concat(
                [frame for _, frame in batch_frames], ignore_index=True
            )
        del batch_frames
        # Keep serialization fast and avoid fragmented-frame warnings after
        # the wide event-audit columns have been assembled.
        bars = bars.copy()
        bars["datetime_utc"] = pd.to_datetime(bars["timestamp_ms"], unit="ms").dt.strftime("%Y-%m-%d %H:%M:%S")
        bars = bars[FINAL_COLS].sort_values(["mint", "timestamp_ms"]).reset_index(drop=True)

        chk = validate(
            bars, args.discontinuity_ratio, args.price_consistency_ratio, args.candle_range_ratio
        )
        out = os.path.join(args.outdir, f"ohlcv_{tf}.csv")
        bars.to_csv(out, index=False, float_format="%.12g")
        real_bars = bars.loc[bars["is_filled"] == 0]
        discontinuities = real_bars.loc[real_bars["price_discontinuity_flag"] == 1].copy()
        discontinuity_cols = [
            "mint", "datetime_utc", "timestamp_ms", "prev_close_ts_ms",
            "price_gap_elapsed_ms", "prev_close", "observed_open", "close",
            "pool_id", "pool_key", "pool_changed", "price_discontinuity_ratio",
            "price_discontinuity_flag", "pool_switch_discontinuity_flag",
        ] + OPEN_EVENT_COLS + CLOSE_EVENT_COLS + PREV_CLOSE_EVENT_COLS
        discontinuity_path = os.path.join(args.outdir, f"flagged_price_discontinuity_{tf}.csv")
        discontinuities[discontinuity_cols].to_csv(
            discontinuity_path, index=False, float_format="%.12g"
        )

        range_anomalies = real_bars.loc[real_bars["candle_range_anomaly_flag"] == 1].copy()
        range_audit_cols = [
            "mint", "datetime_utc", "timestamp_ms", "spot_open", "spot_high",
            "spot_low", "spot_close", "candle_range_ratio", "candle_range_anomaly_flag",
            "pool_id", "pool_key", "pool_changed", "trades", "volume_sol",
            "price_discontinuity_flag", "pool_switch_discontinuity_flag",
            "price_impact_candidate_flag", "data_anomaly_candidate_flag",
            "price_gap_unclassified_flag",
        ] + OPEN_EVENT_COLS + CLOSE_EVENT_COLS
        range_path = os.path.join(args.outdir, f"flagged_candle_range_anomaly_{tf}.csv")
        range_anomalies[range_audit_cols].to_csv(range_path, index=False, float_format="%.12g")

        consistency_events = selected_events.loc[
            selected_events["price_consistency_flag"] == 1
        ].copy()
        consistency_cols = [
            "event_index", "signature", "action", "mint", "pool_id", "pool_key",
            "timestamp_ms", "block", "token_amount", "quote_amount", "quote_mint",
            "quote_price_sol", "n_breakdown", "breakdown_json", "price_sol", "implied_price_sol",
            "execution_price_source", "execution_price_sol", "price_consistency_ratio",
            "pool_spot_vs_execution_ratio", "pool_spot_vs_buy_execution_ratio",
            "pool_spot_vs_sell_execution_ratio",
            "top_level_net_implied_price_sol", "raw_to_implied_ratio",
            "gross_buy_token_amount", "gross_sell_token_amount",
            "gross_buy_quote_sol", "gross_sell_quote_sol", "breakdown_netting_flag",
            "breakdown_net_reconciles_event", "breakdown_net_mismatch_flag",
            "tokens_in_pool", "quote_in_pool", "virtual_tokens_in_pool", "effective_tokens_in_pool",
            "pool_side_reserve_ratio", "pool_depletion_flag",
            "price_impact_candidate_flag", "data_anomaly_candidate_flag",
            "price_gap_unclassified_flag", "participant_quote_balance_delta_sol",
            "participant_quote_balance_delta_residual_sol", "participant_balance_checked",
            "participant_balance_available", "invalid_quote_reason",
        ]
        consistency_path = os.path.join(args.outdir, f"flagged_price_consistency_{tf}.csv")
        consistency_events[consistency_cols].to_csv(
            consistency_path, index=False, float_format="%.12g"
        )

        execution_audit_mask = (
            (selected_events["price_consistency_flag"] == 1)
            | (selected_events["breakdown_net_mismatch_flag"] == 1)
            | (selected_events["netting_explains_price_gap_flag"] == 1)
            | (selected_events["pool_depletion_flag"] == 1)
        )
        execution_audit_events = selected_events.loc[execution_audit_mask].copy()
        execution_audit_cols = [
            "event_index", "signature", "action", "mint", "pool_id", "pool_key", "pool",
            "timestamp_ms", "block", "quote_mint", "quote_price_sol", "decimals",
            "token_amount", "quote_amount", "n_breakdown", "breakdown_json",
            "breakdown_source", "breakdown_valid", "breakdown_valid_components",
            "breakdown_invalid_components", "breakdown_buy_count", "breakdown_sell_count",
            "breakdown_netting_flag", "breakdown_net_zero_flag",
            "gross_buy_token_amount", "gross_sell_token_amount",
            "gross_buy_quote_amount", "gross_sell_quote_amount",
            "gross_buy_quote_sol", "gross_sell_quote_sol", "gross_token_amount",
            "gross_quote_volume_sol", "breakdown_net_token_amount", "breakdown_net_quote_sol",
            "breakdown_net_quote_amount", "breakdown_net_reconciles_event",
            "breakdown_net_direction_matches_event", "breakdown_net_mismatch_flag",
            "price_sol", "implied_price_sol", "top_level_net_implied_price_sol",
            "execution_price_source", "execution_price_sol", "breakdown_buy_execution_price_sol",
            "breakdown_sell_execution_price_sol", "breakdown_gross_execution_vwap_sol",
            "pool_spot_vs_execution_ratio", "price_consistency_flag",
            "tokens_in_pool", "quote_in_pool", "virtual_tokens_in_pool", "effective_tokens_in_pool", "burned_liquidity",
            "pool_created_by", "pool_side_reserve_ratio", "pool_depletion_flag",
            "price_impact_candidate_flag", "data_anomaly_candidate_flag",
            "price_gap_unclassified_flag", "netting_explains_price_gap_flag",
            "participant_quote_balance_delta_sol",
            "participant_quote_balance_delta_residual_sol", "participant_balance_available",
            "participant_balance_checked",
        ]
        execution_audit_path = os.path.join(args.outdir, f"flagged_execution_audit_{tf}.csv")
        execution_audit_events[execution_audit_cols].to_csv(
            execution_audit_path, index=False, float_format="%.12g"
        )

        depletion_events = selected_events.loc[selected_events["pool_depletion_flag"] == 1].copy()
        depletion_path = os.path.join(args.outdir, f"flagged_pool_depletion_{tf}.csv")
        depletion_events[execution_audit_cols].to_csv(
            depletion_path, index=False, float_format="%.12g"
        )
        mismatch_events = selected_events.loc[selected_events["breakdown_net_mismatch_flag"] == 1].copy()
        mismatch_path = os.path.join(args.outdir, f"flagged_breakdown_net_mismatch_{tf}.csv")
        mismatch_events[execution_audit_cols].to_csv(
            mismatch_path, index=False, float_format="%.12g"
        )

        comparison_ratios = selected_events.loc[
            selected_events["price_comparable"] == 1, "pool_spot_vs_execution_ratio"
        ]
        comparison_count = int(len(comparison_ratios))
        consistency_flag_count = int(selected_events["price_consistency_flag"].sum())
        raw_above_execution = int((selected_events.loc[
            selected_events["price_comparable"] == 1, "raw_to_implied_ratio"
        ] > 1.0).sum())
        raw_below_execution = int((selected_events.loc[
            selected_events["price_comparable"] == 1, "raw_to_implied_ratio"
        ] < 1.0).sum())
        quote_event_total = int(real_bars["valid_quote_trades"].sum())
        quote_component_total = int(real_bars["valid_quote_components"].sum())
        vwap_event_total = int(real_bars["vwap_trades"].sum())
        vwap_component_total = int(real_bars["vwap_components"].sum())
        report["timeframes"][tf] = {
            "file": out,
            "price_discontinuity_file": discontinuity_path,
            "price_consistency_file": consistency_path,
            "execution_audit_file": execution_audit_path,
            "pool_depletion_file": depletion_path,
            "breakdown_net_mismatch_file": mismatch_path,
            "candle_range_anomaly_file": range_path,
            "rows": int(len(bars)),
            "mints": int(bars["mint"].nunique()),
            "filled_rows": int(bars["is_filled"].sum()),
            "execution_ohlc_complete_real_bars": int((real_bars["execution_ohlc_complete"] == 1).sum()),
            "execution_ohlc_incomplete_real_bars": int((real_bars["execution_ohlc_complete"] == 0).sum()),
            "execution_coverage_mean": float(real_bars["execution_coverage"].mean()) if real_bars["execution_coverage"].notna().any() else None,
            "buy_execution_bars": int((real_bars["buy_execution_components"] > 0).sum()),
            "sell_execution_bars": int((real_bars["sell_execution_components"] > 0).sum()),
            "candle_range_anomaly_bars": int(len(range_anomalies)),
            "max_candle_range_ratio": float(real_bars["candle_range_ratio"].max()) if not real_bars.empty else None,
            "multi_pool_bars": int((real_bars["pool_count"] > 1).sum()),
            "pool_switches": int(real_bars["pool_changed"].sum()),
            "pool_excluded_trades": int(real_bars["pool_excluded_trades"].sum()),
            "selected_pool_missing_id_trades": int(real_bars["selected_pool_missing_id_trades"].sum()),
            "selected_pool_trade_share": (
                float(real_bars["selected_pool_trade_share"].mean())
                if real_bars["selected_pool_trade_share"].notna().any() else None
            ),
            "selected_pool_volume_share": (
                float(real_bars["selected_pool_volume_share"].mean())
                if real_bars["selected_pool_volume_share"].notna().any() else None
            ),
            "pool_volume_share_roundoff_clipped_bars": int(
                real_bars["pool_volume_share_roundoff_clipped"].sum()
            ),
            "valid_quote_events": quote_event_total,
            "valid_quote_components": quote_component_total,
            "vwap_events": vwap_event_total,
            "vwap_components": vwap_component_total,
            "vwap_event_coverage": float(vwap_event_total / quote_event_total) if quote_event_total else None,
            "vwap_component_coverage": float(vwap_component_total / quote_component_total) if quote_component_total else None,
            "vwap_gross_execution_sol": (
                float(real_bars["vwap_sol_volume"].sum(min_count=1) / real_bars["vwap_token_volume"].sum(min_count=1))
                if real_bars["vwap_token_volume"].sum(min_count=1) > 0 else None
            ),
            "gross_buy_volume_token": float(real_bars["buy_volume_token"].sum(min_count=1)) if real_bars["buy_volume_token"].notna().any() else None,
            "gross_sell_volume_token": float(real_bars["sell_volume_token"].sum(min_count=1)) if real_bars["sell_volume_token"].notna().any() else None,
            "gross_buy_volume_sol": float(real_bars["buy_volume_sol"].sum(min_count=1)) if real_bars["buy_volume_sol"].notna().any() else None,
            "gross_sell_volume_sol": float(real_bars["sell_volume_sol"].sum(min_count=1)) if real_bars["sell_volume_sol"].notna().any() else None,
            "multi_breakdown_selected_events": int((selected_events["n_breakdown"] > 1).sum()),
            "valid_multi_breakdown_selected_events": int(((selected_events["n_breakdown"] > 1) & (selected_events["breakdown_valid"] == 1)).sum()),
            "netted_multi_breakdown_selected_events": int(((selected_events["n_breakdown"] > 1) & (selected_events["breakdown_netting_flag"] == 1)).sum()),
            "reconciled_multi_breakdown_selected_events": int(((selected_events["n_breakdown"] > 1) & (selected_events["breakdown_net_reconciles_event"] == 1)).sum()),
            "breakdown_net_mismatch_events": int(selected_events["breakdown_net_mismatch_flag"].sum()),
            "netting_explains_old_net_price_gap_events": int(selected_events["netting_explains_price_gap_flag"].sum()),
            "price_discontinuity_bars": int(len(discontinuities)),
            "pool_switch_discontinuity_bars": int(real_bars["pool_switch_discontinuity_flag"].sum()),
            "same_pool_discontinuity_bars": int(((real_bars["price_discontinuity_flag"] == 1) & (real_bars["pool_changed"] == 0)).sum()),
            "max_price_discontinuity_ratio": float(discontinuities["price_discontinuity_ratio"].max()) if not discontinuities.empty else None,
            "price_comparison_events": comparison_count,
            "pool_spot_execution_gap_events": consistency_flag_count,
            "price_consistency_flagged_events": consistency_flag_count,
            "pool_spot_execution_gap_share": float(consistency_flag_count / comparison_count) if comparison_count else None,
            "raw_price_above_execution_events": raw_above_execution,
            "raw_price_below_execution_events": raw_below_execution,
            "pool_depletion_events": int(selected_events["pool_depletion_flag"].sum()),
            "price_impact_candidates": int(selected_events["price_impact_candidate_flag"].sum()),
            "data_anomaly_candidates": int(selected_events["data_anomaly_candidate_flag"].sum()),
            "unclassified_price_gap_events": int(selected_events["price_gap_unclassified_flag"].sum()),
            "participant_balance_diagnostics_checked": int(selected_events["participant_balance_checked"].sum()),
            "participant_balance_diagnostics_available": int(selected_events["participant_balance_available"].sum()),
            "participant_balance_residual_abs_median_sol": (
                float(selected_events["participant_quote_balance_delta_residual_sol"].abs().median())
                if selected_events["participant_quote_balance_delta_residual_sol"].notna().any() else None
            ),
            "pool_spot_execution_ratio_median": float(comparison_ratios.median()) if comparison_count else None,
            "pool_spot_execution_ratio_p90": float(comparison_ratios.quantile(0.90)) if comparison_count else None,
            "pool_spot_execution_ratio_p99": float(comparison_ratios.quantile(0.99)) if comparison_count else None,
            "pool_spot_execution_ratio_max": float(comparison_ratios.max()) if comparison_count else None,
            "validation": chk,
        }
        status = "ok" if chk["passed"] else "fail"
        print(f"[{status}] {tf}: {len(bars):,} rows -> {out} | validation passed={chk['passed']}", file=sys.stderr)
        failed |= not chk["passed"]

    with open(os.path.join(args.outdir, "cleaning_report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    if failed:
        sys.exit("OHLCV validation failed. Check cleaning_report.json.")


if __name__ == "__main__":
    main()
