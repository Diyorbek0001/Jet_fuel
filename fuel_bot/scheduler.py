"""Background scheduler for recurring fuel checks."""

from __future__ import annotations

import logging

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .config import Settings
from .database import Database
from .fuel_logic import FuelMonitor
from .handlers import fetch_and_store_samsara, resolve_alert_chat_id
from .messaging import send_messages_with_retry
from .samsara_client import SamsaraClient, SamsaraClientError


LOGGER = logging.getLogger(__name__)


def create_scheduler(
    bot: Bot,
    database: Database,
    settings: Settings,
    monitor: FuelMonitor,
    samsara_client: SamsaraClient,
) -> AsyncIOScheduler:
    """Create an APScheduler instance with the recurring fuel job."""
    scheduler = AsyncIOScheduler(timezone="UTC")

    async def scheduled_check() -> None:
        try:
            readings = await fetch_and_store_samsara(database, samsara_client, return_readings=True)
            actions = monitor.process_readings(readings)
        except SamsaraClientError as exc:
            LOGGER.exception("Scheduled Samsara fuel check failed: %s", exc)
            return

        if not actions:
            return
        if not settings.notifications_enabled:
            LOGGER.info("Notifications disabled; suppressed %s scheduled message(s).", len(actions))
            return

        alert_chat_id = resolve_alert_chat_id(database, settings)
        if alert_chat_id is None:
            LOGGER.warning("Skipping %s scheduled messages because no alert chat is configured.", len(actions))
            return

        await send_messages_with_retry(bot, alert_chat_id, (action.message for action in actions))

    scheduler.add_job(
        scheduled_check,
        "interval",
        minutes=settings.check_interval_minutes,
        id="fuel_check",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    return scheduler
