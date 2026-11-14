"""Runtime enable/disable control and status reporting for configured sources.

The coach command service records operator overrides here; the main supervision
thread applies them so source threads are only created, stopped and joined from
one place. Overrides are kept in memory across configuration reloads but are not
persisted across a service restart.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable, Collection
from configparser import ConfigParser
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .sources.base import Source

LOGGER = logging.getLogger(__name__)
SECTION_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
SOURCE_ACTIONS = {"source_enable": True, "source_disable": False}
SOURCE_VERBS = {"source_enable": "enable", "source_disable": "disable"}
MAX_DURATION_S = 86400
MAX_REASON = 120
STATUS_TOPIC = "coach/sources"
START_RETRY_S = 30.0


def _timestamp(epoch: float | None) -> str | None:
    """Format an optional epoch as a UTC RFC 3339 instant for status payloads."""
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


def controllable_sections(config: ConfigParser) -> tuple[str, ...]:
    """Parse and validate the ``[coach_commands] source_control`` allowlist.

    Parameters
    ----------
    config : ConfigParser
        Loaded service configuration.

    Returns
    -------
    tuple[str, ...]
        Source sections MQTT controllers may enable or disable, in configured order.

    Raises
    ------
    ValueError
        If a listed section is duplicated, malformed, missing or has no known source type.
    """
    if not config.has_section("coach_commands"):
        return ()
    names = [name.strip() for name in config["coach_commands"].get("source_control", "").split(",") if name.strip()]
    if len(names) != len(set(names)):
        raise ValueError("[coach_commands] source_control contains duplicate sections")
    for name in names:
        if not SECTION_NAME.fullmatch(name):
            raise ValueError(f"Invalid source_control section name: {name!r}")
        if not config.has_section(name):
            raise ValueError(f"source_control section not found: {name}")
        source_type = config[name].get("type", "").strip()
        if not source_type:
            raise ValueError(f"Source section [{name}] requires type")
        Source.source_class(source_type)
    return tuple(names)


@dataclass(frozen=True)
class SourceOverride:
    """One operator decision that replaces a source's configured enablement."""

    enabled: bool
    until: float | None
    reason: str
    client_id: str
    changed_at: float

    def as_dict(self) -> dict[str, Any]:
        """Describe the override for status payloads with RFC 3339 timestamps."""
        return {
            "enabled": self.enabled, "until": _timestamp(self.until), "reason": self.reason,
            "client_id": self.client_id, "changed_at": _timestamp(self.changed_at),
        }


