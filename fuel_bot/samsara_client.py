"""Async Samsara API client for vehicle fuel percentage feed data."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from .database import Database


LOGGER = logging.getLogger(__name__)
SAMSARA_STATS_FEED_URL = "https://api.samsara.com/fleet/vehicles/stats/feed"
CURSOR_CONFIG_KEY = "samsara_fuel_feed_end_cursor"


class SamsaraClientError(RuntimeError):
    """Raised when Samsara data cannot be fetched after retries."""


class SamsaraClient:
    """Client for Samsara vehicle stats feed.

    Samsara's fuel percent stat type is `fuelPercents`. The feed returns an
    `endCursor`, which we persist and send back as the `after` parameter on
    later requests when possible.
    """

    def __init__(self, api_tokens: list[str], database: Database, timeout_seconds: float = 20.0) -> None:
        self.api_tokens = api_tokens
        self.database = database
        self.timeout_seconds = timeout_seconds
        self.last_fetch_counts: dict[int, int] = {}
        self.last_fetch_merged_count = 0

    async def fetch_fuel_levels(self) -> list[dict[str, Any]]:
        """Fetch fuel levels from Samsara and return normalized readings."""
        if not self.api_tokens:
            raise SamsaraClientError("No Samsara API token is configured.")

        all_readings: list[dict[str, Any]] = []
        errors: list[str] = []
        success_count = 0
        self.last_fetch_counts = {}
        self.last_fetch_merged_count = 0

        for token_index, api_token in enumerate(self.api_tokens, start=1):
            try:
                token_readings = await self._fetch_with_token(token_index, api_token)
            except SamsaraClientError as exc:
                errors.append(str(exc))
                LOGGER.warning("Samsara token %s did not return fuel data: %s", token_index, exc)
                continue

            success_count += 1
            self.last_fetch_counts[token_index] = len(token_readings)
            LOGGER.info("Samsara token %s returned %s fuel reading(s).", token_index, len(token_readings))
            all_readings.extend(token_readings)

        merged = self._merge_readings(all_readings)
        self.last_fetch_merged_count = len(merged)
        if success_count > 0:
            LOGGER.info(
                "Merged Samsara fuel readings from %s successful token(s): %s unit(s).",
                success_count,
                len(merged),
            )
            return merged

        error_text = "; ".join(errors) if errors else "No fuel readings returned."
        raise SamsaraClientError(f"Unable to fetch Samsara fuel levels: {error_text}")

    async def _fetch_with_token(self, token_index: int, api_token: str) -> list[dict[str, Any]]:
        """Fetch fuel readings for one Samsara token."""
        headers = {
            "Authorization": f"Bearer {api_token}",
            "Accept": "application/json",
        }
        params: dict[str, str] = {"types": "fuelPercents"}

        cursor = self.database.get_config(self._cursor_key(token_index))
        if cursor:
            params["after"] = cursor

        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                    response = await client.get(SAMSARA_STATS_FEED_URL, headers=headers, params=params)
                    response.raise_for_status()
                    payload = response.json()
                    self._persist_cursor(token_index, payload)
                    return self._parse_fuel_payload(payload, token_index)
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
                LOGGER.warning(
                    "Samsara request failed with token %s on attempt %s: %s",
                    token_index,
                    attempt,
                    exc,
                )
                if self._is_unauthorized(exc):
                    break
                await asyncio.sleep(2**attempt)

        raise SamsaraClientError(f"token {token_index}: {last_error}")

    def _is_unauthorized(self, exc: Exception) -> bool:
        """Avoid retrying a token when Samsara says it is unauthorized."""
        if not isinstance(exc, httpx.HTTPStatusError):
            return False
        return exc.response.status_code in {401, 403}

    def _cursor_key(self, token_index: int) -> str:
        return f"{CURSOR_CONFIG_KEY}_{token_index}"

    def _persist_cursor(self, token_index: int, payload: dict[str, Any]) -> None:
        pagination = payload.get("pagination") or {}
        end_cursor = pagination.get("endCursor")
        if end_cursor:
            self.database.set_config(self._cursor_key(token_index), str(end_cursor))

    def _parse_fuel_payload(self, payload: dict[str, Any], token_index: int) -> list[dict[str, Any]]:
        readings: list[dict[str, Any]] = []
        for vehicle in payload.get("data", []):
            fuel_percent = self._latest_fuel_percent(vehicle)
            if fuel_percent is None:
                continue

            vehicle_id = str(vehicle.get("id") or vehicle.get("vehicleId") or "")
            vehicle_name = str(vehicle.get("name") or vehicle.get("vehicleName") or vehicle_id)
            unit_number = self._unit_number_from_vehicle(vehicle)

            readings.append(
                {
                    "unit_number": unit_number,
                    "fuel_percent": fuel_percent,
                    "vehicle_id": vehicle_id or None,
                    "vehicle_name": vehicle_name or None,
                    "source_token_index": token_index,
                }
            )
        return readings

    def _merge_readings(self, readings: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Deduplicate readings if the same unit appears under multiple tokens."""
        merged: dict[str, dict[str, Any]] = {}
        for reading in readings:
            key = str(reading.get("vehicle_id") or reading["unit_number"]).strip().lower()
            merged[key] = reading
        return list(merged.values())

    def _latest_fuel_percent(self, vehicle: dict[str, Any]) -> float | None:
        """Extract the newest fuel percent from Samsara's nested response."""
        values = vehicle.get("fuelPercents")
        if isinstance(values, list) and values:
            latest = values[-1]
            value = latest.get("value") if isinstance(latest, dict) else latest
            return self._to_float(value)

        if isinstance(values, dict):
            return self._to_float(values.get("value"))

        return self._to_float(vehicle.get("fuelPercent"))

    def _unit_number_from_vehicle(self, vehicle: dict[str, Any]) -> str:
        """Choose the most dispatcher-friendly identifier available."""
        name = vehicle.get("name") or vehicle.get("vehicleName")
        external_ids = vehicle.get("externalIds") or {}
        if name:
            return str(name).strip()
        if isinstance(external_ids, dict):
            for key in ("samsara.serial", "licensePlate", "vin"):
                if external_ids.get(key):
                    return str(external_ids[key]).strip()
        return str(vehicle.get("id") or vehicle.get("vehicleId") or "unknown").strip()

    def _to_float(self, value: Any) -> float | None:
        if value is None:
            return None
        try:
            return round(float(value), 1)
        except (TypeError, ValueError):
            return None
