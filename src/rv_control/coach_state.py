"""Track coach entity state from decoded RV-C messages and publish it per entity."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from .coach import Coach, PLACEHOLDER_PATTERN

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
        if not isinstance(definition, dict) or "dgn" not in definition:
            raise ValueError(f"Entity {entity_name!r} state block needs a 'dgn'")
        if _contains_placeholder(definition):
            raise ValueError(f"Entity {entity_name!r} state block has unresolved placeholders: {definition}")
        self.entity_name = entity_name
        self.dgn = _parse_dgn(definition["dgn"])
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
        self._states: dict[str, dict[str, Any]] = {}
        self._last_published: dict[str, float] = {}
        self._by_instance: dict[tuple[int, Any], list[StateBlock]] = {}
        self._by_dgn: dict[int, list[StateBlock]] = {}
        for entity_name, entity in coach.entities.items():
            blocks = entity.get("state")
            if not blocks:
                continue
            for definition in blocks if isinstance(blocks, list) else [blocks]:
                block = StateBlock(entity_name, definition)
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
        return sorted(names)

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
            return dict(self._states.get(entity_name, {}))

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
        with self._lock:
            current = self._states.setdefault(entity_name, {})
            changed = any(current.get(key, object()) != value for key, value in values.items())
            current.update(values)
            last = self._last_published.get(entity_name)
            due = last is None or (self.republish_seconds > 0 and now - last >= self.republish_seconds)
            if not (changed or due):
                return
            self._last_published[entity_name] = now
            snapshot = dict(current)
        self.publish(f"{ENTITY_TOPIC_PREFIX}/{entity_name}", self._payload(entity_name, snapshot))

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
    """Publisher wrapper that forwards everything and feeds decoded RV-C messages to a tracker.

    Sources keep publishing their raw topics unchanged; any payload that looks
    like a decoded RV-C message (it has a ``dgn``) also updates coach entity
    state, which is published through the same wrapped publisher.
    """

    def __init__(self, publisher: Any, tracker_factory: Callable[[Publish], EntityStateTracker]) -> None:
        """Wrap a target publisher and build the tracker that publishes through it.

        Parameters
        ----------
        publisher : Any
            Target (or MultiTarget) with ``connect``, ``publish`` and ``close``.
        tracker_factory : Callable[[Publish], EntityStateTracker]
            Builds the tracker given the wrapped publisher's publish function.
        """
        self.publisher = publisher
        self.tracker = tracker_factory(lambda topic, payload: self.publisher.publish(topic, payload))

    def connect(self) -> None:
        """Connect the wrapped publisher."""
        self.publisher.connect()

    def publish(self, topic: str, payload: dict[str, Any]) -> None:
        """Forward a publish, then update coach entity state from RV-C payloads.

        Tracker failures are logged and never stop the raw publish path.

        Parameters
        ----------
        topic : str
            Relative topic.
        payload : dict[str, Any]
            JSON payload.
        """
        self.publisher.publish(topic, payload)
        if isinstance(payload, dict) and "dgn" in payload:
            try:
                self.tracker.handle(payload)
            except Exception:
                LOGGER.exception("Coach entity state update failed for %s", topic)

    def close(self) -> None:
        """Close the wrapped publisher."""
        self.publisher.close()

    def __getattr__(self, name: str) -> Any:
        """Delegate any other attribute to the wrapped publisher."""
        return getattr(self.publisher, name)


def wrap_publisher(config: Any, publisher: Any) -> Any:
    """Return a publisher that also reports coach entity state, when enabled in ``[coach]``.

    Entity state is enabled when ``config.ini`` has a ``[coach]`` section and
    ``publish_state`` is not false. ``state_republish_seconds`` (default 60)
    controls periodic republishing of unchanged state.

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
    if not section.getboolean("publish_state", fallback=True):
        return publisher
    republish = section.getfloat("state_republish_seconds", fallback=DEFAULT_REPUBLISH_SECONDS)
    coach = Coach.load(config)
    wrapped = CoachStatePublisher(publisher, lambda publish: EntityStateTracker(coach, publish, republish))
    LOGGER.info("Publishing coach entity state for %d entities", len(wrapped.tracker.tracked_entities))
    return wrapped
