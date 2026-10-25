from __future__ import annotations

import json
import threading
from configparser import ConfigParser
from typing import Any

import pytest

from rv_control.targets.mqtt import MqttPublisher
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
    handled = threading.Event()

    def handle(topic: str, payload: dict[str, Any]) -> None:
        """Capture the command and signal completion from the MQTT worker."""
        received.append((topic, payload))
        handled.set()

    publisher = MqttPublisher(
        config,
        handle,
    )
    publisher._start_command_worker()
    publisher._on_message(None, None, type("Message", (), {"topic": "rv/renogy/set", "payload": json.dumps({"register": 1}).encode()})())
    assert handled.wait(timeout=1)
    assert received == [("rv/renogy/set", {"register": 1})]
    publisher._stop_command_worker()


def test_mqtt_command_callback_does_not_run_blocking_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run synchronous source command work on the target's worker, not its caller."""
    import paho.mqtt.client as mqtt

    class FakeClient:
        """Provide the Paho constructor surface without a network connection."""

        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            """Accept the client configuration."""
            self.on_message = None

    monkeypatch.setattr(mqtt, "Client", FakeClient)
    config = ConfigParser()
    config["mqtt"] = {"base_topic": "rv", "write_enabled": "true"}
    started = threading.Event()
    release = threading.Event()
    handler_threads: list[int] = []

    def handle(_topic: str, _payload: dict[str, Any]) -> None:
        """Block until released and record which thread executes the handler."""
        handler_threads.append(threading.get_ident())
        started.set()
        release.wait(timeout=1)

    publisher = MqttPublisher(config, handle)
    publisher._start_command_worker()
    callback_thread = threading.Thread(
        target=publisher._on_message,
        args=(None, None, type(
            "Message", (),
            {"topic": "rv/renogy/set", "payload": b'{"register":1}'},
        )()),
    )
    callback_thread.start()
    callback_thread.join(timeout=0.2)
    assert not callback_thread.is_alive()
    assert started.wait(timeout=1)
    assert handler_threads[0] != callback_thread.ident
    release.set()
    publisher._stop_command_worker()


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