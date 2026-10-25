from __future__ import annotations

import threading
import importlib.util
import json
import logging
from pathlib import Path
from typing import Any

import pytest
import requests
from click.testing import CliRunner

from rv_control.config import load_config
from rv_control.coach_queries import CoachQueries
from rv_control.coach_state import wrap_publisher
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


@pytest.mark.parametrize("custom_topic", ["wled", "/lights/patio/"])
def test_wled_polls_update_coach_brightness(
    config_file: Path, monkeypatch: pytest.MonkeyPatch, custom_topic: str, caplog: pytest.LogCaptureFixture,
) -> None:
    """Polled device changes reach entity state and survive a temporary HTTP failure."""
    config = load_config(str(config_file))
    config["coach"]["overlay"] = str(
        Path(__file__).parents[1] / "src/rv_control/coaches/wled-overlay-example.yml"
    )
    config["coach"]["wled"] = "wled"
    config["wled"]["topic"] = custom_topic
    published: list[tuple[str, dict[str, Any]]] = []

    class Target:
        """Record both raw source and semantic entity publications."""

        def publish(self, topic: str, payload: dict[str, Any]) -> None:
            """Capture a source or entity snapshot."""
            published.append((topic, payload))

    publisher = wrap_publisher(config, Target())
    caplog.set_level(logging.INFO, logger="rv_control.sources.wled")
    stop = threading.Event()
    source = WledSource(config, publisher, stop, "wled")
    brightness = iter([128, None, 255, 255, 0])
    waits: list[float] = []
    closed: list[bool] = []

    def request(*_args: Any, **_kwargs: Any) -> FakeResponse:
        """Return actual device levels, with one simulated outage between polls."""
        bri = next(brightness)
        if bri is None:
            raise requests.Timeout("temporary outage")
        return FakeResponse({"on": bri > 0, "bri": bri})

    def wait(interval: float) -> bool:
        """Advance five deterministic polling cycles without real delays."""
        waits.append(interval)
        if len(waits) == 5:
            stop.set()
        return stop.is_set()

    monkeypatch.setattr(source.session, "request", request)
    monkeypatch.setattr(source.session, "close", lambda: closed.append(True))
    monkeypatch.setattr(stop, "wait", wait)
    source.run()

    assert any(
        record.levelno == logging.INFO and "WLED poll failed for wled" in record.message
        for record in caplog.records
    )
    states = [payload["state"] for topic, payload in published if topic == "coach/entity/patio_light"]
    assert states == [
        {"on": True, "bri": 128, "level": 50.2},
        {"on": True, "bri": 255, "level": 100.0},
        {"on": False, "bri": 0, "level": 0.0},
    ]
    assert len([topic for topic, _ in published if topic == custom_topic.strip("/")]) == 4
    assert waits == [1.0] * 5
    assert closed == [True]
    assert publisher.tracker.snapshots(["patio_light"])["patio_light"]["missing_fields"] == []


def test_wled_coach_refresh_reads_actual_state(
    config_file: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit coach refresh reads WLED without writes and updates observations."""
    config = load_config(str(config_file))
    config["coach"]["overlay"] = str(
        Path(__file__).parents[1] / "src/rv_control/coaches/wled-overlay-example.yml"
    )
    config["coach"]["wled"] = "wled"
    class Target:
        """Accept publications while assertions inspect the coach tracker."""

        def publish(self, topic: str, payload: dict[str, Any]) -> None:
            """Accept a raw or semantic state observation."""

    publisher = wrap_publisher(config, Target())
    source = WledSource(config, publisher, threading.Event(), "wled")
    session = FakeSession()
    source.session = session
    monkeypatch.setattr(source, "is_alive", lambda: True)
    queries = CoachQueries(publisher.tracker.coach, publisher.tracker, {"wled": source})
    try:
        assert queries.source_sections(["patio_light"]) == {"wled"}
        result = queries.run("refresh_states", {"timeout_s": 1}, ["patio_light"], remaining_s=2)
        assert result["data"]["entities"][0]["status"] == "observed"
        assert publisher.tracker.state("patio_light") == {"on": True, "bri": 128, "level": 50.2}
        assert session.calls == [("GET", "http://wled.local/json/state", None)]
    finally:
        source.session.close()


def test_wled_status_ids_request_whole_snapshot(config_file: Path) -> None:
    """The neutral status selection supports whole-controller sources without IDs."""
    class Target:
        """Record the actual snapshot returned by a safe source refresh."""

        def __init__(self) -> None:
            """Initialize captured telemetry."""
            self.published: list[tuple[str, dict[str, Any]]] = []

        def publish(self, topic: str, payload: dict[str, Any]) -> None:
            """Record the source's read-only observation."""
            self.published.append((topic, payload))

    publisher = Target()
    source = WledSource(load_config(str(config_file)), publisher, threading.Event(), "wled")
    source.session = FakeSession()
    try:
        source.request_status(status_ids=[])
        assert publisher.published == [("wled", {"on": True, "bri": 128})]
        assert source.session.calls == [("GET", "http://wled.local/json/state", None)]
    finally:
        source.session.close()


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
