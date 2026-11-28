#!/usr/bin/env python3
"""Report-only reproduction of ``pumpfun_jupiter_compound_v1``.

This tool implements the strategy described in
``docs/new_strategy/pumpfun_jupiter_compounding_strategy_test_spec.md`` as a
research simulation over PumpAPI replay data. It does not touch the paper bot,
does not sign or broadcast, and does not change any deployed rule.

Two signal sources are supported:

``stream``
    A faithful token-creation lane. It scans the raw hourly PumpAPI archives
    for ``create`` events, reconstructs every candidate's point-in-time
    five-minute activity from the subsequent ``buy``/``sell`` prints, and
    simulates entry and exit from the token's creation timestamp.

``channel``
    A clearly labelled adaptation. It uses the ``@AveSignalMonitor``
    ``second_vibe_buy`` cards (the scanner's signal channel) as the candidate
    source. Those cards appear after a Pump.fun pool exists, often after the
    three-minute creation window, so the token-age filter cannot apply. This
    lane is reported as ``pumpfun_amm_migration_adaptation_v1`` and must not be
    presented as the course strategy.

Lanes (spec section 11.2):
``a``  fixed $2 trade size, no compounding, no pause, no cycle cap.
``b``  60/40 win split, $1 play floor, 2-loss pause, 30-cycle cap.
``c``  same as ``b`` but no play-floor reset (ablation).

The simulator is deliberately conservative and point-in-time:

* A candidate is evaluated once, at age 25 s, using only prints at or before
  that instant. If no trade has printed yet it retries every 5 s until 300 s,
  mirroring the spec's metadata-retry loop.
* Take-profit and stop-loss are mark triggers; realized proceeds come from a
  size-aware constant-product fill, never from the trigger threshold.
* There is no forced time exit. Positions unresolved at the end of the data
  are reported as open and also under a separate write-off sensitivity.
* Liquidity is the USD value of **both** sides of the pool. For the Pump.fun
  bonding curve that is ``2 * vQuoteInBondingCurve``; for an AMM pool it is
  ``2 * quoteInPool``. This matches the order of magnitude DexScreener shows
  for a new Pump.fun token; it is an assumption and is recorded per trade.
"""
from __future__ import annotations

import argparse
import bisect
import csv
import heapq
import io
import json
import math
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator, Sequence

import numpy as np
import orjson
import zstandard as zstd

WSOL_MINT = "So11111111111111111111111111111111111111112"

# Replay archives before 2026-11-08 omit ``priceUsd``/``marketCapUsd``; they
# carry only SOL-denominated fields. The USD size is fixed by the strategy, so
# a closed trade's USD PnL is invariant to the assumed SOL/USD rate; the
# liquidity and market-cap gates are not, which is why each day uses its own
# rate rather than one constant. Source: Binance SOLUSDT 1h closes, daily mean.
SOL_USD_BY_DAY: dict[str, float] = {
    "2026-08-09": 76.59,
    "2026-10-03": 119.53,
    "2026-10-04": 121.17,
    "2026-10-05": 120.63,
    "2026-11-08": 88.88,
}
# Days whose archives already carry ``priceUsd`` need no entry: the record's own
# conversion wins and the table is never consulted.


def estimate_sol_usd(files: Sequence[Path], max_records: int = 400) -> float | None:
    """Median SOL/USD from the archive itself.

    A WSOL-quoted record carries ``price`` (SOL per token) and ``priceUsd``, so
    their ratio is that instant's SOL/USD. Using the replay keeps the creator
    buy and the legacy conversion on the same footing as everything else and
    avoids an external price feed.
    """
    values: list[float] = []
    for path in files:
        for event in iter_zst_events(path):
            if event.get("quoteMint") not in (None, WSOL_MINT):
                continue
            price = _finite(event.get("price"))
            price_usd = _finite(event.get("priceUsd"))
            if price and price_usd:
                values.append(price_usd / price)
            if len(values) >= max_records:
                break
        if len(values) >= max_records:
            break
    return float(np.median(values)) if values else None


def sol_usd_from_series(series_by_mint: dict[str, "MintSeries"]) -> float | None:
    """Same estimate, taken from already-loaded channel series."""
    values: list[float] = []
    for series in series_by_mint.values():
        for state in series.states:
            if state.quote_mint not in (None, WSOL_MINT):
                continue
            if state.price_quote and state.price_usd:
                values.append(state.price_usd / state.price_quote)
            if len(values) >= 400:
                return float(np.median(values))
    return float(np.median(values)) if values else None


def creator_buy_in_sol(
    quote_amount: float | None, quote_usd: float | None, sol_usd: float | None
) -> float | None:
    """Convert a create event's quote-denominated buy into SOL.

    A non-WSOL pool's ``quoteAmount`` is in that memecoin, not SOL; comparing it
    with the 20 SOL cap (or scoring it against 20) without conversion was a
    unit error. Missing inputs stay missing rather than becoming zero.
    """
    if quote_amount is None or quote_usd is None or not sol_usd:
        return None
    return quote_amount * quote_usd / sol_usd
STRATEGY_ID = "pumpfun_jupiter_compound_v1"
CHANNEL_STRATEGY_ID = "pumpfun_amm_migration_adaptation_v1"
WINDOW_MS = 300_000
HOUR_MS = 3_600_000

SCAM_KEYWORDS = ("guaranteed", "100x", "airdrop", "safe moon")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StrategyConfig:
    liquidity_min_usd: float = 5_000.0
    liquidity_max_usd: float = 2_000_000.0
    volume_5m_min_usd: float = 2_000.0
    transactions_5m_min: int = 12
    buys_5m_min: int = 8
    buy_sell_ratio_min: float = 1.5
    market_cap_max_usd: float = 10_000_000.0
    wash_volume_to_liquidity_max: float = 50.0
    creator_buy_max_sol: float = 20.0
    starting_trade_size_usd: float = 2.0
    play_floor_usd: float = 1.0
    max_cycles: int = 30
    take_profit_multiple: float = 2.0
    stop_loss_multiple: float = 0.82
    monitor_interval_ms: int = 8_000
    dead_pool_liquidity_usd: float = 25.0
    dead_pool_retry_ms: int = 600_000
    losses_before_pause: int = 2
    loss_pause_ms: int = 300_000
    win_reinvestment_fraction: float = 0.60
    win_savings_fraction: float = 0.40
    fee_rate: float = 0.0125
    entry_delay_ms: int = 25_000
    retry_interval_ms: int = 5_000
    max_token_age_ms: int = 300_000
    max_mark_age_ms: int = 120_000
    entry_slippage_limit_bps: float = 150.0
    sell_slippage_limit_bps: float = 1_000.0
    execution_model: str = "pool_cp_v1"

    def as_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": STRATEGY_ID,
            "strategy_version": "1.1",
            **{key: getattr(self, key) for key in self.__dataclass_fields__},
        }


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


# ---------------------------------------------------------------------------
# Candidate statistics, filters and score
# ---------------------------------------------------------------------------


@dataclass
class CandidateStats:
    mint: str
    age_seconds: float
    liquidity_usd: float | None
    market_cap_usd: float | None
    volume_5m_usd: float
    transactions_5m: int
    buys_5m: int
    sells_5m: int
    creator_buy_sol: float | None
    price_sol: float | None
    quote_usd: float | None
    name: str = ""
    quote_mint: str | None = None
    mark_price_usd: float | None = None
    price_discrepancy: float | None = None

    @property
    def buy_sell_ratio(self) -> float:
        if self.sells_5m > 0:
            return self.buys_5m / self.sells_5m
        if self.buys_5m > 0:
            return float("inf")
        return 0.0


def score_candidate(stats: CandidateStats) -> dict[str, float | None]:
    """Deterministic approximation of the course's 0-100 score (diagnostic)."""
    age = stats.age_seconds
    freshness = (
        10.0
        if age <= 120
        else max(0.0, 10.0 * (300.0 - age) / 180.0)
    )
    buy_pressure = 25.0 * clamp(
        (stats.buy_sell_ratio - 1.3) / (3.0 - 1.3), 0.0, 1.0
    )
    volume = 20.0 * clamp(
        math.log10(max(stats.volume_5m_usd, 500.0) / 500.0)
        / math.log10(50_000.0 / 500.0),
        0.0,
        1.0,
    )
    liquidity = stats.liquidity_usd
    if liquidity is None:
        liquidity_points: float | None = None
    elif 5_000.0 <= liquidity <= 50_000.0:
        liquidity_points = 15.0
    elif liquidity < 5_000.0:
        liquidity_points = 15.0 * clamp(
            (liquidity - 2_500.0) / (5_000.0 - 2_500.0), 0.0, 1.0
        )
    else:
        liquidity_points = 15.0 * clamp(
            (2_000_000.0 - liquidity) / (2_000_000.0 - 50_000.0), 0.0, 1.0
        )
    if stats.creator_buy_sol is None:
        fair_launch: float | None = None
    else:
        fair_launch = 10.0 * clamp(
            1.0 - stats.creator_buy_sol / 20.0, 0.0, 1.0
        )
    known = [
        value
        for value in (freshness, buy_pressure, volume, liquidity_points, fair_launch)
        if value is not None
    ]
    raw = float(sum(known))
    maximum = 80.0
    return {
        "freshness": freshness,
        "buy_pressure": buy_pressure,
        "volume": volume,
        "liquidity": liquidity_points,
        "fair_launch": fair_launch,
        "raw_score_80": raw,
        "score_100": raw / maximum * 100.0,
        "score_complete": fair_launch is not None and liquidity_points is not None,
    }


