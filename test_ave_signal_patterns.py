from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from tools.backtest.analyze_ave_signal_patterns import (
    _flow_features,
    auc,
    bar_features,
    parse_alert,
    spearman,
)
from tools.backtest.ave_signal_pattern_rules import (
    Thresholds,
    build_rules,
    placebo,
)
from tools.backtest.ave_hot_money_strategy import (
    Thresholds as StrategyThresholds,
    decide,
    evaluate,
    fit_thresholds,
    hot_money_mask,
)


def alert(text: str) -> str:
    return text


class AlertParsingTests(unittest.TestCase):
    def test_parses_all_alert_fields(self) -> None:
        text = (
            "\U0001f4b9 Max Pump: 3.5x\n"
            "\U0001f4b0 4 KOL Wallet Buy\n"
            "\U0001f911 Current MC: 12.5K\n"
            "\U0001f4b8 Total Buy 9.5 SOL\n"
            "\U0001f6d2 Inflow\n"
            "Buy 4 SOL\n"
            "Buy 3 SOL\n"
            "Buy 2.5 SOL\n"
        )
        features = parse_alert(text)
        self.assertEqual(features["alert_max_pump"], 3.5)
        self.assertEqual(features["alert_already_pumped"], 1.0)
        self.assertEqual(features["alert_wallet_count"], 4.0)
        self.assertEqual(features["alert_smart_wallet"], 0.0)
        self.assertEqual(features["alert_mc"], 12_500.0)
        self.assertEqual(features["alert_total_buy"], 9.5)
        self.assertEqual(features["alert_inflow_n"], 3.0)
        self.assertEqual(features["alert_inflow_sum"], 9.5)
        self.assertAlmostEqual(features["alert_inflow_top_share"], 4.0 / 9.5)
        self.assertAlmostEqual(
            features["alert_inflow_hhi"], (16.0 + 9.0 + 6.25) / 9.5**2
        )
        self.assertAlmostEqual(features["alert_buy_per_wallet"], 9.5 / 4.0)

    def test_under_one_x_pump_is_not_already_pumped(self) -> None:
        features = parse_alert("\U0001f4b9 Max Pump: < 1x")
        self.assertEqual(features["alert_max_pump"], 0.0)
        self.assertEqual(features["alert_already_pumped"], 0.0)

    def test_missing_fields_are_absent_not_zero(self) -> None:
        features = parse_alert("nothing useful here")
        self.assertNotIn("alert_max_pump", features)
        self.assertNotIn("alert_inflow_n", features)


class BarFeatureTests(unittest.TestCase):
    def make_bars(self) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "close_ts": [60_000, 120_000, 180_000, 240_000],
                "close": [1.0, 2.0, 3.0, 4.0],
                "high": [1.0, 2.0, 3.0, 4.0],
                "low": [1.0, 2.0, 3.0, 4.0],
                "volume_sol": [10.0, 20.0, 30.0, 40.0],
                "trades": [1, 2, 3, 4],
                "buys": [1, 1, 2, 2],
            }
        )

    def test_features_stop_at_decision(self) -> None:
        features = bar_features(self.make_bars(), 180_000, 3.0)
        # The bar closing at 240 s is after the decision and must be ignored.
        self.assertEqual(features["pre_age_min"], 2.0)
        self.assertAlmostEqual(features["pre_ret_1m"], 3.0 / 2.0 - 1.0)
        self.assertAlmostEqual(features["pre_vol_5m"], 60.0)
        self.assertEqual(features["pre_trades_5m"], 6.0)
        self.assertAlmostEqual(features["pre_buy_ratio_5m"], 4.0 / 6.0)

    def test_future_bar_does_not_change_features(self) -> None:
        bars = self.make_bars()
        baseline = bar_features(bars, 180_000, 3.0)
        bars.loc[bars["close_ts"] == 240_000, ["close", "high", "low"]] = 999.0
        bars.loc[bars["close_ts"] == 240_000, "volume_sol"] = 10_000.0
        changed = bar_features(bars, 180_000, 3.0)
        for key in ("pre_age_min", "pre_ret_1m", "pre_vol_5m", "pre_trades_5m"):
            self.assertEqual(baseline[key], changed[key], key)


