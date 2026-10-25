from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Mapping
from typing import Any

import requests

from .writable import ActionDefinition, WritableSource


LOGGER = logging.getLogger(__name__)

STATE_PATTERNS: dict[str, dict[str, Any]] = {
    "on": {"on": True},
    "off": {"on": False},
    "brightness": {"on": True, "bri": "$bri"},
    "color": {"seg": [{"id": "$segment", "col": [["$red", "$green", "$blue"]]}]},
    "effect": {"seg": [{"id": "$segment", "fx": "$effect_id"}]},
    "palette": {"seg": [{"id": "$segment", "pal": "$palette_id"}]},
    "transition": {"transition": "$duration"},
    "preset": {"ps": "$preset_id"},
}


def _render_state(value: Any, params: Mapping[str, Any]) -> Any:
    """Expand a source-owned state recipe using validated action parameters."""
    if isinstance(value, dict):
        return {key: _render_state(item, params) for key, item in value.items()}
    if isinstance(value, list):
        return [_render_state(item, params) for item in value]
    if isinstance(value, str) and value.startswith("$"):
        return params[value[1:]]
    return value


def _match_state(pattern: Any, value: Any, params: dict[str, Any]) -> bool:
    """Recognize a complete legacy state recipe without discarding extra fields."""
    if isinstance(pattern, str) and pattern.startswith("$"):
        params[pattern[1:]] = value
        return True
    if isinstance(pattern, dict):
        return isinstance(value, dict) and pattern.keys() == value.keys() and all(
            _match_state(item, value[key], params) for key, item in pattern.items()
        )
    if isinstance(pattern, list):
        return isinstance(value, list) and len(pattern) == len(value) and all(
            _match_state(item, actual, params) for item, actual in zip(pattern, value)
        )
    return type(pattern) is type(value) and pattern == value


class WledSource(WritableSource, source_name="wled"):
    """Poll and control a WLED controller through its local JSON API."""

    source_name = "wled"
    config_section = "wled"
    supports_status_requests = True

    @classmethod
    def supported_actions(cls) -> dict[str, ActionDefinition]:
        """Describe controller and segment actions supported by the local JSON API."""
        byte = {"type": "integer", "min": 0, "max": 255}
        return {
            "on": ActionDefinition("Turn controller output on.", {}),
            "off": ActionDefinition("Turn controller output off.", {}),
            "brightness": ActionDefinition("Turn output on and set brightness (0-255).", {"bri": dict(byte)}),
            "color": ActionDefinition("Set primary segment RGB without changing output power.", {
                key: dict(byte) for key in ("segment", "red", "green", "blue")
            }),
            "effect": ActionDefinition("Select a segment effect ID.", {
                "segment": dict(byte), "effect_id": dict(byte),
            }),
            "palette": ActionDefinition("Select a segment palette ID.", {
                "segment": dict(byte), "palette_id": dict(byte),
            }),
            "transition": ActionDefinition("Set default transition in 100 ms units.", {
                "duration": {"type": "integer", "min": 0, "max": 65535},
            }),
            "preset": ActionDefinition("Apply an existing saved preset.", {
                "preset_id": {"type": "integer", "min": 1, "max": 250},
            }),
            "state": ActionDefinition("Advanced raw JSON state update; no configuration endpoints.", {
                "state": {"type": "object"},
            }),
        }

    @classmethod
    def translate_frame(cls, frame: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        """Recognize existing coach JSON recipes, retaining arbitrary state updates."""
        payload = dict(frame)
        for name, pattern in STATE_PATTERNS.items():
            params: dict[str, Any] = {}
            if _match_state(pattern, payload, params):
                return name, params
        return "state", {"state": payload}

    @classmethod
    def _encode_action(
        cls, action: str, params: dict[str, Any], section: Mapping[str, str],
        *, reserved: Mapping[int, set[int]] | None = None,
    ) -> dict[str, Any]:
        """Build a local state update without accepting endpoint or host overrides."""
        payload = params["state"] if action == "state" else _render_state(STATE_PATTERNS[action], params)
        if not payload:
            raise ValueError("WLED state payload must be a non-empty object")
        return payload

    @classmethod
    def dispatch_budget(cls, section: Mapping[str, str]) -> float:
        """Budget lock acquisition, connection and response waits using the HTTP timeout."""
        timeout = float(section.get("timeout", "5"))
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("WLED action timeout must be positive and finite")
        return 3 * timeout

    def __init__(self, config: Any, publisher: Any, stop_event: Any, section_name: str | None = None) -> None:
        """Initialize a WLED source without contacting the controller."""
        super().__init__(config, publisher, stop_event, section_name)
        self.session = requests.Session()
        self._request_lock = threading.Lock()

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
        data = self._request_json(method, path, payload)
        if not isinstance(data, dict):
            raise ValueError(f"WLED returned a non-object response from {path}")
        return data

    def _request_json(self, method: str, path: str, payload: dict[str, Any] | None = None) -> object:
        """Perform a serialized, bounded request without assuming the JSON shape."""
        timeout = self.section.getfloat("timeout", fallback=5.0)
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("WLED timeout must be positive and finite")
        if not self._request_lock.acquire(timeout=timeout):
            raise TimeoutError("WLED source is busy with another request")
        try:
            response = self.session.request(
                method,
                f"{self._base_url()}{path}",
                json=payload,
                timeout=timeout,
            )
        finally:
            self._request_lock.release()
        response.raise_for_status()
        return response.json()

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

    def effects(self) -> list[str]:
        """Return effect names in controller ID order without enabling writes."""
        return self._catalog("/json/effects")

    def palettes(self) -> list[str]:
        """Return palette names in controller ID order without enabling writes."""
        return self._catalog("/json/palettes")

    def _catalog(self, path: str) -> list[str]:
        """Read a catalog and reject responses that are not arrays of names."""
        data = self._request_json("GET", path)
        if not isinstance(data, list) or not all(isinstance(name, str) for name in data):
            raise ValueError(f"WLED returned a non-string-array response from {path}")
        return data

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

    def command_ready(self) -> bool:
        """Report whether the daemon owns an active WLED source."""
        return self.is_alive()

    def execute_command(self, payload: dict[str, Any]) -> None:
        """Dispatch state without the legacy handler's log-and-return failure behavior."""
        self.set_state(payload)

    def request_status(self, status_ids: list[int]) -> None:
        """Read actual controller state and publish it without any device writes."""
        topic = self.published_topic_root(self.config, self.section_name)
        self.publisher.publish(topic, self.interrogate())

    def run(self) -> None:
        """Poll WLED state and publish snapshots until the source is stopped."""
        try:
            interval = self.section.getfloat("poll_interval", fallback=60.0)
            if not math.isfinite(interval) or interval <= 0:
                raise ValueError("poll_interval must be positive and finite")
            while not self.stop_event.is_set():
                try:
                    self.request_status([])
                except (OSError, requests.RequestException, ValueError) as error:
                    LOGGER.info("WLED poll failed for %s: %s", self.section_name, error)
                self.stop_event.wait(interval)
        except Exception:
            LOGGER.exception("WLED source instance %s stopped", self.section_name)
        finally:
            self.session.close()
