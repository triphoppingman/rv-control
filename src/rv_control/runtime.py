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
COMPARISONS = {
    "==": operator.eq, "!=": operator.ne, ">=": operator.ge,
    "<=": operator.le, ">": operator.gt, "<": operator.lt,
}
Scalar = str | int | float | bool


class VariablesUnavailable(ValueError):
    """Indicate that required telemetry has not arrived or is no longer fresh."""


class Expression:
    """Compile restricted numeric arithmetic over explicit runtime references."""

    operations = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
                  ast.Div: operator.truediv, ast.Mod: operator.mod}

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
        if len(nodes) > 64 or any(
            not isinstance(node, (ast.BinOp, ast.UnaryOp, ast.Constant, ast.Name, ast.Load,
                                  ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod, ast.UAdd, ast.USub))
            or (isinstance(node, ast.Name) and node.id not in self.names)
            or (isinstance(node, ast.Constant) and
                (type(node.value) not in (int, float) or not _finite_number(node.value)))
            for node in nodes
        ):
            raise ValueError("Derived expressions support finite numbers, references and + - * / % only")

    def evaluate(self, values: Mapping[str, Scalar]) -> Scalar:
        """Evaluate numeric nodes explicitly, never using Python eval."""
        def visit(node: ast.AST) -> int | float:
            """Calculate one validated node with finite, non-boolean operands."""
            if isinstance(node, ast.Name):
                value = values[self.names[node.id]]
            elif isinstance(node, ast.Constant):
                value = node.value
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
        self._lock = threading.Lock()
        self._clock = clock
        self.config = config
        self.constants: dict[str, Scalar] = {}
        self.derived: dict[str, Expression] = {}
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
        all_names = [*self._names, *self.constants, *self.derived]
        if len(set(all_names)) != len(all_names):
            raise ValueError("Duplicate runtime variable")
        for name in self.derived:
            self.dependencies(name)
        self.publish_variables = config.getboolean("runtime", "publish_variables", fallback=False)
        self.variables_topic = config.get("runtime", "variables_topic", fallback="variables").strip("/")
        self.republish_seconds = config.getfloat("runtime", "variables_republish_seconds", fallback=60)
        if not self.variables_topic or any(char in self.variables_topic for char in ("$", "+", "#")):
            raise ValueError("variables_topic must be a literal topic")
        if not math.isfinite(self.republish_seconds) or self.republish_seconds < 0:
            raise ValueError("variables_republish_seconds must be finite and non-negative")
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
                    if binding.namespace not in self._invalid:
                        LOGGER.warning("Runtime binding %s invalidated by incomplete or rejected telemetry", binding.topic)
                    self._invalid.add(binding.namespace)
                else:
                    self._values[binding.namespace] = (self._clock(), values)
                    self._timestamps[binding.namespace] = datetime.now(timezone.utc).isoformat()
                    self._invalid.discard(binding.namespace)

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
                        value = self.derived[name].evaluate({key: item["value"] for key, item in inputs.items()})
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

    def validate_sources(self, config: ConfigParser) -> None:
        """Reject unknown producers and cyclic source dependencies before startup."""
        from .sources.base import Source

        sources = dict(Source.enabled_sources(config))
        graph: dict[str, set[str]] = {name: set() for name in sources}
        for name, source_type in sources.items():
            if self.publish_variables:
                self.validate_publication_topic(source_type.published_topic_root(config, name))
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
        """Connect the wrapped destinations."""
        self.publisher.connect()
        if self.runtime_context.publish_variables:
            with self._lock:
                self._publish_variables()
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

    def _maintain(self) -> None:
        """Check expiry independently of incoming reports until the wrapper is closed."""
        while not self._stop.wait(0.5):
            try:
                with self._lock:
                    self._publish_variables()
            except (OSError, ValueError, RuntimeError):
                LOGGER.exception("Runtime variable publication failed; retrying")

    def close(self) -> None:
        """Close the wrapped publisher and its destinations."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        self.publisher.close()
