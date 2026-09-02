from __future__ import annotations

import json
from configparser import ConfigParser
from typing import Any

from rv_control.mqtt import MqttPublisher
from rv_control.cli import _handle_command


def test_mqtt_command_handler_accepts_json(monkeypatch: Any) -> None:
    """Verify valid JSON MQTT commands reach the configured callback."""
    class FakeClient:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            """Provide the MQTT client attributes needed by the test."""
            self.on_message = None

    import paho.mqtt.client as mqtt

    monkeypatch.setattr(mqtt, "Client", FakeClient)
    config = ConfigParser()
    config["mqtt"] = {"base_topic": "rv", "write_enabled": "true"}
    received = []
    publisher = MqttPublisher(
        config,
        lambda topic, payload: received.append((topic, payload)),
    )
    publisher._on_message(None, None, type("Message", (), {"topic": "rv/renogy/set", "payload": json.dumps({"register": 1}).encode()})())
    assert received == [("rv/renogy/set", {"register": 1})]


def test_mqtt_command_routes_to_source() -> None:
    """Verify a set topic resolves and calls the matching source."""
    class SourceStub:
        def __init__(self) -> None:
            """Initialize an empty command capture."""
            self.payload = None

        def handle_command(self, payload: dict[str, Any]) -> None:
            """Capture the command payload routed by the CLI helper."""
            self.payload = payload

    config = ConfigParser()
    config["mqtt"] = {"base_topic": "rv", "write_enabled": "true"}
    source = SourceStub()
    _handle_command(config, {"renogy_controller": source}, "rv/renogy_controller/set", {"register": 256})
    assert source.payload == {"register": 256}