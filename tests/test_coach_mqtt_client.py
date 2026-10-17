from __future__ import annotations

import json
from configparser import ConfigParser
from datetime import datetime, timezone
from typing import Any

import pytest
from click.testing import CliRunner

from rv_control import coach_mqtt_client as client_module
from rv_control.coach_mqtt_client import (
    CoachMqttClient,
    CoachMqttClientError,
    build_envelope,
    mqtt_command,
    mqtt_query,
    parse_parameters,
    parse_scalar_parameter,
)


def mqtt_config() -> ConfigParser:
    """Create a minimal target-style configuration without reading local credentials."""
    config = ConfigParser()
    config["target"] = {"enabled-targets": "mqtt_test"}
    config["mqtt_test"] = {
        "type": "mqtt",
        "host": "broker.test",
        "port": "1884",
        "username": "test-user",
        "password": "test-password",
        "base_topic": "test-rv",
    }
    return config


class FakePublishInfo:
    """Provide the successful QoS publish interface used by the client."""

    rc = 0

    def wait_for_publish(self, timeout: float | None = None) -> None:
        """Return immediately because the fake broker acknowledges synchronously."""

    def is_published(self) -> bool:
        """Report that the fake broker acknowledged this publication."""
        return True


class FakeMqttClient:
    """Simulate connect, SUBACK, publish, result delivery, and clean shutdown."""

    instances: list[FakeMqttClient] = []

    def __init__(self, *_args: Any, **kwargs: Any) -> None:
        """Save MQTT constructor arguments and initialize callback state."""
        self.constructor_kwargs = kwargs
        self.calls: list[tuple[Any, ...]] = []
        self.username_password: tuple[str, str] | None = None
        self.on_connect: Any = None
        self.on_subscribe: Any = None
        self.on_message: Any = None
        self.on_disconnect: Any = None
        self.subscribed_topic: str | None = None
        self.instances.append(self)

    def username_pw_set(self, username: str, password: str) -> None:
        """Record credentials configured by the client."""
        self.username_password = (username, password)

    def connect(self, host: str, port: int, keepalive: int) -> None:
        """Record the requested broker endpoint."""
        self.calls.append(("connect", host, port, keepalive))

    def loop_start(self) -> None:
        """Record that the network loop was started."""
        self.calls.append(("loop_start",))
        self.on_connect(self, None, {}, 0, None)

    def subscribe(self, topic: str, qos: int) -> tuple[int, int]:
        """Record and acknowledge the result subscription."""
        self.subscribed_topic = topic
        self.calls.append(("subscribe", topic, qos))
        self.on_subscribe(self, None, 17, [qos], None)
        return (0, 17)

    def publish(self, topic: str, payload: str, qos: int, retain: bool) -> FakePublishInfo:
        """Record the outgoing request and synchronously deliver its terminal result."""
        self.calls.append(("publish", topic, payload, qos, retain))
        request = json.loads(payload)
        self.on_message(
            self,
            None,
            type(
                "FakeMessage",
                (),
                {
                    "topic": f"test-rv/coach/result/{request['client_id']}/{request['request_id']}",
                    "payload": json.dumps(
                        {
                            "version": 1,
                            "client_id": request["client_id"],
                            "request_id": request["request_id"],
                            "target": request["target"],
                            "action": request["action"],
                            "status": "accepted",
                        }
                    ).encode(),
                },
            )(),
        )
        self.on_message(
            self,
            None,
            type(
                "FakeMessage",
                (),
                {
                    "topic": f"test-rv/coach/result/{request['client_id']}/{request['request_id']}",
                    "payload": json.dumps(
                        {
                            "version": 1,
                            "client_id": request["client_id"],
                            "request_id": request["request_id"],
                            "target": request["target"],
                            "action": request["action"],
                            "status": "completed",
                            "code": "executed",
                        }
                    ).encode(),
                },
            )(),
        )
        return FakePublishInfo()

    def loop_stop(self) -> None:
        """Record that the network loop was stopped."""
        self.calls.append(("loop_stop",))

    def disconnect(self) -> None:
        """Record broker disconnection."""
        self.calls.append(("disconnect",))