def rejection_reasons(
    stats: CandidateStats,
    config: StrategyConfig,
    *,
    require_age: bool = True,
    require_creator_buy: bool = True,
) -> list[str]:
    """Return every reason the candidate fails; empty means it qualifies."""
    reasons: list[str] = []

    if stats.liquidity_usd is None or stats.market_cap_usd is None:
        reasons.append("MISSING_REQUIRED_DATA")
        # Still report the other filters that can be evaluated.
    # The course requires the creator's initial buy. The creation stream
    # exposes it, so a missing value is missing data, not a pass; the channel
    # adaptation, which often sees no create event, opts out explicitly.
    if require_creator_buy and stats.creator_buy_sol is None:
        reasons.append("MISSING_REQUIRED_DATA")
    if stats.mark_price_usd is None:
        reasons.append("MISSING_POOL_PRICE")
    # No level check against the feed's ``price``. On a Pump AMM the feed price
    # is FDV-based (price x total supply) while the reserves are the pool's, so
    # the two differ structurally by ~20 % on most records. Rejecting on that
    # gap removed nearly the whole AMM universe on archives that lack
    # ``priceUsd``, which would make the days incomparable. The discrepancy is
    # still recorded per candidate as a diagnostic.
    name = stats.name.lower()
    for keyword in SCAM_KEYWORDS:
        if keyword in name:
            reasons.append(f"SCAM_KEYWORD:{keyword}")
            break
    if stats.buys_5m >= 50 and stats.sells_5m < 2:
        reasons.append("HONEYPOT_ACTIVITY")
    if stats.liquidity_usd and stats.liquidity_usd > 0:
        if (
            stats.volume_5m_usd / stats.liquidity_usd
            >= config.wash_volume_to_liquidity_max
        ):
            reasons.append("WASH_VOLUME")
    if (
        stats.creator_buy_sol is not None
        and stats.creator_buy_sol > config.creator_buy_max_sol
    ):
        reasons.append("CREATOR_BUY_TOO_LARGE")

    if require_age and stats.age_seconds > config.max_token_age_ms / 1000.0:
        reasons.append("TOKEN_TOO_OLD")
    if stats.liquidity_usd is not None and not (
        config.liquidity_min_usd <= stats.liquidity_usd <= config.liquidity_max_usd
    ):
        reasons.append("LIQUIDITY_OUT_OF_RANGE")
    if stats.volume_5m_usd < config.volume_5m_min_usd:
        reasons.append("VOLUME_BELOW_MIN")
    if stats.transactions_5m < config.transactions_5m_min:
        reasons.append("TRANSACTIONS_BELOW_MIN")
    if stats.buys_5m < config.buys_5m_min:
        reasons.append("BUYS_BELOW_MIN")
    if stats.buy_sell_ratio < config.buy_sell_ratio_min:
        reasons.append("BUY_SELL_RATIO_BELOW_MIN")
    if (
        stats.market_cap_usd is not None
        and stats.market_cap_usd > config.market_cap_max_usd
    ):
        reasons.append("MARKET_CAP_TOO_HIGH")
    return reasons


# ---------------------------------------------------------------------------
# Pool state and executable fills
# ---------------------------------------------------------------------------


@dataclass
class PoolState:
    """One pool snapshot, denominated in that pool's own quote asset.

    Pump.fun pools are not all SOL-quoted: some tokens quote against a
    memecoin such as Pren1F. All fill arithmetic therefore runs in quote
    units, and ``quote_usd`` (USD per quote unit) converts to dollars. The
    event carries ``price`` (quote units per token) and ``priceUsd``, so
    ``quote_usd = priceUsd / price`` uses only fields from the same record.
    """

    ts_ms: int
    price_sol: float | None
    price_quote: float | None
    price_usd: float | None
    market_cap_usd: float | None
    quote_usd: float | None
    tokens: float | None
    quote: float | None
    pool: str = ""
    pool_id: str = ""
    quote_mint: str | None = None

    def effective_tokens(self) -> float | None:
        """Tradeable token reserve: ``quote / price`` when the record has one.

        A Pump AMM's ``tokensInPool`` counts the pool fee vault as well as the
        constant-product reserve, which makes it ~20 % too large: on 2026-11-18
        the reserve-implied price sat at 0.83x the feed price, and at migration
        the mark dropped 19 % in one tick while the feed price moved 1.3 %.
        The feed price is the true pool price, so the reserve behind it is
        ``quote / price``. On the bonding curve the two views already agree.
        """
        if self.quote and self.price_quote and self.price_quote > 0:
            return self.quote / self.price_quote
        return self.tokens

    def reserve_price_quote(self) -> float | None:
        """Price of the tradeable reserve, in quote units."""
        if self.price_quote and self.price_quote > 0:
            return self.price_quote
        if (
            self.tokens is None
            or self.quote is None
            or self.tokens <= 0
            or self.quote <= 0
        ):
            return None
        return self.quote / self.tokens

    def mark_price_usd(self) -> float | None:
        """One USD mark for both triggers and fills.

        The reserve-derived price is preferred because it is the price of the
        exact reserves used to fill, and it is converted with the same
        event's ``quote_usd``. ``priceUsd`` is only a fallback.
        """
        reserve = self.reserve_price_quote()
        if reserve is not None and self.quote_usd:
            return reserve * self.quote_usd
        return self.price_usd

    def price_discrepancy(self) -> float | None:
        """Relative gap between the raw pool reserves and the feed price.

        Kept as a diagnostic of how much of ``tokensInPool`` is not tradeable
        reserve (the fee vault); it is never used as a filter.
        """
        if not self.price_quote or not self.tokens or not self.quote:
            return None
        if self.tokens <= 0 or self.quote <= 0:
            return None
        return abs((self.quote / self.tokens) / self.price_quote - 1.0)

    def liquidity_usd(self) -> float | None:
        if self.quote is None or self.quote_usd is None or self.quote <= 0:
            return None
        # Both sides of the pool, as DexScreener reports for a new Pump token.
        return 2.0 * self.quote * self.quote_usd

    def with_carry(self, prev: "PoolState | None") -> "PoolState":
        """Fill a conversion field that an ``add``/``remove`` record omits."""
        if self.quote_usd is not None or prev is None:
            return self
        self.quote_usd = prev.quote_usd
        if self.price_quote is None:
            self.price_quote = prev.price_quote
        if self.price_usd is None:
            self.price_usd = prev.price_usd
        if self.quote_mint is None:
            self.quote_mint = prev.quote_mint
        if not self.pool_id:
            self.pool_id = prev.pool_id
        if self.market_cap_usd is None:
            self.market_cap_usd = prev.market_cap_usd
        return self


def pool_state_from_event(
    event: dict[str, Any], sol_usd: float | None = None
) -> PoolState | None:
    quote_mint = event.get("quoteMint")
    is_wsol = quote_mint in (None, WSOL_MINT)
    price_sol = _finite(event.get("priceSol"))
    if price_sol is None and is_wsol:
        price_sol = _finite(event.get("price"))
    price_quote = _finite(event.get("price"))
    price_usd = _finite(event.get("priceUsd"))
    market_cap = _finite(event.get("marketCapUsd"))
    quote_usd = None
    if price_usd and price_quote:
        quote_usd = price_usd / price_quote
    elif is_wsol and sol_usd:
        # Archive predates ``priceUsd``: the quote asset is SOL, so the day's
        # SOL/USD rate is the conversion.
        quote_usd = sol_usd
        if price_usd is None and price_sol:
            price_usd = price_sol * sol_usd
        if market_cap is None:
            market_cap_sol = _finite(event.get("marketCapSol"))
            if market_cap_sol:
                market_cap = market_cap_sol * sol_usd
    if market_cap is None and quote_usd:
        market_cap_quote = _finite(event.get("marketCapQuote"))
        if market_cap_quote:
            market_cap = market_cap_quote * quote_usd
    tokens = _finite(event.get("vTokensInBondingCurve"))
    quote = _finite(event.get("vQuoteInBondingCurve"))
    if not tokens or not quote:
        tokens = _finite(event.get("tokensInPool"))
        quote = _finite(event.get("quoteInPool"))
    if tokens is None or quote is None:
        return None
    ts = _timestamp_ms(event)
    if ts is None:
        return None
    return PoolState(
        ts_ms=ts,
        price_sol=price_sol,
        price_quote=price_quote,
        price_usd=price_usd,
        market_cap_usd=market_cap,
        quote_usd=quote_usd,
        tokens=tokens,
        quote=quote,
        pool=str(event.get("pool") or ""),
        pool_id=str(event.get("poolId") or ""),
        quote_mint=str(quote_mint) if quote_mint else None,
    )


def buy_tokens(
    state: PoolState, quote_in: float, fee_rate: float
) -> float | None:
    """Tokens received for ``quote_in`` units of the pool's quote asset."""
    if quote_in <= 0:
        return None
    net = quote_in * (1.0 - fee_rate)
    tokens = state.effective_tokens()
    if tokens is not None and state.quote is not None and tokens > 0:
        k = tokens * state.quote
        tokens_after = k / (state.quote + net)
        return max(0.0, tokens - tokens_after)
    if state.price_quote and state.price_quote > 0:
        return net / state.price_quote
    return None


def sell_sol(
    state: PoolState, tokens_in: float, fee_rate: float
) -> float | None:
    """Quote-asset proceeds for selling ``tokens_in`` (name kept for callers)."""
    if tokens_in <= 0:
        return None
    tokens = state.effective_tokens()
    if tokens is not None and state.quote is not None and tokens > 0:
        k = tokens * state.quote
        tokens_after = tokens + tokens_in
        quote_after = k / tokens_after
        gross = max(0.0, state.quote - quote_after)
        return gross * (1.0 - fee_rate)
    if state.price_quote and state.price_quote > 0:
        return tokens_in * state.price_quote * (1.0 - fee_rate)
    return None


