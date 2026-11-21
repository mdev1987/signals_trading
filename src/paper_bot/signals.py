"""Signal parsing and the forward-test rule gate.

The regexes and address validation are vendored in :mod:`.signal_parser` so
this package runs on a VPS without the research scripts. They are copied from
the research pipeline and must stay in sync with it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .models import GateDecision, Signal
from .signal_parser import (
    classify_message,
    extract_address,
    infer_network,
    parse_signal_message,
)

SUPPORTED_RULES = (
    "baseline",
    "hot_money_continuation",
    "max_pump_gt_1",
    "max_pump_gt_1_pre_ret_ge_6_3",
    "max_pump_gt_1_pre_ret_ge_6_3_ex_crowded",
    "max_pump_gt_1_pre_ret_ge_6_3_prompt",
    "max_pump_gt_1_pre_ret_ge_10",
    "max_pump_gt_1_pre_ret_ge_10_warm",
    "exclude_crowded_pre",
    "signers_5m_le_147",
    "smart_wallet_only",
)

# Human-readable descriptions used by the startup report and meta.json.
RULE_DESCRIPTIONS = {
    "baseline": "every confirmed BUY signal",
    "hot_money_continuation": (
        "net buying in the 60 s before the alert > 9.46 SOL and alert "
        "Total Buy > 3 SOL; drops late-in-the-run buys (pre_ret_15m <= 2%) "
        "from the quality pool when the live feature is available; exit "
        "after 5 min"
    ),
    "max_pump_gt_1": "signal message says Max Pump > 1x",
    "max_pump_gt_1_pre_ret_ge_6_3": (
        "Max Pump > 1x and pre-signal return >= 6.3 (a +630% run-up)"
    ),
    "max_pump_gt_1_pre_ret_ge_6_3_ex_crowded": (
        "Max Pump > 1x, pre-signal return >= 6.3 (+630%), and not a crowded "
        "late chase (research subset plus tail-risk overlay)"
    ),
    "max_pump_gt_1_pre_ret_ge_6_3_prompt": (
        "Max Pump > 1x, pre-signal return >= 6.3 (+630%), and the Telegram "
        "message reached the bot within 5 s (research entry timing)"
    ),
    "max_pump_gt_1_pre_ret_ge_10": (
        "Max Pump > 1x and pre-signal return >= 10 (+1000% run-up); drops the "
        "weak-momentum band that lost in every live run"
    ),
    "max_pump_gt_1_pre_ret_ge_10_warm": (
        "Max Pump > 1x, pre-signal return >= 10 (+1000%), and at least 10 min "
        "of observed market history so pre_ret is not understated"
    ),
    "exclude_crowded_pre": (
        "drop crowded late chases: signers_5m > 147, trades_5m > 611, "
        "pre_ret <= 7.02; plus the post-signal 5 s buy-share clause"
    ),
    "signers_5m_le_147": "at most 147 unique signers in the prior 5 minutes",
    "smart_wallet_only": "signal is Smart-wallet tagged, not KOL tagged",
}

MAX_PUMP_THRESHOLD = 1.0
PRE_RET_THRESHOLD = 6.3

# Deployed hot-money continuation thresholds from
# ``src/ave_hot_money_strategy.py``. The flow cutoff is the train-day q80,
# fitted once on Oct 3-5 2026; the other two are fixed round floors. Do not
# retune these on live data.
#
# In-sample variant note (2026-11-21, from the 8-day out-of-sample study, see
# output/backtest/ave_signals/patterns_5m_8d/rule_variants.md): on the new
# Nov 18-20 days the pre-30m-volume gate halved the mean return and the win
# rate (OOS mean +28.4% over +51.5% without it, PnL +1.6 vs +12.6 SOL), while
# it added nothing that later-in-the-run filters did not add. It is therefore
# not kept; the flow + total-buy gate is the OOS-validated core, and the
# pre_ret_15m dip overlay below is a separate, optional quality filter.
DEFAULT_RULE = "hot_money_continuation"
HOT_MONEY_FLOW_1M_SOL = 9.456
HOT_MONEY_ALERT_TOTAL_BUY_SOL = 3.0
HOT_MONEY_DIP_PRE_RET_MAX = 2.0

# Live-derived thresholds (2026-11-17). Across 90 lane-A trades the pre_ret
# band below ~10 was the loss source in every run, and trades on tokens with
# less than ~10 min of observed history lost regardless of pre_ret (the live
# pre_ret proxy is understated when the observed window is short). See
# SIGNAL_PATTERN_REPORT.md 9.7 for the per-run breakdown.
MOMENTUM_PRE_RET_THRESHOLD = 10.0
MIN_OBSERVED_HISTORY_MIN = 10.0

# Research entry timing assumes the signal is actionable ~5 s after it was
# posted. Live Telegram delivery is frequently later than that (median ~16 s in
# the 2026-11-16 run), and late entries are the measured loss source, so the
# prompt variant only trades signals that arrived inside this window.
MAX_DELIVERY_LAG_MS = 5_000

# Pre-signal leaf of the research crowded late-chase rule.
CROWDED_PRE_SIGNERS_GT = 147
CROWDED_PRE_TRADES_GT = 611
CROWDED_PRE_RET_MAX = 7.02

TICKER_RE = re.compile(r"\$([^\s(]+)")
PLATFORM_RE = re.compile(r"\(from ([^)]+)\)")

# Second clause of the research "exclude crowded late chase" rule. Despite the
# name, ``bs_0_5s`` is the buy-volume share over the first 5 seconds after the
# signal. Entry is never earlier than signal + 5 s, so both this and
# ``entry_delay_ms`` are known before a fill and can gate the entry.
CROWDED_POST_BS_MAX = 0.71
CROWDED_POST_PRE_RET_MAX = 7.10
CROWDED_POST_ENTRY_DELAY_MS_MAX = 6_147

# Rules that include the post-signal half of the crowded-chase filter. The
# engine must evaluate this at the entry trigger, after the first 5 s of
# trades are known.
POST_SIGNAL_CROWDED_RULES = frozenset(
    {
        "exclude_crowded_pre",
        "max_pump_gt_1_pre_ret_ge_6_3_ex_crowded",
    }
)


def post_signal_crowded(
    *,
    pre_ret: float | None,
    buy_volume_share: float | None,
    entry_delay_ms: int,
) -> bool:
    """Return whether the post-signal clause excludes this entry."""
    if pre_ret is None or buy_volume_share is None:
        return False
    return (
        buy_volume_share <= CROWDED_POST_BS_MAX
        and pre_ret <= CROWDED_POST_PRE_RET_MAX
        and entry_delay_ms <= CROWDED_POST_ENTRY_DELAY_MS_MAX
    )


@dataclass(slots=True)
class GateConfig:
    rule: str = DEFAULT_RULE
    allow_candidate_address: bool = False
    min_mc_usd: float | None = None
    max_mc_usd: float | None = None
    require_history: bool = False

    def __post_init__(self) -> None:
        if self.rule not in SUPPORTED_RULES:
            raise ValueError(
                f"Unsupported rule {self.rule!r}; choose from {SUPPORTED_RULES}"
            )


def parse_signal_text(
    message: str,
    *,
    signal_ts_ms: int,
    received_ts_ms: int,
    channel: str = "unknown",
    message_id: int | None = None,
) -> Signal:
    """Turn one Telegram message into a :class:`Signal`.

    The message is classified with the same conservative rules as
    ``extract_buy_signals.py``. A message without a valid address still
    produces a Signal with ``token_address == ""`` so the gate can record why
    it was skipped instead of silently dropping it.
    """
    message_type, is_candidate, exclusion_reason = classify_message(message)
    address, address_type, method, status = extract_address(message)
    network = infer_network(message, address_type)
    features = parse_signal_message(message)
    if received_ts_ms > 0 and signal_ts_ms > 0:
        # Telegram delivery lag: how stale the signal already was when the bot
        # saw it. Recorded for every signal so the prompt-entry rule and the
        # post-hoc analysis use the same definition.
        features["delivery_lag_ms"] = max(0, received_ts_ms - signal_ts_ms)
    if exclusion_reason:
        features["exclusion_reason"] = exclusion_reason
    ticker_match = TICKER_RE.search(message)
    platform_match = PLATFORM_RE.search(message)
    if ticker_match:
        features["symbol"] = ticker_match.group(1).upper()
    if platform_match:
        features["platform"] = platform_match.group(1).strip()
    return Signal(
        token_address=address or "",
        signal_ts_ms=signal_ts_ms,
        received_ts_ms=received_ts_ms,
        channel=channel,
        message_id=message_id,
        message=message,
        network=network,
        address_status=status or "missing",
        address_type=address_type,
        address_extraction_method=method,
        message_type=message_type,
        is_buy_signal=bool(is_candidate and address),
        is_confirmed_buy_signal=bool(
            is_candidate and address and status == "confirmed"
        ),
        ticker=ticker_match.group(1).upper() if ticker_match else None,
        features=features,
    )


def _feature(signal: Signal, name: str) -> float | None:
    value = signal.features.get(name)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _crowded_pre(
    signers: float | None, trades: float | None, pre_ret: float | None
) -> bool:
    """Return whether the pre-signal features match the crowded-chase leaf."""
    return (
        signers is not None
        and trades is not None
        and pre_ret is not None
        and signers > CROWDED_PRE_SIGNERS_GT
        and trades > CROWDED_PRE_TRADES_GT
        and pre_ret <= CROWDED_PRE_RET_MAX
    )


def _rule_decision(signal: Signal, rule: str) -> tuple[bool, str | None]:
    """Evaluate one rule using only information available at signal time.

    ``exclude_crowded_pre`` and its combined variant also have a post-signal
    clause; the engine evaluates it at the entry trigger via
    :func:`post_signal_crowded`, once the first 5 seconds of trades and the
    actual entry delay are known.
    """
    if rule == "baseline":
        return True, None

    if rule == "hot_money_continuation":
        flow = _feature(signal, "hist_flow_1m_sol")
        total_buy = _feature(signal, "total_buy_sol")
        if flow is None:
            return False, "missing_feature:hist_flow_1m_sol"
        if total_buy is None:
            return False, "missing_feature:total_buy_sol"
        if flow <= HOT_MONEY_FLOW_1M_SOL:
            return False, "rule_flow_1m_lte_9_456"
        if total_buy <= HOT_MONEY_ALERT_TOTAL_BUY_SOL:
            return False, "rule_total_buy_lte_3"
        # Quality overlay, active only when the live feature is available:
        # buys still within 2% of the pre-run price won 67.6% on the
        # out-of-sample days (OOS median +9.2%), while buys that had already
        # moved the market 15 minutes out mostly lost. When the feature is
        # absent the gate passes, so stale/live data differences cannot
        # silently flip decisions.
        dip = _feature(signal, "hist_pre_ret_15m")
        if dip is not None and dip > HOT_MONEY_DIP_PRE_RET_MAX:
            return False, "rule_pre_ret_15m_gt_2"
        return True, None

    if rule == "max_pump_gt_1":
        value = _feature(signal, "max_pump")
        if value is None:
            return False, "missing_feature:max_pump"
        passed = value > MAX_PUMP_THRESHOLD
        return passed, None if passed else "rule_max_pump_le_1"

    if rule in {
        "max_pump_gt_1_pre_ret_ge_6_3",
        "max_pump_gt_1_pre_ret_ge_6_3_ex_crowded",
        "max_pump_gt_1_pre_ret_ge_6_3_prompt",
    }:
        pump = _feature(signal, "max_pump")
        pre_ret = _feature(signal, "hist_pre_ret")
        signers = _feature(signal, "hist_signers_5m")
        trades = _feature(signal, "hist_trades_5m")
        if pump is None:
            return False, "missing_feature:max_pump"
        if pre_ret is None:
            return False, "missing_feature:hist_pre_ret"
        if pre_ret < PRE_RET_THRESHOLD or pump <= MAX_PUMP_THRESHOLD:
            return False, "rule_max_pump_or_pre_ret"
        if rule == "max_pump_gt_1_pre_ret_ge_6_3_ex_crowded":
            if signers is None or trades is None:
                return False, "missing_feature:hist_crowd"
            if _crowded_pre(signers, trades, pre_ret):
                return False, "rule_crowded_late_chase"
        if rule == "max_pump_gt_1_pre_ret_ge_6_3_prompt":
            lag = _feature(signal, "delivery_lag_ms")
            if lag is None:
                return False, "missing_feature:delivery_lag_ms"
            if lag > MAX_DELIVERY_LAG_MS:
                return False, "rule_delivery_lag_gt_5s"
        return True, None

    if rule in {
        "max_pump_gt_1_pre_ret_ge_10",
        "max_pump_gt_1_pre_ret_ge_10_warm",
    }:
        pump = _feature(signal, "max_pump")
        pre_ret = _feature(signal, "hist_pre_ret")
        if pump is None:
            return False, "missing_feature:max_pump"
        if pre_ret is None:
            return False, "missing_feature:hist_pre_ret"
        if pump <= MAX_PUMP_THRESHOLD:
            return False, "rule_max_pump_le_1"
        if pre_ret < MOMENTUM_PRE_RET_THRESHOLD:
            return False, "rule_pre_ret_lt_10"
        if rule == "max_pump_gt_1_pre_ret_ge_10_warm":
            age = _feature(signal, "hist_age_min")
            if age is None:
                return False, "missing_feature:hist_age_min"
            if age < MIN_OBSERVED_HISTORY_MIN:
                return False, "rule_history_too_young"
        return True, None

    if rule == "exclude_crowded_pre":
        signers = _feature(signal, "hist_signers_5m")
        trades = _feature(signal, "hist_trades_5m")
        pre_ret = _feature(signal, "hist_pre_ret")
        if signers is None or trades is None or pre_ret is None:
            return False, "missing_feature:hist_crowd"
        crowded = _crowded_pre(signers, trades, pre_ret)
        return (not crowded), "rule_crowded_late_chase" if crowded else None

    if rule == "signers_5m_le_147":
        signers = _feature(signal, "hist_signers_5m")
        if signers is None:
            return False, "missing_feature:hist_signers_5m"
        passed = signers <= CROWDED_PRE_SIGNERS_GT
        return passed, None if passed else "rule_signers_5m_gt_147"

    if rule == "smart_wallet_only":
        kol = _feature(signal, "wallet_kol")
        if kol is None:
            return False, "missing_feature:wallet_kol"
        passed = kol == 0
        return passed, None if passed else "rule_kol_signal"

    return False, f"unknown_rule:{rule}"


def evaluate_gate(signal: Signal, config: GateConfig) -> GateDecision:
    """Return whether a signal may open a paper position."""
    reasons: list[str] = []

    if signal.network != "solana":
        reasons.append(f"network_not_solana:{signal.network}")
    if not signal.token_address:
        reasons.append("missing_token_address")
    if not signal.is_buy_signal:
        reasons.append(f"not_buy_signal:{signal.message_type}")
    if signal.address_status != "confirmed" and not config.allow_candidate_address:
        reasons.append(f"unconfirmed_address:{signal.address_status}")

    mc = _feature(signal, "mc_usd")
    if config.min_mc_usd is not None and (mc is None or mc < config.min_mc_usd):
        reasons.append("mc_below_min")
    if config.max_mc_usd is not None and (mc is None or mc > config.max_mc_usd):
        reasons.append("mc_above_max")

    rule_ok, rule_reason = _rule_decision(signal, config.rule)
    if not rule_ok and rule_reason:
        reasons.append(rule_reason)

    if config.require_history and signal.features.get("hist_available") is False:
        reasons.append("missing_pre_signal_history")

    return GateDecision(
        allowed=not reasons,
        rule=config.rule,
        reasons=reasons,
        features=dict(signal.features),
    )
