from __future__ import annotations

import threading
from configparser import ConfigParser
from pathlib import Path
from typing import Any

import pytest
import requests
from click.testing import CliRunner

from rv_control.cli import cli
from rv_control.runtime import RuntimeContext, RuntimePublisher, VariablesUnavailable
from rv_control.sources.base import Source
from rv_control.sources.open_meteo import FORECAST_URL, OpenMeteoSource


@pytest.fixture
def weather_config() -> ConfigParser:
    """Provide literal coordinates so one-shot weather tests need no GPS."""
    config = ConfigParser()
    config.read_dict({
        "target": {"enabled-targets": "log"},
        "log": {"type": "log"},
        "source": {"enabled-sources": "weather"},
        "weather": {
            "type": "open_meteo", "latitude": "12.5", "longitude": "-45.5",
            "current": "temperature_2m", "timeout": "2",
        },
        "service": {"daemon_mode": "true"},
    })
    return config


class Response:
    """Simulate HTTP status and JSON responses without a network."""

    def __init__(self, data: Any, error: Exception | None = None) -> None:
        """Store a forecast response or transport failure."""
        self.data = data
        self.error = error

    def raise_for_status(self) -> None:
        """Raise the configured HTTP failure when present."""
        if self.error:
            raise self.error

    def json(self) -> Any:
        """Return the synthetic API response."""
        return self.data