# ---------------------------------------------------------------------------
# Compounding ledger
# ---------------------------------------------------------------------------


@dataclass
class Ledger:
    lane: str
    config: StrategyConfig
    trade_size_usd: float
    saved_usd: float = 0.0
    topups_usd: float = 0.0
    floor_resets: int = 0
    consecutive_losses: int = 0
    pause_until_ms: int = 0
    cycles: int = 0
    pauses: int = 0
    paused_ms: int = 0

    @property
    def compounding(self) -> bool:
        return self.lane in ("b", "c")

    def can_enter(self, now_ms: int) -> bool:
        if self.compounding and self.cycles >= self.config.max_cycles:
            return False
        if self.compounding and now_ms < self.pause_until_ms:
            return False
        return True

    def on_close(
        self, entry_cost_usd: float, proceeds_usd: float, exit_ts_ms: int
    ) -> None:
        self.cycles += 1
        if not self.compounding:
            return
        net = proceeds_usd
        if net > entry_cost_usd:
            self.saved_usd += net * self.config.win_savings_fraction
            self.trade_size_usd = net * self.config.win_reinvestment_fraction
            self.consecutive_losses = 0
        else:
            self.trade_size_usd = max(0.0, net)
            self.consecutive_losses += 1
            if self.consecutive_losses >= self.config.losses_before_pause:
                self.pause_until_ms = exit_ts_ms + self.config.loss_pause_ms
                self.pauses += 1
                self.consecutive_losses = 0
        if (
            self.lane == "b"
            and self.trade_size_usd < self.config.play_floor_usd
        ):
            topup = self.config.starting_trade_size_usd - self.trade_size_usd
            self.topups_usd += topup
            self.trade_size_usd = self.config.starting_trade_size_usd
            self.floor_resets += 1


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass
class CandidateRecord:
    mint: str
    name: str
    evaluated_ts_ms: int
    age_seconds: float
    stats: CandidateStats
    reasons: list[str]
    score: dict[str, float | None]
    entered: bool


@dataclass
class TradeRecord:
    mint: str
    name: str
    launch_ts_ms: int
    entry_ts_ms: int
    exit_ts_ms: int | None
    entry_age_seconds: float
    entry_cost_usd: float
    entry_tokens: float
    entry_reference_price_usd: float
    entry_fill_price_usd: float
    exit_reference_price_usd: float | None
    exit_fill_price_usd: float | None
    proceeds_usd: float | None
    pnl_usd: float | None
    return_pct: float | None
    exit_reason: str
    price_multiple: float | None
    liquidity_usd_at_entry: float | None
    liquidity_usd_at_exit: float | None
    cycle: int
    trade_size_usd: float
    quote_usd: float
    still_open: bool = False
    entry_impact_bps: float | None = None
    exit_impact_bps: float | None = None
    dead_pool_since_ms: int | None = None
    failed_exit_attempts: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            key: getattr(self, key) for key in self.__dataclass_fields__
        }


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _timestamp_ms(event: dict[str, Any]) -> int | None:
    value = event.get("timestamp")
    if value is None:
        value = event.get("timestamp_ms")
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def iter_zst_events(path: Path) -> Iterator[dict[str, Any]]:
    with path.open("rb") as source:
        reader = zstd.ZstdDecompressor().stream_reader(
            source, read_across_frames=True
        )
        with io.BufferedReader(reader, buffer_size=4 << 20) as stream:
            for line in stream:
                if not line or line.isspace():
                    continue
                try:
                    event = orjson.loads(line)
                except Exception:
                    continue
                if isinstance(event, dict):
                    yield event


def pool_day_files(replay_dir: Path, day: str) -> list[Path]:
    directory = replay_dir / day
    if not directory.is_dir():
        raise SystemExit(f"replay day directory not found: {directory}")
    files = sorted(directory.glob("*.jsonl.zst"))
    if not files:
        raise SystemExit(f"no .jsonl.zst archives in {directory}")
    # Two layouts exist: ``HH.jsonl.zst`` (the whole directory is one day) and
    # ``YYYYMMDD_HH.jsonl.zst`` (2026-11-08 spills into the next date). Only
    # the latter needs a date filter, otherwise a 32-hour day would be run.
    compact = day.replace("-", "")
    if files and files[0].name[:8].isdigit():
        files = [path for path in files if path.name.startswith(compact)]
        if not files:
            raise SystemExit(f"no archives for {day} in {directory}")
    return files


# ---------------------------------------------------------------------------
# Stream simulation
# ---------------------------------------------------------------------------


def channel_rejection_reasons(
    stats: CandidateStats, config: StrategyConfig, *, creator_buy_known: bool
) -> list[str]:
    """Rejections for a migration card.

    The card fires after the 300 s creation window, so the age filter cannot be
    applied. The creator-buy cap is applied exactly when the series carries a
    usable create value; when it does not, the absence is an adaptation, not a
    silent pass of a value that exists.
    """
    return rejection_reasons(
        stats,
        config,
        require_age=False,
        require_creator_buy=creator_buy_known,
    )


def dead_pool_step(
    dead_since_ms: int | None, now_ms: int, retry_ms: int
) -> tuple[bool, int, int]:
    """Spec dead-pool state: wait ``retry_ms`` after the first observation.

    Returns ``(attempt, dead_since_ms, next_check_ms)``. The first low-liquidity
    observation only starts the clock, so no sell is attempted before 600 s
    have passed; after an attempt the next check is a further 600 s later. A
    merely positive constant-product output is not evidence of an executable
    route, so the wait is never skipped.
    """
    if dead_since_ms is None:
        return False, now_ms, now_ms + retry_ms
    if now_ms - dead_since_ms < retry_ms:
        return False, dead_since_ms, dead_since_ms + retry_ms
    return True, dead_since_ms, now_ms + retry_ms


def entry_impact_bps(
    state: PoolState, tokens: float, size_usd: float, fee_rate: float
) -> float | None:
    """Fee-excluded entry impact against the pool mark, in basis points."""
    reference = state.mark_price_usd()
    if not reference or tokens <= 0:
        return None
    paid = size_usd * (1.0 - fee_rate) / tokens
    return (paid / reference - 1.0) * 10_000.0


def exit_impact_bps(
    state: PoolState,
    tokens: float,
    proceeds_quote: float,
    quote_usd: float,
    fee_rate: float,
) -> float | None:
    """Fee-excluded exit impact against the pool mark, in basis points."""
    reference = state.mark_price_usd()
    if not reference or tokens <= 0 or quote_usd <= 0:
        return None
    gross = proceeds_quote / (1.0 - fee_rate) * quote_usd / tokens
    return (1.0 - gross / reference) * 10_000.0


PUMP_VENUES = ("pump", "pump-amm")


def continues_pump_lineage(prev: PoolState, new: PoolState) -> bool:
    """True when ``new`` is the same Pump pool, or its one-way AMM migration.

    A mint can be printed by several venues at once (a Meteora or Raydium pool
    alongside Pump's). Following whichever record came last made the mark jump
    between unrelated reserves; one such switch turned a 2x trade into a 60x
    artefact on 2026-08-09. Only Pump's own curve and its AMM successor are the
    same tradeable pool, and the migration happens once, in one direction.
    """
    if new.pool not in PUMP_VENUES:
        return False
    if (
        prev.quote_mint is not None
        and new.quote_mint is not None
        and prev.quote_mint != new.quote_mint
    ):
        return False
    if new.pool_id and prev.pool_id and new.pool_id == prev.pool_id:
        return True
    if not (prev.pool == "pump" and new.pool == "pump-amm"):
        return False
    # The migration must not move the price: where it does (malformed records,
    # a different pool), the position is not marked or exited on that jump.
    before = prev.reserve_price_quote()
    after = new.reserve_price_quote()
    if before and after and not (0.2 <= after / before <= 5.0):
        return False
    return True


@dataclass
class TokenTrack:
    mint: str
    t0_ms: int
    name: str
    symbol: str
    creator_buy_sol: float | None
    trades: deque[tuple[int, float, float, bool]] = field(default_factory=deque)
    state: PoolState | None = None
    evaluated: bool = False
    pool_id: str = ""
    quote_mint: str | None = None

    def accept_state(self, state: PoolState) -> bool:
        """Adopt ``state`` only if it belongs to this token's Pump pool."""
        state = state.with_carry(self.state)
        if state.pool not in PUMP_VENUES:
            return False
        if self.state is None:
            self.state = state
        elif continues_pump_lineage(self.state, state):
            self.state = state
        else:
            return False
        self.pool_id = state.pool_id or self.pool_id
        self.quote_mint = state.quote_mint or self.quote_mint
        return True

    def retire(self) -> None:
        """Mark the candidate final and release its print history.

        Only the pool state is needed after this point, and only while the
        token is the open position, so the trailing-print deque can go.
        """
        self.evaluated = True
        self.trades.clear()


def _trailing_stats(
    track: TokenTrack, now_ms: int
) -> tuple[float, int, int, int]:
    cutoff = now_ms - WINDOW_MS
    while track.trades and track.trades[0][0] <= cutoff:
        track.trades.popleft()
    volume = 0.0
    buys = 0
    sells = 0
    for ts, quote_amount, quote_usd, is_buy in track.trades:
        if ts <= now_ms:
            volume += quote_amount * quote_usd
            if is_buy:
                buys += 1
            else:
                sells += 1
    return volume, buys + sells, buys, sells


