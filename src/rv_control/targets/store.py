from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

from .base import Target

LOGGER = logging.getLogger(__name__)


class StoreTarget(Target, target_name="store"):
    """Keep the latest payload per topic in a local JSON file for easy viewing."""

    config_section = "store"

    def __init__(self, config: Any, command_handler: Any = None, section_name: str | None = None) -> None:
        """Read directory, filename, flush interval, and base topic from this target's section."""
        super().__init__(config, command_handler, section_name)
        section = config[self.section_name] if config.has_section(self.section_name) else {}
        mqtt_section = Target.section_for_type(config, "mqtt")
        fallback = config[mqtt_section].get("base_topic", "rv") if mqtt_section else "rv"
        self.base_topic = section.get("base_topic", fallback).strip("/")
        self.directory = Path(section.get("directory", "data")).expanduser()
        self.filename = section.get("filename", "state.json")
        self.flush_interval = max(0.1, float(section.get("flush_interval", "1")))
        self.path = self.directory / self.filename
        self._values: dict[str, Any] = {}
        self._dirty = False
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    def connect(self) -> None:
        """Create the directory and start the periodic flush thread."""
        self.directory.mkdir(parents=True, exist_ok=True)
        self._wake.clear()
        self._thread = threading.Thread(target=self._flush_loop, name="store-flush", daemon=True)
        self._thread.start()

    def publish(self, topic: str, payload: dict[str, Any]) -> None:
        """Record the latest payload under the same full topic MQTT would use."""
        full_topic = "/".join(part.strip("/") for part in (self.base_topic, topic) if part)
        value = json.loads(json.dumps(payload, default=str))
        with self._lock:
            self._values[full_topic] = value
            self._dirty = True

    def close(self) -> None:
        """Stop the flush thread and write any pending values."""
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        self._flush()

    def _flush_loop(self) -> None:
        """Write pending values every flush interval until closed."""
        while not self._wake.wait(self.flush_interval):
            self._flush()

    def _flush(self) -> None:
        """Atomically write the topic map to disk when it has changed."""
        with self._lock:
            if not self._dirty:
                return
            snapshot = dict(sorted(self._values.items()))
            self._dirty = False
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            handle, temp_name = tempfile.mkstemp(dir=self.directory, prefix=f".{self.filename}.", suffix=".tmp")
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(snapshot, stream, indent=2)
                stream.write("\n")
            os.replace(temp_name, self.path)
        except OSError as error:
            with self._lock:
                self._dirty = True
            LOGGER.warning("Unable to write store file %s: %s", self.path, error)
