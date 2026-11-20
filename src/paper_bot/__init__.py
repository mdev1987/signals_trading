"""Telegram Solana BUY-signal bot.

The package mirrors the research backtest state machine. In the default paper
mode it never signs or broadcasts: fills are quoted through Jupiter Swap API V2
``GET /order`` without a ``taker``. With ``--mode live`` it signs the returned
transaction locally and broadcasts it through ``POST /execute``; that path
requires an explicit ``LIVE_TRADING_ENABLED=1`` opt-in.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
