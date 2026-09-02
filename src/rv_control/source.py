from __future__ import annotations

import logging
import threading
import time
from abc import ABC, abstractmethod
from configparser import ConfigParser
from typing import Any, Callable, TypeVar


LOGGER = logging.getLogger(__name__)
SourceType = TypeVar("SourceType", bound="Source")


class Source(threading.Thread, ABC):
    """Common lifecycle and command interface for telemetry sources."""

    source_name = "source"
    config_section = "source"
    _registry = {}

    def __init_subclass__(cls, *, source_name: str | None = None, **kwargs: Any) -> None:
        """Register each concrete source subclass under its configured name."""
        super().__init_subclass__(**kwargs)
        if source_name:
            cls.source_name = source_name
        if cls.source_name != "source":
            Source.register(cls.source_name, cls)

    @classmethod
    def register(cls, name: str, source_type: type[SourceType]) -> None:
        """Register a source class under its MQTT/configuration name."""
        if not name or not issubclass(source_type, cls):
            raise ValueError("source name and Source subclass are required")
        cls._registry[name] = source_type

    @classmethod
    def source_class(cls, name: str) -> type[Source]:
        """Return the registered source class for a name."""
        try:
            return cls._registry[name]
        except KeyError as error:
            raise ValueError(f"Unknown source: {name}") from error

    @classmethod
    def list_sources(cls) -> tuple[str, ...]:
        """Return registered source names in stable order."""
        return tuple(sorted(cls._registry))

    @classmethod
    def enabled_sources(cls, config: ConfigParser) -> tuple[type[Source], ...]:
        """Return registered source classes enabled by configuration."""
        return tuple(
            source_type
            for source_type in cls._registry.values()
            if config.has_section(source_type.config_section)
            and config[source_type.config_section].getboolean("enabled", fallback=False)
        )

    @classmethod
    def create(cls, name: str, config: ConfigParser, publisher: Any, stop_event: threading.Event) -> Source:
        """Construct a registered source by name."""
        return cls.source_class(name)(config, publisher, stop_event)

    @classmethod
    def check_communications(cls, config: ConfigParser) -> dict[str, dict[str, Any]]:
        """Run communication checks for every registered source."""
        results = {}
        for name, source_type in cls._registry.items():
            section = config[source_type.config_section]
            if not section.getboolean("enabled", fallback=False):
                results[name] = {"ok": True, "message": "disabled"}
                continue
            results[name] = source_type(config, None, threading.Event()).comms_check()
        return results

    @classmethod
    def interrogate_enabled(cls, config: ConfigParser) -> dict[str, dict[str, Any]]:
        """Interrogate every enabled source and return per-source results."""
        results = {}
        for source_type in cls.enabled_sources(config):
            source = source_type(config, None, threading.Event())
            try:
                results[source_type.source_name] = {"ok": True, "data": source.interrogate()}
            except Exception as error:
                LOGGER.exception("%s interrogation failed", source_type.source_name)
                results[source_type.source_name] = {"ok": False, "message": str(error)}
        return results

    @classmethod
    def start_enabled(cls, config: ConfigParser, publisher: Any, stop_event: threading.Event) -> tuple[list[Source], dict[str, Source]]:
        """Start enabled sources and return them indexed by config section."""
        sources = []
        source_map = {}
        for source_type in cls.enabled_sources(config):
            source = source_type(config, publisher, stop_event)
            source.start()
            sources.append(source)
            source_map[source_type.config_section] = source
        return sources, source_map

    @classmethod
    def run_until_stopped(cls, config: ConfigParser, publisher: Any, stop_event: threading.Event, sources: list[Source], source_map: dict[str, Source]) -> None:
        """Supervise source threads and restart failed sources."""
        while not stop_event.is_set():
            for index, source in enumerate(tuple(sources)):
                if source.is_alive() or stop_event.is_set():
                    continue
                source_type = type(source)
                LOGGER.error("Source %s stopped; restarting it", source_type.source_name)
                replacement = source_type(config, publisher, stop_event)
                replacement.start()
                sources[index] = replacement
                source_map[source_type.config_section] = replacement
            stop_event.wait(1)

    @staticmethod
    def wait_for(sources: list[Source], interval: float = 1) -> None:
        """Wait until all source threads have stopped."""
        while any(source.is_alive() for source in sources):
            time.sleep(interval)

    @abstractmethod
    def comms_check(self) -> dict[str, Any]:
        """Check local connectivity and configured device availability."""

    def __init__(self, config: ConfigParser, publisher: Any, stop_event: threading.Event) -> None:
        """Initialize a daemon source thread with shared configuration and shutdown state."""
        super().__init__(name=self.source_name, daemon=True)
        self.config = config
        self.publisher = publisher
        self.stop_event = stop_event

    @abstractmethod
    def run(self) -> None:
        """Collect readings until the source stops or fails."""

    def handle_command(self, payload: dict[str, Any]) -> None:
        """Handle a command received from MQTT, when supported."""
        LOGGER.warning("%s does not support write commands: %s", self.source_name, payload)

    def interrogate(self) -> dict[str, Any]:
        """Return a current source snapshot, when supported."""
        raise NotImplementedError(f"{self.source_name} does not support interrogation")