from __future__ import annotations

import threading
from configparser import ConfigParser
from typing import Any

import pytest
from click.testing import CliRunner

from rv_control.cli import cli
from rv_control.runtime import RuntimeContext, RuntimePublisher, VariablesUnavailable
from rv_control.sources.base import PollingSource, Source


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


def test_constants_and_chained_derivations(runtime_config: ConfigParser) -> None:
    """Combine constants, telemetry and derived values without weakening scalar types."""
    runtime_config["variables.location"]["constants"] = '{"offset": 2, "label": "RV", "ready": true}'
    runtime_config["variables"] = {"constants": '{"count": 4, "zero": 0}'}
    runtime_config["derived.location"] = {
        "shifted": "${location.latitude} + ${location.offset}",
        "combined": "(-${location.shifted} + $count) / 2",
    }
    runtime_config.read_string("[derived.location]\nremainder = $count % 3\n")
    context = RuntimeContext(runtime_config)
    assert context.resolve({"label": "${location.label}", "ready": "${location.ready}", "n": "$zero"}) == {
        "label": "RV", "ready": True, "n": 0,
    }
    context.update("position/TPV", {"mode": 3, "lat": 10, "lon": 20})
    assert context.resolve({
        "a": "${location.shifted}", "b": "${location.combined}", "c": "${location.remainder}",
    }) == {"a": 12, "b": -4, "c": 1}
    assert context.dependencies("location.combined") == {"location.latitude"}
    context.validate_sources(runtime_config)


def test_bounded_window_derivations(runtime_config: ConfigParser) -> None:
    """Aggregate recent direct telemetry samples while preserving ordinary arithmetic."""
    now = [0.0]
    runtime_config["derived.location"] = {
        "average": "avg(${location.latitude}, 10)",
        "minimum": "min(${location.latitude}, 10)",
        "maximum": "max(${location.latitude}, 10)",
        "rate": "rate(${location.latitude}, 10)",
        "adjusted": "(${location.latitude} + 2) * 2",
    }
    context = RuntimeContext(runtime_config, clock=lambda: now[0])
    context.update("position/TPV", {"mode": 3, "lat": 10, "lon": 20})
    now[0] = 2
    context.update("position/TPV", {"mode": 3, "lat": 14, "lon": 20})

    assert context.resolve({
        "average": "${location.average}",
        "minimum": "${location.minimum}",
        "maximum": "${location.maximum}",
        "rate": "${location.rate}",
        "adjusted": "${location.adjusted}",
    }) == {"average": 12, "minimum": 10, "maximum": 14, "rate": 2, "adjusted": 32}

    now[0] = 13
    context.update("position/TPV", {"mode": 3, "lat": 20, "lon": 20})
    assert context.resolve({"average": "${location.average}"}) == {"average": 20}
    with pytest.raises(VariablesUnavailable, match="location.rate"):
        context.resolve({"rate": "${location.rate}"})


@pytest.mark.parametrize("expression", [
    "avg(${location.latitude}, 0)",
    "avg(${location.latitude}, 86401)",
    "avg(${location.longitude} + 1, 10)",
    "avg(${fixed.value}, 10)",
    "sum(${location.latitude}, 10)",
    "avg(${location.latitude}, 10, 20)",
])
def test_invalid_window_derivations_rejected(
    runtime_config: ConfigParser, expression: str,
) -> None:
    """Reject unbounded, unsupported, or non-telemetry window operations at startup."""
    runtime_config["variables.fixed"] = {"constants": '{"value": 3}'}
    runtime_config["derived.example"] = {"result": expression}
    with pytest.raises(ValueError):
        RuntimeContext(runtime_config)


@pytest.mark.parametrize("constants", [
    "[]", "null", '{"x": null}', '{"x": []}', '{"x": {}}',
    '{"x": NaN}', '{"x": Infinity}', '{"bad.name": 1}', "broken",
])
def test_invalid_constants_rejected(runtime_config: ConfigParser, constants: str) -> None:
    """Reject malformed/non-scalar constants before opening any source."""
    runtime_config["variables.fixed"] = {"constants": constants}
    with pytest.raises(ValueError):
        RuntimeContext(runtime_config)


@pytest.mark.parametrize("expression", [
    "__import__('os')", "${location.latitude}.real", "${location.latitude}[0]",
    "abs(${location.latitude})", "${location.latitude} ** 2", "[1, 2]", "True",
    "1e999", "${unknown.x} + 1", "unbound + 1", "1 +", "1+" * 100 + "1",
    "9" * 5000,
])
def test_invalid_derivations_rejected(runtime_config: ConfigParser, expression: str) -> None:
    """Accept only bounded arithmetic, rejecting executable or unknown expressions."""
    runtime_config["derived.example"] = {"result": expression}
    with pytest.raises(ValueError):
        RuntimeContext(runtime_config)


