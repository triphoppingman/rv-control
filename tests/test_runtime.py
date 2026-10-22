from __future__ import annotations

import threading
from configparser import ConfigParser
from typing import Any

import pytest
from click.testing import CliRunner

from rv_control.cli import cli
from rv_control.runtime import RuntimeContext, RuntimePublisher, VariablesUnavailable
from rv_control.sources.base import Source


@pytest.fixture
def runtime_config() -> ConfigParser:
    """Provide an anonymous, hardware-free producer and consumer configuration."""
    config = ConfigParser()
    config.read_string("""
[target]
enabled-targets = log
[log]
type = log
[source]
enabled-sources = gps, weather
[gps]
type = gpsd
topic = position
[variables.location]
topic = position/TPV
fields = latitude:lat, longitude:lon
require = mode >= 2
max_age = 10
[weather]
type = open_meteo
latitude = ${location.latitude}
longitude = ${location.longitude}
topic = weather
[service]
daemon_mode = true
""")
    return config


def test_atomic_freshness_and_invalidation(runtime_config: ConfigParser) -> None:
    """Never mix reports, retain invalid fixes, or resolve expired values."""
    now = [0.0]
    context = RuntimeContext(runtime_config, clock=lambda: now[0])
    settings = {"lat": "${location.latitude}", "lon": "${location.longitude}"}
    with pytest.raises(VariablesUnavailable):
        context.resolve(settings)
    context.update("position/TPV", {"mode": 3, "lat": 12.5, "lon": -45.5})
    assert context.resolve(settings) == {"lat": 12.5, "lon": -45.5}
    now[0] = 9.99
    assert context.resolve(settings)["lat"] == 12.5
    now[0] = 10
    with pytest.raises(VariablesUnavailable, match="stale"):
        context.resolve(settings)
    for report in (
        {"mode": 1, "lat": 1, "lon": 2},
        {"mode": 3, "lat": 1},
        {"mode": 3, "lat": None, "lon": 2},
        {"mode": True, "lat": 1, "lon": 2},
        {"mode": 3, "lat": float("nan"), "lon": 2},
        {"mode": 3, "lat": [1], "lon": 2},
    ):
        context.update("position/TPV", {"mode": 3, "lat": 12.5, "lon": -45.5})
        context.update("position/TPV", report)
        with pytest.raises(VariablesUnavailable):
            context.resolve(settings)
    context.update("position/TPV", {"mode": 2, "lat": 0, "lon": 0})
    assert context.resolve(settings) == {"lat": 0, "lon": 0}


def test_templates_and_exact_topics(runtime_config: ConfigParser) -> None:
    """Support typed scalars, string templates, nested fields and dollar escaping."""
    runtime_config["variables"] = {
        "topic": "other", "fields": "value:nested.value", "require": 'status == "ready"',
    }
    context = RuntimeContext(runtime_config)
    for topic in ("rv/other", "other/subtopic"):
        context.update(topic, {"nested": {"value": 7}, "status": "ready"})
        with pytest.raises(VariablesUnavailable):
            context.resolve({"value": "$value"})
    context.update("other", {"nested": {"value": 7}, "status": "ready"})
    assert context.resolve({
        "typed": "$value", "braced": "${value}", "string": "a${value}b",
        "literal": "$$value", "empty": "",
    }) == {"typed": 7, "braced": 7, "string": "a7b", "literal": "$value", "empty": ""}
    with pytest.raises(ValueError, match="Unknown runtime"):
        context.resolve({"bad": "$HOME"})
    context.update("other", {"nested": {"value": 7}, "status": "offline"})
    with pytest.raises(VariablesUnavailable):
        context.resolve({"value": "$value"})


@pytest.mark.parametrize("template", ["$(id)", "${value:-0}", "$", "${}", "${bad", "$9"])
def test_rejects_shell_and_malformed_syntax(runtime_config: ConfigParser, template: str) -> None:
    """Runtime expansion must never execute or silently preserve shell expressions."""
    with pytest.raises(ValueError, match="syntax"):
        RuntimeContext(runtime_config).resolve({"value": template})


@pytest.mark.parametrize(("key", "value"), [
    ("max_age", "0"), ("max_age", "nan"), ("max_age", "inf"),
    ("topic", "gps/+"), ("fields", "lat"), ("fields", "lat:lat,lat:lon"),
    ("fields", "lat:${other}"), ("require", "__import__('os')"),
    ("require", "mode >= null"), ("require", "mode >= []"),
])
def test_rejects_invalid_bindings(runtime_config: ConfigParser, key: str, value: str) -> None:
    """Binding errors must surface before telemetry collection."""
    runtime_config["variables.location"][key] = value
    with pytest.raises(ValueError):
        RuntimeContext(runtime_config)


@pytest.mark.parametrize("section", ["variables.", "variables..location", "variables.bad-name"])
def test_invalid_namespace(runtime_config: ConfigParser, section: str) -> None:
    """Reject malformed namespaces, including a second unnamed variable group."""
    runtime_config[section] = {"topic": "other", "fields": "value:value"}
    with pytest.raises(ValueError, match="namespace"):
        RuntimeContext(runtime_config)


