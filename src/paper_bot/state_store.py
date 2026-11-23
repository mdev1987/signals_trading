"""Durable key/value state for paper runs, backed by built-in ``sqlite3``.

A restart snapshot has to survive a hard kill without being half-written and
without silently losing open positions. The first implementation used one
atomically replaced JSON file; that is crash-safe for a single writer but has
no transactions, no schema history and no idempotency. This module replaces it
with SQLite:

* WAL journal and ``synchronous=FULL`` so a committed snapshot survives a
  power loss, not just a process crash.
* Every write is one ``BEGIN IMMEDIATE`` transaction, so a reader can never
  observe half a snapshot and two writers cannot interleave.
* ``PRAGMA user_version`` drives forward-only migrations over an existing
  file, so a newer binary refuses an old schema instead of guessing.
* ``processed_events`` makes message handling idempotent across restarts: an
  ``INSERT OR IGNORE`` claims an id exactly once.

The store is synchronous on purpose. Callers include engine callbacks that run
inside the bot's asyncio loop, and a lost snapshot is worse than a few
milliseconds of blocking I/O. It is opened with ``check_same_thread=True`` and
used from the loop thread only.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

_MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (
        """
        CREATE TABLE IF NOT EXISTS state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at REAL NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS processed_events (
            event_id TEXT PRIMARY KEY,
            processed_at REAL NOT NULL
        )
        """,
    ),
}


class StateStore:
    """One SQLite file holding named JSON documents and processed event ids."""

    def __init__(self, path: Path) -> None:
        self.path = path
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(
                self.path,
                timeout=30.0,
                isolation_level=None,  # explicit transactions below
            )
        except (OSError, sqlite3.Error) as exc:
            raise RuntimeError(
                f"cannot open state store at {self.path}: {exc}"
            ) from exc
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=FULL")
            self._conn.execute("PRAGMA busy_timeout=30000")
            self._migrate()
        except sqlite3.Error as exc:
            self.close()
            raise RuntimeError(
                f"cannot initialize state store at {self.path}: {exc}"
            ) from exc
        # No secrets are stored here, but the snapshot still describes wallet
        # balances and open positions; keep it owner-only.
        try:
            os.chmod(self.path, 0o600)
        except OSError:  # pragma: no cover - best effort
            pass

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------
    def _migrate(self) -> None:
        version = int(
            self._conn.execute("PRAGMA user_version").fetchone()[0]
        )
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"state store at {self.path} has schema version {version}; "
                f"this build understands at most {SCHEMA_VERSION}"
            )
        if version == SCHEMA_VERSION:
            return
        for target in range(version + 1, SCHEMA_VERSION + 1):
            statements = _MIGRATIONS[target]
            with self._transaction():
                for statement in statements:
                    self._conn.execute(statement)
                # PRAGMA user_version does not accept a bound parameter.
                self._conn.execute(f"PRAGMA user_version = {target}")

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    # ------------------------------------------------------------------
    # JSON documents
    # ------------------------------------------------------------------
    def load(self, key: str = "state") -> dict[str, Any] | None:
        """Return the document stored under ``key``, or ``None`` if absent.

        A present row that is not a JSON object raises: silently returning
        ``None`` would let a damaged snapshot look like a clean first start.
        """
        try:
            row = self._conn.execute(
                "SELECT value FROM state WHERE key = ?", (key,)
            ).fetchone()
        except sqlite3.Error as exc:
            raise RuntimeError(
                f"cannot read state store at {self.path}: {exc}"
            ) from exc
        if row is None:
            return None
        try:
            payload = json.loads(row[0])
        except (TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"state store at {self.path} has invalid JSON for {key!r}: "
                f"{exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise RuntimeError(
                f"state store at {self.path} has a non-object {key!r}"
            )
        return payload

    def save(self, payload: dict[str, Any], key: str = "state") -> None:
        """Atomically replace ``key`` with ``payload``."""
        if not isinstance(payload, dict):
            raise TypeError("state payload must be a dict")
        document = json.dumps(payload, sort_keys=True)
        try:
            with self._transaction():
                self._conn.execute(
                    """
                    INSERT INTO state (key, value, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value = excluded.value,
                        updated_at = excluded.updated_at
                    """,
                    (key, document, time.time()),
                )
        except sqlite3.Error as exc:
            raise RuntimeError(
                f"cannot write state store at {self.path}: {exc}"
            ) from exc

    def clear(self, key: str = "state") -> None:
        try:
            with self._transaction():
                self._conn.execute("DELETE FROM state WHERE key = ?", (key,))
        except sqlite3.Error as exc:
            raise RuntimeError(
                f"cannot clear state store at {self.path}: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Idempotent event processing
    # ------------------------------------------------------------------
    def claim_event(self, event_id: str) -> bool:
        """Claim ``event_id`` exactly once; ``False`` if it was already seen.

        The claim is durable before the caller acts on the event. It cannot
        make a side effect (an append to ``signals.jsonl``) transactional with
        the claim, so the caller must still be tolerant of a crash between the
        two; the goal is to stop a redelivered message from being processed
        twice after a restart.
        """
        try:
            cursor = self._conn.execute(
                "INSERT OR IGNORE INTO processed_events (event_id, processed_at) "
                "VALUES (?, ?)",
                (event_id, time.time()),
            )
        except sqlite3.Error as exc:
            raise RuntimeError(
                f"cannot record processed event in {self.path}: {exc}"
            ) from exc
        return cursor.rowcount == 1

    def prune_events(self, before_ts: float) -> int:
        """Drop claims older than ``before_ts``; returns the row count."""
        try:
            with self._transaction():
                cursor = self._conn.execute(
                    "DELETE FROM processed_events WHERE processed_at < ?",
                    (before_ts,),
                )
        except sqlite3.Error as exc:
            raise RuntimeError(
                f"cannot prune state store at {self.path}: {exc}"
            ) from exc
        return int(cursor.rowcount)

    def close(self) -> None:
        conn = getattr(self, "_conn", None)
        if conn is None:
            return
        try:
            conn.close()
        except sqlite3.Error:  # pragma: no cover - close during teardown
            pass

    def __del__(self) -> None:  # pragma: no cover - GC safety net
        self.close()

    def __enter__(self) -> StateStore:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


__all__ = ["SCHEMA_VERSION", "StateStore"]
