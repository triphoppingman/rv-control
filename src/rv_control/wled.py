from __future__ import annotations

import logging
import time
from typing import Any

import requests

from .source import Source


LOGGER = logging.getLogger(__name__)


class WledSource(Source, source_name="wled"):
    """Poll and control a WLED controller through its local JSON API."""

    source_name = "wled"
    config_section = "wled"

    def __init__(self, config: Any, publisher: Any, stop_event: Any, section_name: str | None = None) -> None:
        """Initialize a WLED source without contacting the controller."""
        super().__init__(config, publisher, stop_event, section_name)
        self.session = requests.Session()

    def _base_url(self) -> str:
        """Return the configured WLED HTTP base URL without a trailing slash."""
        base_url = self.section.get("base_url", "").strip().rstrip("/")
        if not base_url:
            raise ValueError(f"[{self.section_name}] base_url is required")
        if not base_url.startswith(("http://", "https://")):
            raise ValueError(f"[{self.section_name}] base_url must start with http:// or https://")
        return base_url

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Perform one bounded WLED JSON request and return its object response."""
        response = self.session.request(
            method,
            f"{self._base_url()}{path}",
            json=payload,
            timeout=self.section.getfloat("timeout", fallback=5.0),
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError(f"WLED returned a non-object response from {path}")
        return data

    def comms_check(self) -> dict[str, Any]:
        """Check WLED reachability and report controller identity information."""
        try:
            info = self._request("GET", "/json/info")
            name = info.get("name") or info.get("product") or "WLED"
            version = info.get("ver", "unknown")
            return {"ok": True, "message": f"{name} reachable ({version})"}
        except (OSError, requests.RequestException, ValueError) as error:
            return {"ok": False, "message": f"WLED check failed: {error}"}

    def interrogate(self) -> dict[str, Any]:
        """Read and return the current WLED state once."""
        return self._request("GET", "/json/state")

    def info(self) -> dict[str, Any]:
        """Read and return WLED controller identity and firmware information."""
        return self._request("GET", "/json/info")

    def status(self) -> dict[str, Any]:
        """Read and return the current WLED light state."""
        return self.interrogate()

    def set_state(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Write one WLED state update when source-level writes are enabled."""
        if not self.section.getboolean("write_enabled", fallback=False):
            raise PermissionError(f"[{self.section_name}] write_enabled is false")
        if not isinstance(payload, dict) or not payload:
            raise ValueError("WLED state payload must be a non-empty object")
        return self._request("POST", "/json/state", payload)

    def handle_command(self, payload: dict[str, Any]) -> None:
        """Validate and send a WLED state update when writes are enabled."""
        try:
            self.set_state(payload)
        except (OSError, PermissionError, requests.RequestException, ValueError) as error:
            LOGGER.warning("WLED command failed for %s: %s", self.section_name, error)

    def run(self) -> None:
        """Poll WLED state and publish snapshots until the source is stopped."""
        try:
            interval = self.section.getfloat("poll_interval", fallback=60.0)
            if interval <= 0:
                raise ValueError("poll_interval must be greater than zero")
            topic = self.section.get("topic", self.section_name).strip("/")
            while not self.stop_event.is_set():
                state = self.interrogate()
                self.publisher.publish(topic, state)
                self.stop_event.wait(interval)
        except Exception:
            LOGGER.exception("WLED source instance %s stopped", self.section_name)
        finally:
            self.session.close()
