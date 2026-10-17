"""Opt-in, journaled semantic MQTT commands and read-only coach queries."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import sqlite3
import threading
import time
from collections.abc import Callable
from configparser import ConfigParser
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from .coach import Coach
from .coach_queries import CoachQueries
from .coach_state import EntityStateTracker
from .sources.base import Source

LOGGER = logging.getLogger(__name__)
IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
MAX_PAYLOAD = 8192
MAX_RESULT = 256 * 1024
QUERY_ACTIONS = {"refresh_states", "snapshot", "republish_states", "capabilities", "health", "request_status"}
ENTITY_QUERIES = {"refresh_states", "snapshot", "republish_states"}
ENVELOPE_KEYS = {
    "version", "client_id", "request_id", "issued_at", "expires_at",
    "target", "action", "params", "confirmed",
}
PublishResult = Callable[[str, dict[str, Any], bool], None]


class CommandError(ValueError):
    """Carry a stable, sanitized protocol rejection code."""

    def __init__(self, code: str, message: str) -> None:
        """Save the public code and message without transport internals."""
        super().__init__(message)
        self.code = code


def utc_now() -> float:
    """Return the current UTC epoch for replaceable protocol-clock tests."""
    return time.time()


def timestamp(epoch: float) -> str:
    """Format a protocol timestamp as a UTC RFC 3339 instant."""
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate JSON keys at every nesting level."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def reject_constant(value: str) -> Any:
    """Reject JSON's nonstandard NaN and infinity extensions."""
    raise ValueError(f"Non-finite JSON number: {value}")

def finite_float(value: str) -> float:
    """Reject overflowed JSON exponent numbers, not just literal NaN tokens."""
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Non-finite JSON number")
    return number


def parse_json(payload: bytes) -> dict[str, Any]:
    """Decode a bounded, strict JSON object without losing duplicate keys."""
    if len(payload) > MAX_PAYLOAD:
        raise CommandError("invalid_request", "Request exceeds 8192 bytes")
    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=unique_object, parse_constant=reject_constant, parse_float=finite_float)
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise CommandError("invalid_request", "Invalid JSON object") from error
    if not isinstance(value, dict):
        raise CommandError("invalid_request", "Request must be an object")
    return value


def parse_time(value: Any) -> float:
    """Accept only UTC RFC 3339 instants with an explicit zero offset."""
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)", value,
    ):
        raise CommandError("invalid_request", "Timestamps must be UTC RFC 3339 instants")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError as error:
        raise CommandError("invalid_request", "Invalid timestamp") from error


def validate_envelope(request: dict[str, Any], now: float, *, check_expiry: bool = True) -> float:
    """Validate the versioned envelope and return its expiration epoch."""
    if request.keys() != ENVELOPE_KEYS:
        raise CommandError("invalid_request", "Missing or unknown envelope fields")
    if type(request["version"]) is not int or request["version"] != 1:
        raise CommandError("unsupported_version", "Only version 1 is supported")
    for key in ("client_id", "request_id", "target", "action"):
        if not isinstance(request[key], str) or not IDENTIFIER.fullmatch(request[key]):
            raise CommandError("invalid_request", f"Invalid {key}")
    if type(request["confirmed"]) is not bool or not isinstance(request["params"], dict):
        raise CommandError("invalid_request", "Invalid confirmation or parameters")
    issued, expires = parse_time(request["issued_at"]), parse_time(request["expires_at"])
    if not 0 < expires - issued <= 15:
        raise CommandError("invalid_request", "Request lifetime must be greater than zero and at most 15 seconds")
    if issued > now + 2:
        raise CommandError("clock_skew", "Issue time is more than two seconds in the future")
    if check_expiry and now >= expires:
        raise CommandError("expired", "Request has expired")
    return expires


def validate_params(params: dict[str, Any], schema: dict[str, Any]) -> None:
    """Validate exact scalar parameter names, types and declared value bounds."""
    if params.keys() != schema.keys():
        raise CommandError("invalid_params", "Parameters must exactly match the declared action schema")
    for name, rule in schema.items():
        value = params[name]
        kind = rule["type"]
        valid = (
            (kind == "number" and type(value) in (int, float))
            or (kind == "integer" and type(value) is int)
            or (kind == "boolean" and type(value) is bool)
            or (kind == "string" and isinstance(value, str))
        )
        if not valid:
            raise CommandError("invalid_params", f"Invalid type for {name}")
        if type(value) in (int, float):
            if not math.isfinite(value) or ("min" in rule and value < rule["min"]) or ("max" in rule and value > rule["max"]):
                raise CommandError("invalid_params", f"Out-of-range parameter {name}")
        if "enum" in rule and value not in rule["enum"]:
            raise CommandError("invalid_params", f"Unsupported value for {name}")


