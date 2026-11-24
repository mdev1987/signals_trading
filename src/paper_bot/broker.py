"""Paper fill accounting, matching the research backtest cost model.

For a flat-cost run the maths is identical to ``backtest_signals``::

    size        = notional / entry_price
    entry_value = size * entry_price * (1 + slippage)
    entry_fee   = entry_value * fee
    exit_value  = size * exit_price * (1 - slippage)
    exit_fee    = exit_value * fee
    pnl         = exit_value - exit_fee - entry_value - entry_fee

The live price comes from a Jupiter quote; the backtest's fee and slippage are
then applied on top so paper results stay comparable with ``output/backtest``.
Jupiter's own ``priceImpact`` (percentage points) is stored as a diagnostic and
is not added a second time.

When a :class:`~paper_bot.wallet.PaperWallet` is attached, the broker also
enforces cash and token balances. A partial close sells a fraction of the
remaining position, which is how the engine works around a quote that only
routes for a smaller size.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .models import EntryFill, FillQuote, Position
from .quotes import QuoteError
from .wallet import DUST_SIZE, PaperWallet


@dataclass(slots=True)
class BrokerConfig:
    fee: float = 0.001
    slippage: float = 0.005
    notional_sol: float = 1.0

    def __post_init__(self) -> None:
        if self.fee < 0 or self.slippage < 0:
            raise ValueError("fee and slippage must be non-negative")
        if self.notional_sol <= 0:
            raise ValueError("notional_sol must be positive")


@dataclass(slots=True)
class ExitFill:
    exit_price_sol: float
    size_sold: float
    entry_basis_sol: float
    entry_fee_sol: float
    exit_value_sol: float
    exit_fee_sol: float
    exit_impact: float
    pnl_sol: float
    return_pct: float


class PaperBroker:
    def __init__(
        self, config: BrokerConfig, wallet: PaperWallet | None = None
    ) -> None:
        self.config = config
        self.wallet = wallet

    # ------------------------------------------------------------------
    # Entry
    # ------------------------------------------------------------------
    def open(self, quote: FillQuote) -> EntryFill:
        """Price a buy without touching the wallet."""
        price = quote.price_sol
        if not math.isfinite(price) or price <= 0:
            raise QuoteError("invalid_fill_price", str(price))
        size = self.config.notional_sol / price
        entry_value = size * price * (1.0 + self.config.slippage)
        entry_fee = entry_value * self.config.fee
        return EntryFill(
            entry_price_sol=price,
            size=size,
            entry_value_sol=entry_value,
            entry_fee_sol=entry_fee,
            entry_impact=self.config.slippage,
            quote_source=quote.source,
            quote=quote,
        )

    def reserve(self, entry: EntryFill, mint: str) -> None:
        """Commit a priced entry to the wallet, if one is attached."""
        if self.wallet is None:
            return
        try:
            self.wallet.reserve_entry(
                mint, entry.size, entry.entry_value_sol, entry.entry_fee_sol
            )
        except ValueError as exc:
            raise QuoteError("insufficient_sol", str(exc)) from exc

    # ------------------------------------------------------------------
    # Exit
    # ------------------------------------------------------------------
    def close(
        self, position: Position, quote: FillQuote, *, fraction: float = 1.0
    ) -> ExitFill:
        """Price a sale of ``fraction`` of the remaining position.

        The returned fill is *not* applied to the position or wallet; the
        engine applies it so a failed multi-step exit cannot half-mutate state.
        """
        price = quote.price_sol
        if not math.isfinite(price) or price <= 0:
            raise QuoteError("invalid_fill_price", str(price))
        if not 0 < fraction <= 1.0:
            raise QuoteError("invalid_fraction", str(fraction))
        size_sold = position.size * fraction
        if size_sold <= DUST_SIZE:
            raise QuoteError("exit_dust", str(size_sold))
        entry_basis = position.entry_value_sol * fraction
        entry_fee = position.entry_fee_sol * fraction
        exit_value = size_sold * price * (1.0 - self.config.slippage)
        exit_fee = exit_value * self.config.fee
        pnl = exit_value - exit_fee - entry_basis - entry_fee
        cost_basis = entry_basis + entry_fee
        return ExitFill(
            exit_price_sol=price,
            size_sold=size_sold,
            entry_basis_sol=entry_basis,
            entry_fee_sol=entry_fee,
            exit_value_sol=exit_value,
            exit_fee_sol=exit_fee,
            exit_impact=self.config.slippage,
            pnl_sol=pnl,
            return_pct=pnl / cost_basis if cost_basis > 0 else 0.0,
        )

    def settle(self, position: Position, fill: ExitFill) -> None:
        """Credit a priced sale to the wallet, if one is attached."""
        if self.wallet is None:
            return
        self.wallet.settle_exit(
            position.mint, fill.size_sold, fill.exit_value_sol, fill.exit_fee_sol
        )

    def write_off(self, position: Position) -> None:
        """Remove tokens that could not be sold at any tested size."""
        if self.wallet is None:
            return
        self.wallet.write_off(position.mint, position.size)
