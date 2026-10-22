"""Bind published telemetry to safe, freshness-aware runtime variables."""

from __future__ import annotations

import json
import logging
import math
import operator
import re
import threading
import time
from collections.abc import Callable, Mapping
from configparser import ConfigParser
from dataclasses import dataclass
from typing import Any

LOGGER = logging.getLogger(__name__)
NAME = r"[A-Za-z_][A-Za-z_0-9]*"
PATH = rf"{NAME}(?:\.{NAME})*"
TOKEN = re.compile(rf"\$(?:\{{({PATH})\}}|({NAME})|(\$))")
PREDICATE = re.compile(rf"^\s*({PATH})\s*(==|!=|>=|<=|>|<)\s*(.+?)\s*$")
COMPARISONS = {
    "==": operator.eq, "!=": operator.ne, ">=": operator.ge,
    "<=": operator.le, ">": operator.gt, "<": operator.lt,
}
Scalar = str | int | float | bool


class VariablesUnavailable(ValueError):
    """Indicate that required telemetry has not arrived or is no longer fresh."""


@dataclass(frozen=True)
class Reference:
    """Identify one variable without confusing it with literal template text."""

    name: str


def parse_template(text: str) -> tuple[str | Reference, ...]:
    """Parse Bash-like references and $$ literals without any shell evaluation."""
    parts: list[str | Reference] = []
    position = 0
    for match in TOKEN.finditer(text):
        literal = text[position:match.start()]
        if "$" in literal:
            raise ValueError(f"Invalid runtime variable syntax: {text!r}")
        parts.append(literal)
        parts.append("$" if match[3] else Reference(match[1] or match[2]))
        position = match.end()
    tail = text[position:]
    if "$" in tail:
        raise ValueError(f"Invalid runtime variable syntax: {text!r}")
    parts.append(tail)
    return tuple(part for part in parts if part != "")


def _field(payload: Mapping[str, Any], path: str) -> Any:
    """Read a dotted object path without evaluating expressions or attributes."""
    value: Any = payload
    for name in path.split("."):
        if not isinstance(value, Mapping) or name not in value:
            return None
        value = value[name]
    return value


def _scalar(value: Any) -> bool:
    """Accept only non-null JSON scalars, excluding non-finite numbers."""
    return isinstance(value, (str, int, float, bool)) and (
        not isinstance(value, float) or math.isfinite(value)
    )


class Binding:
    """Compile one atomic group of telemetry fields and its validity predicate."""

    def __init__(self, namespace: str, section: Mapping[str, str]) -> None:
        """Validate a binding once, before any source starts."""
        self.namespace = namespace
        self.topic = section.get("topic", "").strip().strip("/")
        if not self.topic or any(char in self.topic for char in ("$", "+", "#")):
            raise ValueError("Variable binding topic must be a literal, exact telemetry topic")
        self.max_age = float(section.get("max_age", "300"))
        if not math.isfinite(self.max_age) or self.max_age <= 0:
            raise ValueError("Variable binding max_age must be positive and finite")
        self.fields: dict[str, str] = {}
        for entry in section.get("fields", "").split(","):
            alias, separator, path = entry.strip().partition(":")
            alias, path = alias.strip(), path.strip()
            if not separator or not re.fullmatch(NAME, alias) or not re.fullmatch(PATH, path):
                raise ValueError("Variable fields must be comma-separated name:payload.path pairs")
            name = f"{namespace}.{alias}" if namespace else alias
            if name in self.fields:
                raise ValueError(f"Duplicate runtime variable: {name}")
            self.fields[name] = path
        self.predicate: tuple[str, str, Scalar] | None = None
        requirement = section.get("require", "").strip()
        if requirement:
            match = PREDICATE.fullmatch(requirement)
            if not match:
                raise ValueError("Variable require must compare a payload field with a JSON scalar")
            expected = json.loads(match[3])
            if not _scalar(expected):
                raise ValueError("Variable require needs a non-null, finite JSON scalar")
            self.predicate = (match[1], match[2], expected)

    def extract(self, payload: Mapping[str, Any]) -> dict[str, Scalar] | None:
        """Return a complete valid field group, or invalidate the entire group."""
        if self.predicate:
            path, comparison, expected = self.predicate
            actual = _field(payload, path)
            numeric = (
                isinstance(actual, (int, float)) and not isinstance(actual, bool)
                and isinstance(expected, (int, float)) and not isinstance(expected, bool)
            )
            if not _scalar(actual) or not (numeric or type(actual) is type(expected)):
                return None
            if not COMPARISONS[comparison](actual, expected):
                return None
        values = {name: _field(payload, path) for name, path in self.fields.items()}
        return values if all(_scalar(value) for value in values.values()) else None