def validate_schema(schema: Any) -> None:
    """Fail startup on malformed scalar action parameter declarations."""
    if not isinstance(schema, dict):
        raise ValueError("Action schema must be a mapping")
    for name, rule in schema.items():
        if not isinstance(name, str) or not IDENTIFIER.fullmatch(name) or name in {
            "confirm", "confirmed", "transport", "source", "config_section", "bus",
            "can_id", "data", "payload", "url", "base_url", "frames",
        }:
            raise ValueError("Invalid action parameter name")
        if not isinstance(rule, dict) or set(rule) - {"type", "min", "max", "enum"}:
            raise ValueError("Invalid parameter schema")
        if rule.get("type") not in {"number", "integer", "boolean", "string"}:
            raise ValueError("Parameter schema requires a scalar type")
        for bound in ("min", "max"):
            if bound in rule and (type(rule[bound]) not in (int, float) or not math.isfinite(rule[bound]) or rule["type"] not in {"number", "integer"}):
                raise ValueError("Invalid parameter bound")
        if rule.get("min", -math.inf) > rule.get("max", math.inf):
            raise ValueError("Reversed parameter bounds")
        if "enum" in rule:
            if not isinstance(rule["enum"], list) or not rule["enum"]:
                raise ValueError("Parameter enum must be nonempty")
            for value in rule["enum"]:
                validate_params({name: value}, {name: {key: item for key, item in rule.items() if key != "enum"}})


@dataclass(frozen=True)
class ActionStep:
    """One validated transport operation in a fully expanded coach plan."""

    entity: str
    section: str
    wait: float
    payload: dict[str, Any]


