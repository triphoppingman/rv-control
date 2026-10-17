"""Exercise semantic MQTT transport boundaries without connecting a broker."""

from __future__ import annotations

import json
from configparser import ConfigParser
from types import SimpleNamespace
from typing import Any

import pytest

from rv_control.targets.mqtt import MqttTarget


@pytest.fixture
def mqtt_target(monkeypatch: Any) -> tuple[MqttTarget, Any]:
    """Create an inert Paho replacement with inspectable delivery settings."""
    class FakeClient:
        """Capture MQTT calls and expose successful result acknowledgments."""

        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            """Initialize call captures."""
            self.subscriptions: list[Any] = []
            self.publications: list[Any] = []
            self.result = SimpleNamespace(rc=0, wait_for_publish=lambda **_kwargs: None, is_published=lambda: True)

        def subscribe(self, topics: Any) -> tuple[int, int]:
            """Record a restored subscription batch."""
            self.subscriptions.append(topics)
            return 0, 1

        def publish(self, topic: str, payload: str, **kwargs: Any) -> Any:
            """Capture topic, body, QoS and retain."""
            self.publications.append((topic, json.loads(payload), kwargs))
            return self.result

    import paho.mqtt.client as mqtt

    monkeypatch.setattr(mqtt, "Client", FakeClient)
    config = ConfigParser()
    config["mqtt"] = {"base_topic": "custom/base", "write_enabled": "true", "qos": "0", "retain": "true"}
    target = MqttTarget(config, lambda *_args: None)
    return target, target.client


def test_semantic_subscriptions_recover_on_every_connect(mqtt_target: Any) -> None:
    """Semantic QoS is fixed, raw QoS preserved, and reconnect restores all routes."""
    target, client = mqtt_target
    target.coach_service = SimpleNamespace(enabled=True, queries_enabled=True)
    for _ in range(2):
        target._on_connect(client, None, None, 0)
    assert client.subscriptions == [[
        ("custom/base/+/set", 0), ("custom/base/coach/command", 1), ("custom/base/coach/query", 1),
    ]] * 2
    assert target.connected.is_set()
    target._on_disconnect(client, None, None, 1)
    assert not target.connected.is_set()


def test_queries_subscribe_independently_of_writes(mqtt_target: Any) -> None:
    """A read-only service does not subscribe to physical or raw write topics."""
    target, client = mqtt_target
    target.write_enabled = False
    target.coach_service = SimpleNamespace(enabled=False, queries_enabled=True)
    target._on_connect(client, None, None, 0)
    assert client.subscriptions == [[("custom/base/coach/query", 1)]]


def test_empty_base_topic_is_consistent_for_subscribe_route_and_reply(mqtt_target: Any) -> None:
    """An empty configured prefix must not introduce a leading slash on commands."""
    target, client = mqtt_target
    received = []
    target.base_topic = ""
    target.coach_service = SimpleNamespace(enabled=True, queries_enabled=True, receive=lambda *args: received.append(args))
    target._on_connect(client, None, None, 0)
    assert client.subscriptions[0] == [("+/set", 0), ("coach/command", 1), ("coach/query", 1)]
    target._on_message(None, None, SimpleNamespace(topic="coach/query", payload=b"{}", retain=False))
    assert received == [("query", b"{}", False)]
    target.publish_result("coach/result/client/request", {})
    assert client.publications[-1][0] == "coach/result/client/request"


def test_disabled_api_adds_no_semantic_subscriptions(mqtt_target: Any) -> None:
    """Legacy configurations do not inadvertently expose coach endpoints."""
    target, client = mqtt_target
    target.write_enabled = False
    target._on_connect(client, None, None, 0)
    assert not client.subscriptions


def test_routing_preserves_original_json_and_retain(mqtt_target: Any) -> None:
    """Strict validation receives duplicate keys, raw bytes and retain metadata."""
    target, _client = mqtt_target
    received = []
    target.coach_service = SimpleNamespace(receive=lambda *args: received.append(args))
    payload = b'{"version":1,"version":2}'
    target._on_message(None, None, SimpleNamespace(topic="custom/base/coach/command", payload=payload, retain=True))
    assert received == [("command", payload, True)]


def test_results_override_telemetry_qos_and_retain(mqtt_target: Any) -> None:
    """Even retained telemetry configuration cannot retain a command result."""
    target, client = mqtt_target
    target.connected.set()
    target.publish_result("coach/result/client/request", {"status": "accepted"}, True)
    assert client.publications == [("custom/base/coach/result/client/request", {"status": "accepted"}, {"qos": 1, "retain": False})]
    client.result.is_published = lambda: False
    with pytest.raises(TimeoutError):
        target.publish_result("coach/result/client/request", {}, True)
    target.connected.clear()
    with pytest.raises(ConnectionError):
        target.publish_result("coach/result/client/request", {})


def test_connection_rejection_is_not_ready(mqtt_target: Any) -> None:
    """A rejected CONNACK cannot make the command service look connected."""
    target, client = mqtt_target
    target._on_connect(client, None, None, 5)
    assert not target.connected.is_set()
    assert not client.subscriptions
