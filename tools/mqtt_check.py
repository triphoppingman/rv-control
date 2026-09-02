#!/usr/bin/env python3
"""Check an MQTT broker connection and inspect observed project topics."""

from __future__ import annotations

import threading
from typing import Any

import click
import paho.mqtt.client as mqtt


class MqttTopicProbe:
    """Connect to MQTT and collect messages received under a topic prefix."""

    def __init__(self, host: str, port: int, username: str, password: str) -> None:
        """Initialize the probe connection settings and result state."""
        self.connected = threading.Event()
        self.finished = threading.Event()
        self.error: str | None = None
        self.topics: set[str] = set()
        self.values: dict[str, list[str]] = {}
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="rv-control-topic-check")
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.on_disconnect = self._on_disconnect
        if username:
            self.client.username_pw_set(username, password)
        self.host = host
        self.port = port

    def _on_connect(self, _client: Any, _userdata: Any, _flags: Any, reason_code: Any, _properties: Any = None) -> None:
        """Record successful broker connection and subscribe to the probe wildcard."""
        if reason_code == 0:
            self.connected.set()
        else:
            self.error = f"MQTT connection refused: {reason_code}"
            self.finished.set()

    def _on_disconnect(self, _client: Any, _userdata: Any, _flags: Any, reason_code: Any, _properties: Any = None) -> None:
        """Record an unexpected disconnect while the probe is running."""
        if not self.finished.is_set() and reason_code != 0:
            self.error = f"MQTT disconnected: {reason_code}"
            self.finished.set()

    def _on_message(self, _client: Any, _userdata: Any, message: Any) -> None:
        """Record each observed topic and its decoded payload values."""
        topic = str(message.topic)
        payload = bytes(message.payload).decode("utf-8", errors="replace")
        self.topics.add(topic)
        self.values.setdefault(topic, []).append(payload)

    def check(self, base_topic: str, timeout: float, full: bool = False) -> set[str] | dict[str, list[str]]:
        """Connect, subscribe, collect topics or values, and close the connection."""
        topic_filter = f"{base_topic.strip('/')}/#"
        try:
            self.client.connect(self.host, self.port, keepalive=30)
            self.client.loop_start()
            if not self.connected.wait(timeout):
                raise RuntimeError(self.error or "timed out waiting for MQTT connection")
            result = self.client.subscribe(topic_filter, qos=0)
            if result[0] != mqtt.MQTT_ERR_SUCCESS:
                raise RuntimeError(f"subscribe failed: rc={result[0]}")
            self.finished.wait(timeout)
            if self.error:
                raise RuntimeError(self.error)
            return self.values if full else self.topics
        finally:
            self.finished.set()
            self.client.loop_stop()
            self.client.disconnect()


@click.command()
@click.option("--host", default="localhost", show_default=True, help="MQTT broker hostname.")
@click.option("--port", default=1883, show_default=True, type=click.IntRange(min=1, max=65535), help="MQTT broker port.")
@click.option("--username", default="", help="MQTT username.")
@click.option("--password", default="", help="MQTT password.", hide_input=True)
@click.option("--base-topic", default="rv", show_default=True, help="Topic prefix to inspect.")
@click.option("--timeout", default=5.0, show_default=True, type=click.FloatRange(min=1.0), help="Seconds to wait for connection and topics.")
@click.option("--full", is_flag=True, help="Print every received value for each topic and subtopic.")
def main(host: str, port: int, username: str, password: str, base_topic: str, timeout: float, full: bool) -> None:
    """Check MQTT connectivity and inspect topics observed under BASE_TOPIC."""
    probe = MqttTopicProbe(host, port, username, password)
    try:
        observed = probe.check(base_topic, timeout, full=full)
    except (OSError, RuntimeError) as error:
        raise click.ClickException(str(error)) from error
    click.echo(f"MQTT connection to {host}:{port}: OK")
    if observed:
        click.echo(f"Observed {len(observed)} topic(s) under {base_topic.strip('/')}/#:")
        if full:
            for topic in sorted(observed):
                for value in observed[topic]:
                    click.echo(f"  {topic} = {value}")
        else:
            for topic in sorted(observed):
                click.echo(f"  {topic}")
    else:
        click.echo(f"No retained or live topics observed under {base_topic.strip('/')}/# in {timeout:g} seconds.")


if __name__ == "__main__":
    main()
