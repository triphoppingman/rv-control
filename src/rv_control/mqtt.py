from __future__ import annotations

import json
import logging
from typing import Any, Callable

LOGGER = logging.getLogger(__name__)


class MqttPublisher:
    def __init__(self, config: Any, command_handler: Callable[[str, dict[str, Any]], None] | None = None) -> None:
        """Create an MQTT publisher from configuration and an optional command callback."""
        import paho.mqtt.client as mqtt

        section = config["mqtt"]
        self.base_topic = section.get("base_topic", "rv").strip("/")
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="rv-control")
        self._mqtt = mqtt
        username = section.get("username", "")
        if username:
            self.client.username_pw_set(username, section.get("password", ""))
        self.qos = section.getint("qos", fallback=0)
        self.retain = section.getboolean("retain", fallback=False)
        self.write_enabled = section.getboolean("write_enabled", fallback=False)
        self.host = section.get("host", "localhost")
        self.port = section.getint("port", fallback=1883)
        self.command_handler = command_handler

    def connect(self) -> None:
        """Connect to the broker, subscribe for writes, and start the MQTT loop."""
        if self.write_enabled and self.command_handler:
            self.client.on_message = self._on_message
        self.client.connect(self.host, self.port, keepalive=60)
        if self.write_enabled and self.command_handler:
            self.client.subscribe(f"{self.base_topic}/+/set", qos=self.qos)
        self.client.loop_start()

    def _on_message(self, _client: Any, _userdata: Any, message: Any) -> None:
        """Parse one MQTT command message and pass valid JSON to its handler."""
        try:
            payload = json.loads(message.payload.decode("utf-8"))
            self.command_handler(message.topic, payload)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            LOGGER.warning("Ignoring invalid MQTT command on %s: %s", message.topic, error)

    def publish(self, topic: str, payload: dict[str, Any]) -> None:
        """Publish a JSON payload beneath the configured base topic."""
        full_topic = "/".join(part.strip("/") for part in (self.base_topic, topic) if part)
        result = self.client.publish(full_topic, json.dumps(payload, default=str), qos=self.qos, retain=self.retain)
        if result.rc != self._mqtt.MQTT_ERR_SUCCESS:
            LOGGER.warning("MQTT publish failed for %s: rc=%s", full_topic, result.rc)

    def close(self) -> None:
        """Stop the MQTT loop and disconnect the broker client."""
        self.client.loop_stop()
        self.client.disconnect()