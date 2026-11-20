"""Replay-derived liquidity/tail screen, vendored for standalone deployment.

This is a copy of ``backtest_signals.safety_check``. The thresholds live in
:class:`paper_bot.engine.SafetyConfig`; the function only compares them.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any


def safety_check(
    stats: dict[str, Any], args: SimpleNamespace
) -> tuple[bool, list[str]]:
    """Return ``(passed, failing_reasons)`` for the replay-derived screen."""
    reasons: list[str] = []
    # Cold start: the live index has not seen this mint before the signal.
    # Reject explicitly rather than comparing ``None`` or treating zeros as
    # evidence that the token is safe.
    if stats.get("hist_available") is False:
        return False, ["safety_no_pre_signal_history"]
    if stats["pre_trades"] < args.min_pre_trades:
        reasons.append("safety_few_trades")
    if stats["pre_signers"] < args.min_pre_signers:
        reasons.append("safety_few_signers")
    if stats["age_min"] < args.min_age_min:
        reasons.append("safety_young_token")
    if stats["pre_volume_sol"] < args.min_pre_volume_sol:
        reasons.append("safety_low_volume")
    if stats["dd_from_high"] < args.max_dd_from_high:
        reasons.append("safety_deep_drawdown")
    return (not reasons), reasons


__all__ = ["safety_check"]
