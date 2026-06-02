"""Entrypoint for the fuel monitoring Telegram bot."""

from __future__ import annotations

import asyncio
import logging
import signal

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from .config import load_settings
from .database import Database
from .fuel_logic import FuelMonitor
from .handlers import build_router
from .samsara_client import SamsaraClient
from .scheduler import create_scheduler


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


async def main() -> None:
    configure_logging()
    settings = load_settings()

    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required.")

    database = Database(settings.database_path)
    database.initialize()

    monitor = FuelMonitor(
        database=database,
        fuel_threshold=settings.fuel_threshold,
        auto_clear_increase=settings.auto_clear_increase,
        auto_clear_full_level=settings.auto_clear_full_level,
        repeat_alert_minutes=settings.repeat_alert_minutes,
    )
    samsara_client = SamsaraClient(settings.samsara_api_tokens, database)

    bot = Bot(settings.telegram_bot_token)
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(build_router(database, settings, monitor, samsara_client))

    scheduler = create_scheduler(bot, database, settings, monitor, samsara_client)
    scheduler.start()

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    logging.getLogger(__name__).info("Fuel bot started.")
    polling_task = asyncio.create_task(dispatcher.start_polling(bot))
    stop_task = asyncio.create_task(stop_event.wait())

    done, pending = await asyncio.wait(
        {polling_task, stop_task},
        return_when=asyncio.FIRST_COMPLETED,
    )

    for task in pending:
        task.cancel()
    for task in done:
        task.result()

    scheduler.shutdown(wait=False)
    await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
