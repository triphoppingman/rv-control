"""Read-only Open-Meteo forecasts using literal or runtime-bound coordinates."""

from __future__ import annotations

import math
import threading
from configparser import ConfigParser
from datetime import datetime, timezone
from typing import Any

import requests

from .base import PollingSource

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
DEFAULT_CURRENT = "temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m"


class OpenMeteoSource(PollingSource, source_name="open_meteo"):
    """Poll Open-Meteo without knowing which source supplies the coordinates."""

    config_section = "open_meteo"
    runtime_options = ("latitude", "longitude")
    polling_errors = (requests.RequestException, ValueError)
    check_name = "Open-Meteo"
    check_success = "Open-Meteo forecast API is available"

    def __init__(
        self, config: ConfigParser, publisher: Any, stop_event: threading.Event,
        section_name: str | None = None,
    ) -> None:
        """Compile static HTTP settings without opening a network connection."""
        super().__init__(config, publisher, stop_event, section_name)
        self.timeout = self.positive_setting("timeout", 10)
        self.topic = self.section.get("topic", self.section_name).strip().strip("/")
        if not self.topic or any(char in self.topic for char in ("$", "+", "#")):
            raise ValueError(f"[{self.section_name}] topic must be a literal telemetry topic")
        self.validate_literal_settings(self._coordinate)
        self.current = ",".join(
            field.strip() for field in self.section.get("current", DEFAULT_CURRENT).split(",")
            if field.strip()
        )
        if not self.current:
            raise ValueError(f"[{self.section_name}] current must list weather variables")
        daily = self.section.get("daily", "").strip()
        self.daily = ",".join(field.strip() for field in daily.split(",") if field.strip())
        if daily and not self.daily:
            raise ValueError(f"[{self.section_name}] daily must list weather variables or be blank")

    @classmethod
    def validate_config(cls, config: ConfigParser, section_name: str) -> None:
        """Check static options and literal coordinates without HTTP or telemetry."""
        cls(config, None, threading.Event(), section_name)

    @staticmethod
    def _coordinate(value: Any, key: str) -> float:
        """Reject boolean, non-finite, and out-of-range coordinates before HTTP."""
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise ValueError(f"Open-Meteo {key} must be a number")
        try:
            number = float(value)
        except (ValueError, OverflowError) as error:
            raise ValueError(f"Open-Meteo {key} must be a finite number") from error
        limit = 90 if key == "latitude" else 180
        if not math.isfinite(number) or not -limit <= number <= limit:
            raise ValueError(f"Open-Meteo {key} must be between {-limit} and {limit}")
        return number

    def collect(self, settings: dict[str, Any]) -> dict[str, Any]:
        """Fetch one forecast after validating the supplied resolved coordinates."""
        coordinates = {key: self._coordinate(settings[key], key) for key in ("latitude", "longitude")}
        params = {**coordinates, "current": self.current}
        if self.daily:
            params.update(daily=self.daily, timezone="auto", forecast_days=7)
        with requests.Session() as session:
            response = session.get(
                FORECAST_URL, params=params,
                timeout=self.timeout,
            )
            response.raise_for_status()
            data = response.json()
        if not isinstance(data, dict):
            raise ValueError("Open-Meteo returned a non-object response")
        if data.get("error"):
            raise ValueError(f"Open-Meteo API error: {data.get('reason', 'unspecified error')}")
        current = data.get("current")
        if not isinstance(current, dict) or not all(
            key in current for key in ("time", *self.current.split(","))
        ):
            raise ValueError("Open-Meteo response is missing requested current weather fields")
        if self.daily:
            daily = data.get("daily")
            if not isinstance(daily, dict) or not all(
                isinstance(daily.get(key), list) and len(daily[key]) == 7
                for key in ("time", *self.daily.split(","))
            ):
                raise ValueError("Open-Meteo response is missing requested seven-day forecast fields")
        return {
            **data, "requested_location": coordinates,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
