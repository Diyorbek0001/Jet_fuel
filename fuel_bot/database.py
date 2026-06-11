"""Small SQLite data layer for notes, fuel state, config, and events."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now_iso() -> str:
    """Return a compact UTC timestamp that SQLite can store as text."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Database:
    """Thin SQLite wrapper with explicit methods for bot workflows."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def connect(self) -> sqlite3.Connection:
        """Open a SQLite connection with rows addressable by column name."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def initialize(self) -> None:
        """Create all tables and helpful indexes if they do not exist."""
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS unit_notes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    unit_number TEXT NOT NULL,
                    note TEXT NOT NULL,
                    fuel_at_note_creation REAL NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1
                );

                CREATE INDEX IF NOT EXISTS idx_unit_notes_active_unit
                    ON unit_notes (unit_number, active);

                CREATE TABLE IF NOT EXISTS fuel_states (
                    unit_number TEXT PRIMARY KEY,
                    current_fuel REAL NOT NULL,
                    previous_fuel REAL,
                    last_seen_at TEXT NOT NULL,
                    last_alert_at TEXT,
                    last_alert_band INTEGER,
                    samsara_vehicle_id TEXT,
                    samsara_vehicle_name TEXT
                );

                CREATE TABLE IF NOT EXISTS app_config (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS alert_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    unit_number TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    fuel_percent REAL NOT NULL,
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS inactive_units (
                    unit_number TEXT PRIMARY KEY,
                    deactivated_by TEXT NOT NULL,
                    deactivated_at TEXT NOT NULL
                );
                """
            )

    def get_config(self, key: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM app_config WHERE key = ?", (key,)).fetchone()
            return str(row["value"]) if row else None

    def set_config(self, key: str, value: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO app_config (key, value)
                VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

    def get_active_note(self, unit_number: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT * FROM unit_notes
                WHERE unit_number = ? AND active = 1
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (unit_number,),
            ).fetchone()

    def get_active_notes(self) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(
                conn.execute(
                    """
                    SELECT n.* FROM unit_notes n
                    LEFT JOIN inactive_units i ON i.unit_number = n.unit_number
                    WHERE n.active = 1 AND i.unit_number IS NULL
                    ORDER BY n.unit_number COLLATE NOCASE
                    """
                )
            )

    def upsert_note(self, unit_number: str, note: str, fuel_at_creation: float, created_by: str) -> None:
        """Create a note, or replace the active note for the same unit."""
        now = utc_now_iso()
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT id, created_at FROM unit_notes WHERE unit_number = ? AND active = 1 LIMIT 1",
                (unit_number,),
            ).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE unit_notes
                    SET note = ?, fuel_at_note_creation = ?, created_by = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (note, fuel_at_creation, created_by, now, existing["id"]),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO unit_notes
                        (unit_number, note, fuel_at_note_creation, created_by, created_at, updated_at, active)
                    VALUES (?, ?, ?, ?, ?, ?, 1)
                    """,
                    (unit_number, note, fuel_at_creation, created_by, now, now),
                )

    def clear_note(self, unit_number: str) -> bool:
        with self.connect() as conn:
            cur = conn.execute(
                """
                UPDATE unit_notes
                SET active = 0, updated_at = ?
                WHERE unit_number = ? AND active = 1
                """,
                (utc_now_iso(), unit_number),
            )
            return cur.rowcount > 0

    def deactivate_unit(self, unit_number: str, deactivated_by: str) -> bool:
        """Add a unit to the inactive list. Returns True when it changed."""
        now = utc_now_iso()
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO inactive_units
                    (unit_number, deactivated_by, deactivated_at)
                VALUES (?, ?, ?)
                """,
                (unit_number, deactivated_by, now),
            )
            return cur.rowcount > 0

    def activate_unit(self, unit_number: str) -> bool:
        """Remove a unit from the inactive list. Returns True when it changed."""
        with self.connect() as conn:
            cur = conn.execute(
                "DELETE FROM inactive_units WHERE unit_number = ?",
                (unit_number,),
            )
            return cur.rowcount > 0

    def is_unit_inactive(self, unit_number: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM inactive_units WHERE unit_number = ? LIMIT 1",
                (unit_number,),
            ).fetchone()
            return row is not None

    def get_inactive_unit_numbers(self) -> set[str]:
        with self.connect() as conn:
            return {
                str(row["unit_number"])
                for row in conn.execute("SELECT unit_number FROM inactive_units")
            }

    def get_inactive_units(self) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(
                conn.execute(
                    """
                    SELECT * FROM inactive_units
                    ORDER BY unit_number COLLATE NOCASE
                    """
                )
            )

    def upsert_fuel_state(
        self,
        unit_number: str,
        current_fuel: float,
        samsara_vehicle_id: str | None = None,
        samsara_vehicle_name: str | None = None,
    ) -> sqlite3.Row:
        """Store current fuel while preserving previous fuel and alert metadata."""
        now = utc_now_iso()
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT * FROM fuel_states WHERE unit_number = ?",
                (unit_number,),
            ).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE fuel_states
                    SET previous_fuel = current_fuel,
                        current_fuel = ?,
                        last_seen_at = ?,
                        samsara_vehicle_id = COALESCE(?, samsara_vehicle_id),
                        samsara_vehicle_name = COALESCE(?, samsara_vehicle_name)
                    WHERE unit_number = ?
                    """,
                    (current_fuel, now, samsara_vehicle_id, samsara_vehicle_name, unit_number),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO fuel_states
                        (unit_number, current_fuel, previous_fuel, last_seen_at,
                         samsara_vehicle_id, samsara_vehicle_name)
                    VALUES (?, ?, NULL, ?, ?, ?)
                    """,
                    (unit_number, current_fuel, now, samsara_vehicle_id, samsara_vehicle_name),
                )

            return conn.execute(
                "SELECT * FROM fuel_states WHERE unit_number = ?",
                (unit_number,),
            ).fetchone()

    def update_alert_state(self, unit_number: str, band: int) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE fuel_states
                SET last_alert_at = ?, last_alert_band = ?
                WHERE unit_number = ?
                """,
                (utc_now_iso(), band, unit_number),
            )

    def get_fuel_state(self, unit_number: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM fuel_states WHERE unit_number = ?",
                (unit_number,),
            ).fetchone()

    def list_low_fuel_states(self, threshold: int, only_without_notes: bool = False) -> list[sqlite3.Row]:
        """List trucks at or below the threshold, sorted highest fuel first."""
        note_filter = "AND n.id IS NULL" if only_without_notes else ""
        with self.connect() as conn:
            return list(
                conn.execute(
                    f"""
                    SELECT fs.*, n.note, n.fuel_at_note_creation, n.created_by, n.created_at
                    FROM fuel_states fs
                    LEFT JOIN unit_notes n
                        ON n.unit_number = fs.unit_number AND n.active = 1
                    LEFT JOIN inactive_units i
                        ON i.unit_number = fs.unit_number
                    WHERE fs.current_fuel <= ?
                    AND i.unit_number IS NULL
                    {note_filter}
                    ORDER BY fs.current_fuel DESC, fs.unit_number COLLATE NOCASE
                    """,
                    (threshold,),
                )
            )

    def log_event(self, unit_number: str, event_type: str, fuel_percent: float, message: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO alert_events (unit_number, event_type, fuel_percent, message, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (unit_number, event_type, fuel_percent, message, utc_now_iso()),
            )

    def bulk_upsert_fuel_states(self, readings: Iterable[dict[str, Any]]) -> None:
        for reading in readings:
            self.upsert_fuel_state(
                unit_number=str(reading["unit_number"]),
                current_fuel=float(reading["fuel_percent"]),
                samsara_vehicle_id=reading.get("vehicle_id"),
                samsara_vehicle_name=reading.get("vehicle_name"),
            )