def _stats_from_track(
    track: TokenTrack, now_ms: int, config: StrategyConfig
) -> CandidateStats | None:
    state = track.state
    if state is None:
        return None
    volume, transactions, buys, sells = _trailing_stats(track, now_ms)
    return CandidateStats(
        mint=track.mint,
        age_seconds=max(0.0, (now_ms - track.t0_ms) / 1000.0),
        liquidity_usd=state.liquidity_usd(),
        market_cap_usd=state.market_cap_usd,
        volume_5m_usd=volume,
        transactions_5m=transactions,
        buys_5m=buys,
        sells_5m=sells,
        creator_buy_sol=track.creator_buy_sol,
        price_sol=state.price_sol,
        quote_usd=state.quote_usd,
        name=track.name,
        quote_mint=state.quote_mint,
        mark_price_usd=state.mark_price_usd(),
        price_discrepancy=state.price_discrepancy(),
    )


def simulate_stream_day(
    replay_dir: Path,
    day: str,
    config: StrategyConfig,
    lane: str,
    *,
    max_hours: int | None = None,
    sol_usd: float | None = None,
) -> dict[str, Any]:
    files = pool_day_files(replay_dir, day)
    if sol_usd is None:
        sol_usd = SOL_USD_BY_DAY.get(day) or estimate_sol_usd(files)
    if max_hours is not None:
        files = files[:max_hours]

    tracks: dict[str, TokenTrack] = {}
    heap: list[tuple[int, int, str, str]] = []
    sequence = 0
    position: TradeRecord | None = None
    ledger = Ledger(lane=lane, config=config, trade_size_usd=config.starting_trade_size_usd)
    candidates: list[CandidateRecord] = []
    trades: list[TradeRecord] = []
    rejections: Counter[str] = Counter()
    created = 0
    zero_trade_expired = 0
    busy_skipped = 0
    paused_skipped = 0
    cycle_capped = 0
    ranked_out = 0
    last_ts = 0

    def push(ts_ms: int, kind: str, mint: str) -> None:
        nonlocal sequence
        sequence += 1
        heapq.heappush(heap, (ts_ms, sequence, kind, mint))

    def close_position(
        proceeds_sol: float,
        state: PoolState,
        ts_ms: int,
        reason: str,
        price_multiple: float | None,
    ) -> bool:
        nonlocal position
        assert position is not None
        held_mint = position.mint
        quote_usd = state.quote_usd or position.quote_usd
        impact = exit_impact_bps(
            state, position.entry_tokens, proceeds_sol, quote_usd, config.fee_rate
        )
        if impact is not None and impact > config.sell_slippage_limit_bps:
            # No route at any allowed tier: keep the position, as the spec
            # requires, and retry on the next monitoring interval.
            position.failed_exit_attempts += 1
            return False
        proceeds_usd = proceeds_sol * quote_usd
        exit_fill = (
            proceeds_sol * quote_usd / position.entry_tokens
            if position.entry_tokens > 0
            else None
        )
        pnl = proceeds_usd - position.entry_cost_usd
        position.exit_ts_ms = ts_ms
        position.exit_reference_price_usd = state.mark_price_usd()
        position.exit_fill_price_usd = exit_fill
        position.proceeds_usd = proceeds_usd
        position.pnl_usd = pnl
        position.return_pct = (
            pnl / position.entry_cost_usd if position.entry_cost_usd else None
        )
        position.exit_reason = reason
        position.price_multiple = price_multiple
        position.exit_impact_bps = impact
        position.liquidity_usd_at_exit = state.liquidity_usd()
        position.still_open = False
        ledger.on_close(position.entry_cost_usd, proceeds_usd, ts_ms)
        trades.append(position)
        position = None
        tracks.pop(held_mint, None)
        return True

    def screen_candidate(
        mint: str, now_ms: int
    ) -> tuple[str, Any, Any, Any]:
        """Classify a candidate using only prints at or before ``now_ms``.

        Returns ``(verdict, stats, score, reasons)`` where verdict is one of
        ``skip``, ``expired``, ``retry``, ``reject`` or ``enter``.
        """
        track = tracks.get(mint)
        if track is None or track.evaluated:
            return ("skip", None, None, None)
        age_ms = now_ms - track.t0_ms
        if age_ms > config.max_token_age_ms:
            return ("expired", None, None, None)
        if not track.trades:
            return ("retry", None, None, None)
        stats = _stats_from_track(track, now_ms, config)
        if stats is None or stats.mark_price_usd is None or stats.quote_usd is None:
            return ("retry", None, None, None)
        reasons = rejection_reasons(stats, config)
        score = score_candidate(stats)
        if reasons:
            return ("reject", stats, score, reasons)
        return ("enter", stats, score, None)

    def enter(mint: str, now_ms: int, stats: CandidateStats, score: dict[str, Any]) -> None:
        nonlocal position
        track = tracks[mint]
        size_usd = ledger.trade_size_usd
        size_quote = size_usd / stats.quote_usd  # type: ignore[operator]
        tokens = buy_tokens(track.state, size_quote, config.fee_rate)  # type: ignore[arg-type]
        track.retire()
        if tokens is None or tokens <= 0:
            rejections["ENTRY_FILL_UNAVAILABLE"] += 1
            return
        impact = entry_impact_bps(
            track.state, tokens, size_usd, config.fee_rate  # type: ignore[arg-type]
        )
        if impact is not None and impact > config.entry_slippage_limit_bps:
            rejections["ENTRY_SLIPPAGE_TOO_HIGH"] += 1
            candidates.append(
                CandidateRecord(
                    mint=mint, name=track.name, evaluated_ts_ms=now_ms,
                    age_seconds=stats.age_seconds, stats=stats,
                    reasons=["ENTRY_SLIPPAGE_TOO_HIGH"], score=score, entered=False,
                )
            )
            finalize(mint)
            return
        candidates.append(
            CandidateRecord(
                mint=mint,
                name=track.name,
                evaluated_ts_ms=now_ms,
                age_seconds=stats.age_seconds,
                stats=stats,
                reasons=[],
                score=score,
                entered=True,
            )
        )
        position = TradeRecord(
            mint=mint,
            name=track.name,
            launch_ts_ms=track.t0_ms,
            entry_ts_ms=now_ms,
            exit_ts_ms=None,
            entry_age_seconds=stats.age_seconds,
            entry_cost_usd=size_usd,
            entry_tokens=tokens,
            entry_reference_price_usd=stats.mark_price_usd,  # type: ignore[arg-type]
            entry_fill_price_usd=size_usd / tokens,
            entry_impact_bps=impact,
            exit_reference_price_usd=None,
            exit_fill_price_usd=None,
            proceeds_usd=None,
            pnl_usd=None,
            return_pct=None,
            exit_reason="OPEN",
            price_multiple=None,
            liquidity_usd_at_entry=stats.liquidity_usd,
            liquidity_usd_at_exit=None,
            cycle=ledger.cycles + 1,
            trade_size_usd=size_usd,
            quote_usd=stats.quote_usd,  # type: ignore[arg-type]
            still_open=True,
        )
        push(now_ms + config.monitor_interval_ms, "monitor", mint)

    def finalize(mint: str) -> None:
        """Drop a rejected/expired/ranked-out track from memory."""
        track = tracks.pop(mint, None)
        if track is not None:
            track.retire()

    def handle_qualification_group(mints: list[str], now_ms: int) -> None:
        nonlocal position, zero_trade_expired, busy_skipped
        nonlocal paused_skipped, cycle_capped, ranked_out
        eligible: list[tuple[float, int, str, CandidateStats, dict[str, Any]]] = []
        for mint in mints:
            verdict, stats, score, reasons = screen_candidate(mint, now_ms)
            track = tracks.get(mint)
            if track is None:
                continue
            if verdict == "skip":
                continue
            if verdict == "retry":
                push(now_ms + config.retry_interval_ms, "qualify", mint)
                continue
            if verdict == "expired":
                finalize(mint)
                zero_trade_expired += 1
                continue
            if verdict == "reject":
                for reason in reasons:
                    rejections[reason] += 1
                candidates.append(
                    CandidateRecord(
                        mint=mint,
                        name=track.name,
                        evaluated_ts_ms=now_ms,
                        age_seconds=stats.age_seconds,
                        stats=stats,
                        reasons=reasons,
                        score=score,
                        entered=False,
                    )
                )
                finalize(mint)
                continue
            eligible.append(
                (float(score["score_100"]), track.t0_ms, mint, stats, score)
            )

        if not eligible:
            return
        # The spec takes one position at a time and, when several candidates
        # qualify at the same decision timestamp, the highest score_100 wins
        # (ties: earlier launch, then mint).
        eligible.sort(key=lambda item: (-item[0], item[1], item[2]))
        if position is not None:
            busy_skipped += len(eligible)
            for _score, _launch, mint, _stats, _sc in eligible:
                finalize(mint)
            return
        if not ledger.can_enter(now_ms):
            if ledger.compounding and ledger.cycles >= config.max_cycles:
                cycle_capped += len(eligible)
            else:
                paused_skipped += len(eligible)
            for _score, _launch, mint, _stats, _sc in eligible:
                finalize(mint)
            return
        _best_score, _launch, mint, stats, score = eligible[0]
        for _score, _launch, other, _stats, _sc in eligible[1:]:
            finalize(other)
            ranked_out += 1
        enter(mint, now_ms, stats, score)

    def handle_monitor(mint: str, now_ms: int) -> None:
        nonlocal position
        if position is None or position.mint != mint:
            return
        track = tracks.get(mint)
        if track is None or track.state is None:
            push(now_ms + config.monitor_interval_ms, "monitor", mint)
            return
        state = track.state
        if now_ms - state.ts_ms > config.max_mark_age_ms:
            push(now_ms + config.monitor_interval_ms, "monitor", mint)
            return
        liquidity = state.liquidity_usd()
        if liquidity is not None and liquidity < config.dead_pool_liquidity_usd:
            attempt, position.dead_pool_since_ms, next_check = dead_pool_step(
                position.dead_pool_since_ms, now_ms, config.dead_pool_retry_ms
            )
            if not attempt:
                push(next_check, "monitor", mint)
                return
            proceeds = sell_sol(state, position.entry_tokens, config.fee_rate)
            if (
                proceeds is not None
                and proceeds > 0
                and close_position(
                    proceeds, state, now_ms, "DEAD_POOL_RECOVERY", None
                )
            ):
                return
            push(now_ms + config.dead_pool_retry_ms, "monitor", mint)
            return
        position.dead_pool_since_ms = None
        if state.mark_price_usd() is None or position.entry_reference_price_usd <= 0:
            push(now_ms + config.monitor_interval_ms, "monitor", mint)
            return
        multiple = state.mark_price_usd() / position.entry_reference_price_usd  # type: ignore[operator]
        if multiple >= config.take_profit_multiple:
            proceeds = sell_sol(state, position.entry_tokens, config.fee_rate)
            if (
                proceeds is not None
                and proceeds > 0
                and close_position(proceeds, state, now_ms, "TAKE_PROFIT", multiple)
            ):
                return
            push(now_ms + config.monitor_interval_ms, "monitor", mint)
            return
        if multiple <= config.stop_loss_multiple:
            proceeds = sell_sol(state, position.entry_tokens, config.fee_rate)
            if (
                proceeds is not None
                and proceeds > 0
                and close_position(proceeds, state, now_ms, "STOP_LOSS", multiple)
            ):
                return
            push(now_ms + config.monitor_interval_ms, "monitor", mint)
            return
        push(now_ms + config.monitor_interval_ms, "monitor", mint)

    def drain(now_ms: int) -> None:
        while heap and heap[0][0] <= now_ms:
            ts_ms = heap[0][0]
            monitors: list[str] = []
            qualifiers: list[str] = []
            while heap and heap[0][0] == ts_ms:
                _ts, _seq, kind, mint = heapq.heappop(heap)
                if kind == "monitor":
                    monitors.append(mint)
                elif kind == "qualify":
                    qualifiers.append(mint)
            # Close/advance open positions first so the slot can be reused at
            # the same instant, then rank the candidates that qualify together.
            for mint in monitors:
                handle_monitor(mint, ts_ms)
            if qualifiers:
                handle_qualification_group(qualifiers, ts_ms)

    for path in files:
        for event in iter_zst_events(path):
            action = event.get("action")
            if action not in ("create", "buy", "sell", "migrate", "add", "remove"):
                continue
            ts_ms = _timestamp_ms(event)
            if ts_ms is None:
                continue
            if ts_ms < last_ts - 2_000:
                # Out-of-order tail from a previous archive; ignore.
                continue
            if ts_ms > last_ts:
                last_ts = ts_ms
            drain(ts_ms)
            if action == "create":
                if event.get("pool") != "pump":
                    continue
                mint = event.get("mint")
                if not isinstance(mint, str) or not mint or mint in tracks:
                    continue
                state = pool_state_from_event(event, sol_usd)
                if state is None:
                    continue
                created += 1
                track = TokenTrack(
                    mint=mint,
                    t0_ms=ts_ms,
                    name=str(event.get("name") or ""),
                    symbol=str(event.get("symbol") or ""),
                    creator_buy_sol=creator_buy_in_sol(
                        _finite(event.get("quoteAmount")),
                        state.quote_usd,
                        sol_usd,
                    ),
                )
                if not track.accept_state(state):
                    continue
                tracks[mint] = track
                push(ts_ms + config.entry_delay_ms, "qualify", mint)
                continue
            mint = event.get("mint")
            if not isinstance(mint, str):
                continue
            track = tracks.get(mint)
            if track is None:
                continue
            state = pool_state_from_event(event, sol_usd)
            if state is not None and not track.accept_state(state):
                continue
            if action in ("buy", "sell") and not track.evaluated:
                quote_amount = _finite(event.get("quoteAmount"))
                quote_usd = track.state.quote_usd if track.state else None
                if quote_amount is not None and quote_usd:
                    track.trades.append(
                        (ts_ms, quote_amount, quote_usd, action == "buy")
                    )

    # End of data: resolve open position as open-at-end.
    still_open = position
    if still_open is not None:
        still_open.still_open = True
        still_open.exit_reason = "OPEN_AT_END"
        trades.append(still_open)
        position = None

    summary = summarize(
        strategy_id=STRATEGY_ID,
        source="stream",
        day=day,
        lane=lane,
        config=config,
        ledger=ledger,
        candidates=candidates,
        trades=trades,
        extra={
            "files_scanned": len(files),
            "creations_seen": created,
            "zero_trade_expired": zero_trade_expired,
            "skipped_busy": busy_skipped,
            "skipped_paused": paused_skipped,
            "skipped_cycle_cap": cycle_capped,
            "skipped_ranked_out": ranked_out,
            "rejections": dict(sorted(rejections.items())),
            "sol_usd_assumption": sol_usd,
        },
    )
    return summary


