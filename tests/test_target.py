from __future__ import annotations

import json
import threading
import time
from configparser import ConfigParser
from pathlib import Path
from typing import Any

import pytest

from rv_control.targets.base import MultiTarget, QueuedTarget, Target
from rv_control.targets.mqtt import MqttTarget
from rv_control.targets.store import StoreTarget


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


def test_log_target_logs_topic_and_payload(caplog: Any) -> None:
    """Verify the log target emits the full topic and JSON payload."""
    import logging

    config = ConfigParser()
    config.read_dict({"target": {"enabled-targets": "dbg"}, "dbg": {"type": "log", "base_topic": "rv", "level": "DEBUG"}})
    target = Target.build(config)
    with caplog.at_level(logging.DEBUG, logger="rv_control.targets.log"):
        target.publish("renogy", {"v": 1})
    assert 'rv/renogy {"v": 1}' in caplog.text


def test_queued_target_blocks_when_full_and_drains_in_fifo_order() -> None:
    """Block producers at capacity and drain accepted publications before close."""
    entered = threading.Event()
    release = threading.Event()
    delivered: list[tuple[str, dict[str, Any]]] = []

    class BlockingTarget(Target):
        """Hold the first delivery while later events fill the queue."""

        def connect(self) -> None:
            """Start without external resources."""

        def publish(self, topic: str, payload: dict[str, Any]) -> None:
            """Block the first event and record each delivered event."""
            if topic == "first":
                entered.set()
                release.wait(timeout=2)
            delivered.append((topic, payload))

        def close(self) -> None:
            """Record that queued delivery has already completed."""
            assert len(delivered) == 3

    target = BlockingTarget(ConfigParser())
    queued = QueuedTarget(target, queue_depth=1)
    queued.connect()
    queued.publish("first", {"value": 1})
    assert entered.wait(timeout=1)
    queued.publish("second", {"nested": {"value": 2}})
    payload = {"nested": {"value": 3}}
    producer_done = threading.Event()

    def publish_third() -> None:
        """Submit another event and signal after it has entered the queue."""
        queued.publish("third", payload)
        producer_done.set()

    producer = threading.Thread(target=publish_third)
    producer.start()
    time.sleep(0.05)
    assert not producer_done.is_set()
    payload["nested"]["value"] = 99
    release.set()
    assert producer_done.wait(timeout=1)
    producer.join(timeout=1)
    queued.close()

    assert delivered == [
        ("first", {"value": 1}),
        ("second", {"nested": {"value": 2}}),
        ("third", {"nested": {"value": 3}}),
    ]


def test_queued_target_default_depth_and_validation() -> None:
    """Use a finite default queue and reject zero or negative configured depths."""
    assert QueuedTarget.configured_depth(ConfigParser()) == QueuedTarget.DEFAULT_QUEUE_DEPTH
    config = ConfigParser()
    config["service"] = {"publish_queue_depth": "0"}
    with pytest.raises(ValueError, match="greater than zero"):
        QueuedTarget.configured_depth(config)


def test_queued_multi_target_failure_logs_source_provenance(caplog: Any) -> None:
    """Include the source instance and queue delay when one destination fails."""

    class FailingTarget(Target):
        """Raise during queued delivery to exercise contextual reporting."""

        target_name = "failing"

        def connect(self) -> None:
            """Start without external resources."""

        def publish(self, _topic: str, _payload: dict[str, Any]) -> None:
            """Fail one publication."""
            raise OSError("offline")

        def close(self) -> None:
            """Release no resources."""

    config = ConfigParser()
    multi = MultiTarget(config, [FailingTarget(config)])
    queued = QueuedTarget(multi, queue_depth=1)
    queued.connect()
    queued.publish_source("hughes", {"voltage": 120}, "hughes", "hughes_power_watchdog")
    with caplog.at_level("ERROR", logger="rv_control.targets.base"):
        queued.close()

    assert "source=hughes section=hughes_power_watchdog topic=hughes" in caplog.text
    assert "queue_delay=" in caplog.text
