from __future__ import annotations

import threading
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest
import requests
from click.testing import CliRunner

from rv_control.config import load_config
from rv_control.sources.wled import WledSource


class FakeResponse:
    """Provide the response behavior needed by WLED source tests."""

    def __init__(self, data: object) -> None:
        """Store a JSON-compatible response value."""
        self.data = data

    def raise_for_status(self) -> None:
        """Treat the synthetic response as successful."""

    def json(self) -> object:
        """Return the synthetic WLED JSON value."""
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


@pytest.mark.parametrize("catalog", ["effects", "palettes"])
@pytest.mark.parametrize("names", [[], ["Solid", "Blink", "Solid"]])
def test_wled_catalog_preserves_ids(
    config_file: Path, monkeypatch: pytest.MonkeyPatch, catalog: str, names: list[str],
) -> None:
    """Read catalogs with writes disabled, preserving order and duplicate names."""
    source = WledSource(load_config(str(config_file)), None, threading.Event(), "wled")
    calls: list[tuple[str, str, object, float]] = []

    def request(method: str, url: str, json: object, timeout: float) -> FakeResponse:
        """Capture the exact read-only endpoint and configured timeout."""
        calls.append((method, url, json, timeout))
        return FakeResponse(names)

    monkeypatch.setattr(source.session, "request", request)
    try:
        assert getattr(source, catalog)() == names
        assert calls == [("GET", f"http://wled.local/json/{catalog}", None, 1.0)]
    finally:
        source.session.close()


@pytest.mark.parametrize("catalog", ["effects", "palettes"])
@pytest.mark.parametrize("data", [{"error": "unsupported"}, "Solid", None, ["Solid", 1]])
def test_wled_catalog_rejects_wrong_shape(
    config_file: Path, monkeypatch: pytest.MonkeyPatch, catalog: str, data: object,
) -> None:
    """Malformed catalog responses must raise rather than resemble empty success."""
    source = WledSource(load_config(str(config_file)), None, threading.Event(), "wled")
    monkeypatch.setattr(source.session, "request", lambda *_args, **_kwargs: FakeResponse(data))
    try:
        with pytest.raises(ValueError, match="non-string-array"):
            getattr(source, catalog)()
    finally:
        source.session.close()


@pytest.mark.parametrize("operation", ["effects", "palettes", "status"])
def test_wled_http_failure_releases_lock(
    config_file: Path, monkeypatch: pytest.MonkeyPatch, operation: str,
) -> None:
    """Discovery shares existing error propagation and never leaves the lock held."""
    source = WledSource(load_config(str(config_file)), None, threading.Event(), "wled")

    def fail(*_args: Any, **_kwargs: Any) -> FakeResponse:
        """Simulate a transport timeout without contacting a controller."""
        raise requests.Timeout("controller unavailable")

    monkeypatch.setattr(source.session, "request", fail)
    try:
        with pytest.raises(requests.Timeout):
            getattr(source, operation)()
        assert not source._request_lock.locked()
    finally:
        source.session.close()


def test_wled_state_still_requires_object(
    config_file: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Supporting array catalogs must not weaken the state response contract."""
    source = WledSource(load_config(str(config_file)), None, threading.Event(), "wled")
    monkeypatch.setattr(source.session, "request", lambda *_args, **_kwargs: FakeResponse([]))
    try:
        with pytest.raises(ValueError, match="non-object"):
            source.status()
    finally:
        source.session.close()


@pytest.mark.parametrize("command", ["effects", "palettes"])
@pytest.mark.parametrize("fails", [False, True])
def test_wled_tool_catalog(
    config_file: Path, monkeypatch: pytest.MonkeyPatch, command: str, fails: bool,
) -> None:
    """The standalone tool prints ordered arrays or exits with explicit failure."""
    path = Path(__file__).parents[1] / "tools" / "wled_tool.py"
    spec = importlib.util.spec_from_file_location("wled_tool", path)
    assert spec is not None and spec.loader is not None
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)

    def catalog(_source: WledSource) -> list[str]:
        """Return a fixed catalog or simulate a failed discovery request."""
        if fails:
            raise ValueError("invalid catalog")
        return ["Solid", "Blink"]

    monkeypatch.setattr(WledSource, command, catalog)
    result = CliRunner().invoke(
        tool.cli, ["--config", str(config_file), "--section", "wled", command],
    )
    assert result.exit_code == (1 if fails else 0)
    if fails:
        assert "invalid catalog" in result.output
    else:
        assert json.loads(result.output) == ["Solid", "Blink"]
