"""Bind published telemetry to safe, freshness-aware runtime variables."""

from __future__ import annotations

import ast
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
from datetime import datetime, timedelta, timezone
from typing import Any

LOGGER = logging.getLogger(__name__)
NAME = r"[A-Za-z_][A-Za-z_0-9]*"
PATH = rf"{NAME}(?:\.{NAME})*"
TOKEN = re.compile(rf"\$(?:\{{({PATH})\}}|({NAME})|(\$))")
PREDICATE = re.compile(rf"^\s*({PATH})\s*(==|!=|>=|<=|>|<)\s*(.+?)\s*$")
MAX_WINDOW_SECONDS = 86400.0
MAX_WINDOW_SAMPLES = 1024
COMPARISONS = {
    "==": operator.eq, "!=": operator.ne, ">=": operator.ge,
    "<=": operator.le, ">": operator.gt, "<": operator.lt,
}
Scalar = str | int | float | bool


class VariablesUnavailable(ValueError):
    """Indicate that required telemetry has not arrived or is no longer fresh."""


class Expression:
    """Compile restricted arithmetic and bounded telemetry-window operations."""

    operations = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
                  ast.Div: operator.truediv, ast.Mod: operator.mod}
    window_operations = {"avg", "min", "max", "rate"}

    def __init__(self, text: str) -> None:
        """Reject calls, attributes, indexing and unbounded expression structures."""
        if len(text) > 4096:
            raise ValueError("Derived expression exceeds 4096 characters")
        self.names: dict[str, str] = {}
        chunks = []
        for part in parse_template(text):
            if isinstance(part, Reference):
                alias = f"_v{len(self.names)}"
                self.names[alias] = part.name
                chunks.append(alias)
            else:
                chunks.append(part)
        try:
            self.tree = ast.parse("".join(chunks).strip(), mode="eval").body
        except SyntaxError as error:
            raise ValueError("Invalid derived arithmetic expression") from error
        nodes = list(ast.walk(self.tree))
        if len(nodes) > 64:
            raise ValueError("Derived expression exceeds 64 syntax nodes")
        for node in nodes:
            if isinstance(node, ast.Call):
                if (
                    not isinstance(node.func, ast.Name)
                    or node.func.id not in self.window_operations
                    or len(node.args) != 2
                    or node.keywords
                    or not isinstance(node.args[0], ast.Name)
                    or node.args[0].id not in self.names
                    or not isinstance(node.args[1], ast.Constant)
                    or type(node.args[1].value) not in (int, float)
                    or not _finite_number(node.args[1].value)
                    or not 0 < node.args[1].value <= MAX_WINDOW_SECONDS
                ):
                    raise ValueError(
                        "Window operations require avg|min|max|rate(${telemetry.variable}, seconds)"
                    )
                continue
            if not isinstance(node, (
                ast.BinOp, ast.UnaryOp, ast.Constant, ast.Name, ast.Load,
                ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod, ast.UAdd, ast.USub,
            )):
                raise ValueError("Derived expressions support finite numbers, references and + - * / % only")
            if isinstance(node, ast.Name) and node.id not in self.names and not (
                isinstance(getattr(node, "ctx", None), ast.Load)
                and any(isinstance(parent, ast.Call) and parent.func is node for parent in nodes)
                and node.id in self.window_operations
            ):
                raise ValueError(f"Unknown name in derived expression: {node.id}")
            if isinstance(node, ast.Constant) and (
                type(node.value) not in (int, float) or not _finite_number(node.value)
            ):
                raise ValueError("Derived expressions support finite numbers only")

    def evaluate(
        self,
        values: Mapping[str, Scalar],
        history: Mapping[str, list[tuple[float, int | float]]],
        now: float,
    ) -> Scalar:
        """Evaluate validated arithmetic using telemetry history without Python eval."""
        def visit(node: ast.AST) -> int | float:
            """Calculate one validated node and reject nonnumeric intermediate values."""
            if isinstance(node, ast.Name):
                value = values[self.names[node.id]]
            elif isinstance(node, ast.Constant):
                value = node.value
            elif isinstance(node, ast.Call):
                variable = self.names[node.args[0].id]
                window = float(node.args[1].value)
                samples = [
                    (sample_time, sample)
                    for sample_time, sample in history.get(variable, [])
                    if now - window <= sample_time <= now
                ]
                if not samples:
                    raise ValueError("No numeric telemetry samples in the requested window")
                operation = node.func.id
                sample_values = [sample[1] for sample in samples]
                if operation == "avg":
                    value = sum(sample_values) / len(sample_values)
                elif operation == "min":
                    value = min(sample_values)
                elif operation == "max":
                    value = max(sample_values)
                else:
                    elapsed = samples[-1][0] - samples[0][0]
                    if elapsed <= 0:
                        raise ValueError("Rate requires at least two samples at different times")
                    value = (samples[-1][1] - samples[0][1]) / elapsed
            elif isinstance(node, ast.UnaryOp):
                operand = visit(node.operand)
                value = -operand if isinstance(node.op, ast.USub) else operand
            elif isinstance(node, ast.BinOp):
                value = self.operations[type(node.op)](visit(node.left), visit(node.right))
            else:
                raise ValueError("Invalid derived expression node")
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not _finite_number(value):
                raise ValueError("Derived operands/results must be finite numbers, not booleans")
            return value
        return visit(self.tree)

    @property
    def window_references(self) -> set[str]:
        """Return telemetry variables used by temporal aggregation functions."""
        return {
            self.names[node.args[0].id]
            for node in ast.walk(self.tree)
            if isinstance(node, ast.Call)
        }


