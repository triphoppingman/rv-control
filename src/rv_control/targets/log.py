from __future__ import annotations

import json
import logging
from typing import Any

from .base import Target

LOGGER = logging.getLogger("rv_control.targets.log")


class LogTarget(Target, target_name="log"):
    """Log each topic and payload without storing or forwarding anything."""

    config_section = "log"

    def __init__(self, config: Any, command_handler: Any = None, section_name: str | None = None) -> None:
        """Read the log level and base topic from this target's section."""
        super().__init__(config, command_handler, section_name)
        section = config[self.section_name] if config.has_section(self.section_name) else {}
        level = section.get("level", "INFO").strip().upper()
        self.level = logging.getLevelName(level)
        if not isinstance(self.level, int):
            raise ValueError(f"Invalid log target level: {level}")
        mqtt_section = Target.section_for_type(config, "mqtt")
        fallback = config[mqtt_section].get("base_topic", "rv") if mqtt_section else "rv"
        self.base_topic = section.get("base_topic", fallback).strip("/")

    def connect(self) -> None:
        """Nothing to open; logging is always available."""

    def publish(self, topic: str, payload: dict[str, Any]) -> None:
        """Log the full topic and its JSON payload."""
        full_topic = "/".join(part.strip("/") for part in (self.base_topic, topic) if part)
        LOGGER.log(self.level, "%s %s", full_topic, json.dumps(payload, default=str))

    def close(self) -> None:
        """Nothing to release."""
