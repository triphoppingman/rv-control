"""Track coach entity state from source telemetry and publish it per entity."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from .coach import Coach, PLACEHOLDER_PATTERN
from .sources.base import Source

LOGGER = logging.getLogger(__name__)
ENTITY_TOPIC_PREFIX = "coach/entity"
DEFAULT_REPUBLISH_SECONDS = 60.0

Publish = Callable[[str, dict[str, Any]], None]


def _contains_placeholder(value: Any) -> bool:
    """Return True when a nested state value still holds an unresolved ``{name}`` placeholder.

    Parameters
    ----------
    value : Any
        Scalar, list, or mapping from a resolved state block.

    Returns
    -------
    bool
        Whether any string inside ``value`` contains a placeholder.
    """
    if isinstance(value, dict):
        return any(_contains_placeholder(key) or _contains_placeholder(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_placeholder(item) for item in value)
    return isinstance(value, str) and PLACEHOLDER_PATTERN.search(value) is not None


def _parse_dgn(value: Any) -> int:
    """Convert a DGN given as an integer, ``0x`` string, or decoder hex string to an integer.

    Parameters
    ----------
    value : Any
        DGN from the coach YAML (integer) or decoder output (hex string such as ``"1FEDA"``).

    Returns
    -------
    int
        DGN number.
    """
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    text = str(value).strip()
    return int(text, 0) if text.lower().startswith("0x") else int(text, 16)


class StateBlock:
    """One compiled entity state mapping: which messages it accepts and how to convert their fields."""

    def __init__(self, entity_name: str, definition: dict[str, Any]) -> None:
        """Validate and compile a state block from a resolved coach entity.

        Parameters
        ----------
        entity_name : str
            Entity the block belongs to, used in error messages.
        definition : dict[str, Any]
            Block with ``dgn``, optional ``match`` and ``fields``.
        """
        if not isinstance(definition, dict):
            raise ValueError(f"Entity {entity_name!r} state block must be a mapping")
        if _contains_placeholder(definition):
            raise ValueError(f"Entity {entity_name!r} state block has unresolved placeholders: {definition}")
        self.entity_name = entity_name
        self.dgn = _parse_dgn(definition["dgn"]) if "dgn" in definition else None
        self.topic_suffix = definition.get("topic_suffix", "")
        if not isinstance(self.topic_suffix, str):
            raise ValueError(f"Entity {entity_name!r} state topic_suffix must be a string")
        if self.dgn is not None and "topic_suffix" in definition:
            raise ValueError(f"Entity {entity_name!r} state cannot combine dgn and topic_suffix")
        self.match: dict[str, set[Any]] = {}
        for key, expected in (definition.get("match") or {}).items():
            values = expected if isinstance(expected, list) else [expected]
            self.match[str(key)] = set(values)
        fields = definition.get("fields") or {}
        if not isinstance(fields, dict) or not fields:
            raise ValueError(f"Entity {entity_name!r} state block needs 'fields'")
        self.fields: dict[str, dict[str, Any]] = {}
        for attribute, spec in fields.items():
            spec_dict = {"field": spec} if isinstance(spec, str) else dict(spec)
            if "field" not in spec_dict:
                raise ValueError(f"Entity {entity_name!r} state field {attribute!r} needs a 'field'")
            self.fields[str(attribute)] = spec_dict

    @property
    def instances(self) -> set[Any] | None:
        """Return the instances this block matches, or None when it matches any instance."""
        return self.match.get("instance")

    def matches(self, decoded: dict[str, Any]) -> bool:
        """Return True when every ``match`` field of the block equals the decoded message's value.

        Parameters
        ----------
        decoded : dict[str, Any]
            Decoded RV-C message.

        Returns
        -------
        bool
            Whether the message belongs to this block.
        """
        return all(decoded.get(key) in allowed for key, allowed in self.match.items())

    def extract(self, decoded: dict[str, Any]) -> dict[str, Any]:
        """Convert the block's fields from a decoded message into entity attributes.

        Fields absent from the message are skipped; fields that cannot be
        converted are logged once per call and skipped.

        Parameters
        ----------
        decoded : dict[str, Any]
            Decoded RV-C message accepted by :meth:`matches`.

        Returns
        -------
        dict[str, Any]
            Entity attribute values.
        """
        values: dict[str, Any] = {}
        for attribute, spec in self.fields.items():
            raw = decoded.get(spec["field"])
            if raw is None:
                continue
            try:
                values[attribute] = self._convert(raw, spec, decoded)
            except (TypeError, ValueError, ZeroDivisionError) as error:
                LOGGER.debug("Entity %s field %s not converted: %s", self.entity_name, attribute, error)
        return values

    @staticmethod
    def _convert(raw: Any, spec: dict[str, Any], decoded: dict[str, Any]) -> Any:
        """Apply invalid, divide_by, scale, offset, round and map to one raw value.

        Parameters
        ----------
        raw : Any
            Raw decoded field value.
        spec : dict[str, Any]
            Field conversion settings.
        decoded : dict[str, Any]
            Whole decoded message, used for ``divide_by`` field references.

        Returns
        -------
        Any
            Converted value, or None when ``raw`` is listed as invalid.
        """
        if raw in (spec.get("invalid") or []):
            return None
        value = raw
        numeric = any(key in spec for key in ("divide_by", "scale", "offset", "round"))
        if numeric:
            value = float(raw)
            divisor = spec.get("divide_by")
            if divisor is not None:
                divisor_value = decoded.get(divisor) if isinstance(divisor, str) else divisor
                if divisor_value is None:
                    raise ValueError(f"divide_by field {divisor!r} missing")
                value /= float(divisor_value)
            value = value * float(spec.get("scale", 1)) + float(spec.get("offset", 0))
            if "round" in spec:
                digits = int(spec["round"])
                value = round(value, digits) if digits > 0 else int(round(value))
        mapping = spec.get("map")
        if isinstance(mapping, dict):
            key = int(value) if isinstance(value, float) and value.is_integer() else value
            return mapping.get(key, value)
        return value


class EntityStateTracker:
    """Maintain the latest state of every coach entity and publish it on change.

    Each entity's state is published to ``coach/entity/<entity>`` (beneath the
    target's base topic) when any attribute changes, and again after
    ``republish_seconds`` without a change so late subscribers catch up.
    """

    def __init__(
        self,
        coach: Coach,
        publish: Publish,
        republish_seconds: float = DEFAULT_REPUBLISH_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Compile every entity state block into a DGN/instance lookup table.

        Parameters
        ----------
        coach : Coach
            Loaded coach with resolved entities.
        publish : Publish
            Callback receiving a relative topic and JSON payload.
        republish_seconds : float
            Seconds between republishing unchanged state; 0 disables republishing.
        clock : Callable[[], float]
            Monotonic clock, replaceable in tests.
        """
        if republish_seconds < 0:
            raise ValueError("republish_seconds must not be negative")
        self.coach = coach
        self.publish = publish
        self.republish_seconds = republish_seconds
        self.clock = clock
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._publish_lock = threading.Lock()
        self._states: dict[str, dict[str, Any]] = {}
        self._last_published: dict[str, float] = {}
        self._field_generations: dict[str, dict[str, int]] = {}
        self._field_observed_at: dict[str, dict[str, str]] = {}
        self._field_observed_monotonic: dict[str, dict[str, float]] = {}
        self._payloads: dict[str, dict[str, Any]] = {}
        self._by_instance: dict[tuple[int, Any], list[StateBlock]] = {}
        self._by_dgn: dict[int, list[StateBlock]] = {}
        self._by_topic: dict[str, list[StateBlock]] = {}
        for entity_name, entity in coach.entities.items():
            blocks = entity.get("state")
            if not blocks:
                continue
            for definition in blocks if isinstance(blocks, list) else [blocks]:
                block = StateBlock(entity_name, definition)
                if block.dgn is None:
                    section = coach.resolve_config_section(entity, entity_name)
                    if not coach.config.has_section(section):
                        raise ValueError(f"Config section [{section}] not found for entity {entity_name!r} state")
                    source_type = Source.source_class(
                        coach.config[section].get("type", str(entity.get("transport", "")))
                    )
                    root = source_type.published_topic_root(coach.config, section)
                    topic = "/".join(part for part in (root, block.topic_suffix.strip("/")) if part)
                    if not topic or topic.startswith(f"{ENTITY_TOPIC_PREFIX}/"):
                        raise ValueError(f"Entity {entity_name!r} state needs a non-entity source topic")
                    self._by_topic.setdefault(topic, []).append(block)
                    continue
                instances = block.instances
                if instances is None:
                    self._by_dgn.setdefault(block.dgn, []).append(block)
                else:
                    for instance in instances:
                        self._by_instance.setdefault((block.dgn, instance), []).append(block)

    @property
    def tracked_entities(self) -> list[str]:
        """Return the names of entities that have at least one state block."""
        names = {block.entity_name for blocks in self._by_instance.values() for block in blocks}
        names.update(block.entity_name for blocks in self._by_dgn.values() for block in blocks)
        names.update(block.entity_name for blocks in self._by_topic.values() for block in blocks)
        return sorted(names)

    def monitored_fields(self) -> dict[str, list[str]]:
        """Return sorted state attribute names for every monitored entity."""
        fields: dict[str, set[str]] = {}
        for blocks in (*self._by_instance.values(), *self._by_dgn.values(), *self._by_topic.values()):
            for block in blocks:
                fields.setdefault(block.entity_name, set()).update(block.fields)
        return {entity: sorted(names) for entity, names in sorted(fields.items())}

    def entity_status_requests(self, entity_names: list[str] | None = None) -> dict[str, dict[str, list[int]]]:
        """Return source-defined status identifiers per entity and source section."""
        selected = set(entity_names) if entity_names is not None else set(self.tracked_entities)
        unknown = selected.difference(self.tracked_entities)
        if unknown:
            raise ValueError(f"Unknown monitored entities: {', '.join(sorted(unknown))}")
        requests: dict[str, dict[str, set[int]]] = {name: {} for name in selected}
        blocks = [
            block for group in (
                *self._by_instance.values(), *self._by_dgn.values(), *self._by_topic.values()
            ) for block in group
        ]
        for block in blocks:
            if block.entity_name not in selected:
                continue
            entity = self.coach.entities[block.entity_name]
            try:
                section = self.coach.resolve_config_section(entity, block.entity_name)
            except ValueError as error:
                LOGGER.warning("No status request for entity %s: %s", block.entity_name, error)
                continue
            status_ids = requests[block.entity_name].setdefault(section, set())
            if block.dgn is not None:
                status_ids.add(block.dgn)
        return {
            entity: {section: sorted(status_ids) for section, status_ids in sections.items()}
            for entity, sections in requests.items()
        }

    def status_requests(self, entity_names: list[str] | None = None) -> dict[str, list[int]]:
        """Return the source-defined status identifiers required per config section.

        Parameters
        ----------
        entity_names : list[str] | None
            Optional entity selection; ``None`` selects every monitored entity.

        Returns
        -------
        dict[str, list[int]]
            Sorted status identifiers keyed by source section; empty for whole-source reads.
        """
        requests: dict[str, set[int]] = {}
        for sections in self.entity_status_requests(entity_names).values():
            for section, status_ids in sections.items():
                requests.setdefault(section, set()).update(status_ids)
        return {section: sorted(status_ids) for section, status_ids in requests.items()}

    def state(self, entity_name: str) -> dict[str, Any]:
        """Return a copy of the latest known attributes of one entity.

        Parameters
        ----------
        entity_name : str
            Entity name.

        Returns
        -------
        dict[str, Any]
            Latest attribute values; empty when nothing has been received yet.
        """
        with self._lock:
            return deepcopy(self._states.get(entity_name, {}))

    def observation_baseline(self, entity_names: list[str]) -> dict[str, dict[str, int]]:
        """Capture per-field observation generations before a refresh dispatch."""
        fields = self.monitored_fields()
        unknown = set(entity_names).difference(fields)
        if unknown:
            raise ValueError(f"Unknown monitored entities: {', '.join(sorted(unknown))}")
        with self._lock:
            return {
                entity: {
                    field: self._field_generations.get(entity, {}).get(field, 0)
                    for field in fields[entity]
                }
                for entity in entity_names
            }

    def observed_fields_since(
        self,
        baseline: dict[str, dict[str, int]],
        entity_names: list[str] | None = None,
        observed_before: float | None = None,
    ) -> dict[str, list[str]]:
        """Return fields newly observed after baseline and before an optional deadline."""
        selected = list(baseline) if entity_names is None else entity_names
        with self._lock:
            return {
                entity: sorted(
                    field
                    for field, generation in self._field_generations.get(entity, {}).items()
                    if generation > baseline.get(entity, {}).get(field, 0)
                    and (
                        observed_before is None
                        or self._field_observed_monotonic.get(entity, {}).get(field, float("inf"))
                        <= observed_before
                    )
                )
                for entity in selected
            }

    def wait_for_observations(
        self,
        baseline: dict[str, dict[str, int]],
        required_fields: dict[str, list[str]],
        timeout_s: float,
    ) -> bool:
        """Wait at most ``timeout_s`` until every required field is newly observed."""
        deadline = time.monotonic() + max(0.0, timeout_s)

        def complete() -> bool:
            """Check whether every selected field advanced past its baseline."""
            return all(
                self._field_generations.get(entity, {}).get(field, 0)
                > baseline.get(entity, {}).get(field, 0)
                for entity, names in required_fields.items()
                for field in names
            )

        with self._condition:
            return self._condition.wait_for(complete, timeout=max(0.0, deadline - time.monotonic()))

    def snapshots(self, entity_names: list[str] | None = None) -> dict[str, dict[str, Any]]:
        """Return cached entity states and per-field observation timestamps."""
        fields = self.monitored_fields()
        selected = list(fields) if entity_names is None else list(entity_names)
        unknown = set(selected).difference(fields)
        if unknown:
            raise ValueError(f"Unknown monitored entities: {', '.join(sorted(unknown))}")
        with self._lock:
            result: dict[str, dict[str, Any]] = {}
            for entity in selected:
                state = deepcopy(self._states.get(entity, {}))
                observed_at = {
                    field: self._field_observed_at.get(entity, {}).get(field)
                    for field in fields[entity]
                }
                result[entity] = {
                    "entity": entity,
                    "state": state,
                    "observed_at": observed_at,
                    "missing_fields": [field for field in fields[entity] if field not in state],
                }
            return result

    def republish_states(self, entity_names: list[str]) -> dict[str, list[str]]:
        """Republish cached payloads without changing their original timestamps."""
        unknown = set(entity_names).difference(self.monitored_fields())
        if unknown:
            raise ValueError(f"Unknown monitored entities: {', '.join(sorted(unknown))}")
        published: list[str] = []
        missing: list[str] = []
        with self._publish_lock:
            with self._lock:
                payloads = {
                    entity: deepcopy(self._payloads[entity])
                    for entity in entity_names
                    if entity in self._payloads
                }
            for entity in entity_names:
                payload = payloads.get(entity)
                if payload is None:
                    missing.append(entity)
                    continue
                self.publish(f"{ENTITY_TOPIC_PREFIX}/{entity}", payload)
                published.append(entity)
        return {"published": published, "missing": missing}

    def handle(self, decoded: dict[str, Any]) -> None:
        """Update entity state from one decoded RV-C message and publish changed entities.

        Messages without a ``dgn`` or that no entity maps are ignored.

        Parameters
        ----------
        decoded : dict[str, Any]
            Decoded RV-C message.
        """
        if "dgn" not in decoded:
            return
        try:
            dgn = _parse_dgn(decoded["dgn"])
        except (TypeError, ValueError):
            return
        blocks = self._by_instance.get((dgn, decoded.get("instance")), []) + self._by_dgn.get(dgn, [])
        if not blocks:
            return
        updates: dict[str, dict[str, Any]] = {}
        for block in blocks:
            if block.matches(decoded):
                updates.setdefault(block.entity_name, {}).update(block.extract(decoded))
        for entity_name, values in updates.items():
            if values:
                self._apply(entity_name, values)

    def handle_topic(self, topic: str, payload: dict[str, Any]) -> None:
        """Update mapped entities from a source's existing periodic publications."""
        updates: dict[str, dict[str, Any]] = {}
        for block in self._by_topic.get(topic.strip("/"), []):
            if block.matches(payload):
                updates.setdefault(block.entity_name, {}).update(block.extract(payload))
        for entity_name, values in updates.items():
            if values:
                self._apply(entity_name, values)

    def _apply(self, entity_name: str, values: dict[str, Any]) -> None:
        """Merge new attributes into an entity and publish when changed or due for republish.

        Parameters
        ----------
        entity_name : str
            Entity name.
        values : dict[str, Any]
            Newly decoded attribute values.
        """
        now = self.clock()
        observed_at = datetime.now(timezone.utc).isoformat()
        observed_monotonic = time.monotonic()
        with self._publish_lock:
            with self._condition:
                current = self._states.setdefault(entity_name, {})
                changed = any(current.get(key, object()) != value for key, value in values.items())
                current.update(values)
                generations = self._field_generations.setdefault(entity_name, {})
                timestamps = self._field_observed_at.setdefault(entity_name, {})
                monotonic_timestamps = self._field_observed_monotonic.setdefault(entity_name, {})
                for field in values:
                    generations[field] = generations.get(field, 0) + 1
                    timestamps[field] = observed_at
                    monotonic_timestamps[field] = observed_monotonic
                last = self._last_published.get(entity_name)
                due = last is None or (self.republish_seconds > 0 and now - last >= self.republish_seconds)
                self._condition.notify_all()
                if not (changed or due):
                    return
                self._last_published[entity_name] = now
                snapshot = deepcopy(current)
                payload = self._payload(entity_name, snapshot)
                self._payloads[entity_name] = deepcopy(payload)
            self.publish(f"{ENTITY_TOPIC_PREFIX}/{entity_name}", payload)

    def _payload(self, entity_name: str, state: dict[str, Any]) -> dict[str, Any]:
        """Build the published JSON payload: entity metadata plus its state attributes.

        Parameters
        ----------
        entity_name : str
            Entity name.
        state : dict[str, Any]
            Entity attribute values.

        Returns
        -------
        dict[str, Any]
            Payload with ``entity``, ``name``, ``kind``, ``area``, ``state`` and ``updated``.
        """
        entity = self.coach.entities.get(entity_name, {})
        return {
            "entity": entity_name,
            "name": entity.get("name", entity_name),
            "kind": entity.get("kind", ""),
            "area": entity.get("area", ""),
            "state": state,
            "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }


class CoachStatePublisher:
    """Forward raw telemetry and feed source snapshots and RV-C messages to a tracker.

    Sources keep publishing their raw topics unchanged; mapped source topics
    and decoded RV-C messages also update semantic entity state.
    """

    def __init__(
        self,
        publisher: Any,
        tracker_factory: Callable[[Publish], EntityStateTracker],
        request_status_on_start: bool = True,
    ) -> None:
        """Wrap a target publisher and build the tracker that publishes through it.

        Parameters
        ----------
        publisher : Any
            Target (or MultiTarget) with ``connect``, ``publish`` and ``close``.
        tracker_factory : Callable[[Publish], EntityStateTracker]
            Builds the tracker given the wrapped publisher's publish function.
        request_status_on_start : bool
            Whether :meth:`request_initial_status` asks sources for current status.
        """
        self.publisher = publisher
        self.request_status_on_start = request_status_on_start
        self._pending_entity_publications = threading.local()
        self.tracker = tracker_factory(
            self._publish_entity
        )

    def connect(self) -> None:
        """Connect the wrapped publisher."""
        self.publisher.connect()

    def publish(self, topic: str, payload: dict[str, Any]) -> None:
        """Process unattributed telemetry through the coach tracker and destination.

        Tracker failures are logged and never stop the raw publish path.

        Parameters
        ----------
        topic : str
            Relative topic.
        payload : dict[str, Any]
            JSON payload.
        """
        self._publish(topic, payload, "runtime", "runtime")

    def publish_source(
        self, topic: str, payload: dict[str, Any], source_type: str, source_section: str,
    ) -> None:
        """Update coach state synchronously and enqueue the attributed raw publication."""
        self._publish(topic, payload, source_type, source_section)

    def _publish(
        self, topic: str, payload: dict[str, Any], source_type: str, source_section: str,
    ) -> None:
        """Update semantic state before forwarding one publication with its origin."""
        entity_publications: list[tuple[str, dict[str, Any]]] = []
        if isinstance(payload, dict):
            self._pending_entity_publications.publications = entity_publications
            try:
                if "dgn" in payload:
                    self.tracker.handle(payload)
                self.tracker.handle_topic(topic, payload)
            except Exception:
                LOGGER.exception("Coach entity state update failed for %s", topic)
            finally:
                del self._pending_entity_publications.publications
        publish_source = getattr(self.publisher, "publish_source", None)
        if callable(publish_source):
            publish_source(topic, payload, source_type, source_section)
        else:
            self.publisher.publish(topic, payload)
        for entity_topic, entity_payload in entity_publications:
            self._publish_to_target(entity_topic, entity_payload, "coach", "coach")

    def _publish_entity(self, topic: str, payload: dict[str, Any]) -> None:
        """Stage tracked entity messages behind their triggering telemetry publication."""
        pending = getattr(self._pending_entity_publications, "publications", None)
        if pending is not None:
            pending.append((topic, payload))
            return
        self._publish_to_target(topic, payload, "coach", "coach")

    def _publish_to_target(
        self, topic: str, payload: dict[str, Any], source_type: str, source_section: str,
    ) -> None:
        """Forward telemetry with provenance when the destination supports it."""
        publish_source = getattr(self.publisher, "publish_source", None)
        if callable(publish_source):
            publish_source(topic, payload, source_type, source_section)
        else:
            self.publisher.publish(topic, payload)

    def request_initial_status(self, source_map: dict[str, Any]) -> None:
        """Ask running sources once for the mapped telemetry coach entities need.

        Loads that are off are only reported when they change, so without this
        request they stay unknown until toggled. Sources without
        ``request_status`` support are skipped.

        Parameters
        ----------
        source_map : dict[str, Any]
            Started sources keyed by config section.
        """
        if not self.request_status_on_start:
            return
        for section, status_ids in self.tracker.status_requests().items():
            source = source_map.get(section)
            request = getattr(source, "request_status", None)
            if getattr(source, "supports_status_requests", False) is not True or not callable(request):
                LOGGER.debug("Source [%s] cannot request status; skipping", section)
                continue
            try:
                request(status_ids)
            except (ValueError, OSError) as error:
                LOGGER.warning("Coach status request to [%s] failed: %s", section, error)

    def close(self) -> None:
        """Close the wrapped publisher."""
        self.publisher.close()

    def __getattr__(self, name: str) -> Any:
        """Delegate any other attribute to the wrapped publisher."""
        return getattr(self.publisher, name)


def wrap_publisher(config: Any, publisher: Any) -> Any:
    """Return a publisher with a coach tracker when publishing or the query API needs one.

    Entity state is enabled when ``config.ini`` has a ``[coach]`` section and
    ``publish_state`` is not false. When publishing is disabled but the coach
    MQTT API is enabled, a tracker is still created with entity publishing
    disabled so snapshots and refresh observations remain available.
    ``state_republish_seconds`` (default 60) controls periodic republishing of
    unchanged state, and ``request_status_on_start`` (default true) asks RV-C
    nodes for their current status once the sources start.

    Parameters
    ----------
    config : Any
        Loaded application configuration.
    publisher : Any
        Target publisher built for the daemon.

    Returns
    -------
    Any
        ``CoachStatePublisher`` wrapping ``publisher``, or ``publisher`` unchanged.
    """
    if not config.has_section("coach"):
        return publisher
    section = config["coach"]
    publish_state = section.getboolean("publish_state", fallback=True)
    api_enabled = config.has_section("coach_commands") and (
        config["coach_commands"].getboolean("enabled", fallback=False)
        or config["coach_commands"].getboolean("queries_enabled", fallback=False)
    )
    if not publish_state and not api_enabled:
        return publisher
    republish = section.getfloat("state_republish_seconds", fallback=DEFAULT_REPUBLISH_SECONDS)
    request_status = section.getboolean("request_status_on_start", fallback=True)
    coach = Coach.load(config)

    def tracker_factory(publish_entity: Publish) -> EntityStateTracker:
        """Build the tracker with the wrapper's ordered entity-publication callback."""
        def publish_if_enabled(topic: str, payload: dict[str, Any]) -> None:
            """Publish entity state only when state telemetry publishing is enabled."""
            if publish_state:
                publish_entity(topic, payload)

        return EntityStateTracker(coach, publish_if_enabled, republish)

    wrapped = CoachStatePublisher(
        publisher, tracker_factory, request_status
    )
    LOGGER.info("Publishing coach entity state for %d entities", len(wrapped.tracker.tracked_entities))
    return wrapped