@dataclass(frozen=True)
class Reference:
    """Identify one variable without confusing it with literal template text."""

    name: str


@dataclass(frozen=True)
class EventPredicate:
    """Compare one runtime value with a declared JSON scalar."""

    variable: str
    comparison: str
    expected: Scalar

    @classmethod
    def compile(cls, text: str) -> EventPredicate:
        """Parse one safe runtime-reference comparison from event configuration."""
        parts = parse_template(text)
        references = [part for part in parts if isinstance(part, Reference)]
        literals = [part for part in parts if isinstance(part, str)]
        if len(references) != 1 or len(literals) != 1:
            raise ValueError("Event conditions require one runtime variable and one scalar comparison")
        match = PREDICATE.fullmatch(f"{references[0].name} {literals[0]}")
        if not match:
            raise ValueError("Invalid event condition")
        try:
            expected = json.loads(match[3])
        except json.JSONDecodeError as error:
            raise ValueError("Event condition must compare with a JSON scalar") from error
        if not _scalar(expected):
            raise ValueError("Event condition must compare with a non-null finite JSON scalar")
        return cls(match[1], match[2], expected)

    def matches(self, actual: Any) -> bool:
        """Return false for unavailable or incomparable values rather than raising."""
        if not _scalar(actual):
            return False
        numeric = (
            isinstance(actual, (int, float)) and not isinstance(actual, bool)
            and isinstance(self.expected, (int, float)) and not isinstance(self.expected, bool)
        )
        if not numeric and type(actual) is not type(self.expected):
            return False
        return bool(COMPARISONS[self.comparison](actual, self.expected))