def test_concurrent_snapshots(runtime_config: ConfigParser) -> None:
    """Read complete coordinate pairs while another source thread publishes updates."""
    context = RuntimeContext(runtime_config)
    context.update("position/TPV", {"mode": 3, "lat": 0, "lon": 0})
    barrier = threading.Barrier(2)

    def publish() -> None:
        """Rapidly change both coordinates in one producer thread."""
        barrier.wait()
        for value in range(2000):
            context.update("position/TPV", {"mode": 3, "lat": value, "lon": -value})

    thread = threading.Thread(target=publish)
    thread.start()
    barrier.wait()
    try:
        for _ in range(2000):
            snapshot = context.resolve({"lat": "${location.latitude}", "lon": "${location.longitude}"})
            assert snapshot["lat"] == -snapshot["lon"]
    finally:
        thread.join(timeout=5)
    assert not thread.is_alive()


def test_dependency_validation(runtime_config: ConfigParser) -> None:
    """Reject missing producers, ambiguous producers, unknown variables and cycles."""
    RuntimeContext(runtime_config).validate_sources(runtime_config)
    runtime_config["weather"]["latitude"] = "${unknown.latitude}"
    with pytest.raises(ValueError, match="Unknown runtime"):
        RuntimeContext(runtime_config).validate_sources(runtime_config)
    runtime_config["weather"]["latitude"] = "${location.latitude}"
    runtime_config["source"]["enabled-sources"] = "weather"
    with pytest.raises(ValueError, match="no enabled producer"):
        RuntimeContext(runtime_config).validate_sources(runtime_config)
    runtime_config["source"]["enabled-sources"] = "gps, weather"
    runtime_config["weather"]["topic"] = "position"
    with pytest.raises(ValueError, match="Ambiguous"):
        RuntimeContext(runtime_config).validate_sources(runtime_config)
    runtime_config["source"]["enabled-sources"] = "weather"
    with pytest.raises(ValueError, match="Cyclic"):
        RuntimeContext(runtime_config).validate_sources(runtime_config)


def test_multi_source_dependency_cycle(runtime_config: ConfigParser) -> None:
    """Detect cycles crossing multiple otherwise valid source bindings."""
    runtime_config["source"]["enabled-sources"] = "weather, second"
    runtime_config["weather"]["latitude"] = "${second.latitude}"
    runtime_config["weather"]["longitude"] = "${second.longitude}"
    runtime_config["second"] = dict(runtime_config["weather"], topic="second")
    runtime_config["second"]["latitude"] = "${first.latitude}"
    runtime_config["second"]["longitude"] = "${first.longitude}"
    runtime_config["variables.first"] = {
        "topic": "weather", "fields": "latitude:latitude,longitude:longitude",
    }
    runtime_config["variables.second"] = {
        "topic": "second", "fields": "latitude:latitude,longitude:longitude",
    }
    with pytest.raises(ValueError, match="Cyclic"):
        RuntimeContext(runtime_config).validate_sources(runtime_config)


def test_actual_source_topic_roots(runtime_config: ConfigParser) -> None:
    """Use source-declared roots, including legacy defaults and fixed RV-C topics."""
    runtime_config["source"]["enabled-sources"] = "bus, weather"
    runtime_config["bus"] = {"type": "rvc", "topic": "unused-custom-topic"}
    runtime_config["variables.location"]["topic"] = "rvc/GPS_STATUS"
    RuntimeContext(runtime_config).validate_sources(runtime_config)
    runtime_config["variables.location"]["topic"] = "unused-custom-topic/GPS_STATUS"
    with pytest.raises(ValueError, match="no enabled producer"):
        RuntimeContext(runtime_config).validate_sources(runtime_config)
    runtime_config["bus"] = {"type": "renogy"}
    runtime_config["variables.location"]["topic"] = "renogy"
    RuntimeContext(runtime_config).validate_sources(runtime_config)


def test_publisher_forwarding_and_source_recreation(runtime_config: ConfigParser) -> None:
    """Keep shared variables when a supervised source is recreated, without MQTT."""
    events: list[Any] = []

    class Destination:
        """Capture target lifecycle and telemetry."""

        def connect(self) -> None:
            """Record connection."""
            events.append("connect")

        def publish(self, topic: str, payload: dict[str, Any]) -> None:
            """Record unchanged source telemetry."""
            events.append((topic, payload))

        def close(self) -> None:
            """Record cleanup."""
            events.append("close")

    context = RuntimeContext(runtime_config)
    publisher = RuntimePublisher(Destination(), context)
    payload = {"mode": 3, "lat": 12.5, "lon": -45.5}
    publisher.connect()
    publisher.publish("position/TPV", payload)
    source = Source.create("weather", runtime_config, publisher, threading.Event())
    replacement = Source.create("weather", runtime_config, publisher, threading.Event())
    assert source.resolve_settings({"lat": "${location.latitude}"}) == {"lat": 12.5}
    assert replacement.resolve_settings({"lon": "${location.longitude}"}) == {"lon": -45.5}
    publisher.close()
    assert events == ["connect", ("position/TPV", payload), "close"]
    assert events[1][1] is payload


def test_check_rejects_invalid_reference_before_success(
    runtime_config: ConfigParser, tmp_path: Any,
) -> None:
    """The CLI must not print Configuration OK for unusable references."""
    runtime_config["weather"]["latitude"] = "${missing.latitude}"
    path = tmp_path / "test.ini"
    with path.open("w") as stream:
        runtime_config.write(stream)
    result = CliRunner().invoke(cli, ["--config", str(path), "check"])
    assert result.exit_code == 1
    assert "Unknown runtime variables" in result.output
    assert "Configuration OK" not in result.output
