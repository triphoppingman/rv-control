from __future__ import annotations

import logging
import math
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
    supports_status_requests = False
    runtime_options: tuple[str, ...] = ()
    default_topic: str | None = None
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
                importlib.import_module(f".{name}", package="rv_control.sources")
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
    def validate_config(cls, config: ConfigParser, section_name: str) -> None:
        """Validate source-specific static settings without contacting hardware."""

    @classmethod
    def published_topic_root(cls, config: ConfigParser, section_name: str) -> str:
        """Describe this instance's telemetry root for runtime dependency validation."""
        return config[section_name].get("topic", cls.default_topic or section_name).strip().strip("/")

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
            try:
                results[name] = source_type(config, None, threading.Event(), name).comms_check()
            except ValueError as error:
                results[name] = {"ok": False, "message": f"Invalid source configuration: {error}"}
        return results

    @classmethod
    def interrogate_enabled(cls, config: ConfigParser) -> dict[str, dict[str, Any]]:
        """Interrogate every enabled source and return per-source results."""
        results = {}
        for name, source_type in cls.enabled_sources(config):
            try:
                source = source_type(config, None, threading.Event(), name)
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
    def run_until_stopped(
        cls, config: ConfigParser, publisher: Any, stop_event: threading.Event,
        sources: list[Source], source_map: dict[str, Source],
        on_tick: Callable[[], None] | None = None,
    ) -> None:
        """Supervise source threads and restart failed sources."""
        while not stop_event.is_set():
            if on_tick is not None:
                on_tick()
                if stop_event.is_set():
                    break
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

    def resolve_settings(self, settings: dict[str, str]) -> dict[str, Any]:
        """Resolve supported settings atomically using the shared telemetry context."""
        from ..runtime import RuntimeContext

        context = getattr(self.publisher, "runtime_context", None)
        if context is None:
            context = RuntimeContext(self.config)
        return context.resolve(settings)

    def configured_settings(self) -> dict[str, str]:
        """Return only settings explicitly opted into shared runtime resolution."""
        return {key: self.section.get(key, "").strip() for key in self.runtime_options}

    def validate_literal_settings(self, validator: Callable[[Any, str], Any]) -> None:
        """Validate static values at startup while deferring dynamic domain checks."""
        from ..runtime import Reference, parse_template

        for key, value in self.configured_settings().items():
            if not value:
                raise ValueError(f"[{self.section_name}] {key} is required")
            parts = parse_template(value)
            if not any(isinstance(part, Reference) for part in parts):
                validator("".join(part for part in parts if isinstance(part, str)), key)

    @abstractmethod
    def run(self) -> None:
        """Collect readings until the source stops or fails."""

    def handle_command(self, payload: dict[str, Any]) -> None:
        """Handle a command received from MQTT, when supported."""
        LOGGER.warning("%s does not support write commands: %s", self.source_name, payload)

    def command_ready(self) -> bool:
        """Report whether a running source supports acknowledged command dispatch."""
        return False

    def execute_command(self, payload: dict[str, Any]) -> None:
        """Dispatch a command or raise; never infer success from a legacy handler."""
        raise NotImplementedError(f"{self.source_name} has no reliable command interface")

    def request_status(self, dgns: list[int]) -> None:
        """Request mapped telemetry only on sources explicitly supporting safe reads."""
        raise NotImplementedError(f"{self.source_name} has no safe status request interface")

    def interrogate(self) -> dict[str, Any]:
        """Return a current source snapshot, when supported."""
        raise NotImplementedError(f"{self.source_name} does not support interrogation")


class PollingSource(Source):
    """Share per-operation settings, dependency waits and bounded polling retries."""

    polling_errors: tuple[type[Exception], ...] = (ValueError,)
    check_name = "Source"
    check_success = "Source is available"

    def __init__(
        self, config: ConfigParser, publisher: Any, stop_event: threading.Event,
        section_name: str | None = None,
    ) -> None:
        """Compile polling schedules without resolving live inputs or opening hardware."""
        super().__init__(config, publisher, stop_event, section_name)
        self.poll_interval = self.positive_setting("poll_interval", 900)
        self.retry_delay = self.positive_setting("retry_delay", 30)
        self.max_retry_delay = self.positive_setting("max_retry_delay", 900)
        if self.max_retry_delay < self.retry_delay:
            raise ValueError(f"[{self.section_name}] max_retry_delay must be >= retry_delay")

    def positive_setting(self, key: str, default: float) -> float:
        """Read a positive finite duration with a source-specific error location."""
        value = self.section.getfloat(key, fallback=default)
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"[{self.section_name}] {key} must be positive and finite")
        return value

    @abstractmethod
    def collect(self, settings: dict[str, Any]) -> dict[str, Any]:
        """Read one payload using resolved settings, with protocol/domain validation."""

    def interrogate(self) -> dict[str, Any]:
        """Resolve fresh settings for exactly one operation, without waiting."""
        return self.collect(self.resolve_settings(self.configured_settings()))

    def comms_check(self) -> dict[str, Any]:
        """Perform one bounded read, reporting missing inputs without starting producers."""
        try:
            self.interrogate()
        except self.polling_errors as error:
            return {"ok": False, "message": f"{self.check_name} check failed: {error}"}
        return {"ok": True, "message": self.check_success}

    def run(self) -> None:
        """Wait for dependencies and retry failures with interruptible capped backoff."""
        from ..runtime import VariablesUnavailable

        retry = self.retry_delay
        waiting = False
        while not self.stop_event.is_set():
            try:
                payload = self.interrogate()
            except VariablesUnavailable as error:
                if not waiting:
                    LOGGER.warning("%s waiting for telemetry: %s", self.section_name, error)
                waiting = True
                delay = min(5.0, self.poll_interval)
            except self.polling_errors as error:
                LOGGER.warning("%s read failed; retry in %.1fs: %s", self.section_name, retry, error)
                waiting = False
                delay = retry
                retry = min(self.max_retry_delay, retry * 2)
            else:
                self.publisher.publish(self.topic, payload)
                retry = self.retry_delay
                waiting = False
                delay = self.poll_interval
            self.stop_event.wait(delay)