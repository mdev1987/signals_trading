"""Filesystem locations for the standalone bot.

The project can run in two places:

* the research repository (``tg_signals_vbt``), where ``.env``, ``config.ini``,
  ``channels.txt`` and ``output/`` live in the current directory; and
* a VPS deployment of this package, where they live next to ``pyproject.toml``.

Every lookup therefore checks the current directory first and falls back to the
project root. ``PAPER_BOT_HOME`` overrides the project root explicitly, which is
what the oxmgr deployment uses.
"""

from __future__ import annotations

import os
from pathlib import Path


def project_home() -> Path:
    """Return the standalone project root."""
    override = os.environ.get("PAPER_BOT_HOME")
    if override:
        return Path(override).expanduser().resolve()

    package = Path(__file__).resolve()
    # src layout: <project>/src/paper_bot/paths.py
    candidate = package.parents[2]
    if (candidate / "pyproject.toml").exists() and (
        candidate / "src" / "paper_bot"
    ).exists():
        return candidate
    return Path.cwd().resolve()


def data_home() -> Path:
    """Return the directory that holds ``output/``.

    In the research repository this is the current directory; in a standalone
    deployment it is the project root.
    """
    cwd = Path.cwd()
    if (cwd / "output").is_dir() or (cwd / "backtest_signals.py").exists():
        return cwd
    return project_home()


def _first_existing(name: str, *, env_var: str | None = None) -> Path:
    if env_var:
        override = os.environ.get(env_var)
        if override:
            return Path(override).expanduser().resolve()
    for base in (Path.cwd(), project_home()):
        candidate = base / name
        if candidate.exists():
            return candidate
    return project_home() / name


def env_file() -> Path:
    return _first_existing(".env", env_var="PAPER_BOT_ENV")


def channels_file() -> Path:
    return _first_existing("channels.txt", env_var="PAPER_BOT_CHANNELS")


def config_file() -> Path:
    return _first_existing("config.ini", env_var="PAPER_BOT_CONFIG")


def session_file() -> Path:
    override = os.environ.get("PAPER_BOT_SESSION")
    if override:
        path = Path(override).expanduser().resolve()
    else:
        path = project_home() / "paper_bot.session"
    return path if path.suffix == ".session" else path.with_suffix(".session")


__all__ = [
    "channels_file",
    "config_file",
    "data_home",
    "env_file",
    "project_home",
    "session_file",
]
