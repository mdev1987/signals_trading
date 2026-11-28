"""Tests for the early on-chain distribution study."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parents[1]
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from early_flow_study import (  # noqa: E402
    FEATURE_THRESHOLD,
    HeliusClient,
    MintFlow,
    TradeRow,
    auc,
    build_rows,
    decision,
    evaluate,
    flow_from_transactions,
    identify_pool,
    load_cache,
    load_trades,
    permutation_p,
    split_days,
    threshold_report,
)

MINT = "Mint1111111111111111111111111111111111111111"
VAULT = "Vault111111111111111111111111111111111111111"
BUYER = "Buyer111111111111111111111111111111111111111"
SELLER = "Seller11111111111111111111111111111111111111"
CREATE_SIG = "create-signature"
ENTRY_S = 1_700_000_000


def transfer(source: str, destination: str, amount: float, mint: str = MINT) -> dict:
    return {
        "fromUserAccount": source,
        "toUserAccount": destination,
        "mint": mint,
        "tokenAmount": amount,
    }


def tx(signature: str, timestamp: int, transfers: list[dict]) -> dict:
    return {"signature": signature, "timestamp": timestamp, "tokenTransfers": transfers}


def transactions() -> list[dict]:
    return [
        # Creation: the launcher seeds the whole supply into the pool vault.
        tx(CREATE_SIG, ENTRY_S - 3, [transfer("Launcher", VAULT, 1_000_000_000.0)]),
        # A buy removes tokens from the vault.
        tx("buy", ENTRY_S - 1, [transfer(VAULT, BUYER, 50_000_000.0)]),
        # Two sellers, one inside the 15 s feature window and one after it.
        tx("sell-early", ENTRY_S + 4, [transfer(BUYER, VAULT, 20_000_000.0)]),
        tx("sell-late", ENTRY_S + 25, [transfer(SELLER, VAULT, 30_000_000.0)]),
    ]


class PoolIdentificationTests(unittest.TestCase):
    def test_largest_transfer_is_the_creation_deposit(self) -> None:
        pool = identify_pool(transactions(), MINT)
        assert pool is not None
        supply, vault, signature = pool
        self.assertEqual(supply, 1_000_000_000.0)
        self.assertEqual(vault, VAULT)
        self.assertEqual(signature, CREATE_SIG)

    def test_no_transfers_is_none(self) -> None:
        self.assertIsNone(identify_pool([], MINT))


class FlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.flow = flow_from_transactions(
            transactions(),
            MINT,
            window_start_s=ENTRY_S - 240,
            entry_s=ENTRY_S,
        )

    def test_create_deposit_is_not_counted_as_a_sale(self) -> None:
        assert self.flow is not None
        # Only the 20 M sell falls inside the first 15 s.
        self.assertAlmostEqual(self.flow.sold_15s, 20_000_000.0)
        self.assertAlmostEqual(self.flow.sold_60s, 50_000_000.0)

    def test_share_is_normalised_by_the_supply_proxy(self) -> None:
        assert self.flow is not None
        self.assertAlmostEqual(self.flow.share(15), 0.02)
        self.assertAlmostEqual(self.flow.share(60), 0.05)

    def test_buys_and_sellers_are_tracked_separately(self) -> None:
        assert self.flow is not None
        self.assertAlmostEqual(self.flow.bought_15s, 50_000_000.0)
        self.assertEqual(self.flow.sellers_15s, 1)
        self.assertEqual(self.flow.sellers_60s, 2)

    def test_first_sell_is_relative_to_entry(self) -> None:
        assert self.flow is not None
        self.assertAlmostEqual(self.flow.first_sell_s or 0.0, 4.0)

    def test_pre_entry_sales_are_kept_out_of_the_feature(self) -> None:
        rows = [
            tx(CREATE_SIG, ENTRY_S - 30, [transfer("Launcher", VAULT, 1_000_000_000.0)]),
            tx("dump-before", ENTRY_S - 10, [transfer("Sniper", VAULT, 400_000_000.0)]),
            tx("sell-after", ENTRY_S + 3, [transfer(BUYER, VAULT, 10_000_000.0)]),
        ]
        flow = flow_from_transactions(
            rows, MINT, window_start_s=ENTRY_S - 240, entry_s=ENTRY_S
        )
        assert flow is not None
        self.assertAlmostEqual(flow.sold_pre, 400_000_000.0)
        self.assertAlmostEqual(flow.sold_15s, 10_000_000.0)
        self.assertAlmostEqual(flow.share(15), 0.01)


class CacheTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        flow = flow_from_transactions(
            transactions(), MINT, window_start_s=ENTRY_S - 240, entry_s=ENTRY_S
        )
        assert flow is not None
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            from early_flow_study import append_cache

            append_cache(directory, flow)
            loaded = load_cache(directory)
        self.assertIn(MINT, loaded)
        self.assertEqual(loaded[MINT].sold_15s, flow.sold_15s)


class RowTests(unittest.TestCase):
    def _flow(self, mint: str = MINT, sold: float = 20_000_000.0) -> MintFlow:
        return MintFlow(
            mint=mint,
            window_start_s=ENTRY_S - 240,
            supply_proxy=1_000_000_000.0,
            sold_pre=0.0,
            sold_5s=0.0,
            sold_15s=sold,
            sold_30s=sold,
            sold_60s=sold,
            bought_15s=0.0,
            bought_60s=0.0,
            sellers_15s=1,
            sellers_60s=1,
            first_sell_s=4.0,
            transactions=5,
            pages=1,
            truncated=False,
        )

    def test_non_scanner_rules_are_excluded(self) -> None:
        trades = [
            {"mint": MINT, "_rule": "hot_money_continuation", "_day": "2026-11-20",
             "_entry_s": ENTRY_S, "return_pct": -1.0, "pnl_sol": -0.1},
        ]
        self.assertEqual(build_rows(trades, {MINT: self._flow()}), [])

    def test_missing_flow_is_skipped(self) -> None:
        trades = [
            {"mint": MINT, "_rule": "scanner_pumpfun_amm", "_day": "2026-11-22",
             "_entry_s": ENTRY_S, "return_pct": -1.0, "pnl_sol": -0.1},
        ]
        self.assertEqual(build_rows(trades, {}), [])

    def test_row_feature_is_the_fifteen_second_share(self) -> None:
        trades = [
            {"mint": MINT, "_rule": "scanner_pumpfun_amm", "_day": "2026-11-22",
             "_entry_s": ENTRY_S, "return_pct": -1.0, "pnl_sol": -0.1},
        ]
        rows = build_rows(trades, {MINT: self._flow(sold=30_000_000.0)})
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0].feature, 0.03)


class SplitTests(unittest.TestCase):
    def _row(self, day: str, mint: str) -> TradeRow:
        flow = MintFlow(
            mint=mint, window_start_s=0, supply_proxy=1e9, sold_pre=0.0,
            sold_5s=0.0, sold_15s=0.0,
            sold_30s=0.0, sold_60s=0.0, bought_15s=0.0, bought_60s=0.0, sellers_15s=0,
            sellers_60s=0, first_sell_s=None, transactions=1, pages=1, truncated=False,
        )
        return TradeRow(mint=mint, rule="scanner_pumpfun_amm", day=day,
                        entry_ts_ms=0, return_pct=0.2, pnl_sol=0.02, flow=flow)

    def test_last_two_days_are_holdout(self) -> None:
        rows = [self._row(f"2026-11-2{index}", f"m{index}") for index in (2, 3, 4, 5)]
        train, holdout = split_days(rows, holdout_days=2)
        self.assertEqual({row.day for row in train}, {"2026-11-22", "2026-11-23"})
        self.assertEqual({row.day for row in holdout}, {"2026-11-24", "2026-11-25"})

    def test_no_holdout_when_it_would_consume_the_sample(self) -> None:
        rows = [self._row("2026-11-22", "a"), self._row("2026-11-23", "b")]
        train, holdout = split_days(rows, holdout_days=2)
        self.assertEqual(len(train), 2)
        self.assertEqual(holdout, [])


class StatisticsTests(unittest.TestCase):
    def test_auc_perfect_separation(self) -> None:
        self.assertAlmostEqual(auc([0.0, 0.0, 1.0, 1.0], [0, 0, 1, 1]) or 0.0, 1.0)

    def test_auc_with_ties_is_one_half(self) -> None:
        self.assertAlmostEqual(auc([1.0, 1.0, 1.0, 1.0], [0, 1, 0, 1]) or 0.0, 0.5)

    def test_permutation_p_is_bounded_and_deterministic(self) -> None:
        values = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5]
        labels = [0, 0, 0, 1, 1, 1]
        first = permutation_p(values, labels, resamples=200, seed=7)
        second = permutation_p(values, labels, resamples=200, seed=7)
        assert first is not None and second is not None
        self.assertEqual(first, second)
        self.assertGreaterEqual(first, 0.0)
        self.assertLessEqual(first, 1.0)


class ThresholdTests(unittest.TestCase):
    def _row(self, feature: float, ret: float, rule: str = "scanner_pumpfun_amm",
             day: str = "2026-11-22") -> TradeRow:
        flow = MintFlow(
            mint=f"m{feature}{ret}{rule}", window_start_s=0, supply_proxy=1e9,
            sold_pre=0.0, sold_5s=0.0, sold_15s=feature * 1e9, sold_30s=0.0, sold_60s=0.0,
            bought_15s=0.0, bought_60s=0.0, sellers_15s=1, sellers_60s=1,
            first_sell_s=1.0, transactions=2, pages=1, truncated=False,
        )
        return TradeRow(mint=flow.mint, rule=rule, day=day, entry_ts_ms=0,
                        return_pct=ret, pnl_sol=ret / 10, flow=flow)

    def test_threshold_splits_fired_and_kept(self) -> None:
        rows = [
            self._row(0.01, 0.2, day="2026-11-22"),
            self._row(0.05, -1.0, day="2026-11-22"),
            self._row(0.04, -1.0, day="2026-11-23"),
            self._row(0.02, 0.2, day="2026-11-23"),
        ]
        report = threshold_report(rows, FEATURE_THRESHOLD)
        self.assertEqual(report["fired"]["trades"], 2)
        self.assertEqual(report["kept"]["trades"], 2)
        self.assertAlmostEqual(report["fired"]["catastrophic_rate"], 1.0)

    def test_decision_closes_without_fired_rows(self) -> None:
        rows = [self._row(0.01, 0.2, day=f"2026-11-2{index}") for index in (2, 3, 4, 5)]
        result = evaluate(rows, resamples=50)
        self.assertIn("close", decision(result))

    def test_decision_rejects_a_fired_group_that_does_not_beat_the_baseline(self) -> None:
        rows = []
        for index, day in enumerate(("2026-11-22", "2026-11-23", "2026-11-24", "2026-11-25")):
            for offset in range(10):
                rows.append(self._row(0.01 if offset else 0.05, 0.2, day=day))
            rows.append(self._row(0.01, 0.2, day=day))
        result = evaluate(rows, resamples=50)
        self.assertIn("close", decision(result))

    def test_guard_lane_is_reported_separately(self) -> None:
        rows = [
            self._row(0.01, 0.2),
            self._row(0.05, -1.0, rule="scanner_pumpfun_amm_tp50_quote_guard"),
        ]
        result = evaluate(rows, resamples=50)
        self.assertEqual(result["guard_rows"], 1)
        self.assertEqual(result["primary_rows"], 1)


class TradeLoadingTests(unittest.TestCase):
    def test_earliest_entry_wins_for_a_repeated_mint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / "live_20261122_000000"
            run.mkdir()
            (run / "meta.json").write_text(json.dumps({"rule": "scanner_pumpfun_amm"}))
            rows = [
                {"mint": MINT, "entry_ts_ms": 2000, "return_pct": 0.1},
                {"mint": MINT, "entry_ts_ms": 1000, "return_pct": -1.0},
            ]
            (run / "trades.jsonl").write_text(
                "\n".join(json.dumps(row) for row in rows) + "\n"
            )
            trades = load_trades(str(Path(tmp) / "live_*"))
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["_entry_s"], 1)


class ClientPaginationTests(unittest.TestCase):
    def test_pagination_stops_on_a_short_page(self) -> None:
        class FakeClient(HeliusClient):
            def __init__(self) -> None:  # noqa: D401 - test double
                super().__init__("test-key", rate=1000.0)
                self.calls = 0

            def _get(self, url: str) -> list[dict]:
                self.calls += 1
                if self.calls == 1:
                    return [
                        {"signature": f"s{index}", "timestamp": ENTRY_S}
                        for index in range(100)
                    ]
                return [{"signature": "last", "timestamp": ENTRY_S}]

        client = FakeClient()
        collected, pages, truncated = client.mint_transactions(MINT, ENTRY_S)
        self.assertEqual(len(collected), 101)
        self.assertEqual(pages, 2)
        self.assertFalse(truncated)


if __name__ == "__main__":
    unittest.main()
