"""Paper-mode restart recovery.

A forward experiment is only continuous if a process restart resumes the same
wallet, open positions and run directory. This module stores one small
snapshot per lane in SQLite via :class:`~paper_bot.state_store.StateStore`:
atomic replacement, WAL + ``synchronous=FULL`` durability, schema migrations
and durable processed-event claims. ``live.py`` wires it in for paper mode
only.

Live mode does not use this store: its balances and positions are authoritative
on chain, so restoring a local paper wallet would be wrong.

The previous implementation was one atomically replaced JSON file. It is still
imported once, automatically, from ``<lane>.json`` next to the new
``<lane>.sqlite3`` so a deploy does not abandon the open positions of a run in
progress. After the import the legacy file is renamed to ``*.imported``.
"""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .state_store import StateStore

RECOVERY_VERSION = 1
# Sidecar written when the snapshot store itself fails. It deliberately does
# not live in SQLite: the whole point is to survive a write failure in the
# database that holds the snapshot. A restart reconciles this marker with the
# last durable snapshot; a marker with no matching snapshot integrity is
# classified INVALID rather than being silently forgotten.
DEGRADATION_MARKER_SUFFIX = ".degraded"


def _lane_stem(rule: str, entry_mode: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", f"{rule}_{entry_mode}").strip("._")
    return stem or "lane"


def default_state_db(rule: str, entry_mode: str) -> Path:
    """Stable per-lane recovery database, independent of the run directory."""
    return Path("output/recovery") / f"{_lane_stem(rule, entry_mode)}.sqlite3"


def _validate(payload: dict[str, Any], where: Path) -> dict[str, Any]:
    version = payload.get("version")
    if version != RECOVERY_VERSION:
        raise RuntimeError(
            f"recovery state at {where} has unsupported version "
            f"{version!r}; expected {RECOVERY_VERSION}"
        )
    for key, expected in (
        ("run_dir", str),
        ("wallet", dict),
        ("token_balances", dict),
        ("positions", list),
        ("fingerprint", dict),
    ):
        if not isinstance(payload.get(key), expected):
            raise RuntimeError(
                f"recovery state at {where} has no valid {key!r}"
            )
    return payload


class RecoveryStore:
    """Read/write one lane's restart snapshot."""

    def __init__(self, path: Path) -> None:
        # An explicitly passed ``.json`` path is the legacy layout; keep the
        # caller working by opening the SQLite sibling and importing the file.
        self.legacy_path = (
            path if path.suffix == ".json" else path.with_suffix(".json")
        )
        self.path = (
            path.with_suffix(".sqlite3") if path.suffix == ".json" else path
        )
        self._store = StateStore(self.path)
        self._import_legacy()

    # ------------------------------------------------------------------
    # Legacy one-time import
    # ------------------------------------------------------------------
    def _import_legacy(self) -> None:
        if not self.legacy_path.exists():
            return
        if self._store.load() is not None:
            # The database already holds a snapshot; never overwrite newer
            # state with an older JSON file.
            return
        try:
            raw = self.legacy_path.read_text(encoding="utf-8")
            document = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"legacy recovery state at {self.legacy_path} is unreadable: "
                f"{exc}"
            ) from exc
        if not isinstance(document, dict) or "state" not in document:
            raise RuntimeError(
                f"legacy recovery state at {self.legacy_path} has no 'state' "
                "envelope"
            )
        payload = document["state"]
        if not isinstance(payload, dict):
            raise RuntimeError(
                f"legacy recovery state at {self.legacy_path} is not an object"
            )
        self._store.save(_validate(payload, self.legacy_path))
        try:
            self.legacy_path.rename(
                self.legacy_path.with_suffix(".json.imported")
            )
        except OSError:  # pragma: no cover - import already succeeded
            pass

    # ------------------------------------------------------------------
    # Snapshot API
    # ------------------------------------------------------------------
    def exists(self) -> bool:
        return self._store.load() is not None

    def load(self) -> dict[str, Any] | None:
        """Return the saved snapshot, or ``None`` when none has been saved.

        A damaged database, a row that is not an object, or an unsupported
        schema version raises: silently starting a fresh paper wallet would
        discard open positions the operator expected to recover.
        """
        payload = self._store.load()
        if payload is None:
            return None
        return _validate(payload, self.path)

    def save(self, payload: dict[str, Any]) -> None:
        """Atomically replace the saved snapshot."""
        self._store.save({"version": RECOVERY_VERSION, **payload})

    def clear(self) -> None:
        self._store.clear()

    # ------------------------------------------------------------------
    # Durable degradation marker
    # ------------------------------------------------------------------
    @property
    def degradation_marker_path(self) -> Path:
        return self.path.with_name(self.path.name + DEGRADATION_MARKER_SUFFIX)

    def mark_degraded(
        self, *, run_dir: str, lane: str, reason: str
    ) -> None:
        """Record a snapshot-write failure outside the failing database.

        Written atomically (temp file + fsync + rename) so a crash cannot leave
        a half marker. Raises ``RuntimeError`` if the marker cannot be made
        durable; the caller then treats the run as INVALID because a restart
        would not be able to detect the degradation.
        """
        marker = self.degradation_marker_path
        payload = {
            "version": RECOVERY_VERSION,
            "run_dir": run_dir,
            "lane": lane,
            "reason": reason,
            "marked_at": datetime.now(UTC).isoformat(),
        }
        temporary = marker.with_name(marker.name + ".tmp")
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, marker)
            try:
                os.chmod(marker, 0o600)
            except OSError:  # pragma: no cover - best effort
                pass
        except OSError as exc:
            try:
                temporary.unlink()
            except OSError:
                pass
            raise RuntimeError(
                f"cannot write degradation marker at {marker}: {exc}"
            ) from exc

    def load_degradation(self) -> dict[str, Any] | None:
        """Return the durable degradation marker, or ``None`` if absent.

        An unreadable or non-object marker raises: ignoring it would let a
        restart treat a run with a failed snapshot write as clean.
        """
        marker = self.degradation_marker_path
        if not marker.exists():
            return None
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"degradation marker at {marker} is unreadable: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise RuntimeError(
                f"degradation marker at {marker} is not an object"
            )
        return payload

    def clear_degradation(self) -> bool:
        """Remove the marker for a fresh sample; returns whether one existed."""
        marker = self.degradation_marker_path
        try:
            marker.unlink()
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise RuntimeError(
                f"cannot clear degradation marker at {marker}: {exc}"
            ) from exc
        return True

    # ------------------------------------------------------------------
    # Auxiliary counters and idempotency
    # ------------------------------------------------------------------
    def load_aux(self, key: str) -> dict[str, Any] | None:
        return self._store.load(key)

    def save_aux(self, key: str, payload: dict[str, Any]) -> None:
        self._store.save(payload, key)

    def claim_event(self, event_id: str) -> bool:
        return self._store.claim_event(event_id)

    def prune_events(self, before_ts: float) -> int:
        return self._store.prune_events(before_ts)

    def close(self) -> None:
        self._store.close()


__all__ = [
    "DEGRADATION_MARKER_SUFFIX",
    "RECOVERY_VERSION",
    "RecoveryStore",
    "default_state_db",
]
