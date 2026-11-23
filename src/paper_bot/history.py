"""Bounded, causal per-mint market history.

Live features must be computed from trades that printed *before* the signal.
The bot therefore keeps a rolling buffer per watched mint and uses the same
``pre_signal_stats`` definition as the research backtest.

The live feed is a firehose: to make history-dependent rules usable, every
observed mint is indexed, not just the ones already watched. The index is
bounded three ways so a busy stream cannot exhaust memory:

* a per-mint event cap,
* a per-mint time horizon (only recent trades are needed for the 5 minute
  crowd features), and
* a global event cap with LRU eviction of unpinned mints.

The caps are deliberately small: a firehose event costs ~300-600 bytes once
its strings are counted, so the defaults here hold roughly 50-90 MB instead of
the hundreds of megabytes an earlier 600k-event cap used. That matters on a
1.9 GB host that also runs other services: the process must stay far below the
OOM killer, not merely below a theoretical bound. Five-minute crowd features
and ``pre_ret``/``age_min`` are unaffected because the first price/timestamp
survive deque eviction for as long as the mint stays in the index.

Cold-start caveat: a token first seen at signal time has no pre-signal history.
``hist_available`` is then false and history-derived values are ``None`` so
rules that need them reject explicitly instead of guessing from future data.
"""

from __future__ import annotations

from collections import OrderedDict, deque
from typing import Any

import numpy as np

from .models import TradeEvent

# Values returned when a mint has no causal pre-signal history. ``None`` is
# deliberate: a zero would silently pass rules such as ``signers_5m <= 147``.
MISSING_STATS: dict[str, Any] = {
    "hist_available": False,
    "pre_trades": None,
    "pre_signers": None,
    "age_min": None,
    "pre_volume_sol": None,
    "last_price_sol": None,
    "high_price_sol": None,
    "first_price_sol": None,
    "pre_ret": None,
    "pre_ret_15m": None,
    "dd_from_high": None,
    "trades_5m": None,
    "signers_5m": None,
    "buys_5m": None,
    "sells_5m": None,
    # Hot-money continuation features. ``flow_1m_sol`` is net buys minus sells
    # in the 60 s before the signal; ``pre_vol_30m_sol`` is gross traded volume
    # in the 30 min before it. Both are strictly causal.
    "flow_1m_sol": None,
    "pre_vol_30m_sol": None,
}


def pre_signal_stats(
    mint_trades: dict[str, np.ndarray], signal_ts_ms: int
) -> dict[str, Any]:
    """Summarise a token's trading history *strictly before* the signal.

    Vendored from ``backtest_signals.pre_signal_stats`` so the standalone
    package computes exactly the same features as the replay harness.
    """
    ts = mint_trades["ts"]
    prices = mint_trades["price"]
    signers = mint_trades["signer"]
    notional = mint_trades["notional"]

    end = int(np.searchsorted(ts, signal_ts_ms, side="left"))
    if end == 0:
        return {
            "pre_trades": 0,
            "pre_signers": 0,
            "age_min": 0.0,
            "pre_volume_sol": 0.0,
            "last_price_sol": None,
            "high_price_sol": None,
            "first_price_sol": None,
            "pre_ret": 0.0,
            "dd_from_high": 0.0,
        }

    window_prices = prices[:end]
    high = float(window_prices.max())
    last = float(window_prices[-1])
    first = float(window_prices[0])
    # Null/empty signers must not count as distinct participants.
    pre_signers = sum(1 for signer in np.unique(signers[:end]) if signer)
    pre_volume = float(np.nansum(notional[:end]))
    return {
        "pre_trades": int(end),
        "pre_signers": int(pre_signers),
        "age_min": float(signal_ts_ms - int(ts[0])) / 60_000.0,
        "pre_volume_sol": pre_volume if np.isfinite(pre_volume) else 0.0,
        "last_price_sol": last,
        "high_price_sol": high,
        "first_price_sol": first,
        "pre_ret": (last / first - 1.0) if first > 0 else 0.0,
        "dd_from_high": (last / high - 1.0) if high > 0 else 0.0,
    }


