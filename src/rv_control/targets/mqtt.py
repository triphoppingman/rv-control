from __future__ import annotations

import json
import logging
import queue
import threading
from typing import Any, Callable

from .base import Target

LOGGER = logging.getLogger(__name__)
COMMAND_QUEUE_SIZE = 64


class MqttTarget(Target, target_name="mqtt"):
    """Publish telemetry to an MQTT broker and optionally receive set commands."""

    config_section = "mqtt"

    def __init__(self, config: Any, command_handler: Callable[[str, dict[str, Any]], None] | None = None, section_name: str | None = None) -> None:
        """Create an MQTT publisher from its config section and an optional command callback."""
        super().__init__(config, command_handler, section_name)
        import paho.mqtt.client as mqtt

        section = config[self.section_name]
        self.base_topic = section.get("base_topic", "rv").strip("/")
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="rv-control", clean_session=True)
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
        self.coach_service: Any = None
        self.connected = threading.Event()
        self._command_queue: queue.Queue[tuple[str, dict[str, Any]] | None] = queue.Queue(
            maxsize=COMMAND_QUEUE_SIZE,
        )
        self._command_worker: threading.Thread | None = None
        self._command_worker_lock = threading.Lock()
        self._command_worker_stopped = False
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect

    def connect(self) -> None:
        """Connect to the broker, subscribe for writes, and start the MQTT loop."""
        if self.command_handler or self.coach_service is not None:
            self.client.on_message = self._on_message
        self.client.connect(self.host, self.port, keepalive=60)
        if self.write_enabled and self.command_handler:
            self._start_command_worker()
        try:
            self.client.loop_start()
        except BaseException:
            self._stop_command_worker()
            raise

    def _on_connect(self, _client: Any, _userdata: Any, _flags: Any, reason_code: Any, _properties: Any = None) -> None:
        """Restore every enabled subscription after each successful connection."""
        if reason_code != 0:
            self.connected.clear()
            LOGGER.error("MQTT connection rejected: %s", reason_code)
            return
        self.connected.set()
        topics = []
        if self.write_enabled and self.command_handler:
            topics.append((self._topic("+/set"), self.qos))
        if self.coach_service is not None:
            if self.coach_service.enabled:
                topics.append((self._topic("coach/command"), 1))
            if self.coach_service.queries_enabled:
                topics.append((self._topic("coach/query"), 1))
        if topics:
            result, _mid = self.client.subscribe(topics)
            if result != self._mqtt.MQTT_ERR_SUCCESS:
                LOGGER.error("MQTT command subscriptions failed: rc=%s", result)

    def _on_disconnect(self, _client: Any, _userdata: Any, _flags: Any, _reason: Any, _properties: Any = None) -> None:
        """Clear readiness while Paho reconnects without replaying physical work."""
        self.connected.clear()

    def _on_message(self, _client: Any, _userdata: Any, message: Any) -> None:
        """Parse one MQTT command message and pass valid JSON to its handler."""
        prefix = self._topic("coach") + "/"
        if self.coach_service is not None and message.topic in {prefix + "command", prefix + "query"}:
            self.coach_service.receive(message.topic.removeprefix(prefix), message.payload, bool(message.retain))
            return
        try:
            payload = json.loads(message.payload.decode("utf-8"))
            if self.write_enabled and self.command_handler:
                if not isinstance(payload, dict):
                    LOGGER.warning("Ignoring non-object MQTT command on %s", message.topic)
                    return
                try:
                    self._command_queue.put_nowait((message.topic, payload))
                except queue.Full:
                    LOGGER.error("MQTT command queue is full; dropping command on %s", message.topic)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            LOGGER.warning("Ignoring invalid MQTT command on %s: %s", message.topic, error)

    def _start_command_worker(self) -> None:
        """Start the single serialized worker for source command callbacks."""
        with self._command_worker_lock:
            if self._command_worker is not None:
                return
            self._command_worker_stopped = False
            self._command_worker = threading.Thread(
                target=self._process_commands,
                name="mqtt-source-commands",
                daemon=True,
            )
            self._command_worker.start()

    def _process_commands(self) -> None:
        """Run queued source commands away from Paho's network callback thread."""
        while True:
            item = self._command_queue.get()
            try:
                if item is None:
                    return
                topic, payload = item
                if self.command_handler is not None:
                    try:
                        self.command_handler(topic, payload)
                    except Exception:
                        LOGGER.exception("MQTT command handler failed for %s", topic)
            finally:
                self._command_queue.task_done()

    def _stop_command_worker(self) -> None:
        """Stop accepting queued work and join the command worker before teardown."""
        with self._command_worker_lock:
            worker = self._command_worker
            if worker is None or self._command_worker_stopped:
                return
            self._command_worker_stopped = True
        self._command_queue.put(None)
        worker.join()
        with self._command_worker_lock:
            self._command_worker = None

    def publish(self, topic: str, payload: dict[str, Any]) -> None:
        """Publish a JSON payload beneath the configured base topic."""
        full_topic = self._topic(topic)
        result = self.client.publish(full_topic, json.dumps(payload, default=str), qos=self.qos, retain=self.retain)
        if result.rc != self._mqtt.MQTT_ERR_SUCCESS:
            LOGGER.warning("MQTT publish failed for %s: rc=%s", full_topic, result.rc)

    def _topic(self, relative: str) -> str:
        """Join topics consistently for both nested and empty base prefixes."""
        return "/".join(part.strip("/") for part in (self.base_topic, relative) if part.strip("/"))

    def publish_result(self, topic: str, payload: dict[str, Any], wait: bool = False) -> None:
        """Publish a non-retained QoS-1 result, optionally requiring broker acknowledgment."""
        if not self.connected.is_set():
            raise ConnectionError("MQTT broker is disconnected")
        full_topic = self._topic(topic)
        result = self.client.publish(full_topic, json.dumps(payload, allow_nan=False), qos=1, retain=False)
        if result.rc != self._mqtt.MQTT_ERR_SUCCESS:
            raise ConnectionError(f"MQTT result publication failed: rc={result.rc}")
        if wait:
            result.wait_for_publish(timeout=5)
            if not result.is_published():
                raise TimeoutError("MQTT result acknowledgment timed out")

    def close(self) -> None:
        """Stop the MQTT loop and disconnect the broker client."""
        self.client.loop_stop()
        self.client.disconnect()
        self._stop_command_worker()


MqttPublisher = MqttTarget