# ---------------------------------------------------------------------------
# Channel simulation
# ---------------------------------------------------------------------------


def load_channel_signals(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(newline="") as handle:
        for row in csv.DictReader(handle):
            mint = (row.get("mint") or "").strip()
            ts = row.get("signal_timestamp_ms")
            if not mint or not ts:
                continue
            try:
                ts_ms = int(ts)
            except ValueError:
                continue
            rows.append(
                {
                    "mint": mint,
                    "ts_ms": ts_ms,
                    "signal_id": row.get("signal_id"),
                    "signal_type": row.get("signal_type"),
                }
            )
    rows.sort(key=lambda item: (item["ts_ms"], item["mint"]))
    return rows


@dataclass
class MintSeries:
    """Point-in-time event series for one mint, sorted by timestamp."""

    ts: list[int]
    actions: list[str]
    quote_sol: list[float | None]
    states: list[PoolState]
    launch_ts_ms: int
    creator_buy_sol: float | None
    creator_buy_quote: float | None
    creator_buy_quote_usd: float | None
    name: str

    def index_at(self, ts_ms: int) -> int:
        return bisect.bisect_right(self.ts, ts_ms) - 1


def load_mint_series(
    replay_path: Path, mints: set[str], sol_usd: float | None = None
) -> dict[str, MintSeries]:
    raw: dict[str, list[tuple[int, str, float | None, PoolState, str, float | None]]] = {}
    for event in iter_zst_events(replay_path):
        mint = event.get("mint")
        if not isinstance(mint, str) or mint not in mints:
            continue
        action = str(event.get("action"))
        ts_ms = _timestamp_ms(event)
        if ts_ms is None:
            continue
        state = pool_state_from_event(event, sol_usd)
        if state is None:
            continue
        quote_sol = (
            _finite(event.get("quoteAmount"))
            if action in ("buy", "sell", "create")
            else None
        )
        raw.setdefault(mint, []).append(
            (ts_ms, action, quote_sol, state, str(event.get("name") or ""), None)
        )
    series: dict[str, MintSeries] = {}
    for mint, rows in raw.items():
        rows.sort(key=lambda item: item[0])
        # Two filters in one pass: carry conversion fields that add/remove
        # records omit, and keep only the mint's own Pump pool lineage so a
        # third-party pool cannot drive the mark.
        kept: list[tuple[int, str, float | None, PoolState, str, float | None]] = []
        prev_state: PoolState | None = None
        for row in rows:
            state = row[3].with_carry(prev_state)
            if state.pool not in PUMP_VENUES:
                continue
            if prev_state is not None and not continues_pump_lineage(
                prev_state, state
            ):
                continue
            prev_state = state
            kept.append((row[0], row[1], row[2], state, row[4], row[5]))
        if not kept:
            continue
        rows = kept
        launch_ts = rows[0][0]
        creator_buy: float | None = None
        creator_quote: float | None = None
        creator_quote_usd: float | None = None
        name = ""
        for ts, action, quote_sol, create_state, row_name, _ in rows:
            if action == "create":
                launch_ts = ts
                creator_quote = quote_sol
                creator_quote_usd = create_state.quote_usd
                creator_buy = creator_buy_in_sol(
                    quote_sol, create_state.quote_usd, sol_usd
                )
                name = row_name
                break
        series[mint] = MintSeries(
            ts=[row[0] for row in rows],
            actions=[row[1] for row in rows],
            quote_sol=[row[2] for row in rows],
            states=[row[3] for row in rows],
            launch_ts_ms=launch_ts,
            creator_buy_sol=creator_buy,
            creator_buy_quote=creator_quote,
            creator_buy_quote_usd=creator_quote_usd,
            name=name,
        )
    return series


def _channel_stats(
    series: MintSeries,
    now_ms: int,
    mint: str,
    config: StrategyConfig,
) -> CandidateStats | None:
    idx = series.index_at(now_ms)
    if idx < 0:
        return None
    state = series.states[idx]
    cutoff = now_ms - WINDOW_MS
    lo = bisect.bisect_right(series.ts, cutoff)
    volume = 0.0
    buys = 0
    sells = 0
    for i in range(lo, idx + 1):
        action = series.actions[i]
        if action not in ("buy", "sell"):
            continue
        quote_sol = series.quote_sol[i]
        if quote_sol is None:
            continue
        quote_usd = series.states[i].quote_usd or state.quote_usd or 0.0
        volume += quote_sol * quote_usd
        if action == "buy":
            buys += 1
        else:
            sells += 1
    return CandidateStats(
        mint=mint,
        age_seconds=max(0.0, (now_ms - series.launch_ts_ms) / 1000.0),
        liquidity_usd=state.liquidity_usd(),
        market_cap_usd=state.market_cap_usd,
        volume_5m_usd=volume,
        transactions_5m=buys + sells,
        buys_5m=buys,
        sells_5m=sells,
        creator_buy_sol=series.creator_buy_sol,
        price_sol=state.price_sol,
        quote_usd=state.quote_usd,
        name=series.name,
        quote_mint=state.quote_mint,
        mark_price_usd=state.mark_price_usd(),
        price_discrepancy=state.price_discrepancy(),
    )


def simulate_channel_day(
    signals_path: Path,
    replay_path: Path,
    config: StrategyConfig,
    lane: str,
    *,
    sol_usd: float | None = None,
) -> dict[str, Any]:
    day = signals_path.parent.name
    if sol_usd is None:
        # The table must be applied before the series are built: legacy
        # archives have no ``priceUsd``, so without it every record would be
        # missing its quote-USD conversion. The in-archive estimate is only a
        # fallback for days the table does not cover.
        sol_usd = SOL_USD_BY_DAY.get(day)
    signals = load_channel_signals(signals_path)
    mints = {signal["mint"] for signal in signals}
    series_by_mint = load_mint_series(replay_path, mints, sol_usd)
    if sol_usd is None:
        sol_usd = sol_usd_from_series(series_by_mint)
    for series in series_by_mint.values():
        if series.creator_buy_sol is None:
            series.creator_buy_sol = creator_buy_in_sol(
                series.creator_buy_quote,
                series.creator_buy_quote_usd,
                sol_usd,
            )
    ledger = Ledger(lane=lane, config=config, trade_size_usd=config.starting_trade_size_usd)
    trades: list[TradeRecord] = []
    candidates: list[CandidateRecord] = []
    rejections: Counter[str] = Counter()
    skipped_busy = 0
    skipped_paused = 0
    skipped_no_data = 0
    skipped_cycle_cap = 0
    position: TradeRecord | None = None
    # A position closed inside the forward walk occupies the lane until its
    # exit instant. Later signals must not be allowed to enter "into the past".
    busy_until_ms = 0
    n = len(signals)

    def close_position(
        proceeds_sol: float, state: PoolState, ts_ms: int, reason: str, multiple: float
    ) -> bool:
        nonlocal position, busy_until_ms
        assert position is not None
        quote_usd = state.quote_usd or position.quote_usd
        impact = exit_impact_bps(
            state, position.entry_tokens, proceeds_sol, quote_usd, config.fee_rate
        )
        if impact is not None and impact > config.sell_slippage_limit_bps:
            position.failed_exit_attempts += 1
            return False
        proceeds_usd = proceeds_sol * quote_usd
        pnl = proceeds_usd - position.entry_cost_usd
        position.exit_ts_ms = ts_ms
        position.exit_reference_price_usd = state.mark_price_usd()
        position.exit_fill_price_usd = (
            proceeds_sol * quote_usd / position.entry_tokens
            if position.entry_tokens > 0
            else None
        )
        position.proceeds_usd = proceeds_usd
        position.pnl_usd = pnl
        position.return_pct = pnl / position.entry_cost_usd if position.entry_cost_usd else None
        position.exit_reason = reason
        position.price_multiple = multiple
        position.liquidity_usd_at_exit = state.liquidity_usd()
        position.still_open = False
        ledger.on_close(position.entry_cost_usd, proceeds_usd, ts_ms)
        position.exit_impact_bps = impact
        trades.append(position)
        busy_until_ms = ts_ms
        position = None
        return True

    for signal in signals:
        entry_ts = signal["ts_ms"] + 5_000
        if entry_ts < busy_until_ms:
            skipped_busy += 1
            continue
        if not ledger.can_enter(entry_ts):
            if ledger.compounding and ledger.cycles >= config.max_cycles:
                skipped_cycle_cap += 1
            else:
                skipped_paused += 1
            continue
        series = series_by_mint.get(signal["mint"])
        if not series or not series.ts:
            skipped_no_data += 1
            continue
        stats = _channel_stats(series, entry_ts, signal["mint"], config)
        if stats is None or stats.mark_price_usd is None or stats.quote_usd is None:
            skipped_no_data += 1
            continue
        reasons = channel_rejection_reasons(
            stats, config, creator_buy_known=series.creator_buy_sol is not None
        )
        score = score_candidate(stats)
        if reasons:
            for reason in reasons:
                rejections[reason] += 1
            candidates.append(
                CandidateRecord(
                    mint=signal["mint"], name=series.name, evaluated_ts_ms=entry_ts,
                    age_seconds=stats.age_seconds, stats=stats,
                    reasons=reasons, score=score, entered=False,
                )
            )
            continue
        size_usd = ledger.trade_size_usd
        size_quote = size_usd / stats.quote_usd  # type: ignore[operator]
        idx = series.index_at(entry_ts)
        state = series.states[idx]
        tokens = buy_tokens(state, size_quote, config.fee_rate)
        if tokens is None or tokens <= 0:
            rejections["ENTRY_FILL_UNAVAILABLE"] += 1
            continue
        impact = entry_impact_bps(state, tokens, size_usd, config.fee_rate)
        if impact is not None and impact > config.entry_slippage_limit_bps:
            rejections["ENTRY_SLIPPAGE_TOO_HIGH"] += 1
            candidates.append(
                CandidateRecord(
                    mint=signal["mint"], name=series.name, evaluated_ts_ms=entry_ts,
                    age_seconds=stats.age_seconds, stats=stats,
                    reasons=["ENTRY_SLIPPAGE_TOO_HIGH"], score=score, entered=False,
                )
            )
            continue
        candidates.append(
            CandidateRecord(
                mint=signal["mint"], name=series.name, evaluated_ts_ms=entry_ts,
                age_seconds=stats.age_seconds, stats=stats,
                reasons=[], score=score, entered=True,
            )
        )
        position = TradeRecord(
            mint=signal["mint"], name=series.name,
            launch_ts_ms=series.launch_ts_ms, entry_ts_ms=entry_ts, exit_ts_ms=None,
            entry_age_seconds=stats.age_seconds, entry_cost_usd=size_usd,
            entry_tokens=tokens, entry_reference_price_usd=stats.mark_price_usd,
            entry_fill_price_usd=size_usd / tokens, entry_impact_bps=impact,
            exit_reference_price_usd=None, exit_fill_price_usd=None,
            proceeds_usd=None, pnl_usd=None, return_pct=None,
            exit_reason="OPEN", price_multiple=None,
            liquidity_usd_at_entry=stats.liquidity_usd,
            liquidity_usd_at_exit=None, cycle=ledger.cycles + 1,
            trade_size_usd=size_usd, quote_usd=stats.quote_usd, still_open=True,
        )
        # Walk forward on the 8 s grid until a trigger or the data ends.
        cursor = entry_ts + config.monitor_interval_ms
        dead_since: int | None = None
        last_ts = series.ts[-1]
        while cursor <= last_ts and position is not None:
            j = series.index_at(cursor)
            if j < 0:
                cursor += config.monitor_interval_ms
                continue
            row_state = series.states[j]
            if cursor - row_state.ts_ms > config.max_mark_age_ms:
                cursor += config.monitor_interval_ms
                continue
            liquidity = row_state.liquidity_usd()
            if liquidity is not None and liquidity < config.dead_pool_liquidity_usd:
                attempt, dead_since, next_check = dead_pool_step(
                    dead_since, cursor, config.dead_pool_retry_ms
                )
                if not attempt:
                    cursor = next_check
                    continue
                proceeds = sell_sol(row_state, position.entry_tokens, config.fee_rate)
                if (
                    proceeds is not None
                    and proceeds > 0
                    and close_position(
                        proceeds, row_state, cursor, "DEAD_POOL_RECOVERY", 0.0
                    )
                ):
                    break
                cursor += config.dead_pool_retry_ms
                continue
            dead_since = None
            if row_state.mark_price_usd() is None or position.entry_reference_price_usd <= 0:
                cursor += config.monitor_interval_ms
                continue
            multiple = row_state.mark_price_usd() / position.entry_reference_price_usd  # type: ignore[operator]
            if multiple >= config.take_profit_multiple:
                proceeds = sell_sol(row_state, position.entry_tokens, config.fee_rate)
                if (
                    proceeds is not None
                    and proceeds > 0
                    and close_position(
                        proceeds, row_state, cursor, "TAKE_PROFIT", multiple
                    )
                ):
                    break
            elif multiple <= config.stop_loss_multiple:
                proceeds = sell_sol(row_state, position.entry_tokens, config.fee_rate)
                if (
                    proceeds is not None
                    and proceeds > 0
                    and close_position(
                        proceeds, row_state, cursor, "STOP_LOSS", multiple
                    )
                ):
                    break
            cursor += config.monitor_interval_ms
        if position is not None:
            position.still_open = True
            position.exit_reason = "OPEN_AT_END"
            trades.append(position)
            busy_until_ms = math.inf
            position = None

    return summarize(
        strategy_id=CHANNEL_STRATEGY_ID,
        source="channel",
        day=signals_path.parent.name,
        lane=lane,
        config=config,
        ledger=ledger,
        candidates=candidates,
        trades=trades,
        extra={
            "signals": n,
            "signals_with_series": len(series_by_mint),
            "skipped_busy": skipped_busy,
            "skipped_paused": skipped_paused,
            "skipped_cycle_cap": skipped_cycle_cap,
            "skipped_no_data": skipped_no_data,
            "rejections": dict(sorted(rejections.items())),
            "adaptations": [
                "token-age filter not applied (cards fire after the 300 s window)",
                "creator-buy filter applied whenever a create event is present",
                "one position at a time, entry at card timestamp + 5 s",
            ],
            "sol_usd_assumption": sol_usd,
        },
    )


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


def _pct(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    return float(np.percentile(np.asarray(values, dtype=float), q))


def summarize(
    *,
    strategy_id: str,
    source: str,
    day: str,
    lane: str,
    config: StrategyConfig,
    ledger: Ledger,
    candidates: list[CandidateRecord],
    trades: list[TradeRecord],
    extra: dict[str, Any],
) -> dict[str, Any]:
    closed = [trade for trade in trades if not trade.still_open]
    open_trades = [trade for trade in trades if trade.still_open]
    returns = [t.return_pct for t in closed if t.return_pct is not None]
    pnls = [t.pnl_usd for t in closed if t.pnl_usd is not None]
    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r <= 0]
    avg_win = float(np.mean(wins)) if wins else None
    avg_loss = float(np.mean(losses)) if losses else None
    break_even = (
        abs(avg_loss) / (avg_win + abs(avg_loss))
        if avg_win is not None and avg_loss is not None and (avg_win + abs(avg_loss)) > 0
        else None
    )
    gross_win = sum(p for p in pnls if p > 0)
    gross_loss = -sum(p for p in pnls if p <= 0)
    profit_factor = gross_win / gross_loss if gross_loss > 0 else None
    sorted_pnls = sorted(pnls, reverse=True)

    def without_top(k: int) -> float:
        return float(sum(sorted_pnls[k:]))

    reasons = Counter(trade.exit_reason for trade in trades)
    entered = sum(1 for c in candidates if c.entered)
    evaluated = len(candidates)
    # Write-off sensitivity: open positions valued at zero proceeds.
    writeoff_pnl = sum(pnls) + sum(-t.entry_cost_usd for t in open_trades)

    return {
        "strategy_id": strategy_id,
        "source": source,
        "day": day,
        "lane": lane,
        "generated_at": datetime.now(UTC).isoformat(),
        "config": config.as_dict(),
        "config_hash": __import__("hashlib").sha256(
            json.dumps(config.as_dict(), sort_keys=True).encode()
        ).hexdigest()[:16],
        # One artifact is one independent daily compounding run: the ledger
        # starts at the configured stake each day, so the 30-cycle cap, saved
        # balance and pause state do not cross day boundaries. Aggregate
        # reports this boundary rather than implying a continuous account.
        "run_boundary": "one_compounding_run_per_day",
        "execution_model": config.execution_model,
        "signals": {
            "evaluated": evaluated,
            "entered": entered,
            "entry_rate": entered / evaluated if evaluated else None,
            **extra,
        },
        "outcomes": {
            "trades": len(closed),
            "open_at_end": len(open_trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": len(wins) / len(closed) if closed else None,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "median_win": _pct(wins, 50),
            "median_loss": _pct(losses, 50),
            "break_even_win_rate": break_even,
            "pnl_usd": float(sum(pnls)),
            "profit_factor": profit_factor,
            "best_1": sorted_pnls[0] if sorted_pnls else None,
            "best_3": sum(sorted_pnls[:3]),
            "best_5": sum(sorted_pnls[:5]),
            "pnl_without_best_1": without_top(1),
            "pnl_without_best_3": without_top(3),
            "pnl_without_best_5": without_top(5),
            "writeoff_pnl_usd": writeoff_pnl,
            "exit_reasons": dict(reasons),
        },
        "compounding": {
            "lane": lane,
            "starting_trade_size_usd": config.starting_trade_size_usd,
            "ending_trade_size_usd": ledger.trade_size_usd,
            "saved_balance_usd": ledger.saved_usd,
            "external_topups_usd": ledger.topups_usd,
            "floor_resets": ledger.floor_resets,
            "consecutive_loss_pauses": ledger.pauses,
            "completed_cycles": ledger.cycles,
            "return_on_traded_notional": (
                float(sum(pnls)) / sum(t.entry_cost_usd for t in closed)
                if closed
                else None
            ),
        },
        "execution": {
            "fee_rate_per_side": config.fee_rate,
            "take_profit_multiple": config.take_profit_multiple,
            "stop_loss_multiple": config.stop_loss_multiple,
            "monitor_interval_ms": config.monitor_interval_ms,
            "entry_delay_ms": config.entry_delay_ms,
            "max_hold_ms": None,
        },
        "trades": [trade.as_dict() for trade in trades],
        "candidates": [
            {
                "mint": c.mint,
                "evaluated_ts_ms": c.evaluated_ts_ms,
                "age_seconds": c.age_seconds,
                "reasons": c.reasons,
                "entered": c.entered,
                "score_100": c.score["score_100"],
                "raw_score_80": c.score["raw_score_80"],
                "liquidity_usd": c.stats.liquidity_usd,
                "market_cap_usd": c.stats.market_cap_usd,
                "volume_5m_usd": c.stats.volume_5m_usd,
                "transactions_5m": c.stats.transactions_5m,
                "buys_5m": c.stats.buys_5m,
                "sells_5m": c.stats.sells_5m,
                "creator_buy_sol": c.stats.creator_buy_sol,
                "quote_mint": c.stats.quote_mint,
                "quote_usd": c.stats.quote_usd,
                "mark_price_usd": c.stats.mark_price_usd,
                "price_sol": c.stats.price_sol,
                "price_discrepancy": c.stats.price_discrepancy,
                "score_complete": c.score["score_complete"],
            }
            for c in candidates
        ],
    }


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Pooled reporting
# ---------------------------------------------------------------------------


def wilson_interval(successes: int, trials: int, z: float = 1.96) -> tuple[float, float]:
    if trials <= 0:
        return (0.0, 1.0)
    p = successes / trials
    denom = 1.0 + z * z / trials
    centre = (p + z * z / (2 * trials)) / denom
    half = z * math.sqrt(p * (1 - p) / trials + z * z / (4 * trials * trials)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def bootstrap_day_runs(
    results: list[dict[str, Any]], *, seed: int = 20261126, resamples: int = 10_000
) -> tuple[float | None, list[float | None]]:
    """Block bootstrap over day-runs.

    One artifact is one independent daily compounding run, so the resampling
    unit is the whole day, not the individual trade. A day's realised total
    already contains that day's size path, 60/40 split, play floor, pause and
    cycle cap, so resampling days keeps the within-day dependence intact
    while giving the right uncertainty for "N independent daily runs".

    Replaying the ledger across concatenated days would model one continuous
    account instead; that is a different experiment, and no artifact uses
    that boundary (`run_boundary` is asserted here).
    """
    boundary = {result.get("run_boundary") for result in results}
    if boundary != {"one_compounding_run_per_day"}:
        raise ValueError(f"unsupported run boundary for bootstrap: {boundary}")
    daily = np.asarray(
        [
            sum(
                trade["pnl_usd"]
                for trade in result["trades"]
                if not trade["still_open"] and trade["pnl_usd"] is not None
            )
            for result in sorted(results, key=lambda item: item["day"])
        ],
        dtype=float,
    )
    if not len(daily):
        return None, [None, None]
    rng = np.random.default_rng(seed)
    draws = rng.integers(0, len(daily), size=(resamples, len(daily)))
    totals = daily[draws].sum(axis=1)
    return float((totals > 0).mean()), np.percentile(totals, [2.5, 97.5]).tolist()


def pooled_summary(
    results: list[dict[str, Any]], *, seed: int = 20261126, resamples: int = 10_000
) -> dict[str, Any]:
    """Pool closed trades from several day-runs of the same lane.

    The bootstrap resamples whole trades. That is optimistic about serial
    dependence (the lane holds one position at a time), so the interval is a
    lower bound on the real uncertainty.
    """
    all_trades = [trade for result in results for trade in result["trades"]]
    open_trades = [trade for trade in all_trades if trade["still_open"]]
    trades = [
        trade for trade in all_trades
        if not trade["still_open"] and trade["pnl_usd"] is not None
    ]
    returns = [t["return_pct"] for t in trades if t["return_pct"] is not None]
    pnls = [t["pnl_usd"] for t in trades]
    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r <= 0]
    avg_win = float(np.mean(wins)) if wins else None
    avg_loss = float(np.mean(losses)) if losses else None
    break_even = (
        abs(avg_loss) / (avg_win + abs(avg_loss))
        if avg_win is not None and avg_loss is not None and (avg_win + abs(avg_loss)) > 0
        else None
    )
    reasons: Counter[str] = Counter()
    for result in results:
        reasons.update(result["outcomes"]["exit_reasons"])
        reasons.pop("OPEN_AT_END", None)
    array = np.asarray(pnls, dtype=float)
    p_positive, ci = bootstrap_day_runs(results, seed=seed, resamples=resamples)
    win_low, win_high = wilson_interval(len(wins), len(returns))
    traded = sum(t["entry_cost_usd"] for t in trades)
    per_day: list[dict[str, Any]] = []
    for result in sorted(results, key=lambda r: r["day"]):
        closed = [
            t for t in result["trades"]
            if not t["still_open"] and t["pnl_usd"] is not None
        ]
        day_returns = [t["return_pct"] for t in closed if t["return_pct"] is not None]
        per_day.append(
            {
                "day": result["day"],
                "trades": len(closed),
                "open_at_end": sum(1 for t in result["trades"] if t["still_open"]),
                "pnl_usd": float(sum(t["pnl_usd"] for t in closed)),
                "win_rate": (
                    sum(1 for r in day_returns if r > 0) / len(day_returns)
                    if day_returns
                    else None
                ),
                "exits": {
                    reason: count
                    for reason, count in result["outcomes"]["exit_reasons"].items()
                    if reason != "OPEN_AT_END"
                },
            }
        )
    return {
        "lane": results[0]["lane"],
        "source": results[0]["source"],
        "strategy_id": results[0]["strategy_id"],
        "days": sorted(result["day"] for result in results),
        "trades": len(trades),
        "open_at_end": sum(
            1 for r in results for t in r["trades"] if t["still_open"]
        ),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(returns) if returns else None,
        "win_rate_ci": [win_low, win_high],
        "break_even_win_rate": break_even,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "pnl_usd": float(sum(pnls)),
        "pnl_writeoff_usd": float(sum(pnls))
        - sum(t["entry_cost_usd"] for t in open_trades),
        "traded_notional_usd": traded,
        "return_on_notional": float(sum(pnls)) / traded if traded else None,
        "mean_pnl_per_trade": float(np.mean(array)) if len(array) else None,
        "pnl_ci_usd": ci,
        "bootstrap": "block over day-runs, within-day ledger path preserved",
        "p_pnl_positive": p_positive,
        "exit_reasons": dict(reasons),
        "per_day": per_day,
        "config_hash": results[0]["config_hash"],
        "execution_model": results[0].get("execution_model"),
        "run_boundary": results[0].get("run_boundary"),
    }


def print_pooled(summary: dict[str, Any]) -> None:
    print(
        f"{summary['source']}/{summary['lane']}  days={','.join(summary['days'])}  "
        f"trades={summary['trades']} open={summary['open_at_end']}"
    )
    print(
        f"  model={summary.get('execution_model')}  "
        f"run={summary.get('run_boundary')}  "
        f"bootstrap={summary.get('bootstrap')}"
    )
    print(
        f"  win={_fmt_pct(summary['win_rate'])} "
        f"[{_fmt_pct(summary['win_rate_ci'][0])}, {_fmt_pct(summary['win_rate_ci'][1])}] "
        f"BE={_fmt_pct(summary['break_even_win_rate'])} "
        f"avg_win={_fmt_pct(summary['avg_win'])} avg_loss={_fmt_pct(summary['avg_loss'])}"
    )
    print(
        f"  PnL={_fmt_usd(summary['pnl_usd'])} USD over "
        f"{_fmt_usd(summary['traded_notional_usd'])} notional "
        f"({_fmt_pct(summary['return_on_notional'])})  "
        f"95% CI [{summary['pnl_ci_usd'][0]:+.2f}, {summary['pnl_ci_usd'][1]:+.2f}]  "
        f"P(PnL>0)={summary['p_pnl_positive']:.3f}  "
        f"writeoff={_fmt_usd(summary['pnl_writeoff_usd'])}"
    )
    print(f"  exits={summary['exit_reasons']}")
    for row in summary["per_day"]:
        print(
            f"    {row['day']}  n={row['trades']} open={row['open_at_end']} "
            f"PnL={_fmt_usd(row['pnl_usd'])} win={_fmt_pct(row['win_rate'])} "
            f"exits={row['exits']}"
        )


def _fmt_pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:+.2f}%"


def _fmt_usd(value: float | None) -> str:
    return "n/a" if value is None else f"{value:+.4f}"


def print_report(result: dict[str, Any]) -> None:
    print(f"strategy: {result['strategy_id']}  source={result['source']} "
          f"day={result['day']} lane={result['lane']}")
    cfg = result["config"]
    print(f"config_hash={result['config_hash']}  fee/side={cfg['fee_rate']}  "
          f"TP={cfg['take_profit_multiple']}x  SL={cfg['stop_loss_multiple']}x  "
          f"monitor={cfg['monitor_interval_ms']}ms")
    sig = result["signals"]
    print("signals: "
          + ", ".join(f"{k}={v}" for k, v in sig.items() if k != "rejections"))
    if sig.get("rejections"):
        print("rejections: " + ", ".join(
            f"{k}={v}" for k, v in sorted(sig["rejections"].items(), key=lambda kv: -kv[1])
        ))
    out = result["outcomes"]
    print(f"trades={out['trades']} open_at_end={out['open_at_end']} "
          f"win={_fmt_pct(out['win_rate'])} "
          f"avg_win={_fmt_pct(out['avg_win'])} avg_loss={_fmt_pct(out['avg_loss'])} "
          f"BE={_fmt_pct(out['break_even_win_rate'])}")
    print(f"PnL={out['pnl_usd']:+.4f} USD  PF={out['profit_factor']}  "
          f"writeoff={out['writeoff_pnl_usd']:+.4f} USD  "
          f"without_best_1={out['pnl_without_best_1']:+.4f}")
    print(f"exit_reasons: {out['exit_reasons']}")
    comp = result["compounding"]
    print("compounding: "
          f"size={comp['starting_trade_size_usd']}->{comp['ending_trade_size_usd']:.4f} "
          f"saved={comp['saved_balance_usd']:.4f} topups={comp['external_topups_usd']:.4f} "
          f"floor_resets={comp['floor_resets']} pauses={comp['consecutive_loss_pauses']} "
          f"cycles={comp['completed_cycles']}")
    if result.get("signals", {}).get("adaptations"):
        print("adaptations: " + "; ".join(result["signals"]["adaptations"]))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)

    stream = sub.add_parser("stream", help="token-creation stream lane")
    stream.add_argument("--day", required=True, help="YYYY-MM-DD")
    stream.add_argument(
        "--replay-dir",
        type=Path,
        default=Path("/mnt/HDD/PumpAPI_Replay_Zst"),
    )
    stream.add_argument("--lane", choices=("a", "b", "c"), default="a")
    stream.add_argument("--max-hours", type=int, default=None)
    stream.add_argument(
        "--sol-usd",
        type=float,
        default=None,
        help="override the day's SOL/USD rate (archives without priceUsd)",
    )
    stream.add_argument("--json", action="store_true")
    stream.add_argument("--output", type=Path, default=None)

    channel = sub.add_parser("channel", help="signal-channel adaptation lane")
    channel.add_argument("--day", required=True, help="YYYY-MM-DD")
    channel.add_argument(
        "--signals",
        type=Path,
        default=None,
        help="strategy_signals.csv (defaults to output/backtest/signals/<day>/)",
    )
    channel.add_argument(
        "--replay",
        type=Path,
        default=None,
        help="filtered events .jsonl.zst (defaults to the day's replay_filtered)",
    )
    channel.add_argument("--lane", choices=("a", "b", "c"), default="a")
    channel.add_argument(
        "--sol-usd",
        type=float,
        default=None,
        help="override the day's SOL/USD rate (archives without priceUsd)",
    )
    channel.add_argument("--json", action="store_true")
    channel.add_argument("--output", type=Path, default=None)

    aggregate = sub.add_parser("aggregate", help="pool several day-run artifacts")
    aggregate.add_argument(
        "--inputs", nargs="+", type=Path, required=True,
        help="JSON artifacts produced by the stream/channel subcommands",
    )
    aggregate.add_argument("--json", action="store_true")
    return parser


