from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parents[1]
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import prepare_signal_inputs  # noqa: E402

WSOL_MINT = "BKc7Rx2yxzT7eFJq9gKxFuC6JuGmKAhoaoRMNgcgpump"


def message_row(
    message_id: int,
    date: str,
    message: str,
    username: str = "@AveSignalMonitor",
) -> dict:
    return {
        "MessageId": str(message_id),
        "ChatId": "-1001",
        "SenderId": "42",
        "Name": "Ave",
        "Username": username,
        "Message": message,
        "MediaType": "",
        "GroupedId": "",
        "MediaPath": "",
        "Date": date,
        "ReplyToId": "",
        "ForwardedFrom": "",
        "MessageLink": f"https://t.me/{username.lstrip('@')}/{message_id}",
    }


def signal_message(mint: str, chain: str = "solana") -> str:
    return (
        f"🪙 $TEST (from pump.fun)\n🔗 {chain}\n"
        f"CA: {mint}\nLink: https://pro.ave.ai/token/{mint}\n\n"
        "🔢 2nd Vibe Buy Signal\n💹 Max Pump: 2x\n"
    )


def write_messages(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


class PrepareSignalInputsTests(unittest.TestCase):
    def test_keeps_only_solana_signals_with_exactly_one_ca(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "messages.csv"
            out = root / "out"
            write_messages(
                source,
                [
                    message_row(1, "2026-10-03 01:00:00+00:00", signal_message(WSOL_MINT)),
                    message_row(
                        2,
                        "2026-10-03 01:05:00+00:00",
                        signal_message(WSOL_MINT + "x", chain="bsc"),
                    ),
                    message_row(
                        3,
                        "2026-10-03 01:10:00+00:00",
                        "🔢 2nd Vibe Buy Signal\n🔗 solana\nno address here",
                    ),
                    message_row(4, "2026-10-03 01:15:00+00:00", "unrelated chatter"),
                ],
            )

            with self.assertRaises(SystemExit):
                prepare_signal_inputs.main(
                    [
                        "--signals-csv",
                        str(source),
                        "--start",
                        "2026-10-03T00:00:00+00:00",
                        "--end",
                        "2026-10-04T00:00:00+00:00",
                        "--out-dir",
                        str(out),
                    ]
                )

            report = json.loads((out / "signal_ingestion_report.json").read_text())
            self.assertEqual(report["selected_solana_signals"], 1)
            self.assertEqual(report["network_counts"], {"solana": 2, "bsc": 1})
            self.assertEqual(report["invalid_signals"][0]["reason"], "expected_one_CA_found_0")
            self.assertFalse(report["passed"])

    def test_out_of_window_signals_are_reported_and_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "messages.csv"
            out = root / "out"
            write_messages(
                source,
                [
                    message_row(1, "2026-10-03 03:50:00+00:00", signal_message(WSOL_MINT)),
                    message_row(2, "2026-10-03 04:10:00+00:00", signal_message(WSOL_MINT)),
                ],
            )

            exit_code = prepare_signal_inputs.main(
                [
                    "--signals-csv",
                    str(source),
                    "--start",
                    "2026-10-03T04:00:00+00:00",
                    "--end",
                    "2026-10-04T00:00:00+00:00",
                    "--out-dir",
                    str(out),
                ]
            )

            self.assertEqual(exit_code, 0)
            report = json.loads((out / "signal_ingestion_report.json").read_text())
            self.assertEqual(report["signals_outside_replay_window"], ["telegram:1"])
            self.assertEqual(report["selected_solana_signals"], 1)
            rows = list(csv.DictReader((out / "strategy_signals.csv").open()))
            self.assertEqual([row["source_message_id"] for row in rows], ["2"])

    def test_keep_outside_keeps_them_and_fails_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "messages.csv"
            out = root / "out"
            mint_a = "A" * 44
            mint_b = "B" * 44
            write_messages(
                source,
                [
                    message_row(1, "2026-10-03 03:50:00+00:00", signal_message(mint_a)),
                    message_row(2, "2026-10-03 04:10:00+00:00", signal_message(mint_b)),
                ],
            )

            with self.assertRaises(SystemExit):
                prepare_signal_inputs.main(
                    [
                        "--signals-csv",
                        str(source),
                        "--start",
                        "2026-10-03T04:00:00+00:00",
                        "--end",
                        "2026-10-04T00:00:00+00:00",
                        "--out-dir",
                        str(out),
                        "--keep-outside",
                    ]
                )

            report = json.loads((out / "signal_ingestion_report.json").read_text())
            self.assertFalse(report["passed"])
            self.assertEqual(report["selected_solana_signals"], 2)

    def test_expected_signal_count_is_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "messages.csv"
            out = root / "out"
            write_messages(
                source,
                [message_row(1, "2026-10-03 04:10:00+00:00", signal_message(WSOL_MINT))],
            )

            with self.assertRaises(SystemExit):
                prepare_signal_inputs.main(
                    [
                        "--signals-csv",
                        str(source),
                        "--start",
                        "2026-10-03T04:00:00+00:00",
                        "--end",
                        "2026-10-04T00:00:00+00:00",
                        "--out-dir",
                        str(out),
                        "--expected-signals",
                        "2",
                    ]
                )

    def test_signals_are_sorted_and_mints_are_unique(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "messages.csv"
            out = root / "out"
            mint_a = "A" * 40
            mint_b = "B" * 40
            write_messages(
                source,
                [
                    message_row(9, "2026-10-03 05:00:00+00:00", signal_message(mint_b)),
                    message_row(3, "2026-10-03 04:00:00+00:00", signal_message(mint_a)),
                ],
            )

            prepare_signal_inputs.main(
                [
                    "--signals-csv",
                    str(source),
                    "--start",
                    "2026-10-03T00:00:00+00:00",
                    "--end",
                    "2026-10-04T00:00:00+00:00",
                    "--out-dir",
                    str(out),
                ]
            )

            rows = list(csv.DictReader((out / "strategy_signals.csv").open()))
            self.assertEqual([row["source_message_id"] for row in rows], ["3", "9"])
            mints = (out / "target_mints.txt").read_text().split()
            self.assertEqual(mints, sorted([mint_a, mint_b]))

    def test_signals_csv_can_come_from_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "messages.csv"
            out = root / "out"
            write_messages(
                source,
                [message_row(1, "2026-10-03 04:10:00+00:00", signal_message(WSOL_MINT))],
            )
            previous = prepare_signal_inputs.os.environ.get("SIGNALS_CSV")
            prepare_signal_inputs.os.environ["SIGNALS_CSV"] = str(source)
            try:
                exit_code = prepare_signal_inputs.main(
                    [
                        "--start",
                        "2026-10-03T00:00:00+00:00",
                        "--end",
                        "2026-10-04T00:00:00+00:00",
                        "--out-dir",
                        str(out),
                    ]
                )
            finally:
                if previous is None:
                    prepare_signal_inputs.os.environ.pop("SIGNALS_CSV", None)
                else:
                    prepare_signal_inputs.os.environ["SIGNALS_CSV"] = previous

            self.assertEqual(exit_code, 0)
            self.assertTrue((out / "strategy_signals.csv").exists())


if __name__ == "__main__":
    unittest.main()
