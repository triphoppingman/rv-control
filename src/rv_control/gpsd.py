from __future__ import annotations

import json
import logging
import socket
from typing import Any

from .source import Source


LOGGER = logging.getLogger(__name__)


class GpsdSource(Source, source_name="gpsd"):
    """Receive JSON reports from a gpsd TCP server and publish them to MQTT."""

    source_name = "gpsd"
    config_section = "gpsd"

    def _address(self) -> tuple[str, int]:
        """Return the configured gpsd host and TCP port."""
        host = self.section.get("host", "localhost").strip()
        port = self.section.getint("port", fallback=2947)
        if not host or not 1 <= port <= 65535:
            raise ValueError(f"[{self.section_name}] gpsd host and port are invalid")
        return host, port

    def _connect(self, watch: bool = True) -> socket.socket:
        """Connect to gpsd and request either version or watch data."""
        connection = socket.create_connection(self._address(), self.section.getfloat("timeout", fallback=5.0))
        connection.settimeout(self.section.getfloat("timeout", fallback=5.0))
        command = b'?WATCH={"enable":true,"json":true}\n' if watch else b"?VERSION;\n"
        connection.sendall(command)
        return connection

    def comms_check(self) -> dict[str, Any]:
        """Check that the configured gpsd server accepts a TCP connection."""
        try:
            with self._connect(watch=False) as connection:
                response = connection.makefile("r", encoding="utf-8")
                try:
                    if not response.readline():
                        raise OSError("gpsd returned no response")
                finally:
                    response.close()
            return {"ok": True, "message": f"gpsd {self._address()[0]}:{self._address()[1]} is available"}
        except (OSError, ValueError) as error:
            return {"ok": False, "message": f"gpsd check failed: {error}"}

    def run(self) -> None:
        """Read gpsd JSON reports and publish them until the source is stopped."""
        try:
            topic = self.section.get("topic", self.section_name).strip("/")
            with self._connect() as connection:
                stream = connection.makefile("r", encoding="utf-8")
                try:
                    while not self.stop_event.is_set():
                        try:
                            line = stream.readline()
                        except socket.timeout:
                            continue
                        if not line:
                            break
                        try:
                            payload = json.loads(line)
                        except json.JSONDecodeError:
                            LOGGER.debug("Ignoring non-JSON gpsd response")
                            continue
                        if isinstance(payload, dict) and payload.get("class") not in ("DEVICES", "WATCH"):
                            cls = payload["class"]
                            self.publisher.publish(f"{topic}/{cls}", payload)
                finally:
                    stream.close()
        except Exception:
            LOGGER.exception("gpsd source instance %s stopped", self.section_name)