@pytest.mark.parametrize("definitions", [
    {"a": "${example.a}"}, {"a": "${example.b}", "b": "${example.a}"},
])
def test_derived_cycles_fail_at_startup(runtime_config: ConfigParser, definitions: dict[str, str]) -> None:
    """Detect cycles even when no enabled consumer uses those variables."""
    runtime_config["derived.example"] = definitions
    with pytest.raises(ValueError, match="Cyclic"):
        RuntimeContext(runtime_config)


def test_duplicate_runtime_names_rejected(runtime_config: ConfigParser) -> None:
    """Prevent constants or expressions from replacing telemetry names."""
    runtime_config["variables.location"]["constants"] = '{"latitude": 1}'
    with pytest.raises(ValueError, match="Duplicate"):
        RuntimeContext(runtime_config)
    del runtime_config["variables.location"]["constants"]
    runtime_config["derived.location"] = {"latitude": "1"}
    with pytest.raises(ValueError, match="Duplicate"):
        RuntimeContext(runtime_config)


def test_derived_freshness_errors_and_recovery(runtime_config: ConfigParser, caplog: Any) -> None:
    """Inherit the earliest expiry and invalidate on arithmetic/domain failures."""
    now = [0.0]
    runtime_config["variables.second"] = {"topic": "other", "fields": "value:value", "max_age": "3"}
    runtime_config["derived.math"] = {"value": "${location.latitude} / ${second.value}"}
    context = RuntimeContext(runtime_config, clock=lambda: now[0])
    context.update("position/TPV", {"mode": 3, "lat": 12, "lon": 20})
    context.update("other", {"value": 2})
    assert context.resolve({"v": "${math.value}"}) == {"v": 6}
    first_status = context.publication_snapshot()["math"]["status"]["value"]
    now[0] = 2
    assert context.publication_snapshot()["math"]["status"]["value"] == first_status
    now[0] = 3
    with pytest.raises(VariablesUnavailable):
        context.resolve({"v": "${math.value}"})
    for value in (0, True, "2", 10**400):
        context.update("other", {"value": value})
        with pytest.raises(VariablesUnavailable):
            context.resolve({"v": "${math.value}"})
        assert context.publication_snapshot()["math"]["status"]["value"]["reason"] == "calculation_error"
    assert "Derived variable math.value unavailable" in caplog.text
    context.update("other", {"value": 4})
    assert context.resolve({"v": "${math.value}"}) == {"v": 3}
    now[0] = 10
    with pytest.raises(VariablesUnavailable):
        context.resolve({"v": "${math.value}"})
    assert context.resolve({"literal": "unchanged"}) == {"literal": "unchanged"}


def test_source_graph_follows_derived_inputs(runtime_config: ConfigParser) -> None:
    """Derived variables cannot hide source cycles or absent producers."""
    runtime_config["variables.feedback"] = {"topic": "weather", "fields": "latitude:latitude"}
    runtime_config["derived.feedback"] = {"calculated": "${feedback.latitude} + 0"}
    runtime_config["weather"]["latitude"] = "${feedback.calculated}"
    with pytest.raises(ValueError, match="Cyclic"):
        RuntimeContext(runtime_config).validate_sources(runtime_config)
    runtime_config["variables.feedback"]["topic"] = "missing"
    with pytest.raises(ValueError, match="no enabled producer"):
        RuntimeContext(runtime_config).validate_sources(runtime_config)


class RecordingDestination:
    """Record target operations without a broker or hardware."""

    def __init__(self) -> None:
        """Initialize captured messages and lifecycle counts."""
        self.messages: list[tuple[str, dict[str, Any]]] = []
        self.connected = False
        self.closed = False
        self.expired = threading.Event()

    def connect(self) -> None:
        """Record target connection."""
        self.connected = True

    def publish(self, topic: str, payload: dict[str, Any]) -> None:
        """Record telemetry and signal a published expiry transition."""
        self.messages.append((topic, payload))
        if any(status["reason"] == "stale" for status in payload.get("status", {}).values()):
            self.expired.set()

    def close(self) -> None:
        """Record target shutdown."""
        self.closed = True


