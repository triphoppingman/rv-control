"""Read-only Open-Meteo forecasts using literal or runtime-bound coordinates."""

from __future__ import annotations

import logging
import math
import threading
from configparser import ConfigParser
from datetime import datetime, timezone
from typing import Any

import requests

from ..runtime import Reference, VariablesUnavailable, parse_template
from .base import Source

LOGGER = logging.getLogger(__name__)
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
DEFAULT_CURRENT = "temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m"


class OpenMeteoSource(Source, source_name="open_meteo"):
    """Poll Open-Meteo without knowing which source supplies the coordinates."""

    config_section = "open_meteo"
    runtime_options = ("latitude", "longitude")

    def __init__(
        self, config: ConfigParser, publisher: Any, stop_event: threading.Event,
        section_name: str | None = None,
    ) -> None:
        """Compile static HTTP settings without opening a network connection."""
        super().__init__(config, publisher, stop_event, section_name)
        self.timeout = self._positive("timeout", 10)
        self.poll_interval = self._positive("poll_interval", 900)
        self.retry_delay = self._positive("retry_delay", 30)
        self.max_retry_delay = self._positive("max_retry_delay", 900)
        if self.max_retry_delay < self.retry_delay:
            raise ValueError(f"[{self.section_name}] max_retry_delay must be >= retry_delay")
        self.topic = self.section.get("topic", self.section_name).strip().strip("/")
        if not self.topic or any(char in self.topic for char in ("$", "+", "#")):
            raise ValueError(f"[{self.section_name}] topic must be a literal telemetry topic")
        self.coordinates = {key: self.section.get(key, "").strip() for key in self.runtime_options}
        if not all(self.coordinates.values()):
            raise ValueError(f"[{self.section_name}] latitude and longitude are required")
        for key, value in self.coordinates.items():
            parts = parse_template(value)
            if not any(isinstance(part, Reference) for part in parts):
                self._coordinate("".join(part for part in parts if isinstance(part, str)), key)
        self.current = ",".join(
            field.strip() for field in self.section.get("current", DEFAULT_CURRENT).split(",")
            if field.strip()
        )
        if not self.current:
            raise ValueError(f"[{self.section_name}] current must list weather variables")

    @classmethod
    def validate_config(cls, config: ConfigParser, section_name: str) -> None:
        """Check static options and literal coordinates without HTTP or telemetry."""
        cls(config, None, threading.Event(), section_name)

    def _positive(self, key: str, default: float) -> float:
        """Read one positive finite duration and identify invalid configuration."""
        value = self.section.getfloat(key, fallback=default)
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"[{self.section_name}] {key} must be positive and finite")
        return value

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

    def interrogate(self) -> dict[str, Any]:
        """Fetch one forecast or fail immediately if required variables are unavailable."""
        resolved = self.resolve_settings(self.coordinates)
        coordinates = {key: self._coordinate(resolved[key], key) for key in self.runtime_options}
        with requests.Session() as session:
            response = session.get(
                FORECAST_URL, params={**coordinates, "current": self.current},
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
        return {
            **data, "requested_location": coordinates,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }

    def comms_check(self) -> dict[str, Any]:
        """Perform one bounded API read, never waiting for another source to start."""
        try:
            self.interrogate()
            return {"ok": True, "message": "Open-Meteo forecast API is available"}
        except (requests.RequestException, ValueError) as error:
            waiting = False
            return {"ok": False, "message": f"Open-Meteo check failed: {error}"}

    def run(self) -> None:
        """Poll on schedule, waiting for inputs and backing off boundedly on failures."""
        retry = self.retry_delay
        waiting = False
        while not self.stop_event.is_set():
            try:
                payload = self.interrogate()
            except VariablesUnavailable as error:
                if not waiting:
                    LOGGER.warning("%s waiting for telemetry: %s", self.section_name, error)
                waiting = True
                delay = min(5.0, self.poll_interval)
            except (requests.RequestException, ValueError) as error:
                LOGGER.warning("%s forecast failed; retry in %.1fs: %s", self.section_name, retry, error)
                delay = retry
                retry = min(self.max_retry_delay, retry * 2)
            else:
                self.publisher.publish(self.topic, payload)
                retry = self.retry_delay
                waiting = False
                delay = self.poll_interval
            self.stop_event.wait(delay)