def test_build_envelope_uses_required_v1_fields_and_utc_timestamps() -> None:
    """Create the complete v1 envelope and preserve TTL in UTC."""
    instant = datetime(2026, 10, 17, 18, 0, tzinfo=timezone.utc)
    envelope = build_envelope("rv-cli", "request-1", "fan", "on", {"level": 2}, True, 15, instant)
    assert envelope == {
        "version": 1,
        "client_id": "rv-cli",
        "request_id": "request-1",
        "issued_at": "2026-10-17T18:00:00.000000Z",
        "expires_at": "2026-10-17T18:00:15.000000Z",
        "target": "fan",
        "action": "on",
        "params": {"level": 2},
        "confirmed": True,
    }


@pytest.mark.parametrize("value", ["[]", "{}", "NaN", "Infinity"])
def test_parse_scalar_parameter_rejects_containers_and_non_json_values(value: str) -> None:
    """Reject compound and non-finite parameter values."""
    with pytest.raises(ValueError):
        parse_scalar_parameter(value)


def test_parse_parameters_keeps_json_types_and_rejects_duplicates_or_confirm_override() -> None:
    """Parse valid scalar values and prevent duplicate or confirmation-bypass inputs."""
    assert parse_parameters((("a", "true"), ("b", '"text"'), ("c", "3")), command=True) == {
        "a": True,
        "b": "text",
        "c": 3,
    }
    with pytest.raises(ValueError, match="duplicated"):
        parse_parameters((("a", "1"), ("a", "2")), command=True)
    with pytest.raises(ValueError, match="--confirm"):
        parse_parameters((("confirm", "true"),), command=True)


def test_exchange_subscribes_before_one_qos1_nonretained_publish() -> None:
    """Use exact result correlation, QoS 1, clean sessions, and no retained request."""
    FakeMqttClient.instances.clear()
    envelope = build_envelope("rv-cli", "request_42", "kitchen_fan", "on", {}, False, 15)
    client = CoachMqttClient(mqtt_config(), "rv-cli", "request_42", 20, FakeMqttClient)
    result, accepted = client.exchange("command", envelope)
    fake = FakeMqttClient.instances[-1]
    assert result["status"] == "completed"
    assert accepted == {
        "version": 1,
        "client_id": "rv-cli",
        "request_id": "request_42",
        "target": "kitchen_fan",
        "action": "on",
        "status": "accepted",
    }
    assert fake.constructor_kwargs["clean_session"] is True
    assert fake.constructor_kwargs["client_id"].startswith("rv-cli-")
    assert fake.username_password == ("test-user", "test-password")
    subscribe_index = next(index for index, call in enumerate(fake.calls) if call[0] == "subscribe")
    publish_index = next(index for index, call in enumerate(fake.calls) if call[0] == "publish")
    assert subscribe_index < publish_index
    assert fake.subscribed_topic == "test-rv/coach/result/rv-cli/request_42"
    publication = fake.calls[publish_index]
    assert publication[1:] == (
        "test-rv/coach/command",
        json.dumps(envelope, separators=(",", ":")),
        1,
        False,
    )
    assert fake.calls[-2:] == [("loop_stop",), ("disconnect",)]


def test_real_paho_reason_codes_are_supported() -> None:
    """Use Paho v2 ReasonCode objects rather than assuming int conversion works."""
    from paho.mqtt.packettypes import PacketTypes
    from paho.mqtt.reasoncodes import ReasonCode

    client = CoachMqttClient(mqtt_config(), "rv-cli", "reason-code", 45, FakeMqttClient)
    client._on_connect(None, None, None, ReasonCode(PacketTypes.CONNACK, "Success"))
    assert client.connected.is_set()
    client._on_disconnect(None, None, None, ReasonCode(PacketTypes.DISCONNECT, "Normal disconnection"))
    assert client._failure is None


def test_empty_base_topic_has_no_leading_slash() -> None:
    """The CLI matches daemon routing when the configured base topic is empty."""
    config = mqtt_config()
    config["mqtt_test"]["base_topic"] = ""
    client = CoachMqttClient(config, "rv-cli", "empty-base", 45, FakeMqttClient)
    assert client_module._topic(client.base_topic, "command") == "coach/command"
    assert client_module._result_topic(client.base_topic, "rv-cli", "empty-base") == "coach/result/rv-cli/empty-base"