class FlowFeatureTests(unittest.TestCase):
    def make_events(self) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "timestamp_ms": [60_000, 120_000, 180_000, 240_000],
                "action": ["buy", "buy", "sell", "buy"],
                "quote_amount": [1.0, 2.0, 4.0, 100.0],
                "tx_signer": ["a", "b", "c", "d"],
                "market_cap_sol": [100.0, 200.0, 300.0, 400.0],
                "quote_in_pool": [10.0, 20.0, 30.0, 40.0],
            }
        )

    def test_flow_stops_at_decision(self) -> None:
        features = _flow_features(self.make_events(), 180_000)
        self.assertEqual(features["ev_mc_sol"], 300.0)
        self.assertEqual(features["ev_liq_sol"], 30.0)
        self.assertEqual(features["ev_age_min"], 2.0)
        # Only the 180 s sell is inside the last minute.
        self.assertEqual(features["ev_flow_1m"], -4.0)
        # All three pre-decision events are inside the 5-minute window.
        self.assertEqual(features["ev_flow_5m"], -1.0)
        self.assertEqual(features["ev_trades_5m"], 3.0)

    def test_future_event_does_not_change_features(self) -> None:
        events = self.make_events()
        baseline = _flow_features(events, 180_000)
        events.loc[events["timestamp_ms"] == 240_000, "quote_amount"] = 1_000_000.0
        changed = _flow_features(events, 180_000)
        self.assertEqual(baseline["ev_flow_5m"], changed["ev_flow_5m"])
        self.assertEqual(baseline["ev_mc_sol"], changed["ev_mc_sol"])


class RankStatisticsTests(unittest.TestCase):
    def test_auc_perfect_and_inverted(self) -> None:
        target = pd.Series([0, 0, 1, 1])
        self.assertEqual(auc(pd.Series([1.0, 2.0, 3.0, 4.0]), target)[0], 1.0)
        self.assertEqual(auc(pd.Series([4.0, 3.0, 2.0, 1.0]), target)[0], 0.0)

    def test_auc_is_half_for_a_constant_feature(self) -> None:
        target = pd.Series([0, 1, 0, 1])
        self.assertEqual(auc(pd.Series([3.0] * 4), target)[0], 0.5)

    def test_spearman_matches_monotone_relation(self) -> None:
        feature = pd.Series([1.0, 2.0, 3.0, 4.0])
        target = pd.Series([10.0, 20.0, 30.0, 40.0])
        self.assertAlmostEqual(spearman(feature, target), 1.0)
        self.assertAlmostEqual(spearman(feature, -target), -1.0)

    def test_spearman_ignores_missing_values(self) -> None:
        feature = pd.Series([1.0, np.nan, 3.0, 4.0])
        target = pd.Series([1.0, 2.0, 3.0, 4.0])
        self.assertAlmostEqual(spearman(feature, target), 1.0)


class RuleTests(unittest.TestCase):
    def make_features(self) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "signal_id": [f"s{i}" for i in range(5)],
                "ev_flow_1m": [6.0, 1.0, 6.0, 6.0, 0.0],
                "alert_total_buy": [6.0, 6.0, 1.0, 6.0, 6.0],
                "alert_inflow_sum": [6.0, 6.0, 1.0, 6.0, 0.0],
                "alert_inflow_n": [3.0, 3.0, 3.0, 3.0, 1.0],
                "pre_vol_30m": [700.0, 700.0, 700.0, 100.0, 700.0],
                "ev_top_signer_share_5m": [0.01, 0.5, 0.5, 0.5, 0.5],
                "alert_max_pump": [1.0, 1.0, 1.0, 1.0, 9.0],
                "pre_vol_15m": [100.0, 100.0, 100.0, 100.0, 5000.0],
                "pre_log_price": [-20.0, -20.0, -20.0, -20.0, -20.0],
            }
        )

    def thresholds(self) -> Thresholds:
        return Thresholds(
            flow_q70=5.0,
            flow_q80=5.0,
            top_share_q80=0.1,
            vol15_q80=1000.0,
            price_q80=-15.0,
        )

    def test_strict_rule_requires_all_three_conditions(self) -> None:
        features = self.make_features()
        rules = build_rules(features, self.thresholds())
        self.assertEqual(
            features.loc[rules["momentum_strict"], "signal_id"].tolist(), ["s0"]
        )

    def test_avoid_rugs_excludes_late_pump_and_blowoff(self) -> None:
        features = self.make_features()
        rules = build_rules(features, self.thresholds())
        self.assertFalse(bool(rules["avoid_rugs"].iloc[4]))


