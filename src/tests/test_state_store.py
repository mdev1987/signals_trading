"""Crash-recovery and schema tests for the SQLite state store."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parents[1]
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from paper_bot.state_store import SCHEMA_VERSION, StateStore  # noqa: E402


class StateStoreTests(unittest.TestCase):
    def test_round_trip_and_atomic_replace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.sqlite3"
            store = StateStore(path)
            self.assertIsNone(store.load())
            store.save({"wallet": {"sol_balance": 9.9}, "positions": []})
            store.save({"wallet": {"sol_balance": 8.5}, "positions": []})
            loaded = store.load()
            store.close()
        assert loaded is not None
        self.assertEqual(loaded["wallet"], {"sol_balance": 8.5})

    def test_named_keys_are_independent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.sqlite3")
            store.save({"a": 1}, "one")
            store.save({"b": 2}, "two")
            store.clear("one")
            self.assertIsNone(store.load("one"))
            self.assertEqual(store.load("two"), {"b": 2})
            store.close()

    def test_migration_sets_schema_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.sqlite3"
            store = StateStore(path)
            version = store._conn.execute("PRAGMA user_version").fetchone()[0]
            journal = store._conn.execute(
                "PRAGMA journal_mode"
            ).fetchone()[0]
            store.close()
        self.assertEqual(version, SCHEMA_VERSION)
        self.assertEqual(str(journal).lower(), "wal")

    def test_reopening_an_existing_store_keeps_the_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.sqlite3"
            first = StateStore(path)
            first.save({"wallet": {"sol_balance": 7.0}})
            first.close()
            second = StateStore(path)
            loaded = second.load()
            second.close()
        assert loaded is not None
        self.assertEqual(loaded["wallet"], {"sol_balance": 7.0})

    def test_newer_schema_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.sqlite3"
            store = StateStore(path)
            store._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
            store.close()
            with self.assertRaises(RuntimeError):
                StateStore(path)

    def test_corrupt_database_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.sqlite3"
            path.write_bytes(b"this is not sqlite")
            with self.assertRaises(RuntimeError):
                StateStore(path)

    def test_invalid_document_json_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.sqlite3"
            store = StateStore(path)
            store._conn.execute(
                "INSERT INTO state (key, value, updated_at) VALUES (?, ?, ?)",
                ("state", "[1, 2, 3]", 0.0),
            )
            with self.assertRaises(RuntimeError):
                store.load()
            store._conn.execute(
                "UPDATE state SET value = ? WHERE key = ?", ("{broken", "state")
            )
            with self.assertRaises(RuntimeError):
                store.load()
            store.close()

    def test_claim_event_is_idempotent_across_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.sqlite3"
            store = StateStore(path)
            self.assertTrue(store.claim_event("channel:1"))
            self.assertFalse(store.claim_event("channel:1"))
            self.assertTrue(store.claim_event("channel:2"))
            store.close()
            reopened = StateStore(path)
            self.assertFalse(reopened.claim_event("channel:1"))
            self.assertTrue(reopened.claim_event("channel:3"))
            reopened.close()

    def test_prune_events_only_removes_old_claims(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.sqlite3")
            store.claim_event("old")
            store._conn.execute(
                "UPDATE processed_events SET processed_at = 0 "
                "WHERE event_id = ?",
                ("old",),
            )
            store.claim_event("new")
            removed = store.prune_events(1.0)
            self.assertEqual(removed, 1)
            # The pruned id can be claimed again; the recent one cannot.
            self.assertTrue(store.claim_event("old"))
            self.assertFalse(store.claim_event("new"))
            store.close()

    def test_commit_survives_a_process_kill_without_close(self) -> None:
        # ``synchronous=FULL`` plus WAL must make a committed snapshot durable
        # even when the writer dies with ``os._exit`` and never closes SQLite.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.sqlite3"
            script = (
                "import os, sys\n"
                "from pathlib import Path\n"
                f"sys.path.insert(0, {str(SRC_DIR)!r})\n"
                "from paper_bot.state_store import StateStore\n"
                f"store = StateStore(Path({str(path)!r}))\n"
                "store.save({'wallet': {'sol_balance': 4.2}, 'positions': []})\n"
                "store.claim_event('crash:1')\n"
                "os._exit(0)\n"
            )
            env = dict(os.environ)
            env["PYTHONPATH"] = str(SRC_DIR)
            result = subprocess.run(
                [sys.executable, "-c", script],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            store = StateStore(path)
            loaded = store.load()
            self.assertFalse(store.claim_event("crash:1"))
            store.close()
        assert loaded is not None
        self.assertEqual(loaded["wallet"], {"sol_balance": 4.2})
        self.assertEqual(
            json.dumps(loaded["positions"]), json.dumps([])
        )


if __name__ == "__main__":
    unittest.main()
