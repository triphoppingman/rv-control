from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from rv_control.config import load_config
from rv_control.wled import WledSource


class FakeResponse:
    """Provide the response behavior needed by WLED source tests."""

    def __init__(self, data: dict[str, Any]) -> None:
        """Store a JSON-compatible response mapping."""
        self.data = data

    def raise_for_status(self) -> None:
        """Treat the synthetic response as successful."""

    def json(self) -> dict[str, Any]:
        """Return the synthetic WLED JSON object."""
        return self.data


class FakeSession:
    """Capture WLED HTTP requests without network access."""

    def __init__(self) -> None:
        """Initialize captured requests and canned responses."""
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def request(self, method: str, url: str, json: dict[str, Any] | None, timeout: float) -> FakeResponse:
        """Record a request and return a matching synthetic response."""
        self.calls.append((method, url, json))
        if url.endswith("/json/info"):
            return FakeResponse({"name": "Patio WLED", "ver": "0.14.4"})
        return FakeResponse({"on": True, "bri": 128})

    def close(self) -> None:
        """Accept source cleanup."""


def test_wled_comms_and_interrogate(config_file: Path) -> None:
    """Verify WLED checks and interrogation use the configured JSON endpoints."""
    config = load_config(str(config_file))
    source = WledSource(config, None, threading.Event(), "wled")
    session = FakeSession()
    source.session = session
    assert source.comms_check() == {"ok": True, "message": "Patio WLED reachable (0.14.4)"}
    assert source.interrogate() == {"on": True, "bri": 128}
    assert [call[1] for call in session.calls] == ["http://wled.local/json/info", "http://wled.local/json/state"]


def test_wled_command_requires_write_enabled(config_file: Path) -> None:
    """Verify WLED writes are blocked until source-level writes are enabled."""
    config = load_config(str(config_file))
    source = WledSource(config, None, threading.Event(), "wled")
    session = FakeSession()
    source.session = session
    source.handle_command({"on": True})
    assert session.calls == []
    config["wled"]["write_enabled"] = "true"
    source.handle_command({"on": True})
    assert session.calls == [("POST", "http://wled.local/json/state", {"on": True})]
