from __future__ import annotations

import importlib
import logging
from abc import ABC, abstractmethod
from configparser import ConfigParser
from typing import Any, Callable, TypeVar

LOGGER = logging.getLogger(__name__)
TargetType = TypeVar("TargetType", bound="Target")
CommandHandler = Callable[[str, dict[str, Any]], None]


class Target(ABC):
    """Common lifecycle and publish interface for telemetry destinations."""

    target_name = "target"
    config_section = "target"
    _registry: dict[str, type[Target]] = {}

    def __init_subclass__(cls, *, target_name: str | None = None, **kwargs: Any) -> None:
        """Register each concrete target subclass under its configured name."""
        super().__init_subclass__(**kwargs)
        if target_name:
            cls.target_name = target_name
        if cls.target_name != "target":
            Target.register(cls.target_name, cls)

    def __init__(self, config: ConfigParser, command_handler: CommandHandler | None = None, section_name: str | None = None) -> None:
        """Store configuration, the optional inbound command callback, and this instance's section."""
        self.config = config
        self.command_handler = command_handler
        self.section_name = section_name or self.config_section

    @classmethod
    def register(cls, name: str, target_type: type[TargetType]) -> None:
        """Register a target class under its configuration name."""
        if not name or not issubclass(target_type, cls):
            raise ValueError("target name and Target subclass are required")
        cls._registry[name] = target_type

    @classmethod
    def target_class(cls, name: str) -> type[Target]:
        """Return the registered target class for a name, importing its module if needed."""
        if name not in cls._registry:
            try:
                importlib.import_module(f".{name}", package="rv_control.targets")
            except ImportError:
                pass
        try:
            return cls._registry[name]
        except KeyError as error:
            raise ValueError(f"Unknown target: {name}") from error

    @classmethod
    def list_targets(cls) -> tuple[str, ...]:
        """Return registered target names in stable order."""
        return tuple(sorted(cls._registry))

    @classmethod
    def enabled_targets(cls, config: ConfigParser) -> tuple[tuple[str, type[Target]], ...]:
        """Return enabled targets as section names and target classes.

        Uses [target] enabled-targets (section names, each with a type). When
        [target] is absent, falls back to the legacy [service] targets list of
        type names, defaulting to mqtt.
        """
        if config.has_section("target"):
            names = [name.strip() for name in config["target"].get("enabled-targets", "").split(",") if name.strip()]
            if len(names) != len(set(names)):
                raise ValueError("[target] enabled-targets contains duplicate sections")
            targets = []
            for name in names:
                if not config.has_section(name):
                    raise ValueError(f"Enabled target section not found: {name}")
                type_name = config[name].get("type", "").strip()
                if not type_name:
                    raise ValueError(f"Target section [{name}] requires type")
                targets.append((name, cls.target_class(type_name)))
            return tuple(targets)
        raw = config.get("service", "targets", fallback="mqtt") if config.has_section("service") else "mqtt"
        names = [name.strip() for name in raw.split(",") if name.strip()] or ["mqtt"]
        return tuple((name, cls.target_class(name)) for name in dict.fromkeys(names))

    @classmethod
    def section_for_type(cls, config: ConfigParser, type_name: str) -> str | None:
        """Return the first enabled target section of a type, or None."""
        for name, target_type in cls.enabled_targets(config):
            if target_type.target_name == type_name:
                return name
        return None

    @classmethod
    def build(cls, config: ConfigParser, command_handler: CommandHandler | None = None) -> Target:
        """Create the configured targets behind a single publish interface."""
        targets = [target_type(config, command_handler, name) for name, target_type in cls.enabled_targets(config)]
        if not targets:
            raise ValueError("No targets are enabled")
        return targets[0] if len(targets) == 1 else MultiTarget(config, targets)

    @abstractmethod
    def connect(self) -> None:
        """Open the destination and begin any background work."""

    @abstractmethod
    def publish(self, topic: str, payload: dict[str, Any]) -> None:
        """Deliver one JSON-serializable payload for a topic."""

    @abstractmethod
    def close(self) -> None:
        """Flush pending data and release the destination."""


class MultiTarget(Target):
    """Fan out every publish to several targets, isolating individual failures."""

    def __init__(self, config: ConfigParser, targets: list[Target]) -> None:
        """Wrap already-constructed targets."""
        super().__init__(config)
        self.targets = targets

    def connect(self) -> None:
        """Connect every target, closing those already opened if one fails."""
        opened: list[Target] = []
        try:
            for target in self.targets:
                target.connect()
                opened.append(target)
        except BaseException:
            for target in opened:
                self._safe_close(target)
            raise

    def publish(self, topic: str, payload: dict[str, Any]) -> None:
        """Publish to each target; log and continue when one fails."""
        for target in self.targets:
            try:
                target.publish(topic, payload)
            except Exception:
                LOGGER.exception("Target %s failed to publish %s", target.target_name, topic)

    def close(self) -> None:
        """Close every target even if some fail."""
        for target in self.targets:
            self._safe_close(target)

    @staticmethod
    def _safe_close(target: Target) -> None:
        """Close one target, logging instead of raising."""
        try:
            target.close()
        except Exception:
            LOGGER.exception("Target %s failed to close", target.target_name)