class SourceOverrides:
    """Thread-safe runtime overrides shared by every configuration generation."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        """Start with no overrides and a replaceable wall clock for expiry tests."""
        self.clock = clock
        self._lock = threading.Lock()
        self._items: dict[str, SourceOverride] = {}

    def set(self, section: str, override: SourceOverride | None) -> None:
        """Record or clear the override for one source section."""
        with self._lock:
            if override is None:
                self._items.pop(section, None)
            else:
                self._items[section] = override

    def get(self, section: str) -> SourceOverride | None:
        """Return an unexpired override, discarding it once its duration has elapsed."""
        with self._lock:
            override = self._items.get(section)
            if override is not None and override.until is not None and self.clock() >= override.until:
                del self._items[section]
                LOGGER.info("Source control override for %s expired; restoring configured state", section)
                return None
            return override

    def retain(self, sections: Collection[str]) -> None:
        """Drop overrides for sections a reloaded configuration no longer allows."""
        with self._lock:
            for section in set(self._items) - set(sections):
                LOGGER.info("Dropping source control override for %s; it is no longer controllable", section)
                del self._items[section]


class SourceController:
    """Apply source overrides from the supervision thread and describe source status."""

    def __init__(
        self,
        config: ConfigParser,
        overrides: SourceOverrides,
        controllable: Collection[str] = (),
        publish_status: Callable[[dict[str, Any]], None] | None = None,
        connected: Callable[[], bool] | None = None,
        on_started: Callable[[str, Source], None] | None = None,
    ) -> None:
        """Bind one configuration generation to the shared override store.

        Parameters
        ----------
        config : ConfigParser
            Active configuration generation.
        overrides : SourceOverrides
            Store that survives configuration reloads.
        controllable : Collection[str]
            Validated sections MQTT controllers may enable or disable.
        publish_status : Callable[[dict[str, Any]], None] | None
            Retained status publisher, or ``None`` to skip status publication.
        connected : Callable[[], bool] | None
            Broker connection probe used to republish after reconnects.
        on_started : Callable[[str, Source], None] | None
            Callback invoked after a re-enabled source thread starts.
        """
        self.config = config
        self.overrides = overrides
        self.controllable = tuple(controllable)
        self.baseline = tuple(name for name, _source_type in Source.enabled_sources(config))
        self.publish_status = publish_status
        self.connected = connected or (lambda: True)
        self.on_started = on_started
        self.clock = overrides.clock
        overrides.retain(self.controllable)
        self._lock = threading.RLock()
        self._publisher: Any = None
        self._stop_event: threading.Event | None = None
        self._sources: list[Source] = []
        self._source_map: dict[str, Source] = {}
        self._stopping: dict[str, Source] = {}
        self._retry_at: dict[str, float] = {}
        started = self.clock()
        self._since: dict[str, tuple[str, float]] = {}
        self._started = started
        self._published: dict[str, Any] | None = None
        self._was_connected = False

    @property
    def active(self) -> bool:
        """Return whether any source section is under MQTT control."""
        return bool(self.controllable)

    def desired(self, section: str) -> bool:
        """Return the effective enablement: an unexpired override, else configuration."""
        override = self.overrides.get(section)
        return override.enabled if override is not None else section in self.baseline

    def skipped(self) -> set[str]:
        """Return configured sources that start disabled because of a carried override."""
        return {section for section in self.controllable if section in self.baseline and not self.desired(section)}

    def attach(
        self, publisher: Any, stop_event: threading.Event,
        sources: list[Source], source_map: dict[str, Source],
    ) -> None:
        """Adopt the live supervision collections after initial sources start."""
        with self._lock:
            self._publisher = publisher
            self._stop_event = stop_event
            self._sources = sources
            self._source_map = source_map

    def request(
        self, section: str, enabled: bool, duration_s: int | None = None,
        reason: str = "", client_id: str = "",
    ) -> dict[str, Any]:
        """Record an operator decision for the supervision thread to apply.

        Setting the configured state without a duration clears the override.

        Returns
        -------
        dict[str, Any]
            Desired state, expiry and current lifecycle state for the result payload.
        """
        if section not in self.controllable:
            raise ValueError("Source is not controllable")
        now = self.clock()
        if duration_s is None and enabled == (section in self.baseline):
            self.overrides.set(section, None)
        else:
            until = now + duration_s if duration_s is not None else None
            self.overrides.set(section, SourceOverride(enabled, until, reason, client_id, now))
        LOGGER.info(
            "Source control: client=%s source=%s enabled=%s duration_s=%s", client_id or "-",
            section, enabled, duration_s,
        )
        override = self.overrides.get(section)
        return {
            "source": section, "enabled": enabled,
            "until": _timestamp(override.until) if override is not None else None,
            "state": self._state(section),
        }

    def stopping_sources(self) -> list[Source]:
        """Return disabled source threads that may still be shutting down."""
        with self._lock:
            return list(self._stopping.values())

    def tick(self) -> None:
        """Apply desired states and publish status changes from the supervision thread."""
        with self._lock:
            if self._stop_event is not None and not self._stop_event.is_set():
                for section in self.controllable:
                    self._apply(section)
            self._publish_if_changed()

    def _apply(self, section: str) -> None:
        """Stop a disabled source or start an enabled one once its old thread exits."""
        desired = self.desired(section)
        current = self._source_map.get(section)
        stopping = self._stopping.get(section)
        if stopping is not None and not stopping.is_alive():
            del self._stopping[section]
            stopping = None
            LOGGER.info("Source %s stopped", section)
        if not desired and current is not None:
            LOGGER.info("Disabling source %s", section)
            event = getattr(current, "stop_event", None)
            if isinstance(event, threading.Event) and event is not self._stop_event:
                event.set()
            else:
                LOGGER.error("Source %s has no private stop event; it cannot be disabled", section)
                return
            self._source_map.pop(section, None)
            self._sources[:] = [source for source in self._sources if source is not current]
            self._stopping[section] = current
        elif desired and current is None and stopping is None and self.clock() >= self._retry_at.get(section, 0.0):
            try:
                source = Source.create(section, self.config, self._publisher, threading.Event())
                source.start()
            except Exception:
                LOGGER.exception("Unable to start source %s; retrying in %.0fs", section, START_RETRY_S)
                self._retry_at[section] = self.clock() + START_RETRY_S
                return
            self._retry_at.pop(section, None)
            LOGGER.info("Enabled source %s", section)
            self._sources.append(source)
            self._source_map[section] = source
            if self.on_started is not None:
                try:
                    self.on_started(section, source)
                except Exception:
                    LOGGER.exception("Post-start hook failed for source %s", section)

    def _state(self, section: str) -> str:
        """Classify one section as running, restarting, starting, stopping or disabled."""
        with self._lock:
            current = self._source_map.get(section)
            stopping = self._stopping.get(section)
        if current is not None:
            return "running" if current.is_alive() else "restarting"
        if stopping is not None and stopping.is_alive():
            return "stopping"
        return "starting" if self.desired(section) else "disabled"

    def snapshot(self) -> dict[str, Any]:
        """Describe every configured or controllable source for dashboards."""
        with self._lock:
            sections = list(self.baseline) + [name for name in self.controllable if name not in self.baseline]
            entries = []
            for section in sections:
                state = self._state(section)
                previous = self._since.get(section)
                if previous is None or previous[0] != state:
                    self._since[section] = (state, self.clock() if previous is not None else self._started)
                override = self.overrides.get(section)
                entries.append({
                    "source": section,
                    "type": self.config[section].get("type", "").strip(),
                    "controllable": section in self.controllable,
                    "configured_enabled": section in self.baseline,
                    "enabled": self.desired(section),
                    "state": state,
                    "since": _timestamp(self._since[section][1]),
                    "override": override.as_dict() if override is not None else None,
                })
            return {"version": 1, "online": True, "sources": entries}

    def _publish_if_changed(self) -> None:
        """Publish retained status after a change or a broker reconnect."""
        if self.publish_status is None:
            return
        connected = self.connected()
        reconnected = connected and not self._was_connected
        self._was_connected = connected
        snapshot = self.snapshot()
        if not connected or (snapshot == self._published and not reconnected):
            return
        try:
            self.publish_status(snapshot)
        except Exception as error:
            LOGGER.warning("Unable to publish source status: %s", error)
            self._was_connected = False
            return
        self._published = snapshot

    def close(self) -> None:
        """Replace retained status with an offline marker during orderly shutdown."""
        if self.publish_status is None or not self.connected():
            return
        try:
            self.publish_status({"version": 1, "online": False, "sources": []})
        except Exception as error:
            LOGGER.warning("Unable to publish offline source status: %s", error)