class ControllerPolicy:
    """Validate static controller grants and bind them to loaded action schemas."""

    def __init__(self, coach: Coach, path: Path, monitored: dict[str, list[str]]) -> None:
        """Read one local policy file and validate every grant before subscribing."""
        try:
            with path.open(encoding="utf-8") as handle:
                data = YAML(typ="safe").load(handle)
        except YAMLError as error:
            raise ValueError("Invalid controller policy YAML") from error
        if not isinstance(data, dict) or set(data) != {"controllers"} or not isinstance(data["controllers"], dict) or not data["controllers"]:
            raise ValueError("Policy requires a nonempty controllers mapping")
        self.coach = coach
        self.controllers: dict[str, Any] = data["controllers"]
        if set(coach.entities) & set(coach.command_groups):
            raise ValueError("Entity and group names must not overlap")
        for client, policy in self.controllers.items():
            if not isinstance(client, str) or not IDENTIFIER.fullmatch(client) or not isinstance(policy, dict) or set(policy) - {"actions", "queries"}:
                raise ValueError("Invalid controller policy")
            actions = policy.get("actions", {})
            queries = policy.get("queries", {})
            if not isinstance(actions, dict) or not isinstance(queries, dict):
                raise ValueError("Controller actions and queries must be mappings")
            for target, grants in actions.items():
                if not isinstance(target, str) or not IDENTIFIER.fullmatch(target) or target == "coach":
                    raise ValueError("Invalid or reserved policy target")
                definition = self.definition(target)
                if not isinstance(grants, dict) or not grants:
                    raise ValueError("Action grants must be nonempty mappings")
                for action, grant in grants.items():
                    if not isinstance(action, str) or not IDENTIFIER.fullmatch(action) or action in QUERY_ACTIONS:
                        raise ValueError("Invalid or reserved physical action name")
                    if action not in definition.get("actions", {}):
                        raise ValueError("Policy references unknown action")
                    self.schema(target, action)
                    if not isinstance(grant, dict) or set(grant) - {"params", "confirm"} or type(grant.get("confirm", False)) is not bool:
                        raise ValueError("Invalid action grant")
                    self.schema(target, action, grant)
            for action, grant in queries.items():
                if action not in QUERY_ACTIONS or not isinstance(grant, dict):
                    raise ValueError("Invalid query grant")
                if action in ENTITY_QUERIES:
                    entities = grant.get("entities")
                    if set(grant) != {"entities"} or not isinstance(entities, list) or not entities:
                        raise ValueError("Query grant requires entities")
                    if entities != ["*"] and (any(not isinstance(item, str) or item not in monitored for item in entities) or len(set(entities)) != len(entities)):
                        raise ValueError("Query grant references invalid entities")
                elif grant:
                    raise ValueError("Query grant does not accept options")
        for client, policy in self.controllers.items():
            for target, grants in policy.get("actions", {}).items():
                for action in grants:
                    self._validate_expansion(client, target, action)

    def definition(self, target: str) -> dict[str, Any]:
        """Return a known entity or group definition without opening sources."""
        if target not in self.coach.entities and target not in self.coach.command_groups:
            raise ValueError("Policy references unknown target")
        return self.coach.entities.get(target) or self.coach.command_groups[target]

    def schema(self, target: str, action: str, grant: dict[str, Any] | None = None) -> dict[str, Any]:
        """Combine declared schemas with policy bounds that may only narrow them."""
        definition = self.definition(target)
        schemas = definition.get("action_schemas", {})
        if target in self.coach.command_groups:
            schema = schemas.get(action, {})
        elif action not in schemas:
            raise ValueError("MQTT actions require a declared action_schemas entry")
        else:
            schema = schemas[action]
        validate_schema(schema)
        narrowed = {name: dict(rule) for name, rule in schema.items()}
        restrictions = (grant or {}).get("params", {})
        if not isinstance(restrictions, dict) or set(restrictions) - set(schema):
            raise ValueError("Policy references unknown parameters")
        for name, rule in restrictions.items():
            if not isinstance(rule, dict) or set(rule) - {"type", "min", "max", "enum"}:
                raise ValueError("Invalid policy parameter bounds")
            original = schema[name]
            merged = dict(original, **rule)
            validate_schema({name: merged})
            if merged["type"] != original["type"]:
                raise ValueError("Policy cannot change parameter types")
            if merged.get("min", -math.inf) < original.get("min", -math.inf) or merged.get("max", math.inf) > original.get("max", math.inf):
                raise ValueError("Policy cannot widen parameter bounds")
            if "enum" in original and any(value not in original["enum"] for value in merged["enum"]):
                raise ValueError("Policy cannot widen parameter enums")
            narrowed[name] = merged
        return narrowed

    def grant(self, client: str, target: str, action: str) -> dict[str, Any]:
        """Return an explicit action grant, denying unlisted controllers and actions."""
        grant = self.controllers.get(client, {}).get("actions", {}).get(target, {}).get(action)
        if grant is None:
            raise CommandError("forbidden", "Controller is not authorized for this action")
        return grant

    def expand(
        self, target: str, action: str, params: dict[str, Any], chain: tuple[str, ...] = (),
        groups: list[tuple[str, str, dict[str, Any]]] | None = None,
    ) -> list[tuple[str, str, dict[str, Any]]]:
        """Expand a bounded acyclic group into entity actions without executing it."""
        if len(chain) >= 16 or target in chain:
            raise CommandError("invalid_request", "Cyclic or excessive group expansion")
        if target in self.coach.entities:
            if action not in self.coach.entities[target]["actions"]:
                raise CommandError("unknown_action", "Unknown entity action")
            return [(target, action, params)]
        group = self.coach.command_groups.get(target)
        if group is None:
            raise CommandError("unknown_target", "Unknown target")
        actions = group.get("actions", {})
        if action not in actions:
            raise CommandError("unknown_action", "Unknown group action")
        if groups is not None:
            groups.append((target, action, params))
        steps = (
            [{"entity": member, "action": action} for member in group.get("members", [])]
            if isinstance(actions, list) else actions[action]
        )
        if not isinstance(steps, list) or not steps or len(steps) > 64:
            raise CommandError("invalid_request", "Invalid or excessive group action")
        result: list[tuple[str, str, dict[str, Any]]] = []
        for step in steps:
            if not isinstance(step, dict) or set(step) - {"entity", "action", "params"} or not isinstance(step.get("entity"), str) or not isinstance(step.get("action", action), str) or not isinstance(step.get("params", {}), dict) or any(not isinstance(key, str) for key in step.get("params", {})):
                raise CommandError("invalid_request", "Invalid group member")
            result.extend(self.expand(step["entity"], step.get("action", action), dict(step.get("params", {}), **params), chain + (target,), groups))
            if len(result) > 64:
                raise CommandError("invalid_request", "Excessive group expansion")
        return result

    def _validate_expansion(self, client: str, target: str, action: str) -> None:
        """Fail startup when a group contains unknown, forbidden or undeclared actions."""
        groups: list[tuple[str, str, dict[str, Any]]] = []
        expanded = self.expand(target, action, {}, groups=groups)
        for group, group_action, _params in groups:
            self.schema(group, group_action, self.grant(client, group, group_action))
        for entity, member_action, params in expanded:
            self.schema(entity, member_action, self.grant(client, entity, member_action))
            if target in self.coach.command_groups:
                schema = self.schema(entity, member_action, self.grant(client, entity, member_action))
                group_schema = self.schema(target, action)
                if not group_schema:
                    validate_params(params, schema)


