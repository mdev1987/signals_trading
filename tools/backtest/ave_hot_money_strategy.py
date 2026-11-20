#!/usr/bin/env python3
"""Canonical definition of the Ave "hot money" continuation strategy.

This module is the single source of truth for the rule that came out of the
PIT-safe pattern search in ``analyze_ave_signal_patterns.py`` and was validated
in ``ave_signal_pattern_rules.py``.

The rule
--------
At the entry decision (``signal_timestamp_ms + 5 s``), enter only when all
three conditions hold:

* ``ev_flow_1m > 9.5 SOL`` - net buys minus sells in the prior minute;
* ``alert_total_buy > 3 SOL`` - the "Total Buy" figure printed in the alert;
* ``pre_vol_30m > 600 SOL`` - traded volume in the prior 30 minutes.

Exit after 5 minutes. Every input is known at the decision timestamp, so there
is no look-ahead. The 9.5 SOL flow cutoff is the train-day 80th percentile;
it is fitted from data, not hand-picked.

This is research, not the execution simulator. It does not model slots, cash,
routing, liquidity impact or PIT eligibility, and it intentionally does not
import ``execution_simulator``.

Usage (from the repository root)::

    # print the rule, fitted thresholds and the re-computed validation
    python tools/backtest/ave_hot_money_strategy.py

    # machine-readable form
    python tools/backtest/ave_hot_money_strategy.py --json
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
FEATURES_PATH = Path("output/backtest/ave_signals/patterns/features.csv")
OUTCOMES_PATH = Path("output/backtest/ave_signals/signals_backtest.csv")

TRAIN_DAYS = ("2026-10-03", "2026-10-04", "2026-10-05")
HOLDOUT_DAYS = ("2026-08-09", "2026-11-08")
HORIZON_MINUTES = 5
ENTRY_LATENCY_MS = 5_000
FEE_RATE = 0.0125
STAKE_SOL = 0.1

FLOW_QUANTILE = 0.80
ALERT_TOTAL_BUY_SOL = 3.0
VOLUME_30M_SOL = 600.0

# Columns the rule is allowed to read. All are cut off at the decision
# timestamp by the feature builder; keeping this list explicit makes an
# accidental future column obvious in review.
REQUIRED_FEATURES = ("ev_flow_1m", "alert_total_buy", "pre_vol_30m")


@dataclass(frozen=True)
class Thresholds:
    """Fitted entry thresholds, in SOL."""

    flow_1m_sol: float
    alert_total_buy_sol: float
    volume_30m_sol: float


@dataclass(frozen=True)
class Decision:
    """Explainable result for one signal."""

    enter: bool
    checks: dict[str, bool]
    values: dict[str, float]
    thresholds: Thresholds
    horizon_minutes: int = HORIZON_MINUTES
    entry_latency_ms: int = ENTRY_LATENCY_MS

    def failed_checks(self) -> list[str]:
        return [name for name, passed in self.checks.items() if not passed]


def fit_thresholds(
    features: pd.DataFrame, quantile: float = FLOW_QUANTILE
) -> Thresholds:
    """Fit the flow percentile on train days only.

    ``alert_total_buy`` and 30-minute volume use round, interpretable floors;
    only the flow threshold is quantile-fitted. Holdout rows never influence
    the result.
    """
    if "is_train" not in features:
        raise KeyError("features must contain an 'is_train' column")
    train = features.loc[features["is_train"].astype(bool)]
    if train.empty:
        raise ValueError("no train rows")
    flow = pd.to_numeric(train["ev_flow_1m"], errors="coerce").dropna()
    if flow.empty:
        raise ValueError("no finite ev_flow_1m values on train days")
    return Thresholds(
        flow_1m_sol=float(flow.quantile(quantile)),
        alert_total_buy_sol=ALERT_TOTAL_BUY_SOL,
        volume_30m_sol=VOLUME_30M_SOL,
    )


def hot_money_mask(
    features: pd.DataFrame, thresholds: Thresholds
) -> pd.Series:
    """Boolean entry mask. Missing or non-finite inputs never enter."""
    missing = [column for column in REQUIRED_FEATURES if column not in features]
    if missing:
        raise KeyError(f"missing feature columns: {missing}")
    mask = (
        (pd.to_numeric(features["ev_flow_1m"], errors="coerce") > thresholds.flow_1m_sol)
        & (
            pd.to_numeric(features["alert_total_buy"], errors="coerce")
            > thresholds.alert_total_buy_sol
        )
        & (
            pd.to_numeric(features["pre_vol_30m"], errors="coerce")
            > thresholds.volume_30m_sol
        )
    )
    return mask.fillna(False).astype(bool)


def decide(row: pd.Series, thresholds: Thresholds) -> Decision:
    """Evaluate one signal and explain which conditions passed."""
    values = {
        "ev_flow_1m": float(pd.to_numeric(row["ev_flow_1m"], errors="coerce")),
        "alert_total_buy": float(
            pd.to_numeric(row["alert_total_buy"], errors="coerce")
        ),
        "pre_vol_30m": float(pd.to_numeric(row["pre_vol_30m"], errors="coerce")),
    }
    checks = {
        "flow_1m": bool(
            np.isfinite(values["ev_flow_1m"])
            and values["ev_flow_1m"] > thresholds.flow_1m_sol
        ),
        "alert_total_buy": bool(
            np.isfinite(values["alert_total_buy"])
            and values["alert_total_buy"] > thresholds.alert_total_buy_sol
        ),
        "volume_30m": bool(
            np.isfinite(values["pre_vol_30m"])
            and values["pre_vol_30m"] > thresholds.volume_30m_sol
        ),
    }
    return Decision(
        enter=all(checks.values()),
        checks=checks,
        values=values,
        thresholds=thresholds,
    )


def load_inputs(
    repo_root: Path,
    features_path: Path = FEATURES_PATH,
    outcomes_path: Path = OUTCOMES_PATH,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    features = pd.read_csv(repo_root / features_path)
    outcomes = pd.read_csv(repo_root / outcomes_path)
    outcomes = outcomes[outcomes["status"] == "closed"].copy()
    return features, outcomes


def evaluate(
    features: pd.DataFrame,
    outcomes: pd.DataFrame,
    thresholds: Thresholds,
    horizon: int = HORIZON_MINUTES,
) -> dict[str, object]:
    """Re-compute the validated rule statistics from the saved tables."""
    mask = hot_money_mask(features, thresholds)
    selected = features.loc[mask, ["signal_id", "is_train"]].copy()
    block = outcomes[
        (outcomes["horizon_minutes"] == horizon)
        & (outcomes["signal_id"].isin(set(selected["signal_id"])))
    ].copy()
    if block.empty:
        raise ValueError(f"no closed outcomes at {horizon} minutes")
    block = block.merge(selected, on="signal_id", how="left")
    returns = pd.to_numeric(block["net_return"], errors="coerce").dropna()
    if returns.empty:
        raise ValueError("no finite returns for the selected signals")

    by_day = block.groupby("day")["net_return"].mean()
    baseline = outcomes[outcomes["horizon_minutes"] == horizon]
    baseline_returns = pd.to_numeric(
        baseline["net_return"], errors="coerce"
    ).dropna()

    def split_mean(flag: bool) -> float:
        subset = block.loc[block["is_train"].astype(bool) == flag, "net_return"]
        return float(subset.mean()) if not subset.empty else float("nan")

    return {
        "horizon_minutes": horizon,
        "n": int(len(returns)),
        "n_features": int(len(features)),
        "share": float(len(returns) / len(features)),
        "win_rate": float((returns > 0).mean()),
        "mean_net_return": float(returns.mean()),
        "median_net_return": float(returns.median()),
        "big_win_rate": float((returns >= 1.0).mean()),
        "rug_rate": float((returns <= -0.9).mean()),
        "days_positive": int((by_day > 0).sum()),
        "days_total": int(len(by_day)),
        "train_mean": split_mean(True),
        "holdout_mean": split_mean(False),
        "baseline_n": int(len(baseline_returns)),
        "baseline_win_rate": float((baseline_returns > 0).mean()),
        "baseline_mean_net_return": float(baseline_returns.mean()),
        "baseline_median_net_return": float(baseline_returns.median()),
        "total_net_pnl_sol": float(block["net_pnl_sol"].sum()),
        "by_day": {
            str(day): {
                "n": int(len(group)),
                "win_rate": float((group["net_return"] > 0).mean()),
                "mean_net_return": float(group["net_return"].mean()),
                "median_net_return": float(group["net_return"].median()),
            }
            for day, group in block.groupby("day")
        },
    }


def spec() -> dict[str, object]:
    """Static, human-readable description of the strategy."""
    return {
        "name": "ave_hot_money_continuation",
        "version": 1,
        "status": "research-validated-out-of-sample",
        "signal_source": "@AveSignalMonitor (source_username exact match)",
        "entry": {
            "decision": "signal_timestamp_ms + entry_latency_ms",
            "price": "latest real 1m close with close_ts <= decision",
            "position": "one fixed SOL stake per signal",
        },
        "exit": {
            "decision": "entry decision + horizon_minutes",
            "price": "latest real 1m close with close_ts <= exit decision",
            "no_forced_close": True,
        },
        "rule": {
            "ev_flow_1m": f"> {FLOW_QUANTILE:.0%} of train-day net flow (about 9.5 SOL)",
            "alert_total_buy": f"> {ALERT_TOTAL_BUY_SOL:g} SOL",
            "pre_vol_30m": f"> {VOLUME_30M_SOL:g} SOL",
        },
        "horizon_minutes": HORIZON_MINUTES,
        "entry_latency_ms": ENTRY_LATENCY_MS,
        "fee_rate_per_side": FEE_RATE,
        "stake_sol": STAKE_SOL,
        "train_days": list(TRAIN_DAYS),
        "holdout_days": list(HOLDOUT_DAYS),
        "required_features": list(REQUIRED_FEATURES),
    }


def _fmt_pct(value: float) -> str:
    return f"{100.0 * value:+.1f} %"


def render_report(
    thresholds: Thresholds, result: dict[str, object]
) -> str:
    lines = [
        "# Ave hot-money continuation strategy",
        "",
        "```text",
        f"ev_flow_1m      > {thresholds.flow_1m_sol:.2f} SOL"
        f"   # train q{FLOW_QUANTILE:.0%} net flow, prior minute",
        f"alert_total_buy > {thresholds.alert_total_buy_sol:g} SOL"
        "     # Total Buy printed in the alert",
        f"pre_vol_30m     > {thresholds.volume_30m_sol:g} SOL"
        "   # prior 30-minute traded volume",
        "```",
        "",
        f"Entry: signal + {ENTRY_LATENCY_MS // 1000} s, latest real 1m close.",
        f"Exit: entry + {HORIZON_MINUTES} min, latest real 1m close.",
        f"Fees: {100 * FEE_RATE:.2f} % per side.",
        "",
        "## Re-computed validation",
        "",
        f"- Universe: {result['n_features']} priceable signals; "
        f"rule keeps {result['n']} ({100 * result['share']:.1f} %).",
        f"- Win rate {100 * result['win_rate']:.1f} % vs baseline "
        f"{100 * result['baseline_win_rate']:.1f} %.",
        f"- Mean {_fmt_pct(result['mean_net_return'])} vs baseline "
        f"{_fmt_pct(result['baseline_mean_net_return'])}.",
        f"- Median {_fmt_pct(result['median_net_return'])} vs baseline "
        f"{_fmt_pct(result['baseline_median_net_return'])}.",
        f"- 2x+ {100 * result['big_win_rate']:.1f} %, rug "
        f"{100 * result['rug_rate']:.1f} %.",
        f"- Positive on {result['days_positive']}/{result['days_total']} days.",
        f"- Train mean {_fmt_pct(result['train_mean'])}, holdout mean "
        f"{_fmt_pct(result['holdout_mean'])}.",
        f"- Total net PnL {result['total_net_pnl_sol']:+.2f} SOL at "
        f"{STAKE_SOL} SOL per signal.",
        "",
        "| Day | n | Win | Mean | Median |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for day, stats in result["by_day"].items():
        lines.append(
            f"| {day} | {stats['n']} | {100 * stats['win_rate']:.0f} % | "
            f"{_fmt_pct(stats['mean_net_return'])} | "
            f"{_fmt_pct(stats['median_net_return'])} |"
        )
    lines += [
        "",
        "This is signal research, not the execution simulator. It does not "
        "model slots, cash, routing, liquidity impact or PIT eligibility.",
        "",
    ]
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--features", type=Path, default=FEATURES_PATH)
    parser.add_argument("--outcomes", type=Path, default=OUTCOMES_PATH)
    parser.add_argument("--horizon", type=int, default=HORIZON_MINUTES)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    repo_root = args.repo_root.resolve()
    features, outcomes = load_inputs(repo_root, args.features, args.outcomes)
    thresholds = fit_thresholds(features)
    result = evaluate(features, outcomes, thresholds, args.horizon)

    if args.json:
        payload = {
            "spec": spec(),
            "thresholds": asdict(thresholds),
            "validation": result,
        }
        print(json.dumps(payload, indent=2, default=float))
    else:
        print(render_report(thresholds, result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