class UnacknowledgedSubscribeClient(FakeMqttClient):
    """Leave SUBACK pending to prove requests cannot publish before subscription."""

    def subscribe(self, topic: str, qos: int) -> tuple[int, int]:
        """Record the subscription but intentionally withhold its acknowledgement."""
        self.subscribed_topic = topic
        self.calls.append(("subscribe", topic, qos))
        return (0, 99)


def test_exchange_never_publishes_before_suback() -> None:
    """Fail the request after bounded SUBACK wait without publishing any command."""
    FakeMqttClient.instances.clear()
    envelope = build_envelope("rv-cli", "request-43", "fan", "on", {}, False, 1)
    client = CoachMqttClient(mqtt_config(), "rv-cli", "request-43", 0.01, UnacknowledgedSubscribeClient)
    with pytest.raises(CoachMqttClientError, match="subscription acknowledgement"):
        client.exchange("command", envelope)
    fake = FakeMqttClient.instances[-1]
    assert not any(call[0] == "publish" for call in fake.calls)


def test_query_arguments_validate_action_specific_parameters() -> None:
    """Check refresh selections and reject unsupported or conflicting query options."""
    runner = CliRunner()
    captured: dict[str, Any] = {}

    class FakeRequestClient:
        """Capture CLI-created query requests in place of connecting to a broker."""

        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            """Initialize a request capture stub."""

        def exchange(self, request_type: str, envelope: dict[str, Any]) -> tuple[dict[str, Any], None]:
            """Save the generated query envelope and return a successful fake result."""
            captured["request_type"] = request_type
            captured["envelope"] = envelope
            return (
                {
                    "version": 1,
                    "client_id": envelope["client_id"],
                    "request_id": envelope["request_id"],
                    "status": "completed",
                    "code": "queried",
                },
                None,
            )

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(client_module, "CoachMqttClient", FakeRequestClient)
    try:
        context = runner.invoke(
            mqtt_query,
            ["refresh_states", "--entity", "kitchen_fan", "--entity", "patio_light", "--refresh-timeout", "7"],
            obj=mqtt_config(),
        )
        assert context.exit_code == 0, context.output
        assert captured["request_type"] == "query"
        assert captured["envelope"]["target"] == "coach"
        assert captured["envelope"]["params"] == {
            "entities": ["kitchen_fan", "patio_light"],
            "timeout_s": 7,
        }
        invalid = runner.invoke(mqtt_query, ["health", "--entity", "fan"], obj=mqtt_config())
        assert invalid.exit_code != 0
        missing_lookup = runner.invoke(mqtt_query, ["request_status"], obj=mqtt_config())
        assert missing_lookup.exit_code != 0
        same_lookup = runner.invoke(
            mqtt_query,
            ["request_status", "--request-id", "same", "--lookup-request-id", "same"],
            obj=mqtt_config(),
        )
        assert same_lookup.exit_code != 0
    finally:
        monkeypatch.undo()


def test_command_click_uses_confirm_and_typed_json_params() -> None:
    """Pass command options as JSON types into the versioned envelope."""
    runner = CliRunner()
    captured: dict[str, Any] = {}

    class FakeRequestClient:
        """Capture the command request without connecting to a live broker."""

        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            """Initialize a request capture stub."""

        def exchange(self, request_type: str, envelope: dict[str, Any]) -> tuple[dict[str, Any], None]:
            """Save the generated command envelope and return a successful fake result."""
            captured["envelope"] = envelope
            return (
                {
                    "version": 1,
                    "client_id": envelope["client_id"],
                    "request_id": envelope["request_id"],
                    "status": "completed",
                    "code": "executed",
                },
                None,
            )

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(client_module, "CoachMqttClient", FakeRequestClient)
    try:
        result = runner.invoke(
            mqtt_command,
            ["fan", "brightness", "--request-id", "command_1", "--confirm", "-p", "level", "12.5"],
            obj=mqtt_config(),
        )
        assert result.exit_code == 0, result.output
        assert captured["envelope"]["params"] == {"level": 12.5}
        assert captured["envelope"]["confirmed"] is True
        assert captured["envelope"]["request_id"] == "command_1"
        too_short = runner.invoke(
            mqtt_command,
            ["fan", "on", "--ttl", "10", "--timeout", "5"],
            obj=mqtt_config(),
        )
        assert too_short.exit_code != 0
    finally:
        monkeypatch.undo()