def test_read_only_runtime_events_publish_hysteretic_transitions(
    runtime_config: ConfigParser,
) -> None:
    """Publish active and cleared event records without issuing source commands."""
    runtime_config["events.battery_low"] = {
        "when": "${location.latitude} < 12",
        "clear_when": "${location.latitude} > 12.5",
    }
    destination = RecordingDestination()
    publisher = RuntimePublisher(destination, RuntimeContext(runtime_config))
    publisher.connect()
    try:
        publisher.publish("position/TPV", {"mode": 3, "lat": 11, "lon": 20})
        publisher.publish("position/TPV", {"mode": 3, "lat": 12.2, "lon": 20})
        assert len([topic for topic, _payload in destination.messages if topic.startswith("events/")]) == 1
        publisher.publish("position/TPV", {"mode": 3, "lat": 13, "lon": 20})
    finally:
        publisher.close()

    transitions = [
        (topic, payload)
        for topic, payload in destination.messages
        if topic == "events/battery_low"
    ]
    assert [payload["state"] for _topic, payload in transitions] == ["active", "cleared"]
    assert [payload["previous_state"] for _topic, payload in transitions] == ["inactive", "active"]
    assert transitions[0][1]["value"] == 11
    assert transitions[1][1]["value"] == 13
    assert destination.closed


def test_runtime_event_activation_delay_and_unavailable_input(runtime_config: ConfigParser) -> None:
    """Debounce activation on the runtime clock and retain state through missing inputs."""
    now = [0.0]
    runtime_config["events.battery_low"] = {
        "when": "${location.latitude} < 12",
        "for_seconds": "5",
    }
    context = RuntimeContext(runtime_config, clock=lambda: now[0])
    context.update("position/TPV", {"mode": 3, "lat": 11, "lon": 20})
    assert context.evaluate_events() == []
    now[0] = 5
    assert context.evaluate_events()[0]["state"] == "active"
    context.update("position/TPV", {"mode": 1, "lat": 11, "lon": 20})
    assert context.evaluate_events() == []
    context.update("position/TPV", {"mode": 3, "lat": 13, "lon": 20})
    assert context.evaluate_events()[0]["state"] == "cleared"


def test_runtime_event_validation_and_producer_requirement(runtime_config: ConfigParser) -> None:
    """Validate configured event references and prevent events without telemetry producers."""
    runtime_config["events.battery_low"] = {"when": "${location.latitude} < 12"}
    RuntimeContext(runtime_config).validate_sources(runtime_config)
    runtime_config["source"]["enabled-sources"] = "weather"
    with pytest.raises(ValueError, match="no enabled producer"):
        RuntimeContext(runtime_config).validate_sources(runtime_config)
    runtime_config["source"]["enabled-sources"] = "gps, weather"
    runtime_config["runtime"] = {"events_topic": "position"}
    with pytest.raises(ValueError, match="overlaps telemetry"):
        RuntimeContext(runtime_config).validate_sources(runtime_config)


def test_runtime_event_rejects_unsupported_condition_and_options(runtime_config: ConfigParser) -> None:
    """Keep event evaluation to one declarative comparison and known options."""
    runtime_config["events.bad"] = {"when": "${location.latitude} + 1 < 12"}
    with pytest.raises(ValueError, match="Invalid event condition"):
        RuntimeContext(runtime_config)
    runtime_config["events.bad"] = {
        "when": "${location.latitude} < 12",
        "command": "unsafe",
    }
    with pytest.raises(ValueError, match="accept only"):
        RuntimeContext(runtime_config)


