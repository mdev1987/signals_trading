from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path

import pyarrow.parquet as pq
import zstandard as zstd

sys.path.insert(0, str(Path(__file__).parent / "tools" / "backtest"))

from export_parquet import EVENT_SCHEMA, convert_events, convert_ohlcv, event_row  # noqa: E402

WSOL = "So11111111111111111111111111111111111111112"


def write_events(path: Path, events: list[dict]) -> None:
    with path.open("wb") as target:
        with zstd.ZstdCompressor(level=1).stream_writer(target, closefd=False) as writer:
            for event in events:
                writer.write(json.dumps(event).encode("utf-8"))
                writer.write(b"\n")


class EventRowTests(unittest.TestCase):
    def test_maps_top_level_fields(self) -> None:
        row = event_row(
            {
                "timestamp": 1_000,
                "mint": "mint-1",
                "pool": "pump-amm",
                "poolId": "pool-1",
                "action": "buy",
                "quoteMint": WSOL,
                "signature": "sig",
                "priceSol": 1.5e-7,
                "tokensInPool": 1_000.0,
                "quoteInPool": 100.0,
                "poolFeeRate": 0.01,
                "decimals": 6,
            }
        )

        self.assertEqual(row["timestamp_ms"], 1_000)
        self.assertEqual(row["pool_id"], "pool-1")
        self.assertEqual(row["quote_mint"], WSOL)
        self.assertEqual(row["price_sol"], 1.5e-7)
        self.assertEqual(row["decimals"], 6)

    def test_rejects_bool_and_string_numbers(self) -> None:
        row = event_row({"timestamp": True, "priceSol": "1.2", "mint": 7})

        self.assertIsNone(row["timestamp_ms"])
        self.assertIsNone(row["price_sol"])
        self.assertEqual(row["mint"], "7")

    def test_reduces_breakdown_to_leg_summary(self) -> None:
        row = event_row(
            {
                "timestamp": 1_000,
                "mint": "mint-1",
                "txSigner": "signer",
                "breakdown": [
                    {"trader": "a", "action": "buy", "quoteAmount": 2.0, "tokenAmount": 20.0},
                    {"trader": "a", "action": "buy", "quoteAmount": 1.0, "tokenAmount": 10.0},
                    {"trader": "b", "action": "sell", "quoteAmount": 0.5, "tokenAmount": 5.0},
                ],
            }
        )

        self.assertEqual(row["leg_count"], 3)
        self.assertEqual(row["leg_traders"], 2)
        self.assertAlmostEqual(row["leg_buy_quote"], 3.0)
        self.assertAlmostEqual(row["leg_sell_quote"], 0.5)
        self.assertAlmostEqual(row["leg_buy_token"], 30.0)
        self.assertAlmostEqual(row["leg_sell_token"], 5.0)

    def test_missing_breakdown_uses_signer_as_single_trader(self) -> None:
        row = event_row({"timestamp": 1_000, "txSigner": "signer"})

        self.assertEqual(row["leg_count"], 0)
        self.assertEqual(row["leg_traders"], 1)


class ConvertEventsTests(unittest.TestCase):
    def test_round_trips_a_filtered_stream(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "events.jsonl.zst"
            target = Path(directory) / "events.parquet"
            write_events(
                source,
                [
                    {
                        "timestamp": 1_000,
                        "mint": "mint-1",
                        "pool": "pump",
                        "poolId": "pool-1",
                        "action": "buy",
                        "quoteMint": WSOL,
                        "quoteAmount": 0.1,
                        "tokenAmount": 1_000.0,
                    },
                    {"not": "a relevant shape"},
                ],
            )

            report = convert_events(source, target, batch_size=1)

            table = pq.read_table(target)
            self.assertEqual(report["rows"], 2)
            self.assertEqual(table.num_rows, 2)
            self.assertEqual(table.schema.names, EVENT_SCHEMA.names)
            self.assertEqual(table.column("mint").to_pylist(), ["mint-1", None])


class ConvertOhlcvTests(unittest.TestCase):
    def test_preserves_rows_and_columns(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "ohlcv_1m.csv"
            with source.open("w", newline="", encoding="utf-8") as target:
                writer = csv.DictWriter(
                    target, fieldnames=["mint", "timestamp_ms", "open", "close"]
                )
                writer.writeheader()
                writer.writerow(
                    {"mint": "mint-1", "timestamp_ms": 60_000, "open": 1.0, "close": 1.1}
                )
                writer.writerow(
                    {"mint": "mint-1", "timestamp_ms": 120_000, "open": 1.1, "close": 1.2}
                )

            report = convert_ohlcv(source, None)

            output = source.with_suffix(".parquet")
            self.assertTrue(output.exists())
            self.assertEqual(report["rows"], 2)
            self.assertEqual(report["columns"], 4)
            self.assertEqual(pq.read_table(output).column("mint").to_pylist(), ["mint-1", "mint-1"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
