"""Single shared paper wallet.

The research backtest sizes every signal independently at 1 SOL. A live account
cannot do that: it has one SOL balance and one token balance per mint. This
ledger makes the paper run behave like the account a live bot will use, so
"insufficient SOL" and "token balance already spent" surface during forward
testing instead of after wiring a real key.

The wallet is deliberately simple and synchronous. It never signs or sends
anything; it only tracks what the quoted fills would have done.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

DUST_SIZE = 1e-9


@dataclass(slots=True)
class WalletConfig:
    starting_sol: float = 10.0
    min_sol_reserve: float = 0.0

    def __post_init__(self) -> None:
        if self.starting_sol <= 0:
            raise ValueError("starting_sol must be positive")
        if self.min_sol_reserve < 0:
            raise ValueError("min_sol_reserve must be non-negative")
        if self.min_sol_reserve >= self.starting_sol:
            raise ValueError("min_sol_reserve must be below starting_sol")


class PaperWallet:
    def __init__(self, config: WalletConfig | None = None) -> None:
        self.config = config or WalletConfig()
        self.starting_sol = self.config.starting_sol
        self.sol_balance = self.config.starting_sol
        self.token_balances: dict[str, float] = {}
        self.fees_paid_sol = 0.0
        self.entries = 0
        self.exits = 0
        self.write_offs = 0

    # ------------------------------------------------------------------
    # Cash / position checks
    # ------------------------------------------------------------------
    def available_sol(self) -> float:
        return self.sol_balance - self.config.min_sol_reserve

    def can_afford(self, sol_amount: float) -> bool:
        return sol_amount >= 0 and self.available_sol() + 1e-12 >= sol_amount

    def token_balance(self, mint: str) -> float:
        return self.token_balances.get(mint, 0.0)

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------
    def reserve_entry(
        self, mint: str, size: float, sol_out: float, fee: float
    ) -> None:
        """Pay for a buy and credit the token balance."""
        total = sol_out + fee
        if not self.can_afford(total):
            raise ValueError(
                f"insufficient SOL: need {total:.9f}, have {self.available_sol():.9f}"
            )
        self.sol_balance -= total
        self.fees_paid_sol += fee
        self.token_balances[mint] = self.token_balances.get(mint, 0.0) + size
        self.entries += 1

    def settle_exit(
        self, mint: str, size_sold: float, sol_in: float, fee: float
    ) -> None:
        """Receive sale proceeds and remove the sold token amount."""
        balance = self.token_balances.get(mint, 0.0)
        if size_sold > balance + DUST_SIZE:
            raise ValueError(
                f"selling {size_sold} but wallet holds {balance} of {mint}"
            )
        remaining = max(0.0, balance - size_sold)
        if remaining <= DUST_SIZE:
            self.token_balances.pop(mint, None)
        else:
            self.token_balances[mint] = remaining
        self.sol_balance += sol_in - fee
        self.fees_paid_sol += fee
        self.exits += 1

    def write_off(self, mint: str, size: float) -> None:
        """Remove tokens that could not be sold; no SOL is credited."""
        balance = self.token_balances.get(mint, 0.0)
        remaining = max(0.0, balance - size)
        if remaining <= DUST_SIZE:
            self.token_balances.pop(mint, None)
        else:
            self.token_balances[mint] = remaining
        self.write_offs += 1

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------
    def equity_sol(self, marks: dict[str, float] | None = None) -> float:
        """Cash plus marked token value. Unmarked tokens count as zero."""
        marks = marks or {}
        value = self.sol_balance
        for mint, size in self.token_balances.items():
            price = marks.get(mint)
            if price is not None and price > 0:
                value += size * price
        return value

    def snapshot(self, marks: dict[str, float] | None = None) -> dict[str, Any]:
        return {
            "starting_sol": self.starting_sol,
            "sol_balance": self.sol_balance,
            "available_sol": self.available_sol(),
            "token_balances": dict(self.token_balances),
            "equity_sol": self.equity_sol(marks),
            "fees_paid_sol": self.fees_paid_sol,
            "entries": self.entries,
            "exits": self.exits,
            "write_offs": self.write_offs,
        }
