"""Telegram command handlers for dispatchers."""

from __future__ import annotations

import logging
import re

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup

from .config import Settings
from .database import Database
from .fuel_logic import FuelMonitor, FuelReading, format_percent, fuel_color
from .messaging import send_messages_with_retry
from .samsara_client import SamsaraClient, SamsaraClientError


LOGGER = logging.getLogger(__name__)

LOW_FUEL_BUTTON = "⛽ Low Fuel"
ATTENTION_BUTTON = "🚨 Needs Notes"
NOTES_BUTTON = "📝 Active Notes"
CHECK_FUEL_BUTTON = "🔄 Check Fuel"
ADD_NOTE_BUTTON = "➕ Add Note"
CLEAR_NOTE_BUTTON = "✅ Clear Note"
CANCEL_BUTTON = "Cancel"


class BotFlow(StatesGroup):
    """Simple prompt states for button-driven dispatcher actions."""

    waiting_for_note = State()
    waiting_for_clear_note = State()


def build_router(
    database: Database,
    settings: Settings,
    monitor: FuelMonitor,
    samsara_client: SamsaraClient,
) -> Router:
    """Register all bot commands and return an aiogram router."""
    router = Router()

    async def send_actions(bot: Bot, actions: list[object]) -> None:
        alert_chat_id = resolve_alert_chat_id(database, settings)
        if alert_chat_id is None:
            LOGGER.warning("No alert chat configured; generated %s action(s).", len(actions))
            return
        await send_messages_with_retry(bot, alert_chat_id, (action.message for action in actions))

    @router.message(Command("start"))
    async def start(message: Message) -> None:
        database.set_config("last_start_chat_id", str(message.chat.id))
        await message.answer(
            "⛽ Fuel Monitor Bot\n\n"
            "Use the buttons below for the daily workflow.",
            reply_markup=main_menu_keyboard(),
        )

    @router.message(Command("fuel"))
    async def fuel(message: Message) -> None:
        await show_low_fuel(message)

    @router.message(Command("fuel_attention"))
    async def fuel_attention(message: Message) -> None:
        await show_fuel_attention(message)

    @router.message(Command("note"))
    async def note(message: Message) -> None:
        if await save_note_from_reply(message, command_args(message)):
            return
        await save_note_from_text(message, command_args(message))

    @router.message(Command("clear_note"))
    async def clear_note(message: Message) -> None:
        await clear_note_for_unit(message, command_args(message))

    @router.message(Command("notes"))
    async def notes(message: Message) -> None:
        await show_notes(message)

    @router.message(Command("checkfuel"))
    async def checkfuel(message: Message, bot: Bot) -> None:
        await run_checkfuel(message, bot)

    @router.message(Command("testfuel"))
    async def testfuel(message: Message, bot: Bot) -> None:
        await test_fuel_from_text(message, bot, command_args(message))

    @router.message(Command("set_alert_chat"))
    async def set_alert_chat(message: Message) -> None:
        chat_id = message.chat.id
        database.set_config("alert_chat_id", str(chat_id))
        await message.answer(
            "✅ Alert chat saved in the bot database.\n"
            f"Current chat ID: {chat_id}\n"
            "You can also place this value in ALERT_CHAT_ID in .env.",
            reply_markup=main_menu_keyboard(),
        )

    @router.message(F.text == LOW_FUEL_BUTTON)
    async def low_fuel_button(message: Message) -> None:
        await show_low_fuel(message)

    @router.message(F.text == ATTENTION_BUTTON)
    async def attention_button(message: Message) -> None:
        await show_fuel_attention(message)

    @router.message(F.text == NOTES_BUTTON)
    async def notes_button(message: Message) -> None:
        await show_notes(message)

    @router.message(F.text == CHECK_FUEL_BUTTON)
    async def check_fuel_button(message: Message, bot: Bot) -> None:
        await run_checkfuel(message, bot)

    @router.message(F.text == ADD_NOTE_BUTTON)
    async def add_note_button(message: Message, state: FSMContext) -> None:
        await state.set_state(BotFlow.waiting_for_note)
        await message.answer(
            "📝 Send the unit and note like this:\n"
            "1002 Sent to Love's Atlanta",
            reply_markup=cancel_keyboard(),
        )

    @router.message(F.text == CLEAR_NOTE_BUTTON)
    async def clear_note_button(message: Message, state: FSMContext) -> None:
        await state.set_state(BotFlow.waiting_for_clear_note)
        await message.answer("✅ Send the unit number to clear.", reply_markup=cancel_keyboard())

    @router.message(F.text == CANCEL_BUTTON)
    async def cancel_button(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer("Cancelled.", reply_markup=main_menu_keyboard())

    @router.message(BotFlow.waiting_for_note)
    async def note_prompt_response(message: Message, state: FSMContext) -> None:
        await save_note_from_text(message, message.text or "")
        await state.clear()

    @router.message(BotFlow.waiting_for_clear_note)
    async def clear_note_prompt_response(message: Message, state: FSMContext) -> None:
        await clear_note_for_unit(message, message.text or "")
        await state.clear()

    async def show_low_fuel(message: Message) -> None:
        try:
            await fetch_and_store_samsara(database, samsara_client)
        except SamsaraClientError as exc:
            LOGGER.warning("Could not refresh Samsara fuel before low fuel view: %s", exc)
        rows = database.list_low_fuel_states(settings.fuel_threshold)
        await message.answer(format_fuel_rows(rows, "⛽ Trucks at 60% or below"), reply_markup=main_menu_keyboard())

    async def show_fuel_attention(message: Message) -> None:
        rows = database.list_low_fuel_states(settings.fuel_threshold, only_without_notes=True)
        await message.answer(format_fuel_rows(rows, "🚨 Trucks needing dispatcher notes"), reply_markup=main_menu_keyboard())

    async def save_note_from_text(message: Message, text: str) -> None:
        parts = text.strip().split(maxsplit=1)
        if len(parts) < 2:
            await message.answer(
                "Send the unit and note like this:\n"
                "1002 Sent to Love's Atlanta",
                reply_markup=main_menu_keyboard(),
            )
            return

        unit, note_text = parts[0].strip(), parts[1].strip()
        await save_note_for_unit(message, unit, note_text)

    async def save_note_from_reply(message: Message, text: str) -> bool:
        note_text = strip_wrapping_quotes(text.strip())
        if not message.reply_to_message:
            return False
        if not note_text:
            await message.answer(
                "Reply with /note followed by the dispatcher note, like:\n"
                "/note Sent to Love's Atlanta",
                reply_markup=main_menu_keyboard(),
            )
            return True

        unit = unit_from_alert_message(message.reply_to_message)
        if unit is None:
            await message.answer(
                "Reply with /note only on a fuel alert that includes a Unit line.",
                reply_markup=main_menu_keyboard(),
            )
            return True

        await save_note_for_unit(message, unit, note_text)
        return True

    async def save_note_for_unit(message: Message, unit: str, note_text: str) -> None:
        state = database.get_fuel_state(unit)
        fuel_at_creation = float(state["current_fuel"]) if state else 0.0
        created_by = user_label(message)
        database.upsert_note(unit, note_text, fuel_at_creation, created_by)
        await message.answer(
            "📝 Note saved\n"
            f"Unit: {unit}\n"
            f"Fuel at note creation: {format_percent(fuel_at_creation)}\n"
            f"Note: {note_text}",
            reply_markup=main_menu_keyboard(),
        )

    async def clear_note_for_unit(message: Message, text: str) -> None:
        unit = text.strip()
        if not unit:
            await message.answer("Send the unit number to clear.", reply_markup=main_menu_keyboard())
            return
        cleared = database.clear_note(unit)
        if cleared:
            await message.answer(f"✅ Cleared note for unit {unit}.", reply_markup=main_menu_keyboard())
        else:
            await message.answer(f"ℹ️ No active note found for unit {unit}.", reply_markup=main_menu_keyboard())

    async def show_notes(message: Message) -> None:
        rows = database.get_active_notes()
        if not rows:
            await message.answer("✅ No active fuel notes.", reply_markup=main_menu_keyboard())
            return
        lines = ["📝 Active Fuel Notes", ""]
        for row in rows:
            lines.append(
                f"• {row['unit_number']} - {row['note']}\n"
                f"  Fuel then: {format_percent(float(row['fuel_at_note_creation']))}\n"
                f"  By: {row['created_by']}"
            )
        await message.answer("\n".join(lines), reply_markup=main_menu_keyboard())

    async def run_checkfuel(message: Message, bot: Bot) -> None:
        try:
            readings = await fetch_and_store_samsara(database, samsara_client, return_readings=True)
        except SamsaraClientError as exc:
            await message.answer(f"⚠️ Samsara fuel check failed: {exc}", reply_markup=main_menu_keyboard())
            return

        actions = monitor.process_readings(readings)
        await send_actions(bot, actions)
        await message.answer(
            "✅ Fuel check complete.\n"
            f"Samsara readings: {format_samsara_fetch_summary(samsara_client)}\n"
            f"Alerts/completions sent: {len(actions)}",
            reply_markup=main_menu_keyboard(),
        )

    async def test_fuel_from_text(message: Message, bot: Bot, text: str) -> None:
        args = text.split()
        if len(args) != 2:
            await message.answer(
                "Send the unit and test fuel percent like this:\n"
                "1002 45",
                reply_markup=main_menu_keyboard(),
            )
            return
        unit = args[0]
        try:
            fuel_percent = float(args[1])
        except ValueError:
            await message.answer("Fuel percent must be a number, like 45 or 70.", reply_markup=main_menu_keyboard())
            return

        actions = monitor.process_reading(FuelReading(unit, fuel_percent))
        await send_actions(bot, actions)
        if actions:
            await message.answer(f"✅ Test processed for {unit}. Action sent to alert chat.", reply_markup=main_menu_keyboard())
        else:
            await message.answer(f"✅ Test processed for {unit}. No alert needed.", reply_markup=main_menu_keyboard())

    return router


async def fetch_and_store_samsara(
    database: Database,
    samsara_client: SamsaraClient,
    return_readings: bool = False,
) -> list[FuelReading]:
    """Fetch Samsara fuel levels and store them as fuel states."""
    raw_readings = await samsara_client.fetch_fuel_levels()
    readings = [
        FuelReading(
            unit_number=str(item["unit_number"]),
            fuel_percent=float(item["fuel_percent"]),
            vehicle_id=item.get("vehicle_id"),
            vehicle_name=item.get("vehicle_name"),
        )
        for item in raw_readings
    ]
    if not return_readings:
        database.bulk_upsert_fuel_states(raw_readings)
    return readings


def resolve_alert_chat_id(database: Database, settings: Settings) -> int | None:
    """Prefer .env, then /set_alert_chat."""
    if settings.alert_chat_id is not None:
        return settings.alert_chat_id
    stored = database.get_config("alert_chat_id")
    if stored:
        return int(stored)
    return None


def command_args(message: Message) -> str:
    text = message.text or ""
    return text.split(maxsplit=1)[1] if len(text.split(maxsplit=1)) > 1 else ""


def unit_from_alert_message(message: Message) -> str | None:
    text = message.text or message.caption or ""
    match = re.search(r"(?im)^Unit:\s*(\S+)", text)
    return match.group(1).strip() if match else None


def strip_wrapping_quotes(text: str) -> str:
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        return text[1:-1].strip()
    return text


def user_label(message: Message) -> str:
    user = message.from_user
    if user is None:
        return "unknown"
    if user.username:
        return f"@{user.username}"
    return str(user.full_name or user.id)


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    """Persistent button menu for the dispatcher workflow."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=LOW_FUEL_BUTTON), KeyboardButton(text=ATTENTION_BUTTON)],
            [KeyboardButton(text=NOTES_BUTTON), KeyboardButton(text=CHECK_FUEL_BUTTON)],
            [KeyboardButton(text=ADD_NOTE_BUTTON), KeyboardButton(text=CLEAR_NOTE_BUTTON)],
        ],
        resize_keyboard=True,
        input_field_placeholder="Choose an action",
    )


def cancel_keyboard() -> ReplyKeyboardMarkup:
    """One-button keyboard shown while the bot is waiting for typed details."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=CANCEL_BUTTON)]],
        resize_keyboard=True,
        input_field_placeholder="Send details or cancel",
    )


def format_fuel_rows(rows: list[object], title: str) -> str:
    if not rows:
        return f"{title}\n\n✅ No trucks found."

    lines = [title, ""]
    for row in rows:
        fuel = float(row["current_fuel"])
        note = row["note"] or "No note"
        lines.append(f"{fuel_color(fuel)} {row['unit_number']} — {format_percent(fuel)}")
        lines.append(f"Note: {note}")
        lines.append("")
    return "\n".join(lines).rstrip()


def format_samsara_fetch_summary(samsara_client: SamsaraClient) -> str:
    """Summarize the most recent Samsara fetch for dispatcher testing."""
    parts = []
    for token_index in range(1, len(samsara_client.api_tokens) + 1):
        count = samsara_client.last_fetch_counts.get(token_index, 0)
        parts.append(f"token {token_index}: {count}")
    parts.append(f"merged: {samsara_client.last_fetch_merged_count}")
    return ", ".join(parts)