class RuntimeEvent:
    """Track one opt-in, read-only condition and its active/cleared transitions."""

    def __init__(self, name: str, section: Mapping[str, str]) -> None:
        """Validate event conditions and bounded activation delay at startup."""
        if set(section) - {"when", "clear_when", "for_seconds"}:
            raise ValueError("Event sections accept only when, clear_when and for_seconds")
        self.name = name
        self.when = EventPredicate.compile(section.get("when", ""))
        clear_when = section.get("clear_when", "").strip()
        self.clear_when = EventPredicate.compile(clear_when) if clear_when else None
        self.for_seconds = float(section.get("for_seconds", "0"))
        if (
            not math.isfinite(self.for_seconds)
            or not 0 <= self.for_seconds <= MAX_WINDOW_SECONDS
        ):
            raise ValueError("Event for_seconds must be finite and between 0 and 86400")
        self.active = False
        self.pending_since: float | None = None

    def evaluate(self, values: Mapping[str, dict[str, Any]], now: float) -> dict[str, Any] | None:
        """Advance debounce state and return one event only when state changes."""
        required = (
            (self.when.variable, self.clear_when.variable)
            if self.active and self.clear_when else (self.when.variable,)
        )
        if any(name is not None and values[name]["reason"] for name in required):
            self.pending_since = None
            return None

        when_value = values[self.when.variable]["value"]
        if self.active:
            should_clear = (
                self.clear_when.matches(values[self.clear_when.variable]["value"])
                if self.clear_when else not self.when.matches(when_value)
            )
            if not should_clear:
                return None
            previous, state = "active", "cleared"
            self.active = False
        else:
            if not self.when.matches(when_value):
                self.pending_since = None
                return None
            if self.pending_since is None:
                self.pending_since = now
            if now - self.pending_since < self.for_seconds:
                return None
            previous, state = "inactive", "active"
            self.active = True
            self.pending_since = None

        observed = values[self.when.variable]
        return {
            "event": self.name,
            "state": state,
            "previous_state": previous,
            "variable": self.when.variable,
            "value": observed["value"],
            "updated_at": observed["updated_at"],
            "occurred_at": datetime.now(timezone.utc).isoformat(),
        }


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


