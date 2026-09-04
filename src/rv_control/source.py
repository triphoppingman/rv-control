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
        if name not in cls._registry:
            try:
                import importlib
                importlib.import_module(f".{name}", package="rv_control")
            except ImportError:
                pass
        try:
            return cls._registry[name]
        except KeyError as error:
            raise ValueError(f"Unknown source: {name}") from error

    @classmethod
    def list_sources(cls) -> tuple[str, ...]:
        """Return registered source names in stable order."""
        return tuple(sorted(cls._registry))

    @classmethod
    def enabled_sources(cls, config: ConfigParser) -> tuple[tuple[str, type[Source]], ...]:
        """Return configured source instances as section names and source classes."""
        if not config.has_section("source"):
            raise ValueError("Missing required [source] configuration section")
        names = [name.strip() for name in config["source"].get("enabled-sources", "").split(",") if name.strip()]
        if len(names) != len(set(names)):
            raise ValueError("[source] enabled-sources contains duplicate sections")
        sources = []
        for name in names:
            if not config.has_section(name):
                raise ValueError(f"Enabled source section not found: {name}")
            source_name = config[name].get("type", "").strip()
            if not source_name:
                raise ValueError(f"Source section [{name}] requires type")
            sources.append((name, cls.source_class(source_name)))
        return tuple(sources)

    @classmethod
    def create(cls, name: str, config: ConfigParser, publisher: Any, stop_event: threading.Event) -> Source:
        """Construct a configured source instance by its section name."""
        source_name = config[name].get("type", "").strip()
        if not source_name:
            raise ValueError(f"Source section [{name}] requires type")
        return cls.source_class(source_name)(config, publisher, stop_event, name)

    @classmethod
    def check_communications(cls, config: ConfigParser) -> dict[str, dict[str, Any]]:
        """Run communication checks for every registered source."""
        results = {}
        for name, source_type in cls.enabled_sources(config):
            results[name] = source_type(config, None, threading.Event(), name).comms_check()
        return results

    @classmethod
    def interrogate_enabled(cls, config: ConfigParser) -> dict[str, dict[str, Any]]:
        """Interrogate every enabled source and return per-source results."""
        results = {}
        for name, source_type in cls.enabled_sources(config):
            source = source_type(config, None, threading.Event(), name)
            try:
                results[name] = {"ok": True, "data": source.interrogate()}
            except Exception as error:
                LOGGER.exception("%s interrogation failed", name)
                results[name] = {"ok": False, "message": str(error)}
        return results

    @classmethod
    def start_enabled(cls, config: ConfigParser, publisher: Any, stop_event: threading.Event) -> tuple[list[Source], dict[str, Source]]:
        """Start enabled sources and return them indexed by config section."""
        sources = []
        source_map = {}
        for name, source_type in cls.enabled_sources(config):
            source = source_type(config, publisher, stop_event, name)
            LOGGER.info("Starting source instance %s (%s)", name, source_type.source_name)
            source.start()
            sources.append(source)
            source_map[name] = source
        return sources, source_map

    @classmethod
    def run_until_stopped(cls, config: ConfigParser, publisher: Any, stop_event: threading.Event, sources: list[Source], source_map: dict[str, Source]) -> None:
        """Supervise source threads and restart failed sources."""
        while not stop_event.is_set():
            for index, source in enumerate(tuple(sources)):
                if source.is_alive() or stop_event.is_set():
                    continue
                source_type = type(source)
                LOGGER.error("Source %s stopped; restarting it", source.section_name)
                replacement = source_type(config, publisher, stop_event, source.section_name)
                replacement.start()
                sources[index] = replacement
                source_map[replacement.section_name] = replacement
            stop_event.wait(1)

    @staticmethod
    def wait_for(sources: list[Source], interval: float = 1) -> None:
        """Wait until all source threads have stopped."""
        while any(source.is_alive() for source in sources):
            time.sleep(interval)

    @abstractmethod
    def comms_check(self) -> dict[str, Any]:
        """Check local connectivity and configured device availability."""

    def __init__(self, config: ConfigParser, publisher: Any, stop_event: threading.Event, section_name: str | None = None) -> None:
        """Initialize a daemon source thread with shared configuration and shutdown state."""
        self.config = config
        self.section_name = section_name or self.config_section
        super().__init__(name=self.section_name, daemon=True)
        self.publisher = publisher
        self.stop_event = stop_event

    @property
    def section(self) -> Any:
        """Return this source instance's configuration section."""
        return self.config[self.section_name]

    @abstractmethod
    def run(self) -> None:
        """Collect readings until the source stops or fails."""

    def handle_command(self, payload: dict[str, Any]) -> None:
        """Handle a command received from MQTT, when supported."""
        LOGGER.warning("%s does not support write commands: %s", self.source_name, payload)

    def interrogate(self) -> dict[str, Any]:
        """Return a current source snapshot, when supported."""
        raise NotImplementedError(f"{self.source_name} does not support interrogation")