def test_variable_publication_and_periodic_expiry(runtime_config: ConfigParser) -> None:
    """Publish independent namespace state while preserving measurement timestamps."""
    now = [0.0]
    runtime_config["runtime"] = {
        "publish_variables": "true", "variables_topic": "state/variables",
        "variables_republish_seconds": "5",
    }
    runtime_config["variables.location"]["constants"] = '{"name": "RV"}'
    runtime_config["variables"] = {"constants": '{"enabled": true}'}
    runtime_config["derived.location"] = {"double": "${location.latitude} * 2"}
    context = RuntimeContext(runtime_config, clock=lambda: now[0])
    destination = RecordingDestination()
    publisher = RuntimePublisher(destination, context)
    publisher.connect()
    try:
        assert destination.connected
        by_topic = dict(destination.messages)
        assert by_topic["state/variables"]["values"] == {"enabled": True}
        initial = by_topic["state/variables/location"]
        assert initial["values"] == {"latitude": None, "longitude": None, "name": "RV", "double": None}
        assert initial["status"]["latitude"]["reason"] == "missing"
        assert initial["status"]["name"]["available"]
        report = {"mode": 3, "lat": 12, "lon": 24}
        publisher.publish("position/TPV", report)
        assert ("position/TPV", report) in destination.messages
        valid = destination.messages[-1][1]
        assert valid["values"]["double"] == 24
        assert valid["status"]["double"]["expires_at"] == valid["status"]["latitude"]["expires_at"]
        count = len(destination.messages)
        publisher._publish_variables()
        assert len(destination.messages) == count
        now[0] = 5
        publisher._publish_variables()
        assert len(destination.messages) == count + 2
        assert dict(destination.messages)["state/variables/location"]["status"] == valid["status"]
        now[0] = 10
        publisher._publish_variables()
        stale = dict(destination.messages)["state/variables/location"]
        assert stale["values"]["latitude"] is None
        assert stale["values"]["double"] is None
        assert stale["values"]["name"] == "RV"
        assert stale["status"]["latitude"]["reason"] == "stale"
        assert stale["status"]["latitude"]["expires_at"] == valid["status"]["latitude"]["expires_at"]
        publisher.publish("position/TPV", report)
        assert destination.messages[-1][1]["values"]["latitude"] == 12
        publisher.publish("position/TPV", {"mode": 1})
        assert destination.messages[-1][1]["status"]["latitude"]["reason"] == "invalid"
    finally:
        publisher.close()
    assert destination.closed
    assert publisher._thread is not None and not publisher._thread.is_alive()


def test_timer_expires_without_incoming_reports(runtime_config: ConfigParser) -> None:
    """The connected maintenance thread publishes staleness with no source traffic."""
    now = [0.0]
    runtime_config["runtime"] = {"publish_variables": "true", "variables_republish_seconds": "0"}
    context = RuntimeContext(runtime_config, clock=lambda: now[0])
    destination = RecordingDestination()
    publisher = RuntimePublisher(destination, context)
    publisher.connect()
    try:
        publisher.publish("position/TPV", {"mode": 3, "lat": 12, "lon": 24})
        now[0] = 10
        assert destination.expired.wait(2)
    finally:
        publisher.close()
    count = len(destination.messages)
    assert publisher._thread is not None and not publisher._thread.is_alive()
    assert len(destination.messages) == count


def test_publication_default_off(runtime_config: ConfigParser) -> None:
    """Existing targets receive only unchanged source telemetry by default."""
    destination = RecordingDestination()
    publisher = RuntimePublisher(destination, RuntimeContext(runtime_config))
    publisher.connect()
    publisher.publish("position/TPV", {"mode": 3, "lat": 12, "lon": 24})
    publisher.close()
    assert len(destination.messages) == 1
    assert publisher._thread is None


@pytest.mark.parametrize(("key", "value"), [
    ("variables_topic", ""), ("variables_topic", "bad/+"),
    ("variables_topic", "position"), ("variables_republish_seconds", "-1"),
    ("variables_republish_seconds", "nan"), ("publish_variables", "invalid"),
])
def test_publication_validation(runtime_config: ConfigParser, key: str, value: str) -> None:
    """Reject invalid schedules and telemetry/output collisions before startup."""
    runtime_config["runtime"] = {"publish_variables": "true", key: value}
    with pytest.raises(ValueError):
        RuntimeContext(runtime_config).validate_sources(runtime_config)


def test_shared_polling_receives_literals_constants_and_live_values(runtime_config: ConfigParser) -> None:
    """Any polling adapter receives typed settings without importing runtime machinery."""
    runtime_config["variables.fixed"] = {"constants": '{"count": 7}'}

    class Consumer(PollingSource):
        """Model a protocol adapter that only understands resolved settings."""

        runtime_options = ("latitude", "literal", "constant")

        def collect(self, settings: dict[str, Any]) -> dict[str, Any]:
            """Return supplied domain settings with no binding logic."""
            return settings

    runtime_config["weather"]["literal"] = "plain"
    runtime_config["weather"]["constant"] = "${fixed.count}"
    context = RuntimeContext(runtime_config)
    publisher = RuntimePublisher(RecordingDestination(), context)
    source = Consumer(runtime_config, publisher, threading.Event(), "weather")
    with pytest.raises(VariablesUnavailable):
        source.interrogate()
    for latitude in (12, 18):
        context.update("position/TPV", {"mode": 3, "lat": latitude, "lon": 24})
        assert source.interrogate() == {"latitude": latitude, "literal": "plain", "constant": 7}
    assert source.comms_check()["ok"]


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