def _finite_number(value: Any) -> bool:
    """Reject nonnumeric, non-finite and oversized numeric arithmetic values."""
    return (
        isinstance(value, int) and not isinstance(value, bool) and value.bit_length() <= 1024
        or isinstance(value, float) and math.isfinite(value)
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
        try:
            datetime.now(timezone.utc) + timedelta(seconds=self.max_age)
        except OverflowError as error:
            raise ValueError("Variable binding max_age exceeds representable expiry timestamps") from error
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
        self._history: dict[str, list[tuple[float, int | float]]] = {}
        self._lock = threading.Lock()
        self._clock = clock
        self.config = config
        self.constants: dict[str, Scalar] = {}
        self.derived: dict[str, Expression] = {}
        self.events: dict[str, RuntimeEvent] = {}
        self._timestamps: dict[str, str] = {}
        self._calculation_errors: set[str] = set()
        for section_name in config.sections():
            if section_name != "variables" and not section_name.startswith("variables."):
                continue
            namespace = "" if section_name == "variables" else section_name[len("variables."):]
            if section_name != "variables" and not re.fullmatch(PATH, namespace):
                raise ValueError(f"Invalid variable namespace: [{section_name}]")
            section = config[section_name]
            constants = json.loads(section.get("constants", "{}", raw=True))
            if not isinstance(constants, dict):
                raise ValueError("Variable constants must be a JSON object")
            for key, value in constants.items():
                if not re.fullmatch(NAME, key) or not _scalar(value):
                    raise ValueError("Variable constants require names and non-null finite JSON scalars")
                name = f"{namespace}.{key}" if namespace else key
                self.constants[name] = value
            if "topic" not in section and constants:
                if any(key in section for key in ("fields", "require", "max_age")):
                    raise ValueError("Telemetry fields/require/max_age need a topic")
                continue
            try:
                binding = Binding(namespace, config[section_name])
            except ValueError as error:
                raise ValueError(f"[{section_name}] {error}") from error
            for name in binding.fields:
                if name in self._names:
                    raise ValueError(f"Duplicate runtime variable: {name}")
                self._names[name] = binding
            self.bindings.append(binding)
        for section_name in config.sections():
            if section_name == "derived" or section_name.startswith("derived."):
                namespace = "" if section_name == "derived" else section_name[len("derived."):]
                if (namespace and not re.fullmatch(PATH, namespace)) or section_name == "derived.":
                    raise ValueError(f"Invalid derived namespace: [{section_name}]")
                for key, text in config.items(section_name, raw=True):
                    if not re.fullmatch(NAME, key):
                        raise ValueError(f"Invalid derived name: {key}")
                    name = f"{namespace}.{key}" if namespace else key
                    self.derived[name] = Expression(text)
        for name, expression in self.derived.items():
            for variable in expression.window_references:
                    if variable not in self._names:
                        raise ValueError(
                            f"Window operations require a directly bound telemetry variable: {variable}"
                        )
        all_names = [*self._names, *self.constants, *self.derived]
        if len(set(all_names)) != len(all_names):
            raise ValueError("Duplicate runtime variable")
        for name in self.derived:
            self.dependencies(name)
        for section_name in config.sections():
            if not section_name.startswith("events."):
                continue
            name = section_name[len("events."):]
            if not re.fullmatch(PATH, name):
                raise ValueError(f"Invalid runtime event name: [{section_name}]")
            try:
                event = RuntimeEvent(name, config[section_name])
            except ValueError as error:
                raise ValueError(f"[{section_name}] {error}") from error
            for variable in (event.when.variable, event.clear_when.variable if event.clear_when else None):
                if variable is not None:
                    self.dependencies(variable)
            self.events[name] = event
        self.publish_variables = config.getboolean("runtime", "publish_variables", fallback=False)
        self.variables_topic = config.get("runtime", "variables_topic", fallback="variables").strip("/")
        self.events_topic = config.get("runtime", "events_topic", fallback="events").strip("/")
        self.republish_seconds = config.getfloat("runtime", "variables_republish_seconds", fallback=60)
        if not self.variables_topic or any(char in self.variables_topic for char in ("$", "+", "#")):
            raise ValueError("variables_topic must be a literal topic")
        if not math.isfinite(self.republish_seconds) or self.republish_seconds < 0:
            raise ValueError("variables_republish_seconds must be finite and non-negative")
        if self.events and (
            not self.events_topic
            or any(char in self.events_topic for char in ("$", "+", "#"))
            or self.events_topic == self.variables_topic
            or self.events_topic.startswith(self.variables_topic + "/")
            or self.variables_topic.startswith(self.events_topic + "/")
        ):
            raise ValueError("events_topic must be literal and separate from variables_topic")
        if self.publish_variables:
            for binding in self.bindings:
                self.validate_publication_topic(binding.topic)

    def validate_publication_topic(self, topic: str) -> None:
        """Keep generated variable topics separate from source telemetry namespaces."""
        if (topic == self.variables_topic or topic.startswith(self.variables_topic + "/")
                or self.variables_topic.startswith(topic + "/")):
            raise ValueError(f"Variable publication topic overlaps telemetry topic: {topic}")

    def dependencies(self, name: str, chain: tuple[str, ...] = ()) -> set[str]:
        """Resolve transitive telemetry dependencies, rejecting unknown names and cycles."""
        if name in chain:
            raise ValueError(f"Cyclic derived variable dependency: {name}")
        if name in self.derived:
            return set().union(*(self.dependencies(item, (*chain, name))
                                 for item in self.derived[name].names.values()))
        if name in self.constants:
            return set()
        if name not in self._names:
            raise ValueError(f"Unknown runtime variables: {name}")
        return {name}

    def references(self, text: str) -> set[str]:
        """Validate template syntax and reject references with no binding."""
        names = {part.name for part in parse_template(text) if isinstance(part, Reference)}
        unknown = names - (self._names.keys() | self.constants.keys() | self.derived.keys())
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
                    for name in binding.fields:
                        self._history.pop(name, None)
                    if binding.namespace not in self._invalid:
                        LOGGER.warning("Runtime binding %s invalidated by incomplete or rejected telemetry", binding.topic)
                    self._invalid.add(binding.namespace)
                else:
                    received = self._clock()
                    self._values[binding.namespace] = (received, values)
                    self._timestamps[binding.namespace] = datetime.now(timezone.utc).isoformat()
                    self._invalid.discard(binding.namespace)
                    for name, value in values.items():
                        if isinstance(value, (int, float)) and not isinstance(value, bool):
                            samples = self._history.setdefault(name, [])
                            samples.append((received, value))
                            del samples[:-MAX_WINDOW_SAMPLES]
                        else:
                            self._history.pop(name, None)

    def _snapshot(self) -> dict[str, dict[str, Any]]:
        """Evaluate all values under the caller's lock, inheriting input expiry."""
        now = self._clock()
        result: dict[str, dict[str, Any]] = {}

        def evaluate(name: str) -> dict[str, Any]:
            """Memoize a value or its explicit unavailable reason within this snapshot."""
            if name in result:
                return result[name]
            value: Any = None
            reason: str | None = None
            expires: float | None = None
            expires_at: str | None = None
            updated: str | None = None
            if name in self.constants:
                value = self.constants[name]
            elif name in self.derived:
                inputs = {item: evaluate(item) for item in self.derived[name].names.values()}
                if any(item["reason"] for item in inputs.values()):
                    reason = "dependency_unavailable"
                else:
                    try:
                        value = self.derived[name].evaluate(
                            {key: item["value"] for key, item in inputs.items()},
                            self._history,
                            now,
                        )
                    except (ValueError, ArithmeticError) as error:
                        reason = "calculation_error"
                        if name not in self._calculation_errors:
                            LOGGER.warning("Derived variable %s unavailable: %s", name, error)
                        self._calculation_errors.add(name)
                    else:
                        self._calculation_errors.discard(name)
                deadlines = [item["expires"] for item in inputs.values() if item["expires"] is not None]
                expires = min(deadlines) if deadlines else None
                expiry_times = [item["expires_at"] for item in inputs.values() if item["expires_at"]]
                expires_at = min(expiry_times) if expiry_times else None
                times = [item["updated_at"] for item in inputs.values() if item["updated_at"]]
                updated = min(times) if times else None
            else:
                binding = self._names[name]
                entry = self._values.get(binding.namespace)
                if entry is None:
                    reason = "invalid" if binding.namespace in self._invalid else "missing"
                else:
                    expires = entry[0] + binding.max_age
                    updated = self._timestamps.get(binding.namespace)
                    if updated:
                        expires_at = (datetime.fromisoformat(updated) + timedelta(seconds=binding.max_age)).isoformat()
                    if now >= expires:
                        reason = "stale"
                    else:
                        value = entry[1][name]
            result[name] = {
                "value": value, "reason": reason, "expires": expires,
                "updated_at": updated, "expires_at": expires_at,
            }
            return result[name]

        for name in (*self._names, *self.constants, *self.derived):
            evaluate(name)
        return result

    def resolve(self, settings: Mapping[str, str]) -> dict[str, Any]:
        """Resolve all settings from one snapshot, preserving whole-reference types."""
        templates = {key: parse_template(value) for key, value in settings.items()}
        required = set().union(*(self.references(value) for value in settings.values()))
        snapshot: dict[str, Scalar] = {}
        with self._lock:
            values = self._snapshot()
            unavailable = [name for name in sorted(required) if values[name]["reason"]]
            snapshot = {name: values[name]["value"] for name in required}
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

    def publication_snapshot(self) -> dict[str, dict[str, Any]]:
        """Describe each namespace with per-variable validity and original measurement times."""
        with self._lock:
            values = self._snapshot()
            groups: dict[str, dict[str, Any]] = {}
            for name, item in values.items():
                namespace, _, key = name.rpartition(".")
                group = groups.setdefault(namespace, {"namespace": namespace, "values": {}, "status": {}})
                group["values"][key] = item["value"]
                group["status"][key] = {
                    "available": item["reason"] is None, "reason": item["reason"],
                    "updated_at": item["updated_at"],
                    "expires_at": item["expires_at"],
                }
            return groups

    def evaluate_events(self) -> list[dict[str, Any]]:
        """Return read-only transition records for currently evaluable event rules."""
        with self._lock:
            values = self._snapshot()
            now = self._clock()
            transitions = []
            for event in self.events.values():
                transition = event.evaluate(values, now)
                if transition is not None:
                    transitions.append(transition)
            return transitions

    def validate_sources(self, config: ConfigParser) -> None:
        """Reject unknown producers and cyclic source dependencies before startup."""
        from .sources.base import Source

        sources = dict(Source.enabled_sources(config))
        graph: dict[str, set[str]] = {name: set() for name in sources}
        for name, source_type in sources.items():
            if self.publish_variables:
                self.validate_publication_topic(source_type.published_topic_root(config, name))
            if self.events:
                topic_root = source_type.published_topic_root(config, name)
                if (
                    topic_root
                    and (self.events_topic == topic_root
                         or self.events_topic.startswith(topic_root + "/")
                         or topic_root.startswith(self.events_topic + "/"))
                ):
                    raise ValueError(f"Runtime event topic overlaps telemetry topic: {topic_root}")
            source_type.validate_config(config, name)
            for key in source_type.runtime_options:
                references = self.references(config[name].get(key, ""))
                for variable in set().union(*(self.dependencies(item) for item in references)):
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
        for event_name, event in self.events.items():
            event_variables = {
                event.when.variable,
                *([event.clear_when.variable] if event.clear_when else []),
            }
            for variable in event_variables:
                for telemetry_variable in self.dependencies(variable):
                    topic = self._names[telemetry_variable].topic
                    producers = []
                    for candidate, candidate_type in sources.items():
                        root = candidate_type.published_topic_root(config, candidate)
                        if root and (topic == root or topic.startswith(root + "/")):
                            producers.append((len(root), candidate))
                    if not producers:
                        raise ValueError(
                            f"Runtime event {event_name} depends on {telemetry_variable} "
                            f"with no enabled producer for {topic}"
                        )
                    longest = max(producer[0] for producer in producers)
                    if len([candidate for length, candidate in producers if length == longest]) != 1:
                        raise ValueError(
                            f"Runtime event {event_name} has ambiguous producers for "
                            f"{telemetry_variable}"
                        )
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
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._published: dict[str, tuple[str, float]] = {}

    def connect(self) -> None:
        """Connect destinations and start maintenance for configured publications."""
        self.publisher.connect()
        if self.runtime_context.publish_variables:
            with self._lock:
                self._publish_variables()
        if self.runtime_context.publish_variables or self.runtime_context.events:
            self._thread = threading.Thread(target=self._maintain, name="runtime-variables", daemon=True)
            self._thread.start()

    def publish(self, topic: str, payload: dict[str, Any]) -> None:
        """Update variables before forwarding telemetry unchanged to its destinations."""
        self.publish_source(topic, payload, "runtime", "runtime")

    def publish_source(
        self, topic: str, payload: dict[str, Any], source_type: str, source_section: str,
    ) -> None:
        """Update runtime bindings synchronously, then forward attributed telemetry."""
        with self._lock:
            self.runtime_context.update(topic, payload)
            publish_source = getattr(self.publisher, "publish_source", None)
            if callable(publish_source):
                publish_source(topic, payload, source_type, source_section)
            else:
                self.publisher.publish(topic, payload)
            if self.runtime_context.publish_variables:
                self._publish_variables()
            self._publish_events()

    def _publish_variables(self) -> None:
        """Emit changed/expired groups or periodic snapshots without rebinding them."""
        context = self.runtime_context
        now = context._clock()
        for namespace, payload in context.publication_snapshot().items():
            signature = json.dumps(payload, sort_keys=True, allow_nan=False)
            previous = self._published.get(namespace)
            if (previous is not None and signature == previous[0]
                    and (context.republish_seconds == 0 or now - previous[1] < context.republish_seconds)):
                continue
            topic = context.variables_topic + ("/" + namespace.replace(".", "/") if namespace else "")
            self.publisher.publish(topic, {
                **payload, "published_at": datetime.now(timezone.utc).isoformat(),
            })
            self._published[namespace] = (signature, now)

    def _publish_events(self) -> None:
        """Publish only configured read-only state transitions through existing targets."""
        context = self.runtime_context
        for event in context.evaluate_events():
            name = event["event"].replace(".", "/")
            self.publisher.publish(f"{context.events_topic}/{name}", event)

    def _maintain(self) -> None:
        """Check variable expiry and event debounce timers without source traffic."""
        while not self._stop.wait(0.5):
            try:
                with self._lock:
                    if self.runtime_context.publish_variables:
                        self._publish_variables()
                    self._publish_events()
            except (OSError, ValueError, RuntimeError):
                LOGGER.exception("Runtime variable publication failed; retrying")

    def close(self) -> None:
        """Close the wrapped publisher and its destinations."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        self.publisher.close()
