#!/usr/bin/env python3
"""Historical snapshot-based execution simulator for PumpAPI JSONL events."""

from __future__ import annotations

import argparse
import csv
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterator

WSOL_MINT = "So11111111111111111111111111111111111111112"
RELEVANT_ACTIONS = {"buy", "sell", "create", "createPool", "migrate", "add", "remove"}


def _to_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _loads(line: bytes) -> dict[str, Any] | None:
    try:
        try:
            import orjson

            value = orjson.loads(line)
        except ImportError:
            value = json.loads(line)
    except (ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def iter_jsonl_events(path: Path) -> Iterator[dict[str, Any]]:
    """Stream JSONL or Zstandard-compressed JSONL events from disk."""
    if not path.exists():
        raise FileNotFoundError(path)

    if path.name.endswith(".zst"):
        try:
            import zstandard as zstd
        except ImportError as exc:
            raise RuntimeError(
                "Reading .zst files requires zstandard. Install it with: uv add zstandard"
            ) from exc

        with path.open("rb") as source:
            reader = zstd.ZstdDecompressor().stream_reader(
                source,
                read_across_frames=True,
            )
            import io

            for line in io.BufferedReader(reader, buffer_size=1 << 20):
                event = _loads(line)
                if event is not None:
                    yield event
        return

    with path.open("rb") as source:
        for line in source:
            event = _loads(line)
            if event is not None:
                yield event


@dataclass(slots=True)
class PoolSnapshot:
    timestamp_ms: int
    mint: str
    pool_id: str
    pool: str
    quote_mint: str
    token_reserve: float | None
    quote_reserve: float | None
    fee_rate: float | None
    signature: str | None
    action: str
    reserve_model: str | None = None
    unsupported_reason: str | None = None

    @property
    def key(self) -> tuple[str, str]:
        return self.mint, self.pool_id


def snapshot_from_event(event: dict[str, Any]) -> PoolSnapshot | None:
    """Extract post-event pool state from a PumpAPI event."""
    action = str(event.get("action") or "")
    if action not in RELEVANT_ACTIONS:
        return None

    mint = str(event.get("mint") or "").strip()
    pool_id = str(event.get("poolId") or "").strip()
    pool = str(event.get("pool") or "").strip().lower()
    quote_mint = str(event.get("quoteMint") or "").strip()
    timestamp = _to_float(event.get("timestamp", event.get("timestamp_ms")))

    if not mint or not pool_id or timestamp is None:
        return None

    timestamp_ms = int(timestamp)
    token_reserve: float | None = None
    quote_reserve: float | None = None
    reserve_model: str | None = None
    fee_rate = _to_float(event.get("poolFeeRate"))
    reason: str | None = None

    if quote_mint != WSOL_MINT:
        reason = "non_wsol_quote"
    elif pool == "pump":
        token_reserve = _to_float(event.get("vTokensInBondingCurve"))
        quote_reserve = _to_float(event.get("vQuoteInBondingCurve"))
        reserve_model = "bonding_curve"
        if token_reserve is None or quote_reserve is None:
            reason = "missing_bonding_curve_reserves"
    elif pool == "pump-amm":
        tokens_in_pool = _to_float(event.get("tokensInPool"))
        quote_in_pool = _to_float(event.get("quoteInPool"))
        virtual_tokens = _to_float(event.get("virtualTokensInPool"))
        virtual_quote = _to_float(event.get("virtualQuoteInPool"))
        virtual_tokens_active = virtual_tokens is not None and virtual_tokens != 0.0
        virtual_quote_active = virtual_quote is not None and virtual_quote != 0.0

        if tokens_in_pool is None or quote_in_pool is None:
            reason = "missing_amm_reserves"
        elif virtual_tokens_active and virtual_quote_active:
            # The feed uses exactly one virtual reserve style per pool. Seeing both
            # non-zero makes the price identity ambiguous, so refuse to guess.
            reason = "ambiguous_virtual_reserves"
        elif virtual_tokens_active:
            # Price identity: quoteInPool / (tokensInPool + virtualTokensInPool)
            token_reserve = tokens_in_pool + virtual_tokens
            quote_reserve = quote_in_pool
            reserve_model = "amm_virtual_tokens"
        elif virtual_quote is not None:
            # Price identity: (quoteInPool + virtualQuoteInPool) / tokensInPool
            token_reserve = tokens_in_pool
            quote_reserve = quote_in_pool + virtual_quote
            reserve_model = "amm_virtual_quote"
        else:
            reason = "missing_amm_reserves"
    else:
        reason = "unsupported_pool_model"

    if reason is None and (
        token_reserve is None
        or quote_reserve is None
        or token_reserve <= 0
        or quote_reserve <= 0
    ):
        reason = "non_positive_reserves"

    if reason is None and (fee_rate is None or not 0.0 <= fee_rate < 1.0):
        reason = "missing_or_invalid_pool_fee_rate"

    return PoolSnapshot(
        timestamp_ms=timestamp_ms,
        mint=mint,
        pool_id=pool_id,
        pool=pool,
        quote_mint=quote_mint,
        token_reserve=token_reserve,
        quote_reserve=quote_reserve,
        fee_rate=fee_rate,
        signature=str(event.get("signature") or "") or None,
        action=action,
        reserve_model=reserve_model,
        unsupported_reason=reason,
    )


@dataclass(slots=True)
class PoolState:
    mint: str
    pool_id: str
    pool: str
    quote_mint: str
    raw_token_reserve: float
    raw_quote_reserve: float
    shadow_token_reserve: float
    shadow_quote_reserve: float
    fee_rate: float
    last_snapshot_ms: int
    last_signature: str | None
    reserve_model: str | None = None

    @property
    def spot_price(self) -> float:
        return self.shadow_quote_reserve / self.shadow_token_reserve


@dataclass(slots=True)
class FillResult:
    order_id: str
    timestamp_ms: int
    mint: str
    pool_id: str
    side: str
    amount: float
    status: str
    pool: str | None = None
    reserve_model: str | None = None
    state_timestamp_ms: int | None = None
    state_age_ms: int | None = None
    reference_spot_price: float | None = None
    token_amount: float | None = None
    quote_gross: float | None = None
    quote_fee: float | None = None
    quote_net: float | None = None
    average_price_quote_per_token: float | None = None
    price_impact_bps: float | None = None
    fee_rate: float | None = None
    reserve_token_before: float | None = None
    reserve_quote_before: float | None = None
    reserve_token_after: float | None = None
    reserve_quote_after: float | None = None
    signature: str | None = None
    reason: str | None = None


class ExecutionSimulator:
    """Replay pool reserve snapshots and simulate SOL-quoted Pump executions.

    Pool reserves are advanced by applying observed historical reserve deltas to a
    shadow state. Simulated orders modify that shadow state but never rewrite the
    historical source events.
    """

    def __init__(
        self,
        events_path: str | Path,
        *,
        max_state_age_ms: int | None = 60_000,
        max_price_impact_bps: float | None = None,
    ) -> None:
        self.events_path = Path(events_path)
        self.max_state_age_ms = max_state_age_ms
        self.max_price_impact_bps = max_price_impact_bps
        self._events = iter_jsonl_events(self.events_path)
        self._next_snapshot: PoolSnapshot | None = None
        self._last_event_timestamp_ms = -1
        self._last_order_timestamp_ms = -1
        self._line_index = 0
        self._states: dict[tuple[str, str], PoolState] = {}
        self._metadata: dict[tuple[str, str], PoolSnapshot] = {}
        self._eof = False
        self.stats = {
            "events_read": 0,
            "pool_snapshots_seen": 0,
            "valid_snapshots_applied": 0,
            "invalid_snapshots_seen": 0,
            "pool_states_initialized": 0,
            "pool_state_updates": 0,
        }

    def _read_next_snapshot(self) -> PoolSnapshot | None:
        while True:
            try:
                event = next(self._events)
            except StopIteration:
                self._eof = True
                return None

            self._line_index += 1
            self.stats["events_read"] += 1
            snapshot = snapshot_from_event(event)
            if snapshot is None:
                continue

            if snapshot.timestamp_ms < self._last_event_timestamp_ms:
                raise ValueError(
                    "Input events are not timestamp ordered: "
                    f"line {self._line_index} has {snapshot.timestamp_ms}, "
                    f"previous timestamp was {self._last_event_timestamp_ms}."
                )
            self._last_event_timestamp_ms = snapshot.timestamp_ms
            return snapshot

    def _apply_snapshot(self, snapshot: PoolSnapshot) -> None:
        self.stats["pool_snapshots_seen"] += 1
        key = snapshot.key
        self._metadata[key] = snapshot

        if snapshot.unsupported_reason is not None:
            self.stats["invalid_snapshots_seen"] += 1
            return

        assert snapshot.token_reserve is not None
        assert snapshot.quote_reserve is not None
        assert snapshot.fee_rate is not None

        state = self._states.get(key)
        if state is None:
            self._states[key] = PoolState(
                mint=snapshot.mint,
                pool_id=snapshot.pool_id,
                pool=snapshot.pool,
                quote_mint=snapshot.quote_mint,
                raw_token_reserve=snapshot.token_reserve,
                raw_quote_reserve=snapshot.quote_reserve,
                shadow_token_reserve=snapshot.token_reserve,
                shadow_quote_reserve=snapshot.quote_reserve,
                fee_rate=snapshot.fee_rate,
                last_snapshot_ms=snapshot.timestamp_ms,
                last_signature=snapshot.signature,
                reserve_model=snapshot.reserve_model,
            )
            self.stats["pool_states_initialized"] += 1
            self.stats["valid_snapshots_applied"] += 1
            return

        token_delta = snapshot.token_reserve - state.raw_token_reserve
        quote_delta = snapshot.quote_reserve - state.raw_quote_reserve
        shadow_token = state.shadow_token_reserve + token_delta
        shadow_quote = state.shadow_quote_reserve + quote_delta

        if shadow_token <= 0 or shadow_quote <= 0:
            self.stats["invalid_snapshots_seen"] += 1
            # Keep a concrete reason on the latest metadata so a later order
            # reports why the pool state is unusable instead of reason=None.
            snapshot.unsupported_reason = "shadow_reserve_became_non_positive"
            self._states.pop(key, None)
            return

        state.raw_token_reserve = snapshot.token_reserve
        state.raw_quote_reserve = snapshot.quote_reserve
        state.shadow_token_reserve = shadow_token
        state.shadow_quote_reserve = shadow_quote
        state.fee_rate = snapshot.fee_rate
        state.last_snapshot_ms = snapshot.timestamp_ms
        state.last_signature = snapshot.signature
        state.pool = snapshot.pool
        state.quote_mint = snapshot.quote_mint
        state.reserve_model = snapshot.reserve_model
        self.stats["pool_state_updates"] += 1
        self.stats["valid_snapshots_applied"] += 1

    def advance_before(self, timestamp_ms: int) -> None:
        """Apply all snapshots strictly earlier than the requested execution time."""
        if self._next_snapshot is None and not self._eof:
            self._next_snapshot = self._read_next_snapshot()

        while self._next_snapshot is not None and self._next_snapshot.timestamp_ms < timestamp_ms:
            self._apply_snapshot(self._next_snapshot)
            self._next_snapshot = self._read_next_snapshot()

    def execute(
        self,
        *,
        order_id: str,
        timestamp_ms: int,
        mint: str,
        pool_id: str,
        side: str,
        amount: float,
    ) -> FillResult:
        """Execute a quote-sized buy or token-sized sell against the shadow pool."""
        normalized_side = side.strip().lower()
        result = FillResult(
            order_id=order_id,
            timestamp_ms=int(timestamp_ms),
            mint=mint,
            pool_id=pool_id,
            side=normalized_side,
            amount=float(amount),
            status="rejected",
        )

        if normalized_side not in {"buy", "sell"}:
            result.status = "invalid_order"
            result.reason = "side_must_be_buy_or_sell"
            return result
        if not mint or not pool_id:
            result.status = "invalid_order"
            result.reason = "mint_and_pool_id_are_required"
            return result
        if not math.isfinite(amount) or amount <= 0:
            result.status = "invalid_order"
            result.reason = "amount_must_be_finite_and_positive"
            return result
        if int(timestamp_ms) < self._last_order_timestamp_ms:
            result.status = "invalid_order"
            result.reason = "orders_must_be_non_decreasing_in_time"
            return result
        self._last_order_timestamp_ms = int(timestamp_ms)

        self.advance_before(int(timestamp_ms))
        key = (mint, pool_id)
        state = self._states.get(key)
        metadata = self._metadata.get(key)

        if state is None:
            result.status = "unsupported_pool" if metadata else "no_pool_state"
            result.reason = (
                metadata.unsupported_reason if metadata else "no_valid_snapshot_before_execution"
            )
            return result

        if (
            metadata is not None
            and metadata.unsupported_reason is not None
            and metadata.timestamp_ms >= state.last_snapshot_ms
        ):
            # A snapshot at the same millisecond was processed after the last
            # valid state. It may be a later event for the same post-event slot,
            # so refuse the order rather than trusting an older state.
            result.status = "unsupported_pool"
            result.reason = metadata.unsupported_reason
            return result

        state_age = int(timestamp_ms) - state.last_snapshot_ms
        result.pool = state.pool
        result.reserve_model = state.reserve_model
        result.state_timestamp_ms = state.last_snapshot_ms
        result.state_age_ms = state_age
        result.signature = state.last_signature
        result.fee_rate = state.fee_rate

        if self.max_state_age_ms is not None and state_age > self.max_state_age_ms:
            result.status = "stale_pool_state"
            result.reason = "latest_pool_snapshot_exceeds_max_state_age"
            return result

        token_before = state.shadow_token_reserve
        quote_before = state.shadow_quote_reserve
        spot_before = quote_before / token_before
        fee_rate = state.fee_rate

        result.reference_spot_price = spot_before
        result.reserve_token_before = token_before
        result.reserve_quote_before = quote_before

        if normalized_side == "buy":
            # Fees are always charged in the quote asset, on the gross amount:
            # the user pays `amount` SOL and the fee is deducted before the swap.
            quote_gross = amount
            quote_fee = quote_gross * fee_rate
            quote_net = quote_gross - quote_fee
            token_out = token_before * quote_net / (quote_before + quote_net)
            if token_out <= 0 or token_out >= token_before:
                result.status = "invalid_fill"
                result.reason = "buy_output_outside_reserve_bounds"
                return result
            curve_average_price = quote_net / token_out
            price_impact_bps = (curve_average_price / spot_before - 1.0) * 10_000.0
            average_price = quote_gross / token_out
            token_after = token_before - token_out
            quote_after = quote_before + quote_net
        else:
            token_in = amount
            if token_in >= token_before:
                result.status = "insufficient_liquidity"
                result.reason = "sell_size_is_not_smaller_than_token_reserve"
                return result
            quote_gross = quote_before * token_in / (token_before + token_in)
            quote_fee = quote_gross * fee_rate
            quote_net = quote_gross - quote_fee
            token_out = token_in
            curve_average_price = quote_gross / token_in
            price_impact_bps = (1.0 - curve_average_price / spot_before) * 10_000.0
            average_price = quote_net / token_in
            token_after = token_before + token_in
            quote_after = quote_before - quote_gross

        result.token_amount = token_out
        result.quote_gross = quote_gross
        result.quote_fee = quote_fee
        result.quote_net = quote_net
        result.average_price_quote_per_token = average_price
        result.price_impact_bps = max(0.0, price_impact_bps)
        result.reserve_token_after = token_after
        result.reserve_quote_after = quote_after

        if (
            self.max_price_impact_bps is not None
            and result.price_impact_bps > self.max_price_impact_bps
        ):
            result.status = "price_impact_exceeded"
            result.reason = "price_impact_is_above_configured_limit"
            return result

        state.shadow_token_reserve = token_after
        state.shadow_quote_reserve = quote_after
        result.status = "filled"
        return result


def load_orders(path: Path) -> list[dict[str, Any]]:
    required = {"timestamp_ms", "mint", "pool_id", "side", "amount"}
    with path.open("r", newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source)
        if reader.fieldnames is None:
            raise ValueError("Orders CSV is empty or has no header.")
        missing = sorted(required - set(reader.fieldnames))
        if missing:
            raise ValueError(f"Orders CSV is missing columns: {missing}")
        orders = []
        for index, row in enumerate(reader, start=1):
            timestamp = _to_float(row.get("timestamp_ms"))
            amount = _to_float(row.get("amount"))
            if timestamp is None or amount is None:
                raise ValueError(f"Invalid timestamp_ms or amount in orders row {index + 1}.")
            orders.append(
                {
                    "order_id": str(row.get("order_id") or index),
                    "timestamp_ms": int(timestamp),
                    "mint": str(row.get("mint") or "").strip(),
                    "pool_id": str(row.get("pool_id") or "").strip(),
                    "side": str(row.get("side") or "").strip().lower(),
                    "amount": amount,
                }
            )

    orders.sort(key=lambda item: item["timestamp_ms"])
    return orders


def run_orders(
    events_path: Path,
    orders_path: Path,
    output_path: Path,
    *,
    max_state_age_ms: int | None,
    max_price_impact_bps: float | None,
) -> dict[str, int]:
    orders = load_orders(orders_path)
    simulator = ExecutionSimulator(
        events_path,
        max_state_age_ms=max_state_age_ms,
        max_price_impact_bps=max_price_impact_bps,
    )

    results: list[FillResult] = []
    previous_timestamp = -1
    for order in orders:
        if order["timestamp_ms"] < previous_timestamp:
            raise ValueError("Orders must be sorted by timestamp_ms.")
        previous_timestamp = order["timestamp_ms"]
        results.append(simulator.execute(**order))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=list(asdict(results[0]).keys()) if results else list(FillResult.__dataclass_fields__.keys()))
        writer.writeheader()
        for result in results:
            writer.writerow(asdict(result))

    counts: dict[str, int] = {}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
    counts["orders_total"] = len(results)
    counts.update({f"source_{key}": value for key, value in simulator.stats.items()})
    return counts


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Simulate SOL-quoted Pump/Pump AMM fills from raw PumpAPI JSONL events."
    )
    parser.add_argument("events", type=Path, help="Raw PumpAPI .jsonl or .jsonl.zst file")
    parser.add_argument("orders", type=Path, help="CSV with timestamp_ms,mint,pool_id,side,amount")
    parser.add_argument("-o", "--output", type=Path, default=Path("execution_fills.csv"))
    parser.add_argument(
        "--max-state-age-ms",
        type=int,
        default=60_000,
        help="Reject snapshots older than this value; use 0 to disable age rejection.",
    )
    parser.add_argument(
        "--max-price-impact-bps",
        type=float,
        default=None,
        help="Optional maximum curve price impact in basis points.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    max_state_age_ms = None if args.max_state_age_ms == 0 else args.max_state_age_ms
    if max_state_age_ms is not None and max_state_age_ms < 0:
        raise SystemExit("--max-state-age-ms must be >= 0")
    if args.max_price_impact_bps is not None and args.max_price_impact_bps < 0:
        raise SystemExit("--max-price-impact-bps must be >= 0")

    counts = run_orders(
        args.events,
        args.orders,
        args.output,
        max_state_age_ms=max_state_age_ms,
        max_price_impact_bps=args.max_price_impact_bps,
    )
    print(json.dumps(counts, indent=2))
    print(f"Fills written: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
