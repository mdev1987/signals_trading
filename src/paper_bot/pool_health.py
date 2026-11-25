"""Pool-health measurements for open positions. Observational only.

This module is the data layer the execution-research plan calls for. It does
not gate entries, does not trigger exits, and is not imported by the engine:
it turns a :class:`paper_bot.models.TradeEvent` into reserve state, prices a
position-sized sell against a constant-product pool, and produces one
snapshot row with hard-separated column groups.

Three rules are enforced by construction:

* **Observed**, **derived** and **label** columns never share a name space.
  They are prefixed ``obs_``, ``der_`` and ``label_``, so a predictor cannot be
  confused with an outcome.
* No derived value uses information after its snapshot timestamp. The
  ``peak_*`` inputs are supplied by the caller from earlier rows only.
* A label is computed from the realized exit alone, never from a predictor
  threshold. ``label_exit`` is the only label function here and it mirrors the
  engine's own ``NEAR_ZERO_EXIT_RATIO``.

The sell model is deliberately the same one the research simulator uses: a
constant-product sale with the feed's all-in SOL fee, applied to the gross
quote output. ``src/tests/test_pool_health.py`` pins it against
``execution_simulator.ExecutionSimulator`` so the two cannot drift silently.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .models import WSOL_MINT, TradeEvent

# Must match ``paper_bot.engine.NEAR_ZERO_EXIT_RATIO``. A labeled outcome, not
# a tunable: the point of the dataset is to find predictors of *this* event.
NEAR_ZERO_EXIT_RATIO = 0.01

# Mirrors ``execution_simulator``: a drained pool reports dust reserves that
# would otherwise look like infinite depth at a price of 1 SOL/token.
MIN_QUOTE_RESERVE_SOL = 1e-4
MIN_TOKEN_RESERVE = 1e-3

BONDING_CURVE_POOLS = frozenset({"pump", "raydium-launchpad"})
VAULT_CP_POOLS = frozenset({"raydium-cpmm", "meteora-damm-v2"})
PUMP_AMM_POOL = "pump-amm"
CONCENTRATED_LIQUIDITY_POOLS = frozenset(
    {"raydium-clmm", "meteora-dlmm", "orca-whirlpool"}
)

OBSERVED_COLUMNS = (
    "obs_run_id",
    "obs_mint",
    "obs_timestamp_ms",
    "obs_pool",
    "obs_reserve_model",
    "obs_token_reserve",
    "obs_quote_reserve",
    "obs_quote_price_sol",
    "obs_fee_rate",
    "obs_pool_age_ms",
    "obs_entry_cost_sol",
    "obs_size_tokens",
    "obs_reserve_exit_value_sol",
    "obs_jupiter_exit_quote_sol",
    "obs_jupiter_quote_status",
    "obs_quote_timestamp_ms",
    "obs_unsupported_reason",
)

DERIVED_COLUMNS = (
    "der_exit_value_ratio",
    "der_drawdown_from_entry",
    "der_drawdown_from_peak",
    "der_reserve_model_error",
    "der_net_flow_sol",
    "der_trade_count",
    "der_time_since_last_print_ms",
)

LABEL_COLUMNS = ("label_exit",)

EXIT_NORMAL = "NORMAL_EXIT"
EXIT_NEAR_ZERO = "NEAR_ZERO_EXIT"
EXIT_ROUTE_FAILURE = "ROUTE_FAILURE"
EXIT_LIQUIDITY_COLLAPSE = "LIQUIDITY_COLLAPSE"


@dataclass(frozen=True, slots=True)
class ReserveState:
    """Post-event pool state, normalized to a constant-product identity.

    ``unsupported_reason`` is set instead of raising: an unusable snapshot is
    still evidence (the feed omitted reserves, the pool is concentrated
    liquidity, ...) and must be written, not dropped.
    """

    mint: str
    timestamp_ms: int
    pool: str | None
    reserve_model: str | None
    token_reserve: float | None
    quote_reserve: float | None
    quote_price_sol: float | None
    fee_rate: float | None
    unsupported_reason: str | None

    @property
    def usable(self) -> bool:
        return (
            self.unsupported_reason is None
            and self.token_reserve is not None
            and self.token_reserve > 0
            and self.quote_reserve is not None
            and self.quote_reserve > 0
            and self.quote_price_sol is not None
            and self.quote_price_sol > 0
            and self.fee_rate is not None
            and 0.0 <= self.fee_rate < 1.0
        )

    @property
    def spot_price_sol(self) -> float | None:
        if not self.usable:
            return None
        assert self.quote_reserve is not None
        assert self.token_reserve is not None
        assert self.quote_price_sol is not None
        return self.quote_reserve / self.token_reserve * self.quote_price_sol


def _unsupported(
    trade: TradeEvent, pool: str | None, reason: str
) -> ReserveState:
    return ReserveState(
        mint=trade.mint,
        timestamp_ms=trade.timestamp_ms,
        pool=pool,
        reserve_model=None,
        token_reserve=None,
        quote_reserve=None,
        quote_price_sol=trade.quote_price_sol,
        fee_rate=None,
        unsupported_reason=reason,
    )


def reserve_state_from_trade(trade: TradeEvent) -> ReserveState:
    """Normalize one event's reserves using the feed's own price identity.

    The rules mirror ``execution_simulator.snapshot_from_event``. A pool whose
    model cannot be priced is returned with ``unsupported_reason`` rather than
    guessed at; pricing a virtual-reserve pool as a plain vault would overstate
    depth by the size of the virtual reserve.
    """
    pool = trade.pool
    if not pool:
        return _unsupported(trade, pool, "missing_pool")

    quote_price_sol: float | None
    if trade.quote_mint == WSOL_MINT:
        quote_price_sol = 1.0
    else:
        quote_price_sol = trade.quote_price_sol
        if quote_price_sol is None or quote_price_sol <= 0:
            return _unsupported(trade, pool, "missing_quote_price")

    token_reserve: float | None = None
    quote_reserve: float | None = None
    reserve_model: str | None = None

    if pool in BONDING_CURVE_POOLS:
        token_reserve = trade.v_tokens_in_bonding_curve
        quote_reserve = trade.v_quote_in_bonding_curve
        reserve_model = "bonding_curve"
        if token_reserve is None or quote_reserve is None:
            return _unsupported(trade, pool, "missing_bonding_curve_reserves")
    elif pool == PUMP_AMM_POOL:
        tokens = trade.tokens_in_pool
        quote = trade.quote_in_pool
        if tokens is None or quote is None:
            return _unsupported(trade, pool, "missing_amm_reserves")
        virtual_tokens = trade.virtual_tokens_in_pool
        virtual_quote = trade.virtual_quote_in_pool
        virtual_tokens_active = virtual_tokens is not None and virtual_tokens != 0.0
        virtual_quote_active = virtual_quote is not None and virtual_quote != 0.0
        if virtual_tokens_active and virtual_quote_active:
            # The feed uses one virtual-reserve style per pool. Both non-zero
            # makes the price identity ambiguous, so refuse to guess.
            return _unsupported(trade, pool, "ambiguous_virtual_reserves")
        if virtual_tokens_active:
            assert virtual_tokens is not None
            token_reserve = tokens + virtual_tokens
            quote_reserve = quote
            reserve_model = "amm_virtual_tokens"
        elif virtual_quote_active:
            assert virtual_quote is not None
            token_reserve = tokens
            quote_reserve = quote + virtual_quote
            reserve_model = "amm_virtual_quote"
        else:
            return _unsupported(trade, pool, "missing_amm_reserves")
    elif pool in VAULT_CP_POOLS:
        token_reserve = trade.tokens_in_pool
        quote_reserve = trade.quote_in_pool
        reserve_model = "amm_vault"
        if token_reserve is None or quote_reserve is None:
            return _unsupported(trade, pool, "missing_amm_reserves")
    elif pool in CONCENTRATED_LIQUIDITY_POOLS:
        return _unsupported(trade, pool, "unsupported_concentrated_liquidity")
    else:
        return _unsupported(trade, pool, "unsupported_pool_model")

    if (
        token_reserve is None
        or quote_reserve is None
        or token_reserve <= 0
        or quote_reserve <= 0
    ):
        return _unsupported(trade, pool, "non_positive_reserves")
    if (
        token_reserve < MIN_TOKEN_RESERVE
        or quote_reserve * quote_price_sol < MIN_QUOTE_RESERVE_SOL
    ):
        return _unsupported(trade, pool, "pool_reserves_below_floor")

    fee_rate = (
        trade.sol_route_fee_rate
        if trade.sol_route_fee_rate is not None
        else trade.pool_fee_rate
    )
    if fee_rate is None or not 0.0 <= fee_rate < 1.0:
        return _unsupported(trade, pool, "missing_or_invalid_pool_fee_rate")

    return ReserveState(
        mint=trade.mint,
        timestamp_ms=trade.timestamp_ms,
        pool=pool,
        reserve_model=reserve_model,
        token_reserve=token_reserve,
        quote_reserve=quote_reserve,
        quote_price_sol=quote_price_sol,
        fee_rate=fee_rate,
        unsupported_reason=None,
    )


def sell_quote_sol(state: ReserveState, tokens_in: float) -> float | None:
    """Net SOL from selling ``tokens_in`` into the pool, or ``None``.

    Constant product, fee on the gross quote output, quote converted with the
    feed's ``quotePriceSol`` oracle. This is the same convention as
    ``ExecutionSimulator.execute(side="sell")``.
    """
    if not state.usable:
        return None
    if tokens_in <= 0:
        return None
    assert state.token_reserve is not None
    assert state.quote_reserve is not None
    assert state.quote_price_sol is not None
    assert state.fee_rate is not None
    if tokens_in >= state.token_reserve:
        return None
    quote_gross = state.quote_reserve * tokens_in / (state.token_reserve + tokens_in)
    quote_gross_sol = quote_gross * state.quote_price_sol
    net = quote_gross_sol * (1.0 - state.fee_rate)
    return net if net > 0 else None


def snapshot_row(
    *,
    run_id: str,
    state: ReserveState,
    entry_cost_sol: float,
    size_tokens: float,
    pool_age_ms: int | None,
    reserve_exit_value_sol: float | None,
    peak_reserve_exit_value_sol: float | None = None,
    jupiter_exit_quote_sol: float | None = None,
    jupiter_quote_status: str = "not_sampled",
    quote_timestamp_ms: int | None = None,
    net_flow_sol: float | None = None,
    trade_count: int | None = None,
    time_since_last_print_ms: int | None = None,
) -> dict[str, Any]:
    """One snapshot row with ``obs_``/``der_`` columns separated by prefix.

    ``reserve_exit_value_sol`` is the caller's full-position sale estimate from
    ``sell_quote_sol``; ``None`` means the reserve model could not price it.
    """
    exit_ratio = (
        reserve_exit_value_sol / entry_cost_sol
        if reserve_exit_value_sol is not None and entry_cost_sol > 0
        else None
    )
    drawdown_from_entry = None if exit_ratio is None else exit_ratio - 1.0
    drawdown_from_peak = (
        reserve_exit_value_sol / peak_reserve_exit_value_sol - 1.0
        if reserve_exit_value_sol is not None
        and peak_reserve_exit_value_sol is not None
        and peak_reserve_exit_value_sol > 0
        else None
    )
    model_error = (
        jupiter_exit_quote_sol / reserve_exit_value_sol - 1.0
        if jupiter_exit_quote_sol is not None
        and reserve_exit_value_sol is not None
        and reserve_exit_value_sol > 0
        else None
    )
    return {
        "obs_run_id": run_id,
        "obs_mint": state.mint,
        "obs_timestamp_ms": state.timestamp_ms,
        "obs_pool": state.pool,
        "obs_reserve_model": state.reserve_model,
        "obs_token_reserve": state.token_reserve,
        "obs_quote_reserve": state.quote_reserve,
        "obs_quote_price_sol": state.quote_price_sol,
        "obs_fee_rate": state.fee_rate,
        "obs_pool_age_ms": pool_age_ms,
        "obs_entry_cost_sol": entry_cost_sol,
        "obs_size_tokens": size_tokens,
        "obs_reserve_exit_value_sol": reserve_exit_value_sol,
        "obs_jupiter_exit_quote_sol": jupiter_exit_quote_sol,
        "obs_jupiter_quote_status": jupiter_quote_status,
        "obs_quote_timestamp_ms": quote_timestamp_ms,
        "obs_unsupported_reason": state.unsupported_reason,
        "der_exit_value_ratio": exit_ratio,
        "der_drawdown_from_entry": drawdown_from_entry,
        "der_drawdown_from_peak": drawdown_from_peak,
        "der_reserve_model_error": model_error,
        "der_net_flow_sol": net_flow_sol,
        "der_trade_count": trade_count,
        "der_time_since_last_print_ms": time_since_last_print_ms,
    }


def classify_exit(
    *,
    exit_value_sol: float | None,
    entry_cost_sol: float,
    route_failed: bool = False,
    min_reserve_ratio: float | None = None,
) -> str:
    """Label one closed position from its realized exit only.

    Precedence: a route failure is its own outcome; a sale that returned almost
    nothing is ``NEAR_ZERO_EXIT``; a position whose *observed* reserve value
    fell below 1 % before the horizon is ``LIQUIDITY_COLLAPSE`` even if the
    horizon sale routed. ``NEAR_ZERO_EXIT`` uses the engine's own ratio, so it
    is the same event the PnL accounting calls catastrophic, not a new cut.
    """
    if route_failed:
        return EXIT_ROUTE_FAILURE
    ratio = (
        exit_value_sol / entry_cost_sol
        if exit_value_sol is not None and entry_cost_sol > 0
        else None
    )
    if ratio is None or ratio < NEAR_ZERO_EXIT_RATIO:
        return EXIT_NEAR_ZERO
    if min_reserve_ratio is not None and min_reserve_ratio < NEAR_ZERO_EXIT_RATIO:
        return EXIT_LIQUIDITY_COLLAPSE
    return EXIT_NORMAL