class PlaceboTests(unittest.TestCase):
    def test_placebo_is_deterministic_for_a_seed(self) -> None:
        features = pd.DataFrame({"signal_id": ["a", "b"], "is_train": [True, False]})
        rule = pd.Series([True, False])
        outcomes = pd.DataFrame(
            {
                "signal_id": ["a", "b", "c", "d", "e"],
                "horizon_minutes": [5] * 5,
                "net_return": [0.5, -0.2, 0.1, 0.3, -0.1],
            }
        )
        first = placebo(features, outcomes, rule, 5, draws=200, seed=11)
        second = placebo(features, outcomes, rule, 5, draws=200, seed=11)
        self.assertEqual(first, second)
        self.assertEqual(first["size"], 1)


class StrategyModuleTests(unittest.TestCase):
    def make_features(self) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "signal_id": ["t1", "t2", "t3", "h1", "h2"],
                "is_train": [True, True, True, False, False],
                "ev_flow_1m": [1.0, 2.0, 3.0, 1000.0, 2000.0],
                "alert_total_buy": [5.0, 5.0, 5.0, 5.0, 5.0],
                "pre_vol_30m": [700.0, 700.0, 700.0, 700.0, 700.0],
            }
        )

    def test_fit_thresholds_reads_train_rows_only(self) -> None:
        features = self.make_features()
        first = fit_thresholds(features)
        features.loc[~features["is_train"], "ev_flow_1m"] = -999.0
        second = fit_thresholds(features)
        self.assertEqual(first, second)
        self.assertAlmostEqual(first.flow_1m_sol, 2.6)
        self.assertEqual(first.alert_total_buy_sol, 3.0)
        self.assertEqual(first.volume_30m_sol, 600.0)

    def test_mask_requires_every_condition(self) -> None:
        features = self.make_features()
        features.loc[
            ~features["is_train"],
            ["ev_flow_1m", "alert_total_buy", "pre_vol_30m"],
        ] = 0.0
        features.loc[0, "ev_flow_1m"] = 0.0
        features.loc[1, "alert_total_buy"] = 1.0
        features.loc[2, "pre_vol_30m"] = 100.0
        thresholds = StrategyThresholds(
            flow_1m_sol=2.5, alert_total_buy_sol=3.0, volume_30m_sol=600.0
        )
        self.assertFalse(hot_money_mask(features, thresholds).any())

    def test_mask_ignores_missing_values(self) -> None:
        features = self.make_features()
        features.loc[0, "ev_flow_1m"] = np.nan
        thresholds = StrategyThresholds(
            flow_1m_sol=2.5, alert_total_buy_sol=3.0, volume_30m_sol=600.0
        )
        self.assertFalse(bool(hot_money_mask(features, thresholds).iloc[0]))

    def test_decide_explains_failed_checks(self) -> None:
        thresholds = StrategyThresholds(
            flow_1m_sol=2.5, alert_total_buy_sol=3.0, volume_30m_sol=600.0
        )
        row = pd.Series(
            {
                "ev_flow_1m": 3.0,
                "alert_total_buy": 1.0,
                "pre_vol_30m": 700.0,
            }
        )
        decision = decide(row, thresholds)
        self.assertFalse(decision.enter)
        self.assertEqual(decision.failed_checks(), ["alert_total_buy"])

    def test_evaluate_recomputes_return_statistics(self) -> None:
        features = pd.DataFrame(
            {
                "signal_id": ["a", "b", "c"],
                "is_train": [True, False, False],
                "ev_flow_1m": [5.0, 5.0, 5.0],
                "alert_total_buy": [5.0, 5.0, 5.0],
                "pre_vol_30m": [700.0, 700.0, 700.0],
            }
        )
        outcomes = pd.DataFrame(
            {
                "signal_id": ["a", "b", "c", "d"],
                "horizon_minutes": [5, 5, 5, 5],
                "status": ["closed"] * 4,
                "day": ["d1", "d2", "d2", "d2"],
                "net_return": [0.5, -0.25, 0.0, 9.0],
                "net_pnl_sol": [0.05, -0.025, 0.0, 0.9],
            }
        )
        thresholds = StrategyThresholds(
            flow_1m_sol=2.5, alert_total_buy_sol=3.0, volume_30m_sol=600.0
        )
        result = evaluate(features, outcomes, thresholds, horizon=5)
        self.assertEqual(result["n"], 3)
        self.assertEqual(result["days_positive"], 1)
        self.assertEqual(result["days_total"], 2)
        self.assertAlmostEqual(result["win_rate"], 1.0 / 3.0)
        self.assertAlmostEqual(result["total_net_pnl_sol"], 0.025)
        self.assertEqual(result["baseline_n"], 4)


if __name__ == "__main__":
    unittest.main()
