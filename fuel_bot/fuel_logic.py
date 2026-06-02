"""Shared fuel alert and note auto-clear logic."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .database import Database


@dataclass(frozen=True)
class FuelReading:
    """Normalized fuel reading from Samsara or /testfuel."""

    unit_number: str
    fuel_percent: float
    vehicle_id: str | None = None
    vehicle_name: str | None = None


@dataclass(frozen=True)
class FuelAction:
    """An action that the bot should announce to the alert chat."""

    unit_number: str
    event_type: str
    fuel_percent: float
    message: str


class FuelMonitor:
    """Applies threshold, anti-spam, and note auto-clear rules."""

    def __init__(
        self,
        database: Database,
        fuel_threshold: int,
        auto_clear_increase: int,
        auto_clear_full_level: int,
        repeat_alert_minutes: int = 29,
    ) -> None:
        self.database = database
        self.fuel_threshold = fuel_threshold
        self.auto_clear_increase = auto_clear_increase
        self.auto_clear_full_level = auto_clear_full_level
        self.repeat_alert_after = timedelta(minutes=repeat_alert_minutes)

    def process_readings(self, readings: list[FuelReading]) -> list[FuelAction]:
        """Store readings and return alerts/completions that should be sent."""
        actions: list[FuelAction] = []
        for reading in readings:
            actions.extend(self.process_reading(reading))
        return actions

    def process_reading(self, reading: FuelReading) -> list[FuelAction]:
        """Apply all rules to one unit using the current fuel percent."""
        unit = reading.unit_number.strip()
        fuel = round(float(reading.fuel_percent), 1)

        state = self.database.upsert_fuel_state(
            unit_number=unit,
            current_fuel=fuel,
            samsara_vehicle_id=reading.vehicle_id,
            samsara_vehicle_name=reading.vehicle_name,
        )
        note = self.database.get_active_note(unit)

        if note:
            completion = self._maybe_auto_clear_note(unit, fuel, note)
            return [completion] if completion else []

        if fuel <= self.fuel_threshold and self._should_alert(state, fuel):
            band = self.alert_band(fuel)
            message = self.low_fuel_message(unit, fuel)
            self.database.update_alert_state(unit, band)
            self.database.log_event(unit, "low_fuel_alert", fuel, message)
            return [FuelAction(unit, "low_fuel_alert", fuel, message)]

        return []

    def _maybe_auto_clear_note(self, unit: str, current_fuel: float, note: object) -> FuelAction | None:
        fuel_at_note = float(note["fuel_at_note_creation"])
        increase = round(current_fuel - fuel_at_note, 1)
        should_clear = (
            increase >= self.auto_clear_increase
            or current_fuel >= self.auto_clear_full_level
        )
        if not should_clear:
            return None

        self.database.clear_note(unit)
        message = (
            "✅ Fuel Event Completed\n"
            f"Unit: {unit}\n"
            f"Previous Fuel: {format_percent(fuel_at_note)}\n"
            f"Current Fuel: {format_percent(current_fuel)}\n"
            f"Increase: +{format_percent(increase)}\n"
            "Note automatically cleared."
        )
        self.database.log_event(unit, "note_auto_cleared", current_fuel, message)
        return FuelAction(unit, "note_auto_cleared", current_fuel, message)

    def _should_alert(self, state: object, current_fuel: float) -> bool:
        """Avoid repeated alerts until the repeat window has passed."""
        last_band = state["last_alert_band"]
        last_alert_at = parse_iso_datetime(state["last_alert_at"])

        if last_alert_at is None or last_band is None:
            return True

        return datetime.now(timezone.utc) - last_alert_at >= self.repeat_alert_after

    def alert_band(self, fuel_percent: float) -> int:
        """Return lower alert band: 60, 50, 40, 30, 20, 10, or 0."""
        if fuel_percent <= 0:
            return 0
        if fuel_percent <= 10:
            return 10
        if fuel_percent <= 20:
            return 20
        if fuel_percent <= 30:
            return 30
        if fuel_percent <= 40:
            return 40
        if fuel_percent <= 50:
            return 50
        return 60

    def low_fuel_message(self, unit: str, fuel: float) -> str:
        color = fuel_color(fuel)
        return (
            f"{color} Low Fuel Alert\n"
            f"Unit: {unit}\n"
            f"Fuel: {format_percent(fuel)}\n"
            "No dispatcher note is active."
        )


def fuel_color(fuel_percent: float) -> str:
    """Map fuel percentage to the requested Telegram color emoji."""
    if fuel_percent <= 20:
        return "🔴"
    if fuel_percent <= 40:
        return "🟠"
    return "🟡"


def format_percent(value: float) -> str:
    """Show whole numbers without .0 but keep one decimal when useful."""
    if float(value).is_integer():
        return f"{int(value)}%"
    return f"{value:.1f}%"


def parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