def _group_key(result: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(result["strategy_id"]),
        str(result["source"]),
        str(result["lane"]),
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = StrategyConfig()

    if args.mode == "aggregate":
        grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        hashes: dict[tuple[str, str, str], tuple[str, ...]] = {}
        for path in args.inputs:
            result = json.loads(path.read_text())
            key = _group_key(result)
            identity = (
                str(result["config_hash"]),
                str(result.get("execution_model")),
                str(result.get("run_boundary")),
                str(result.get("strategy_id")),
            )
            if key in hashes and hashes[key] != identity:
                raise SystemExit(
                    f"refusing to pool {key}: config {hashes[key]} vs {identity} ({path})"
                )
            hashes[key] = identity
            grouped.setdefault(key, []).append(result)
        summaries = [
            pooled_summary(results)
            for results in sorted(grouped.values(), key=lambda rs: (rs[0]["source"], rs[0]["lane"]))
        ]
        if args.json:
            print(json.dumps(summaries, indent=2, sort_keys=True, default=str))
        else:
            for summary in summaries:
                print_pooled(summary)
        return 0

    if args.mode == "stream":
        result = simulate_stream_day(
            args.replay_dir, args.day, config, args.lane,
            max_hours=args.max_hours, sol_usd=args.sol_usd,
        )
        default_dir = Path("output/compound_study")
    else:
        signals = args.signals or Path("output/backtest/signals") / args.day / "strategy_signals.csv"
        replay = args.replay or (
            Path("output/backtest/days")
            / args.day
            / "replay_filtered"
            / "target_mints_events.jsonl.zst"
        )
        result = simulate_channel_day(
            signals, replay, config, args.lane, sol_usd=args.sol_usd
        )
        default_dir = Path("output/compound_study")

    if args.json:
        payload = json.dumps(result, indent=2, sort_keys=True, default=str)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(payload + "\n")
        else:
            print(payload)
    else:
        print_report(result)
        target = args.output or (
            default_dir / args.day / f"{args.mode}_lane{args.lane}.json"
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(result, indent=2, sort_keys=True, default=str) + "\n")
        print(f"artifact: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