class MarketHistory:
    def __init__(
        self,
        *,
        max_events_per_mint: int = 3_000,
        max_mints: int = 10_000,
        max_total_events: int = 150_000,
        horizon_ms: int = 900_000,
    ) -> None:
        if max_events_per_mint < 1 or max_mints < 1 or max_total_events < 1:
            raise ValueError("history bounds must be positive")
        if horizon_ms < 1:
            raise ValueError("horizon_ms must be positive")
        self.max_events_per_mint = max_events_per_mint
        self.max_mints = max_mints
        self.max_total_events = max_total_events
        self.horizon_ms = horizon_ms
        self._events: OrderedDict[str, deque[TradeEvent]] = OrderedDict()
        # Reference-counted pins: an A/B pair shares one history index, so a
        # mint must stay pinned until the last lane releases it.
        self._pinned: dict[str, int] = {}
        # First/high observations survive deque eviction so pre_ret and age do
        # not silently change when a busy mint exceeds max_events_per_mint.
        self._first_price: dict[str, float] = {}
        self._first_ts: dict[str, int] = {}
        self._high_price: dict[str, float] = {}
        self._last_ts: dict[str, int] = {}
        self._total_events = 0

    @property
    def total_events(self) -> int:
        return self._total_events

    @property
    def mint_count(self) -> int:
        return len(self._events)

    def pin(self, mint: str) -> None:
        """Keep a mint in memory even when the LRU is full."""
        self._pinned[mint] = self._pinned.get(mint, 0) + 1
        self._events.setdefault(mint, deque(maxlen=self.max_events_per_mint))
        self._events.move_to_end(mint)

    def unpin(self, mint: str) -> None:
        """Release one pin; eviction is allowed after the last release."""
        count = self._pinned.get(mint, 0) - 1
        if count > 0:
            self._pinned[mint] = count
        else:
            self._pinned.pop(mint, None)

    def add(self, event: TradeEvent) -> None:
        if not event.price_valid:
            return
        price = float(event.price_sol)
        self._first_price.setdefault(event.mint, price)
        self._first_ts.setdefault(event.mint, event.timestamp_ms)
        previous_ts = self._last_ts.get(event.mint)
        last_ts = (
            event.timestamp_ms
            if previous_ts is None
            else max(previous_ts, event.timestamp_ms)
        )
        self._last_ts[event.mint] = last_ts
        high = self._high_price.get(event.mint)
        if high is None or price > high:
            self._high_price[event.mint] = price
        bucket = self._events.get(event.mint)
        if bucket is None:
            bucket = deque(maxlen=self.max_events_per_mint)
            self._events[event.mint] = bucket
        elif len(bucket) >= self.max_events_per_mint:
            # ``deque(maxlen=...)`` silently evicts the oldest item on append.
            self._total_events -= 1
        bucket.append(event)
        self._total_events += 1
        self._events.move_to_end(event.mint)
        self._prune_bucket(event.mint, bucket, last_ts - self.horizon_ms)
        self._evict_if_needed()

    def _prune_bucket(
        self, mint: str, bucket: deque[TradeEvent], cutoff_ms: int
    ) -> None:
        """Drop events older than the feature horizon."""
        while bucket and bucket[0].timestamp_ms < cutoff_ms:
            bucket.popleft()
            self._total_events -= 1
        if not bucket:
            self._drop(mint)

    def _drop(self, mint: str) -> None:
        bucket = self._events.pop(mint, None)
        if bucket is not None:
            self._total_events -= len(bucket)
        self._first_price.pop(mint, None)
        self._first_ts.pop(mint, None)
        self._high_price.pop(mint, None)
        self._last_ts.pop(mint, None)

    def _evict_if_needed(self) -> None:
        while (
            len(self._events) > self.max_mints
            or self._total_events > self.max_total_events
        ):
            victim = next(
                (mint for mint in self._events if mint not in self._pinned), None
            )
            if victim is None:
                # Everything is pinned (active entries/positions). Keep the
                # pins but trim their oldest events so memory stays bounded.
                victim = next(iter(self._events), None)
                if victim is None:
                    return
                bucket = self._events.get(victim)
                if bucket:
                    bucket.popleft()
                    self._total_events -= 1
                if not bucket:
                    self._drop(victim)
                continue
            self._drop(victim)

    def events(self, mint: str) -> list[TradeEvent]:
        return list(self._events.get(mint, ()))

    def last_price(self, mint: str) -> tuple[float | None, int | None]:
        bucket = self._events.get(mint)
        if not bucket:
            return None, None
        latest = max(bucket, key=lambda event: (event.timestamp_ms, event.event_index))
        return latest.price_sol, latest.timestamp_ms

    def last_trade(
        self, mint: str
    ) -> tuple[float | None, int | None, int | None]:
        """Latest observed price, timestamp and token decimals for ``mint``.

        The timer entry path quotes at a fixed delay instead of waiting for a
        trade print, so it needs a reference price and the token decimals
        without an event in hand.
        """
        bucket = self._events.get(mint)
        if not bucket:
            return None, None, None
        latest = max(bucket, key=lambda event: (event.timestamp_ms, event.event_index))
        return latest.price_sol, latest.timestamp_ms, latest.decimals

    def first_buy_between(
        self,
        mint: str,
        start_ms: int,
        end_ms: int,
        *,
        min_notional_sol: float = 0.0,
    ) -> int | None:
        """Timestamp of the earliest eligible BUY print in ``[start_ms, end_ms)``.

        Used to detect that Telegram delivered a card after the research entry
        print had already happened: the live bot cannot buy in the past, so the
        signal is skipped instead of starting a new entry window.
        ``min_notional_sol`` must be the same entry-size floor the live trigger
        and the replay use; a sub-cent print is not the print the strategy
        would have taken, so it must not count as a missed entry.
        """
        bucket = self._events.get(mint)
        if not bucket:
            return None
        stamps = []
        for event in bucket:
            if not event.is_buy:
                continue
            if not (start_ms <= event.timestamp_ms < end_ms):
                continue
            if min_notional_sol > 0:
                notional = event.quote_notional_sol
                if notional is None or notional < min_notional_sol:
                    continue
            stamps.append(event.timestamp_ms)
        return min(stamps) if stamps else None

    def buy_volume_share(
        self, mint: str, start_ms: int, end_ms: int
    ) -> float | None:
        """Buy notional / total notional in ``[start_ms, end_ms)``.

        This is the research ``bs_0_5s`` feature: the name says 0.5 s but the
        window is the first 5 s after the signal. It returns ``None`` when no
        volume printed in the window, matching a null feature in the backtest.
        """
        bucket = self._events.get(mint)
        if not bucket:
            return None
        total = 0.0
        bought = 0.0
        for event in bucket:
            if start_ms <= event.timestamp_ms < end_ms:
                value = event.notional_sol
                if value <= 0:
                    continue
                total += value
                if event.is_buy:
                    bought += value
        if total <= 0:
            return None
        return bought / total

    def stats(self, mint: str, signal_ts_ms: int) -> dict[str, Any]:
        """Pre-signal statistics, matching ``backtest_signals.pre_signal_stats``."""
        bucket = self._events.get(mint)
        if not bucket:
            return dict(MISSING_STATS)

        events = list(bucket)
        events.sort(key=lambda event: (event.timestamp_ms, event.event_index))
        ts = np.fromiter((event.timestamp_ms for event in events), dtype=np.int64)
        end = int(np.searchsorted(ts, signal_ts_ms, side="left"))
        if end == 0:
            return dict(MISSING_STATS)

        price = np.fromiter(
            (event.price_sol or 0.0 for event in events), dtype=np.float64
        )
        notional = np.fromiter(
            (event.notional_sol for event in events), dtype=np.float64
        )
        signer = np.array([event.tx_signer or "" for event in events], dtype=object)
        is_buy = np.array([event.is_buy for event in events], dtype=bool)

        market = {
            "ts": ts,
            "price": price,
            "notional": notional,
            "signer": signer,
            "is_buy": is_buy,
        }
        stats = pre_signal_stats(market, signal_ts_ms)

        # Restore the true first/high observations even when the deque has
        # dropped older events. This keeps ``pre_ret`` and ``age_min`` causal
        # and stable instead of depending on the buffer size.
        first_price = self._first_price.get(mint)
        last_price = stats.get("last_price_sol")
        if first_price is not None and first_price > 0 and last_price:
            stats["first_price_sol"] = first_price
            stats["pre_ret"] = last_price / first_price - 1.0
        first_ts = self._first_ts.get(mint)
        if first_ts is not None:
            stats["age_min"] = (signal_ts_ms - first_ts) / 60_000.0
        high_price = self._high_price.get(mint)
        if high_price is not None and high_price > 0 and last_price:
            stats["high_price_sol"] = high_price
            stats["dd_from_high"] = last_price / high_price - 1.0

        # Five-minute crowd features used by the research rules.
        start_5m = int(np.searchsorted(ts, signal_ts_ms - 300_000, side="left"))
        window_signers = {str(value) for value in signer[start_5m:end] if str(value)}
        window_actions = is_buy[start_5m:end]

        # Hot-money continuation features: prior 60 s net flow and prior 30 min
        # gross volume. ``end`` is a left search, so no trade at or after the
        # signal can enter either window.
        start_1m = int(np.searchsorted(ts, signal_ts_ms - 60_000, side="left"))
        window_notional_1m = notional[start_1m:end]
        window_buy_1m = is_buy[start_1m:end]
        flow_1m_sol = float(window_notional_1m[window_buy_1m].sum()) - float(
            window_notional_1m[~window_buy_1m].sum()
        )
        start_30m = int(np.searchsorted(ts, signal_ts_ms - 1_800_000, side="left"))
        pre_vol_30m_sol = float(notional[start_30m:end].sum())

        stats.update(
            {
                "hist_available": True,
                "trades_5m": max(0, end - start_5m),
                "signers_5m": len(window_signers),
                "buys_5m": int(window_actions.sum()),
                "sells_5m": int((~window_actions).sum()),
                "flow_1m_sol": flow_1m_sol,
                "pre_vol_30m_sol": pre_vol_30m_sol,
                "pre_ret_15m": None,
            }
        )

        # Live proxy for the research ``pre_ret_15m`` bar feature: the
        # last real price strictly before the signal divided by the last
        # price at or before the 15 minutes before it (side="right" so an
        # event exactly at the boundary is still in the past window,
        # mirroring ``past = close <= start_ts`` in the backtester).
        base_idx = int(np.searchsorted(ts, signal_ts_ms - 900_000, side="right")) - 1
        last_price = stats.get("last_price_sol")
        if base_idx >= 0 and last_price and price[base_idx] > 0:
            stats["pre_ret_15m"] = last_price / float(price[base_idx]) - 1.0
        return stats


def attach_history_features(
    features: dict[str, Any], stats: dict[str, Any]
) -> dict[str, Any]:
    """Add ``hist_*`` keys without overwriting message-derived features."""
    merged = dict(features)
    for key, value in stats.items():
        merged[f"hist_{key}" if key != "hist_available" else key] = value
    return merged
