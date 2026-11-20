"""Loading and normalising the validated per-day replay events.

The source is ``output/backtest/days/<day>/parquet/events.parquet``: one row per
decoded pool event with the fields needed for volume, wallet and liquidity
features.  Nothing here mutates the archives; the file is only read.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from .config import DATASET_ROOT

EVENT_COLUMNS: tuple[str, ...] = (
    "timestamp_ms",
    "mint",
    "pool",
    "pool_id",
    "action",
    "quote_mint",
    "signature",
    "tx_signer",
    "price_sol",
    "market_cap_sol",
    "quote_amount",
    "token_amount",
    "supply",
    "tokens_in_pool",
    "quote_in_pool",
    "v_tokens_in_bonding_curve",
    "v_quote_in_bonding_curve",
    "pool_fee_rate",
    "leg_count",
    "leg_traders",
)

TRADE_ACTIONS = frozenset({"buy", "sell"})
LIQUIDITY_ACTIONS = frozenset({"add", "remove"})
NUMERIC_COLUMNS: tuple[str, ...] = (
    "price_sol",
    "market_cap_sol",
    "quote_amount",
    "token_amount",
    "supply",
    "tokens_in_pool",
    "quote_in_pool",
    "v_tokens_in_bonding_curve",
    "v_quote_in_bonding_curve",
    "pool_fee_rate",
    "leg_count",
)


class FeatureDataError(RuntimeError):
    """Raised when a required archive is missing or unusable."""


@dataclass(slots=True)
class EventData:
    day: str
    path: Path
    frame: pd.DataFrame
    fingerprint: dict[str, Any] = field(default_factory=dict)
    quality: dict[str, Any] = field(default_factory=dict)


def event_path(day: str, repo_root: Path) -> Path:
    return repo_root / DATASET_ROOT / day / "parquet" / "events.parquet"


def file_fingerprint(path: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return {
        "path": str(path),
        "sha256": digest.hexdigest(),
        "bytes": size,
        "modified_ns": path.stat().st_mtime_ns,
    }


def _as_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or (isinstance(value, float) and np.isnan(value)):
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def load_events(
    day: str,
    *,
    repo_root: Path,
    path: Path | None = None,
    mints: list[str] | None = None,
) -> EventData:
    """Read one day of events and normalise dtypes and wallet lists."""

    parquet_path = Path(path) if path is not None else event_path(day, repo_root)
    if not parquet_path.exists():
        raise FeatureDataError(f"Missing event archive: {parquet_path}")

    available = set(pq.ParquetFile(parquet_path).schema_arrow.names)
    columns = [name for name in EVENT_COLUMNS if name in available]
    table = pq.read_table(parquet_path, columns=columns)
    frame = table.to_pandas()
    if frame.empty:
        raise FeatureDataError(f"Event archive is empty: {parquet_path}")

    frame["mint"] = frame["mint"].astype(str)
    frame["timestamp_ms"] = pd.to_numeric(
        frame["timestamp_ms"], errors="coerce"
    ).astype("int64")
    frame["action"] = frame["action"].astype(str).str.lower()
    frame["signature"] = frame.get("signature", "").astype(str)
    frame["pool"] = frame.get("pool", "unknown").astype(str)
    frame["pool_id"] = frame.get("pool_id", "").astype(str)
    frame["tx_signer"] = frame.get("tx_signer", "").astype(str)
    for name in NUMERIC_COLUMNS:
        if name in frame:
            frame[name] = pd.to_numeric(frame[name], errors="coerce")
    # ``leg_traders`` in the aggregate parquet is a *count* of traders in the
    # transaction, not a list of wallet addresses.  The only wallet address the
    # archive stores is ``tx_signer``; wallet features therefore use the signer
    # and treat multi-leg transactions as a single participant.  Exact per-wallet
    # legs would require re-parsing the JSONL ``breakdown`` field.
    if "leg_traders" not in frame:
        frame["leg_traders"] = 0
    frame["leg_traders"] = frame["leg_traders"].map(_as_int)

    frame = frame.dropna(subset=["timestamp_ms", "mint"])
    frame = frame[frame["mint"].str.len() > 0]
    if mints is not None:
        frame = frame[frame["mint"].isin(set(mints))]
    frame = frame.sort_values(
        ["mint", "timestamp_ms", "signature"], kind="stable"
    ).reset_index(drop=True)

    quality = {
        "rows": int(len(frame)),
        "mints": int(frame["mint"].nunique()),
        "first_timestamp_ms": int(frame["timestamp_ms"].min()),
        "last_timestamp_ms": int(frame["timestamp_ms"].max()),
        "action_counts": {
            str(key): int(value)
            for key, value in frame["action"].value_counts().sort_index().items()
        },
        "rows_without_signers": int(
            (
                frame["action"].isin(TRADE_ACTIONS)
                & (frame["tx_signer"].str.len() == 0)
            ).sum()
        ),
        "multi_trader_events": int(
            ((frame["leg_traders"] > 1) & frame["action"].isin(TRADE_ACTIONS)).sum()
        ),
        "rows_without_pool_id": int((frame["pool_id"].str.len() == 0).sum()),
    }
    return EventData(
        day=day,
        path=parquet_path,
        frame=frame,
        fingerprint=file_fingerprint(parquet_path),
        quality=quality,
    )


def explode_wallets(events: pd.DataFrame) -> pd.DataFrame:
    """One row per trade event, attributed to its transaction signer.

    The aggregate parquet stores ``leg_traders`` as a count, not as addresses,
    so the signer is the only wallet identity available without re-parsing the
    raw JSONL.  For normal Pump trades the signer is the trader; bundled
    multi-leg transactions (well under 1 % of events) are counted once.
    """

    columns = [
        "mint",
        "timestamp_ms",
        "bucket_start_ms",
        "signature",
        "action",
        "wallet",
        "wallet_quote_sol",
        "wallet_token",
    ]
    trades = events[events["action"].isin(TRADE_ACTIONS)].copy()
    if trades.empty:
        return pd.DataFrame(columns=columns)
    trades = trades[trades["tx_signer"].str.len() > 0]
    if trades.empty:
        return pd.DataFrame(columns=columns)
    trades["wallet"] = trades["tx_signer"].astype(str)
    trades["wallet_quote_sol"] = trades["quote_amount"].fillna(0.0)
    trades["wallet_token"] = trades["token_amount"].fillna(0.0)
    return trades[columns].reset_index(drop=True)


def add_bucket(frame: pd.DataFrame, bar_ms: int) -> pd.DataFrame:
    """Attach the bar bucket a timestamp belongs to."""

    out = frame.copy()
    out["bucket_start_ms"] = (
        out["timestamp_ms"] // bar_ms
    ) * bar_ms
    return out