class RuntimeContext:
    """Maintain atomic variable groups independently of producers and consumers."""

    def __init__(self, config: ConfigParser, clock: Callable[[], float] = time.monotonic) -> None:
        """Compile bindings without reading hardware, environment variables, or files."""
        self.bindings: list[Binding] = []
        self._names: dict[str, Binding] = {}
        self._values: dict[str, tuple[float, dict[str, Scalar]]] = {}
        self._invalid: set[str] = set()
        self._lock = threading.Lock()
        self._clock = clock
        for section_name in config.sections():
            if section_name != "variables" and not section_name.startswith("variables."):
                continue
            namespace = "" if section_name == "variables" else section_name[len("variables."):]
            if section_name != "variables" and not re.fullmatch(PATH, namespace):
                raise ValueError(f"Invalid variable namespace: [{section_name}]")
            try:
                binding = Binding(namespace, config[section_name])
            except ValueError as error:
                raise ValueError(f"[{section_name}] {error}") from error
            for name in binding.fields:
                if name in self._names:
                    raise ValueError(f"Duplicate runtime variable: {name}")
                self._names[name] = binding
            self.bindings.append(binding)

    def references(self, text: str) -> set[str]:
        """Validate template syntax and reject references with no binding."""
        names = {part.name for part in parse_template(text) if isinstance(part, Reference)}
        unknown = names - self._names.keys()
        if unknown:
            raise ValueError(f"Unknown runtime variables: {', '.join(sorted(unknown))}")
        return names

    def update(self, topic: str, payload: Mapping[str, Any]) -> None:
        """Record matching reports atomically, invalidating incomplete or rejected reports."""
        with self._lock:
            for binding in self.bindings:
                if topic.strip("/") != binding.topic:
                    continue
                values = binding.extract(payload)
                if values is None:
                    self._values.pop(binding.namespace, None)
                    if binding.namespace not in self._invalid:
                        LOGGER.warning("Runtime binding %s invalidated by incomplete or rejected telemetry", binding.topic)
                    self._invalid.add(binding.namespace)
                else:
                    self._values[binding.namespace] = (self._clock(), values)
                    self._invalid.discard(binding.namespace)

    def resolve(self, settings: Mapping[str, str]) -> dict[str, Any]:
        """Resolve all settings from one snapshot, preserving whole-reference types."""
        templates = {key: parse_template(value) for key, value in settings.items()}
        required = set().union(*(self.references(value) for value in settings.values()))
        snapshot: dict[str, Scalar] = {}
        with self._lock:
            now = self._clock()
            unavailable = []
            for name in sorted(required):
                binding = self._names[name]
                entry = self._values.get(binding.namespace)
                if entry is None or now - entry[0] >= binding.max_age:
                    unavailable.append(name)
                else:
                    snapshot[name] = entry[1][name]
        if unavailable:
            raise VariablesUnavailable(
                f"Runtime variables unavailable or stale: {', '.join(unavailable)}"
            )
        resolved: dict[str, Any] = {}
        for key, parts in templates.items():
            if len(parts) == 1 and isinstance(parts[0], Reference):
                resolved[key] = snapshot[parts[0].name]
            else:
                resolved[key] = "".join(
                    str(snapshot[part.name]) if isinstance(part, Reference) else part for part in parts
                )
        return resolved

    def validate_sources(self, config: ConfigParser) -> None:
        """Reject unknown producers and cyclic source dependencies before startup."""
        from .sources.base import Source

        sources = dict(Source.enabled_sources(config))
        graph: dict[str, set[str]] = {name: set() for name in sources}
        for name, source_type in sources.items():
            source_type.validate_config(config, name)
            for key in source_type.runtime_options:
                for variable in self.references(config[name].get(key, "")):
                    topic = self._names[variable].topic
                    producers = []
                    for candidate, candidate_type in sources.items():
                        root = candidate_type.published_topic_root(config, candidate)
                        if root and (topic == root or topic.startswith(root + "/")):
                            producers.append((len(root), candidate))
                    if not producers:
                        raise ValueError(f"[{name}] {variable} has no enabled producer for {topic}")
                    longest = max(length for length, _ in producers)
                    matches = [producer for length, producer in producers if length == longest]
                    if len(matches) != 1:
                        raise ValueError(f"Ambiguous producers for runtime variable {variable}")
                    graph[name].add(matches[0])
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(name: str) -> None:
            """Walk source dependencies and reject back edges, including self-dependencies."""
            if name in visiting:
                raise ValueError(f"Cyclic runtime variable dependency involving [{name}]")
            if name in visited:
                return
            visiting.add(name)
            for dependency in graph[name]:
                visit(dependency)
            visiting.remove(name)
            visited.add(name)

        for name in graph:
            visit(name)


class RuntimePublisher:
    """Observe the source publish path without changing destination behavior."""

    def __init__(self, publisher: Any, context: RuntimeContext) -> None:
        """Wrap the existing publisher and expose shared context to source instances."""
        self.publisher = publisher
        self.runtime_context = context

    def connect(self) -> None:
        """Connect the wrapped destinations."""
        self.publisher.connect()

    def publish(self, topic: str, payload: dict[str, Any]) -> None:
        """Update variables before forwarding telemetry unchanged to its destinations."""
        self.runtime_context.update(topic, payload)
        self.publisher.publish(topic, payload)

    def close(self) -> None:
        """Close the wrapped publisher and its destinations."""
        self.publisher.close()