class RequestJournal:
    """Bounded durable request identities with crash-recovery outcomes."""

    def __init__(self, path: Path, limit: int, now: float) -> None:
        """Open an exclusive daemon journal and mark abandoned work interrupted."""
        import fcntl

        path = path.resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file_lock = path.with_suffix(path.suffix + ".lock").open("a")
        database: sqlite3.Connection | None = None
        try:
            fcntl.flock(self._file_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            database = sqlite3.connect(path, check_same_thread=False)
            self.db = database
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute("CREATE TABLE IF NOT EXISTS requests (client TEXT, request TEXT, digest TEXT NOT NULL, expires REAL NOT NULL, result TEXT NOT NULL, intent INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(client,request))")
            self.db.commit()
        except (OSError, sqlite3.Error):
            if database is not None:
                database.close()
            self._file_lock.close()
            raise
        self.limit = limit
        self.lock = threading.RLock()
        try:
            with self.db:
                for client, request, raw, intent in self.db.execute("SELECT client,request,result,intent FROM requests"):
                    result = json.loads(raw)
                    if result["status"] == "accepted":
                        physical_intent = intent and result.get("target") != "coach"
                        result.update(status="failed", code="interrupted", message="Service stopped before recording a terminal result", outcome="unknown" if physical_intent else "not_started", timestamp=timestamp(now))
                        self.db.execute("UPDATE requests SET result=? WHERE client=? AND request=?", (json.dumps(result), client, request))
                self.db.execute("DELETE FROM requests WHERE expires + 60 < ?", (now,))
        except (sqlite3.Error, ValueError, KeyError, TypeError):
            self.close()
            raise

    def lookup(self, client: str, request: str) -> tuple[str, dict[str, Any]] | None:
        """Read only the named controller's latest request result."""
        with self.lock:
            row = self.db.execute("SELECT digest,result FROM requests WHERE client=? AND request=?", (client, request)).fetchone()
            return (row[0], json.loads(row[1])) if row else None

    def reserve(self, request: dict[str, Any], digest: str, expires: float, result: dict[str, Any], now: float) -> None:
        """Persist admission, refusing to evict identities still inside their retention window."""
        with self.lock, self.db:
            self.db.execute("DELETE FROM requests WHERE expires + 60 < ?", (now,))
            if self.db.execute("SELECT COUNT(*) FROM requests").fetchone()[0] >= self.limit:
                raise CommandError("journal_full", "Request journal is full")
            self.db.execute("INSERT INTO requests(client,request,digest,expires,result) VALUES (?,?,?,?,?)", (request["client_id"], request["request_id"], digest, expires, json.dumps(result)))

    def save(self, request: dict[str, Any], result: dict[str, Any], *, intent: bool = False) -> None:
        """Durably save execution intent or a terminal outcome before publishing it."""
        with self.lock, self.db:
            self.db.execute("UPDATE requests SET result=?,intent=MAX(intent,?) WHERE client=? AND request=?", (json.dumps(result, allow_nan=False), int(intent), request["client_id"], request["request_id"]))

    def close(self) -> None:
        """Release the database and single-daemon journal lock."""
        self.db.close()
        self._file_lock.close()


class CoachCommandService:
    """Validate MQTT requests and execute one bounded plan/query outside callbacks."""

    def __init__(
        self, config: ConfigParser, coach: Coach, tracker: EntityStateTracker,
        sources: dict[str, Source], publish: PublishResult,
        connected: Callable[[], bool], clock: Callable[[], float] = utc_now,
    ) -> None:
        """Validate configuration, load policy, and recover the durable journal."""
        self.config, self.coach, self.sources = config, coach, sources
        self.publish, self.connected, self.clock = publish, connected, clock
        self.queries = CoachQueries(coach, tracker, sources)
        section = config["coach_commands"]
        self.enabled = section.getboolean("enabled", fallback=False)
        self.queries_enabled = section.getboolean("queries_enabled", fallback=False)
        from .targets.base import Target

        mqtt_sections = [name for name, target in Target.enabled_targets(config) if target.target_name == "mqtt"]
        if len(mqtt_sections) != 1:
            raise ValueError("Coach API requires exactly one enabled MQTT target")
        self.mqtt_section = mqtt_sections[0]
        if self.enabled and not config[self.mqtt_section].getboolean("write_enabled", fallback=False):
            raise ValueError("Enabled coach commands require MQTT write_enabled")
        config_dir = Path(config.get("_meta", "path", fallback="config.ini")).resolve().parent
        policy_path = section.get("controllers_file", "").strip()
        if not policy_path:
            raise ValueError("Coach API requires controllers_file")
        self.policy = ControllerPolicy(coach, config_dir / policy_path, self.queries.entities())
        limit = section.getint("journal_max_entries", fallback=1024)
        if not 1 <= limit <= 10000:
            raise ValueError("journal_max_entries must be between 1 and 10000")
        self.journal = RequestJournal(config_dir / section.get("journal_file", "data/coach-commands.sqlite3"), limit, clock())
        self._admission = threading.Lock()
        self._busy = threading.Lock()
        self._closing = False
        self._worker: threading.Thread | None = None
        self._last_refresh: dict[str, float] = {}
        self._last_source_refresh: dict[str, float] = {}
        self.started = time.monotonic()

    def result(self, request: dict[str, Any], status: str, code: str, message: str) -> dict[str, Any]:
        """Build a sanitized, consistently correlated result envelope."""
        return dict(
            version=1, client_id=request["client_id"], request_id=request["request_id"],
            target=request.get("target", ""), action=request.get("action", ""),
            status=status, code=code, message=message, timestamp=timestamp(self.clock()),
            outcome="not_started", steps_completed=0,
        )

    def _publish(self, result: dict[str, Any], wait: bool = False) -> None:
        """Publish only to a derived, validated result topic with forced delivery settings."""
        if len(json.dumps(result, allow_nan=False).encode()) > MAX_RESULT:
            raise CommandError("response_too_large", "Requested result exceeds 256 KiB")
        self.publish(f"coach/result/{result['client_id']}/{result['request_id']}", result, wait)

    def receive(self, kind: str, payload: bytes, retained: bool = False) -> None:
        """Admit a strict request without blocking MQTT's network callback thread."""
        request: dict[str, Any] = {}
        try:
            request = parse_json(payload)
            if retained:
                raise CommandError("invalid_request", "Retained requests are forbidden")
            expires = validate_envelope(request, self.clock(), check_expiry=False)
            digest = hashlib.sha256(json.dumps([kind, request], sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
            with self._admission:
                if self._closing:
                    raise CommandError("source_unavailable", "Service is shutting down")
                previous = self.journal.lookup(request["client_id"], request["request_id"])
                if previous:
                    if previous[0] != digest:
                        raise CommandError("request_id_conflict", "Request identity was already used with different content")
                    self._publish(previous[1])
                    return
                validate_envelope(request, self.clock())
                if not self._busy.acquire(blocking=False):
                    raise CommandError("busy", "Another command or query is in progress")
                try:
                    prepared = self._prepare(kind, request, expires)
                    accepted = self.result(request, "accepted", "accepted", "Request admitted; no physical outcome is confirmed")
                    self.journal.reserve(request, digest, expires, accepted, self.clock())
                    LOGGER.info("Coach request admitted: client=%s request=%s target=%s action=%s", request["client_id"], request["request_id"], request["target"], request["action"])
                    worker = threading.Thread(target=self._execute, args=(kind, request, expires, prepared, accepted), name="coach-command", daemon=True)
                    try:
                        worker.start()
                    except RuntimeError:
                        self.journal.save(request, self.result(request, "rejected", "service_error", "Unable to start request worker"))
                        raise
                    self._worker = worker
                except BaseException:
                    self._busy.release()
                    raise
        except CommandError as error:
            self._reject(request, error.code, str(error))
        except (OSError, sqlite3.Error, ValueError, TypeError, OverflowError, RuntimeError):
            LOGGER.exception("Coach request admission failed")
            self._reject(request, "service_error", "Request could not be admitted")

    def _reject(self, request: dict[str, Any], code: str, message: str) -> None:
        """Log rejections and reply only when both correlation identifiers are safe."""
        LOGGER.warning("Coach request rejected: code=%s client=%s request=%s", code, request.get("client_id") if isinstance(request.get("client_id"), str) and IDENTIFIER.fullmatch(request["client_id"]) else "invalid", request.get("request_id") if isinstance(request.get("request_id"), str) and IDENTIFIER.fullmatch(request["request_id"]) else "invalid")
        if all(isinstance(request.get(key), str) and IDENTIFIER.fullmatch(request[key]) for key in ("client_id", "request_id")):
            safe = {key: request[key] for key in ("client_id", "request_id")}
            safe.update({key: request[key] if isinstance(request.get(key), str) and IDENTIFIER.fullmatch(request[key]) else "" for key in ("target", "action")})
            try:
                self._publish(self.result(safe, "rejected", code, message))
            except (OSError, RuntimeError, ValueError):
                LOGGER.exception("Unable to publish coach rejection")

    def _prepare(self, kind: str, request: dict[str, Any], expires: float) -> list[ActionStep] | list[str]:
        """Authorize and preflight the entire request before admission."""
        if kind == "query":
            if not self.queries_enabled:
                raise CommandError("forbidden", "Monitoring API is disabled")
            return self._prepare_query(request, expires)
        if kind != "command" or not self.enabled:
            raise CommandError("forbidden", "Command API is disabled")
        if request["target"] == "coach" or request["action"] in QUERY_ACTIONS:
            raise CommandError("invalid_request", "Monitoring requires the query topic")
        return self._plan(request)

    def _plan(self, request: dict[str, Any], *, availability: bool = True) -> list[ActionStep]:
        """Expand, authorize and build all member operations before the first write."""
        client, target, action = request["client_id"], request["target"], request["action"]
        if target not in self.coach.entities and target not in self.coach.command_groups:
            raise CommandError("unknown_target", "Unknown target")
        if action not in self.policy.definition(target).get("actions", {}):
            raise CommandError("unknown_action", "Unknown action")
        grant = self.policy.grant(client, target, action)
        validate_params(request["params"], self.policy.schema(target, action, grant))
        groups: list[tuple[str, str, dict[str, Any]]] = []
        expanded = self.policy.expand(target, action, request["params"], groups=groups)
        plan: list[ActionStep] = []
        if grant.get("confirm") and not request["confirmed"]:
            raise CommandError("confirmation_required", "Deliberate user confirmation is required")
        for group, group_action, params in groups:
            group_grant = self.policy.grant(client, group, group_action)
            validate_params(params, self.policy.schema(group, group_action, group_grant))
            if group_grant.get("confirm") and not request["confirmed"]:
                raise CommandError("confirmation_required", "Deliberate user confirmation is required")
        duration = 0.0
        for entity_name, member_action, params in expanded:
            member_grant = self.policy.grant(client, entity_name, member_action)
            validate_params(params, self.policy.schema(entity_name, member_action, member_grant))
            entity = self.coach.entities[entity_name]
            if (entity.get("confirm") or member_grant.get("confirm")) and not request["confirmed"]:
                raise CommandError("confirmation_required", "Deliberate user confirmation is required")
            try:
                section = self.coach.resolve_config_section(entity, entity_name)
                frames = self.coach.prepare_action(entity_name, member_action, params)
            except (KeyError, ValueError, PermissionError) as error:
                raise CommandError("invalid_params", "Configured action cannot be preflighted") from error
            if not frames:
                raise CommandError("invalid_request", "Action has no transport operations")
            if availability:
                self._check_source(section)
            transport = entity.get("transport")
            timeout = self.config.getfloat(section, "timeout", fallback=5.0) if transport == "wled" else 1.0
            if not math.isfinite(timeout) or not 0 < timeout <= 10:
                raise CommandError("invalid_request", "Source timeout is not safely bounded")
            for wait, payload in frames:
                duration += wait + (3 * timeout if transport == "wled" else timeout)
                plan.append(ActionStep(entity_name, section, wait, payload))
                if len(plan) > 64 or duration > 30:
                    raise CommandError("invalid_request", "Action plan exceeds 64 steps or 30 seconds")
        return plan

    def _check_source(self, section: str) -> Source:
        """Recheck every write gate and require an existing, ready daemon source."""
        if not self.enabled or not self.config[self.mqtt_section].getboolean("write_enabled", fallback=False) or not self.config[section].getboolean("write_enabled", fallback=False):
            raise CommandError("writes_disabled", "Physical writes are disabled")
        source = self.sources.get(section)
        if source is None or not source.command_ready():
            raise CommandError("source_unavailable", "Required source is unavailable")
        return source

    def _prepare_query(self, request: dict[str, Any], expires: float) -> list[str]:
        """Validate monitoring grants, bounded selections and refresh rate limits."""
        action, params = request["action"], request["params"]
        if request["target"] != "coach" or action not in QUERY_ACTIONS or request["confirmed"]:
            raise CommandError("invalid_request", "Invalid monitoring operation")
        grant = self.policy.controllers.get(request["client_id"], {}).get("queries", {}).get(action)
        if grant is None:
            raise CommandError("forbidden", "Monitoring operation is not authorized")
        if action not in ENTITY_QUERIES:
            expected = {"request_id"} if action == "request_status" else set()
            if params.keys() != expected:
                raise CommandError("invalid_params", "Unexpected query parameters")
            if action == "request_status" and (not isinstance(params["request_id"], str) or not IDENTIFIER.fullmatch(params["request_id"]) or params["request_id"] == request["request_id"]):
                raise CommandError("invalid_params", "Lookup requires a different valid request ID")
            return []
        if set(params) - ({"entities", "timeout_s"} if action == "refresh_states" else {"entities"}):
            raise CommandError("invalid_params", "Unexpected monitoring parameters")
        known = self.queries.entities()
        entities = params.get("entities", list(known))
        if not isinstance(entities, list) or not 1 <= len(entities) <= 200 or any(not isinstance(item, str) or item not in known for item in entities) or len(set(entities)) != len(entities):
            raise CommandError("invalid_params", "Selection requires 1–200 unique monitored entities")
        if grant["entities"] != ["*"] and any(entity not in grant["entities"] for entity in entities):
            raise CommandError("forbidden", "Selection includes unauthorized entities")
        if action == "refresh_states":
            timeout = params.get("timeout_s", 5)
            if type(timeout) is not int or not 1 <= timeout <= 10:
                raise CommandError("invalid_params", "timeout_s must be an integer from 1 to 10")
            if expires - self.clock() < timeout:
                raise CommandError("expired", "Insufficient lifetime for the refresh observation window")
            now = time.monotonic()
            sections = self.queries.source_sections(entities)
            if now - self._last_refresh.get(request["client_id"], -math.inf) < 10 or any(now - self._last_source_refresh.get(section, -math.inf) < 5 for section in sections):
                raise CommandError("rate_limited", "Refresh rate limit exceeded")
        return entities

    def _execute(
        self, kind: str, request: dict[str, Any], expires: float,
        prepared: list[ActionStep] | list[str], accepted: dict[str, Any],
    ) -> None:
        """Publish admission, persist intent and dispatch; never automatically retry."""
        completed = 0
        attempted = False
        progress: dict[str, int] = {}
        result = accepted
        start = time.monotonic()
        try:
            self._publish(accepted, wait=True)
            if self.clock() >= expires:
                raise CommandError("expired", "Request expired before dispatch")
            if kind == "query":
                entities = [item for item in prepared if isinstance(item, str)]
                result = self._query(request, entities, expires)
            else:
                plan = [item for item in prepared if isinstance(item, ActionStep)]
                for step in plan:
                    if step.wait:
                        self.coach.sleep(step.wait)
                    source = self._check_source(step.section)
                    if not attempted:
                        if self.clock() >= expires:
                            raise CommandError("expired", "Request expired before the first write")
                        self.journal.save(request, accepted, intent=True)
                        source = self._check_source(step.section)
                        if self.clock() >= expires:
                            raise CommandError("expired", "Request expired while recording execution intent")
                    attempted = True
                    source.execute_command(step.payload)
                    completed += 1
                    progress[step.entity] = progress.get(step.entity, 0) + 1
                result = self.result(request, "completed", "executed", "Configured action dispatched; physical state is not confirmed")
                result.update(outcome="dispatched", steps_completed=completed, data={"entities": [{"entity": name, "steps_completed": count} for name, count in progress.items()]})
        except CommandError as error:
            result = self.result(request, "failed" if attempted else "rejected", error.code, str(error))
            result.update(outcome="partial" if completed else "not_started", steps_completed=completed)
        except Exception as error:
            LOGGER.error("Coach request execution failed: client=%s request=%s error_type=%s", request["client_id"], request["request_id"], type(error).__name__)
            result = self.result(request, "failed" if attempted or kind == "query" else "rejected", "execution_failed", "Dispatch failed; inspect telemetry before any new request")
            result.update(outcome="unknown" if attempted else "not_started", steps_completed=completed)
        finally:
            if kind == "command" and completed and result["status"] != "completed":
                result["data"] = {"entities": [{"entity": name, "steps_completed": count} for name, count in progress.items()]}
            try:
                size = len(json.dumps(result, allow_nan=False).encode())
            except (ValueError, TypeError, OverflowError):
                LOGGER.exception("Coach result cannot be encoded: client=%s request=%s", request["client_id"], request["request_id"])
                result = self.result(request, "failed", "execution_failed", "Monitoring result could not be represented as finite JSON")
                size = 0
            if size > MAX_RESULT:
                result = self.result(request, "rejected", "response_too_large", "Requested result exceeds 256 KiB")
            try:
                self.journal.save(request, result)
                self._publish(result, wait=True)
            except Exception:
                LOGGER.exception("Unable to persist/publish coach terminal result: client=%s request=%s", request["client_id"], request["request_id"])
            LOGGER.info("Coach request client=%s request=%s target=%s action=%s status=%s code=%s outcome=%s duration=%.3f", request["client_id"], request["request_id"], request["target"], request["action"], result["status"], result["code"], result["outcome"], time.monotonic() - start)
            self._busy.release()

    def _query(self, request: dict[str, Any], entities: list[str], expires: float) -> dict[str, Any]:
        """Run only authorized safe queries; never call the coach action executor."""
        action = request["action"]
        if action in ENTITY_QUERIES:
            if action == "refresh_states":
                if expires - self.clock() < request["params"].get("timeout_s", 5):
                    raise CommandError("expired", "Insufficient lifetime for refresh")
                now = time.monotonic()
                self._last_refresh[request["client_id"]] = now
                for section in self.queries.source_sections(entities):
                    self._last_source_refresh[section] = now
                self.journal.save(request, self.result(request, "accepted", "accepted", "Refresh intent recorded"), intent=True)
                if expires - self.clock() < request["params"].get("timeout_s", 5):
                    raise CommandError("expired", "Refresh expired while recording its intent")
            fragment = self.queries.run(action, request["params"], entities, expires - self.clock())
            result = self.result(request, fragment["status"], fragment["code"], "Monitoring operation finished")
            result["data"] = fragment["data"]
            return result
        if action == "request_status":
            recorded = self.journal.lookup(request["client_id"], request["params"]["request_id"])
            if recorded is None:
                raise CommandError("request_not_found", "No request found for this controller")
            data = {"result": recorded[1]}
        elif action == "capabilities":
            data = self._capabilities(request["client_id"])
        else:
            data = {
                "uptime_s": round(time.monotonic() - self.started, 3),
                "ready": not self._closing and self.connected() and bool(self.sources),
                "broker_connected": self.connected(), "busy": False,
                "sources": [{"type": source.source_name, "available": source.is_alive(), "commands_available": source.command_ready(), "monitoring_supported": source.supports_status_requests} for source in self.sources.values()],
            }
        result = self.result(request, "completed", "queried", "Monitoring operation finished")
        result["data"] = data
        return result

    def _capabilities(self, client: str) -> dict[str, Any]:
        """Describe only authorized actions and monitoring, with no transport secrets."""
        policy = self.policy.controllers[client]
        targets: dict[str, Any] = {}
        for target, grants in policy.get("actions", {}).items():
            actions: dict[str, Any] = {}
            for action, grant in grants.items():
                schema = self.policy.schema(target, action, grant)
                requires_confirmation = bool(grant.get("confirm"))
                reasons: set[str] = set()
                groups: list[tuple[str, str, dict[str, Any]]] = []
                expanded = self.policy.expand(target, action, {}, groups=groups)
                requires_confirmation |= any(self.policy.grant(client, group, group_action).get("confirm", False) for group, group_action, _params in groups)
                for entity, member_action, _params in expanded:
                    definition = self.coach.entities[entity]
                    requires_confirmation |= bool(definition.get("confirm") or self.policy.grant(client, entity, member_action).get("confirm"))
                    try:
                        section = self.coach.resolve_config_section(definition, entity)
                        self._check_source(section)
                    except CommandError as error:
                        reasons.add(error.code)
                    except (ValueError, KeyError):
                        reasons.add("source_unavailable")
                actions[action] = {"params": schema, "confirm": requires_confirmation, "available": not reasons, "reasons": sorted(reasons)}
            targets[target] = {"actions": actions}
        allowed: set[str] = set()
        for action, grant in policy.get("queries", {}).items():
            if action in ENTITY_QUERIES:
                allowed.update(self.queries.entities() if grant["entities"] == ["*"] else grant["entities"])
        monitoring: dict[str, Any] = {}
        for entity, fields in self.queries.entities().items():
            if entity not in allowed:
                continue
            sections = self.queries.source_sections([entity])
            sources = [self.sources.get(section) for section in sections]
            monitoring[entity] = {"fields": fields, "refresh_supported": bool(sources) and all(source is not None and source.supports_status_requests for source in sources), "available": bool(sources) and all(source is not None and source.is_alive() for source in sources)}
        return {"targets": targets, "monitoring": monitoring, "queries": policy.get("queries", {})}

    def close(self) -> None:
        """Stop admission and finish the bounded worker before closing its journal."""
        with self._admission:
            self._closing = True
        if self._worker:
            self._worker.join()
        self.journal.close()


def api_enabled(config: ConfigParser) -> bool:
    """Return whether either explicitly enabled semantic API requires setup."""
    return config.getboolean("coach_commands", "enabled", fallback=False) or config.getboolean("coach_commands", "queries_enabled", fallback=False)
