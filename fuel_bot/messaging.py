"""Telegram message sending helpers."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable

from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter


LOGGER = logging.getLogger(__name__)


async def send_messages_with_retry(
    bot: Bot,
    chat_id: int,
    messages: Iterable[str],
    pause_seconds: float = 1.0,
) -> int:
    """Send a message batch while respecting Telegram flood-control retries."""
    sent = 0
    for message in messages:
        while True:
            try:
                await bot.send_message(chat_id, message)
                sent += 1
                if pause_seconds:
                    await asyncio.sleep(pause_seconds)
                break
            except TelegramRetryAfter as exc:
                delay = exc.retry_after + 1
                LOGGER.warning("Telegram flood control hit; retrying in %s second(s).", delay)
                await asyncio.sleep(delay)
    return sent
