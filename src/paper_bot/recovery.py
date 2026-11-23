"""Paper-mode restart recovery.

A forward experiment is only continuous if a process restart resumes the same
wallet, open positions and run directory. This module stores a small plain-JSON
snapshot in one atomically replaced file; the engine supplies and consumes the
payload, and ``live.py`` wires it in for paper mode only.

Live mode does not use this store: its balances and positions are authoritative
on chain, so restoring a local paper wallet would be wrong.

Why not PickleDB?
    ``pickledb`` was tried here and rejected. Its methods are dual sync/async:
    inside a running event loop ``db.set()``/``db.save()`` return a coroutine
    instead of doing the work. The engine persists state from synchronous
    callbacks that run inside the bot's loop, so every snapshot would have been
    an un-awaited coroutine and the file would silently never be written.
    This module therefore writes the identical ``{"state": {...}}`` JSON
    envelope itself: encode, write a sibling temp file, ``os.replace``. That is
    the same crash-safety property without the async trap, and a file written by
    PickleDB still loads here.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

RECOVERY_VERSION = 1


def _lane_stem(rule: str, entry_mode: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", f"{rule}_{entry_mode}").strip("._")
    return stem or "lane"


def default_state_db(rule: str, entry_mode: str) -> Path:
    """Stable per-lane recovery file, independent of the timestamped run dir."""
    return Path("output/recovery") / f"{_lane_stem(rule, entry_mode)}.json"


class RecoveryStore:
    """Read/write one lane's restart snapshot."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def exists(self) -> bool:
        return self.path.exists()

    def load(self) -> dict[str, Any] | None:
        """Return the saved snapshot, or ``None`` when the file is absent.

        A file that exists but cannot be parsed, is not an object, or has an
        unsupported schema version raises: silently starting a fresh paper
        wallet would discard open positions the operator expected to recover.
        """
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise RuntimeError(
                f"cannot read recovery state at {self.path}: {exc}"
            ) from exc
        try:
            document = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"recovery state at {self.path} is not valid JSON: {exc}"
            ) from exc
        payload = document.get("state") if isinstance(document, dict) else None
        if payload is None:
            return None
        if not isinstance(payload, dict):
            raise RuntimeError(
                f"recovery state at {self.path} is not a JSON object"
            )
        version = payload.get("version")
        if version != RECOVERY_VERSION:
            raise RuntimeError(
                f"recovery state at {self.path} has unsupported version "
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
                    f"recovery state at {self.path} has no valid {key!r}"
                )
        return payload

    def save(self, payload: dict[str, Any]) -> None:
        """Atomically replace the saved snapshot.

        The write is synchronous on purpose: callers include synchronous engine
        callbacks inside the event loop, and losing the write would silently
        disable restart recovery.
        """
        document = json.dumps(
            {"state": {"version": RECOVERY_VERSION, **payload}},
            sort_keys=True,
        )
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(document, encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as exc:
            raise RuntimeError(
                f"cannot write recovery state at {self.path}: {exc}"
            ) from exc

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            return
