"""Regression tests for the standalone MQTT inspection tool."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

from click.testing import CliRunner


def _load_mqtt_check_module() -> Any:
    """Load the standalone tool module for direct behavior testing."""
    tool_path = Path(__file__).parents[1] / "tools" / "mqtt_check.py"
    spec = importlib.util.spec_from_file_location("mqtt_check", tool_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_topic_filter_collects_values_for_exact_topic(monkeypatch: Any) -> None:
    """Verify an exact topic subscription returns each received payload value."""
    mqtt_check = _load_mqtt_check_module()

    class FakeClient:
        """Simulate MQTT callbacks and a retained message for one topic."""

        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            """Initialize callback placeholders and subscription tracking."""
            self.on_connect: Any = None
            self.on_disconnect: Any = None
            self.on_message: Any = None
            self.subscriptions: list[str] = []

        def connect(self, *_args: Any, **_kwargs: Any) -> None:
            """Report a successful connection through the configured callback."""
            self.on_connect(self, None, None, 0)

        def loop_start(self) -> None:
            """Provide the production client's loop-start interface."""

        def loop_stop(self) -> None:
            """Provide the production client's loop-stop interface."""

        def disconnect(self) -> None:
            """Provide the production client's disconnect interface."""

        def subscribe(self, topic_filter: str, qos: int = 0) -> tuple[int, int]:
            """Record the filter and deliver an example retained payload."""
            self.subscriptions.append(topic_filter)
            self.on_message(self, None, type("Message", (), {"topic": topic_filter, "payload": b"first"})())
            self.on_message(self, None, type("Message", (), {"topic": topic_filter, "payload": b"second"})())
            return mqtt_check.mqtt.MQTT_ERR_SUCCESS, 1

    monkeypatch.setattr(mqtt_check.mqtt, "Client", FakeClient)
    probe = mqtt_check.MqttTopicProbe("localhost", 1883, "", "")

    values = probe.check("rv", timeout=0, full=True, topic="rv/renogy/status")

    assert probe.client.subscriptions == ["rv/renogy/status"]
    assert values == {"rv/renogy/status": ["first", "second"]}


def test_topic_option_prints_values_without_full_flag(monkeypatch: Any) -> None:
    """Verify the CLI enables value output automatically for ``--topic``."""
    mqtt_check = _load_mqtt_check_module()
    calls: list[tuple[str, float, bool, str | None]] = []

    class Probe:
        """Return a representative payload collection without a broker."""

        def __init__(self, *_args: Any) -> None:
            """Accept the production probe's connection arguments."""
            self.retained = {"rv/renogy/status": [False, False]}

        def check(self, base_topic: str, timeout: float, full: bool, topic: str | None) -> dict[str, list[str]]:
            """Capture the requested mode and return two values for the topic."""
            calls.append((base_topic, timeout, full, topic))
            return {"rv/renogy/status": ["first", "second"]}

    monkeypatch.setattr(mqtt_check, "MqttTopicProbe", Probe)

    result = CliRunner().invoke(mqtt_check.main, ["--topic", "rv/renogy/status"])

    assert result.exit_code == 0
    assert calls == [("rv", 5.0, True, "rv/renogy/status")]
    assert "  rv/renogy/status = first" in result.output
    assert "  rv/renogy/status = second" in result.output