def test_literal_one_shot_request_and_output(
    weather_config: ConfigParser, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Preserve API data, encode query parameters, and always close HTTP sessions."""
    calls: list[dict[str, Any]] = []
    closed: list[bool] = []
    data = {
        "latitude": 12.6, "longitude": -45.6,
        "current": {"time": "2026-01-01T12:00", "temperature_2m": 20.5},
        "current_units": {"temperature_2m": "\u00b0C"},
    }

    def get(_session: Any, url: str, **kwargs: Any) -> Response:
        """Capture encoded request inputs and return weather data."""
        calls.append({"url": url, **kwargs})
        return Response(data)

    monkeypatch.setattr(requests.Session, "get", get)
    monkeypatch.setattr(requests.Session, "close", lambda _session: closed.append(True))
    source = OpenMeteoSource(weather_config, None, threading.Event(), "weather")
    result = source.interrogate()
    assert calls == [{
        "url": FORECAST_URL,
        "params": {"latitude": 12.5, "longitude": -45.5, "current": "temperature_2m"},
        "timeout": 2.0,
    }]
    assert result["current"] == data["current"]
    assert result["current_units"] == data["current_units"]
    assert result["latitude"] == 12.6
    assert result["requested_location"] == {"latitude": 12.5, "longitude": -45.5}
    assert result["fetched_at"].endswith("+00:00")
    assert "fetched_at" not in data
    assert closed == [True]
    assert source.comms_check()["ok"] is True
    assert closed == [True, True]


@pytest.mark.parametrize("response", [
    Response([], None), Response({"error": True, "reason": "bad field"}),
    Response({}), Response({"current": {}}),
    Response({"current": {"time": "now"}}),
    Response({}, requests.HTTPError("429 Too Many Requests")),
])
def test_bad_response_closes_session(
    weather_config: ConfigParser, monkeypatch: pytest.MonkeyPatch, response: Response,
) -> None:
    """Malformed weather and HTTP failures must not publish success or leak sessions."""
    closed: list[bool] = []
    monkeypatch.setattr(requests.Session, "get", lambda *_args, **_kwargs: response)
    monkeypatch.setattr(requests.Session, "close", lambda _session: closed.append(True))
    source = OpenMeteoSource(weather_config, None, threading.Event(), "weather")
    with pytest.raises((requests.RequestException, ValueError)):
        source.interrogate()
    assert closed == [True]
    assert source.comms_check()["ok"] is False
    assert closed == [True, True]


@pytest.mark.parametrize(("key", "value"), [
    ("latitude", "91"), ("latitude", "-91"), ("longitude", "181"),
    ("latitude", "nan"), ("longitude", "inf"), ("latitude", ""),
    ("timeout", "0"), ("timeout", "nan"), ("poll_interval", "-1"),
    ("retry_delay", "0"), ("max_retry_delay", "1"), ("current", ","),
    ("latitude", "$(id)"), ("topic", ""),
])
def test_static_validation_without_http(
    weather_config: ConfigParser, key: str, value: str,
) -> None:
    """Reject malformed static configuration before any network or daemon work."""
    weather_config["weather"][key] = value
    with pytest.raises(ValueError):
        RuntimeContext(weather_config).validate_sources(weather_config)


@pytest.mark.parametrize(("key", "value"), [
    ("latitude", True), ("longitude", -181), ("latitude", float("nan")),
    ("latitude", "not a number"), ("latitude", 10**400),
])
def test_dynamic_coordinates_checked_before_http(
    weather_config: ConfigParser, monkeypatch: pytest.MonkeyPatch, key: str, value: Any,
) -> None:
    """Variable resolution must not bypass coordinate validation."""
    source = OpenMeteoSource(weather_config, None, threading.Event(), "weather")
    monkeypatch.setattr(source, "resolve_settings", lambda _settings: {
        "latitude": value if key == "latitude" else 12.5,
        "longitude": value if key == "longitude" else -45.5,
    })

    def no_http(*_args: Any, **_kwargs: Any) -> None:
        """Fail the test if validation ever opens an HTTP request."""
        pytest.fail("Invalid coordinates reached HTTP")

    monkeypatch.setattr(requests.Session, "get", no_http)
    with pytest.raises(ValueError):
        source.interrogate()


def test_gps_binding_to_weather_request(
    weather_config: ConfigParser, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resolve fresh GPS data generically, ignoring wrong topics and invalid fixes."""
    weather_config["source"]["enabled-sources"] = "position, weather"
    weather_config["position"] = {"type": "gpsd", "topic": "gps"}
    weather_config["variables.location"] = {
        "topic": "gps/TPV", "fields": "latitude:lat,longitude:lon",
        "require": "mode >= 2",
    }
    weather_config["weather"]["latitude"] = "${location.latitude}"
    weather_config["weather"]["longitude"] = "${location.longitude}"
    context = RuntimeContext(weather_config)
    context.validate_sources(weather_config)
    published: list[Any] = []

    class Destination:
        """Accept both GPS and weather publications without MQTT."""

        def publish(self, topic: str, payload: dict[str, Any]) -> None:
            """Record telemetry without changing it."""
            published.append((topic, payload))

    publisher = RuntimePublisher(Destination(), context)
    source = Source.create("weather", weather_config, publisher, threading.Event())
    requests_made: list[Any] = []

    def get(_session: Any, _url: str, **kwargs: Any) -> Response:
        """Capture coordinate pairs and supply current weather."""
        requests_made.append(kwargs["params"])
        return Response({"current": {"time": "now", "temperature_2m": 20}})

    monkeypatch.setattr(requests.Session, "get", get)
    for payload in ({"mode": 1}, {"mode": 3, "lat": 12.5}):
        publisher.publish("gps/TPV", payload)
        with pytest.raises(VariablesUnavailable):
            source.interrogate()
    assert requests_made == []
    publisher.publish("gps/TPV", {"mode": 3, "lat": 12.5, "lon": -45.5})
    forecast = source.interrogate()
    assert requests_made == [{
        "latitude": 12.5, "longitude": -45.5, "current": "temperature_2m",
    }]
    publisher.publish("weather", forecast)
    assert published[-1] == ("weather", forecast)
    publisher.publish("gps/TPV", {"mode": 1})
    assert source.comms_check()["ok"] is False
    assert len(requests_made) == 1


def test_poll_schedule_backoff_and_stop(
    weather_config: ConfigParser, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bound retries, reset after success, wait for inputs, and stop interruptibly."""
    weather_config["weather"]["max_retry_delay"] = "90"
    stop = threading.Event()
    events: list[float] = []
    published: list[Any] = []
    source = OpenMeteoSource(weather_config, None, stop, "weather")
    source.publisher = type("Publisher", (), {"publish": lambda _self, *args: published.append(args)})()
    outcomes = iter([
        VariablesUnavailable("waiting"), requests.Timeout("offline"),
        requests.Timeout("offline"), requests.Timeout("offline"),
        requests.Timeout("offline"), {"current": "success"}, requests.Timeout("offline"),
    ])

    def interrogate() -> dict[str, Any]:
        """Deliver one deterministic dependency or API outcome per poll."""
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def wait(delay: float) -> bool:
        """Record waits without sleeping, stopping after the planned iterations."""
        events.append(delay)
        if len(events) == 7:
            stop.set()
        return stop.is_set()

    monkeypatch.setattr(source, "interrogate", interrogate)
    monkeypatch.setattr(stop, "wait", wait)
    source.run()
    assert events == [5, 30, 60, 90, 90, 900, 30]
    assert published == [("weather", {"current": "success"})]


def test_oneshot_with_variables_does_not_start_producers(
    weather_config: ConfigParser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One-shot CLI checks must be bounded even when daemon mode is enabled."""
    weather_config["variables.location"] = {
        "topic": "gps/TPV", "fields": "latitude:lat,longitude:lon",
    }
    weather_config["weather"]["latitude"] = "${location.latitude}"
    weather_config["weather"]["longitude"] = "${location.longitude}"
    path = tmp_path / "weather.ini"
    with path.open("w") as stream:
        weather_config.write(stream)

    def forbidden(*_args: Any, **_kwargs: Any) -> None:
        """Prohibit source threads and HTTP for an unresolved one-shot invocation."""
        pytest.fail("One-shot command started a producer or HTTP request")

    monkeypatch.setattr(Source, "start_enabled", forbidden)
    monkeypatch.setattr(requests.Session, "get", forbidden)
    for command in ("comms-check", "interrogate"):
        result = CliRunner().invoke(cli, ["--config", str(path), command])
        assert result.exit_code == 1
        assert "unavailable or stale" in result.output


@pytest.mark.parametrize("command", ["comms-check", "interrogate"])
def test_oneshot_reports_static_configuration_errors(
    weather_config: ConfigParser, tmp_path: Path, command: str,
) -> None:
    """Invalid static settings must produce a per-source failure, not an uncaught error."""
    weather_config["weather"]["timeout"] = "0"
    path = tmp_path / "invalid.ini"
    with path.open("w") as stream:
        weather_config.write(stream)
    result = CliRunner().invoke(cli, ["--config", str(path), command])
    assert result.exit_code == 1
    assert "timeout must be positive and finite" in result.output
    assert isinstance(result.exception, SystemExit)


def test_daemon_wires_shared_context(
    weather_config: ConfigParser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Daemon startup passes one shared context through startup and supervision."""
    import rv_control.cli as cli_module

    path = tmp_path / "weather.ini"
    with path.open("w") as stream:
        weather_config.write(stream)
    events: list[str] = []

    class Destination:
        """Record target lifecycle for CLI wiring."""

        def connect(self) -> None:
            """Record destination connection."""
            events.append("connect")

        def close(self) -> None:
            """Record destination cleanup."""
            events.append("close")

    def start(config: ConfigParser, publisher: Any, stop: threading.Event) -> tuple[list[Any], dict[str, Any]]:
        """Verify real sources receive the shared publisher context."""
        assert isinstance(publisher, RuntimePublisher)
        source = Source.create("weather", config, publisher, stop)
        monkeypatch.setattr(source, "join", lambda **_kwargs: None)
        events.append("start")
        return [source], {"weather": source}

    def supervise(_config: Any, publisher: Any, _stop: Any, sources: Any, _map: Any) -> None:
        """Verify restarted sources will use the same context-bearing publisher."""
        assert publisher is sources[0].publisher
        events.append("supervise")

    monkeypatch.setattr(cli_module.Target, "build", lambda *_args: Destination())
    monkeypatch.setattr(Source, "start_enabled", start)
    monkeypatch.setattr(Source, "run_until_stopped", supervise)
    result = CliRunner().invoke(cli, ["--config", str(path), "run"])
    assert result.exit_code == 0, result.output
    assert events == ["connect", "start", "supervise", "close"]
