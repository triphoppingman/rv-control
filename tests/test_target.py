from __future__ import annotations

import json
from configparser import ConfigParser
from pathlib import Path
from typing import Any

from rv_control.mqtt import MqttTarget
from rv_control.store import StoreTarget
from rv_control.target import MultiTarget, Target


def _config(tmp_path: Path, targets: str = "store") -> ConfigParser:
    """Build a config selecting targets with a temporary store directory."""
    config = ConfigParser()
    config.read_dict({"target": {"enabled-targets": targets}, "mqtt": {"type": "mqtt", "base_topic": "rv"}, "store": {"type": "store", "directory": str(tmp_path), "flush_interval": "0.1"}})
    return config


def test_store_writes_topic_keyed_json(tmp_path: Path) -> None:
    """Verify the store keeps the latest payload under the full MQTT topic."""
    target = Target.build(_config(tmp_path))
    assert isinstance(target, StoreTarget)
    target.connect()
    target.publish("renogy", {"v": 1})
    target.publish("renogy", {"v": 2})
    target.publish("rvc/DC_SOURCE_STATUS_1", {"a": 1})
    target.close()
    assert json.loads((tmp_path / "state.json").read_text()) == {"rv/renogy": {"v": 2}, "rv/rvc/DC_SOURCE_STATUS_1": {"a": 1}}


def test_default_target_is_mqtt_and_multi_fans_out(tmp_path: Path) -> None:
    """Verify the default is MQTT and multiple names produce a fan-out target."""
    assert Target.enabled_targets(ConfigParser()) == (("mqtt", MqttTarget),)
    multi = Target.build(_config(tmp_path, "store, mqtt"))
    assert isinstance(multi, MultiTarget)


def test_multi_target_isolates_failures() -> None:
    """Verify one failing target does not block the others."""
    seen: list[Any] = []

    class Bad(Target):
        def connect(self) -> None:
            """No-op."""

        def publish(self, topic: str, payload: dict[str, Any]) -> None:
            """Always fail."""
            raise RuntimeError("boom")

        def close(self) -> None:
            """No-op."""

    class Good(Bad):
        def publish(self, topic: str, payload: dict[str, Any]) -> None:
            """Record the call."""
            seen.append((topic, payload))

    config = ConfigParser()
    MultiTarget(config, [Bad(config), Good(config)]).publish("t", {"x": 1})
    assert seen == [("t", {"x": 1})]


def test_legacy_service_targets_and_missing_type(tmp_path: Path) -> None:
    """Verify the legacy [service] list works and typeless sections are rejected."""
    legacy = ConfigParser()
    legacy.read_dict({"service": {"targets": "mqtt, store"}})
    assert [name for name, _ in Target.enabled_targets(legacy)] == ["mqtt", "store"]
    bad = ConfigParser()
    bad.read_dict({"target": {"enabled-targets": "x"}, "x": {}})
    try:
        Target.enabled_targets(bad)
    except ValueError as error:
        assert "requires type" in str(error)
    else:
        raise AssertionError("expected ValueError")
