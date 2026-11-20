"""Telethon listener for Telegram signal channels.

The reporting bot uses the Bot API (``python-telegram-bot``); reading channels
still needs a user session, so Telethon is used here. The session must be
authorised once with ``paper-bot login``; after that the daemon can start
without a TTY.

Credentials are read from ``config.ini`` (``[Telegram]``) or from
``TELEGRAM_API_ID`` / ``TELEGRAM_API_HASH`` / ``TELEGRAM_PHONE``.
"""

from __future__ import annotations

import configparser
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from telethon import TelegramClient, events

LOGGER = logging.getLogger(__name__)


class TelegramConfigError(RuntimeError):
    """Raised when the Telethon session cannot be configured or authorised."""


@dataclass(slots=True)
class TelegramConfig:
    api_id: int
    api_hash: str
    phone: str | None
    session_file: Path


def _env(env: dict[str, str], *names: str) -> str | None:
    for name in names:
        value = env.get(name) or os.environ.get(name)
        if value:
            return value.strip()
    return None


def load_telegram_config(
    config_path: Path,
    *,
    env: dict[str, str] | None = None,
    session_path: Path | None = None,
) -> TelegramConfig:
    """Load Telethon credentials from the environment, then ``config.ini``."""
    env = env or {}
    parser = configparser.ConfigParser()
    if config_path.exists():
        parser.read(config_path, encoding="utf-8")

    section = parser["Telegram"] if parser.has_section("Telegram") else {}

    api_id = _env(env, "TELEGRAM_API_ID", "API_ID") or section.get("api_id")
    api_hash = _env(env, "TELEGRAM_API_HASH", "API_HASH") or section.get("api_hash")
    phone = _env(env, "TELEGRAM_PHONE", "PHONE") or section.get("phone")
    raw_session = (
        str(session_path)
        if session_path is not None
        else _env(env, "PAPER_BOT_SESSION")
        or section.get("session_file")
    )

    if not api_id or not api_hash:
        raise TelegramConfigError(
            f"Telegram api_id/api_hash missing. Set them in {config_path} "
            "([Telegram] api_id/api_hash) or TELEGRAM_API_ID/TELEGRAM_API_HASH."
        )
    try:
        api_id_int = int(api_id)
    except ValueError as exc:
        raise TelegramConfigError(f"api_id is not an integer: {api_id!r}") from exc

    if raw_session:
        session_file = Path(raw_session).expanduser()
        if session_file.suffix != ".session":
            session_file = session_file.with_suffix(".session")
        session_file = session_file.resolve()
    else:
        session_file = (config_path.parent / "paper_bot.session").resolve()

    return TelegramConfig(
        api_id=api_id_int,
        api_hash=api_hash,
        phone=phone,
        session_file=session_file,
    )


def _message_ts_ms(event: Any) -> int:
    date = getattr(event.message, "date", None)
    if isinstance(date, datetime):
        if date.tzinfo is None:
            date = date.replace(tzinfo=UTC)
        return int(date.timestamp() * 1000)
    return int(datetime.now(UTC).timestamp() * 1000)


def _channel_name(event: Any, fallback: str) -> str:
    chat = getattr(event, "chat", None)
    username = getattr(chat, "username", None)
    if username:
        return str(username)
    title = getattr(chat, "title", None)
    if title:
        return str(title)
    return fallback


class TelegramListener:
    """Read new messages from the configured channels with a user session."""

    def __init__(
        self,
        config: TelegramConfig,
        channels: list[str],
        on_message: Callable[..., Awaitable[None]],
    ) -> None:
        self.config = config
        self.channels = channels
        self.on_message = on_message
        self.client: TelegramClient | None = None

    async def login(self) -> str:
        """Authorise the session interactively (run once, not from the daemon)."""
        self.config.session_file.parent.mkdir(parents=True, exist_ok=True)
        client = TelegramClient(
            str(self.config.session_file), self.config.api_id, self.config.api_hash
        )
        try:
            await client.start(phone=self.config.phone)
            me = await client.get_me()
            return str(getattr(me, "username", None) or getattr(me, "id", "unknown"))
        finally:
            await client.disconnect()

    async def run(self) -> None:
        """Connect, resolve channels and consume messages until disconnected."""
        self.config.session_file.parent.mkdir(parents=True, exist_ok=True)
        client = TelegramClient(
            str(self.config.session_file), self.config.api_id, self.config.api_hash
        )
        self.client = client
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            raise TelegramConfigError(
                "Telegram session is not authorised. Run `paper-bot login` once "
                f"to create {self.config.session_file}."
            )

        entities: list[Any] = []
        for channel in self.channels:
            try:
                entities.append(await client.get_entity(channel))
            except Exception as exc:  # noqa: BLE001 - one bad channel is not fatal
                LOGGER.warning(
                    "Could not resolve Telegram channel %s: %s", channel, exc
                )
        if not entities:
            await client.disconnect()
            raise TelegramConfigError(
                "No Telegram channels could be resolved; check channels.txt and "
                "that the session is a member of them."
            )

        async def handler(event: Any) -> None:
            message = getattr(event.message, "message", None) or ""
            if not message.strip():
                return
            fallback = self.channels[0] if self.channels else "unknown"
            await self.on_message(
                channel=_channel_name(event, fallback),
                message=message,
                message_id=int(getattr(event.message, "id", 0) or 0),
                ts_ms=_message_ts_ms(event),
            )

        client.add_event_handler(handler, events.NewMessage(chats=entities))
        LOGGER.info(
            "Telegram listener connected as %s; watching %s",
            self.config.session_file.name,
            ", ".join(self.channels),
        )
        try:
            await client.run_until_disconnected()
        finally:
            await client.disconnect()
            self.client = None

    async def close(self) -> None:
        if self.client is not None:
            await self.client.disconnect()
            self.client = None


__all__ = [
    "TelegramConfig",
    "TelegramConfigError",
    "TelegramListener",
    "load_telegram_config",
]
