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
import uuid
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
from .operating_states import STATE_TOPIC
from .sources.base import Source
from .source_control import MAX_DURATION_S, MAX_REASON, SOURCE_ACTIONS, SOURCE_VERBS, SourceController, controllable_sections
from .sources.writable import SourceAction, WritableSource

LOGGER = logging.getLogger(__name__)
IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
MAX_PAYLOAD = 8192
MAX_RESULT = 256 * 1024
QUERY_ACTIONS = {"refresh_states", "snapshot", "republish_states", "capabilities", "health", "request_status", "sources", "states", "routines", "actions"}
SOURCE_SETTLE_SECONDS = 5.0
EXECUTION_TOPIC = "coach/execution"
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
    action: SourceAction


@dataclass(frozen=True)
class PreparedCommand:
    """A complete command plan that passed authorization and transport preflight."""

    steps: tuple[ActionStep, ...]


@dataclass(frozen=True)
class PreparedQuery:
    """A validated query selection admitted for asynchronous execution."""

    entities: tuple[str, ...]


@dataclass(frozen=True)
class PreparedSourceControl:
    """A validated request to enable or disable one allowlisted source."""

    section: str
    enabled: bool
    duration_s: int | None
    reason: str


@dataclass(frozen=True)
class PreparedState:
    """One authorized operating-state recipe, including ordered source lifecycle operations."""

    name: str
    steps: tuple[ActionStep | PreparedSourceControl, ...]
    unchanged: bool = False
    routine: bool = False


PreparedOperation = PreparedCommand | PreparedQuery | PreparedSourceControl | PreparedState


class ControllerPolicy:
    """Validate static controller grants and bind them to loaded action schemas."""

    def __init__(
        self, coach: Coach, path: Path, monitored: dict[str, list[str]],
        source_control: tuple[str, ...] = (),
    ) -> None:
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
        for client, policy in self.controllers.items():
            if not isinstance(client, str) or not IDENTIFIER.fullmatch(client) or not isinstance(policy, dict) or set(policy) - {"actions", "queries", "sources", "states", "routines"}:
                raise ValueError("Invalid controller policy")
            self._validate_source_grants(policy.get("sources", {}), source_control)
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
                    self.schema(target, action, self.grant(client, target, action))
            for kind, definitions in (("routines", coach.routines.definitions), ("states", coach.operating_states.definitions)):
                grants = policy.get(kind, {})
                if not isinstance(grants, dict):
                    raise ValueError(f"Controller {kind} grants must be a mapping")
                for name, grant in grants.items():
                    if name not in definitions:
                        raise ValueError(f"Policy references unknown {kind} ID {name!r}")
                    if not isinstance(grant, dict) or set(grant) - {"confirm"} or type(grant.get("confirm", False)) is not bool:
                        raise ValueError(f"Invalid {kind} grant")
                    if kind == "states":
                        for routine in definitions[name]["routines"]:
                            self.recipe_grant(client, routine, routine=True)
            for name in policy.get("routines", {}):
                for step in coach.routines.definitions[name]["actions"]:
                    if "source" in step:
                        section = coach.resolve_config_section({"target": step["source"]}, step["source"])
                        if section not in source_control:
                            raise ValueError(f"Routine {name!r} source is not in source_control")
                        self.source_grant(client, section, step["action"])
                    else:
                        target, action = step["entity"], step["action"]
                        validate_params(step.get("params", {}), self.schema(target, action, self.grant(client, target, action)))

    def recipe_grant(self, client: str, name: str, *, routine: bool = False) -> dict[str, Any]:
        """Require an explicit routine/state grant in addition to every member action grant."""
        kind = "routines" if routine else "states"
        grant = self.controllers.get(client, {}).get(kind, {}).get(name)
        if grant is None:
            raise CommandError("forbidden", f"Controller is not authorized for this {kind} ID")
        return grant

    @staticmethod
    def _validate_source_grants(grants: Any, source_control: tuple[str, ...]) -> None:
        """Require source grants to name allowlisted sections and enable/disable only."""
        if not isinstance(grants, dict):
            raise ValueError("Controller source grants must be a mapping")
        for section, verbs in grants.items():
            if section not in source_control:
                raise ValueError(
                    f"Policy grants control of source {section!r}, which is not listed in [coach_commands] source_control"
                )
            if not isinstance(verbs, dict) or not verbs or set(verbs) - {"enable", "disable"}:
                raise ValueError("Source grants require enable and/or disable")
            for grant in verbs.values():
                if not isinstance(grant, dict) or set(grant) - {"confirm"} or type(grant.get("confirm", False)) is not bool:
                    raise ValueError("Invalid source grant")

    def source_grant(self, client: str, section: str, verb: str) -> dict[str, Any]:
        """Return an explicit source-control grant, denying everything unlisted."""
        grant = self.controllers.get(client, {}).get("sources", {}).get(section, {}).get(verb)
        if grant is None:
            raise CommandError("forbidden", "Controller is not authorized to control this source")
        return grant

    def definition(self, target: str) -> dict[str, Any]:
        """Return a known entity definition without opening sources."""
        if target not in self.coach.entities:
            raise ValueError(
                f"Policy references unknown target {target!r}; "
                "check [coach] specfile/overlay or remove the target's controller action grants"
            )
        return self.coach.entities[target]

    def schema(self, target: str, action: str, grant: dict[str, Any] | None = None) -> dict[str, Any]:
        """Combine declared schemas with policy bounds that may only narrow them."""
        definition = self.definition(target)
        schemas = definition.get("action_schemas", {})
        if action not in schemas:
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


class CoachRequestPlanner:
    """Authorize and preflight requests before the service admits worker execution."""

    def __init__(
        self,
        config: ConfigParser,
        coach: Coach,
        policy: ControllerPolicy,
        queries: CoachQueries,
        sources: dict[str, Source],
        mqtt_section: str,
        commands_enabled: bool,
        queries_enabled: bool,
        clock: Callable[[], float],
    ) -> None:
        """Retain the immutable dependencies and rate limits used during planning."""
        self.config = config
        self.coach = coach
        self.policy = policy
        self.queries = queries
        self.sources = sources
        self.mqtt_section = mqtt_section
        self.commands_enabled = commands_enabled
        self.queries_enabled = queries_enabled
        self.clock = clock
        self._last_refresh: dict[str, float] = {}
        self._last_source_refresh: dict[str, float] = {}
        self.source_controller: SourceController | None = None

    def validate_query(self, request: dict[str, Any], expires: float) -> list[str]:
        """Expose query validation as the planner's single validation entry point."""
        return self._validate_query(request, expires)

    def prepare(self, kind: str, request: dict[str, Any], expires: float) -> PreparedOperation:
        """Return a fully validated command or query without performing I/O."""
        if kind == "query":
            if not self.queries_enabled:
                raise CommandError("forbidden", "Monitoring API is disabled")
            return PreparedQuery(tuple(self.validate_query(request, expires)))
        if kind != "command" or not self.commands_enabled:
            raise CommandError("forbidden", "Command API is disabled")
        if request["target"] == "coach" and request["action"] in SOURCE_ACTIONS:
            return self.prepare_source_control(request)
        if request["target"] == "coach" and request["action"] == "set_state":
            return self.prepare_state(request)
        if request["target"] == "coach" and request["action"] == "execute_routine":
            return self.prepare_state(request, routine=True)
        if request["target"] == "coach" or request["action"] in QUERY_ACTIONS:
            raise CommandError("invalid_request", "Monitoring requires the query topic")
        return PreparedCommand(tuple(self.plan(request)))

    def plan(self, request: dict[str, Any], *, availability: bool = True) -> list[ActionStep]:
        """Authorize an entity action and build every physical operation before dispatch."""
        client, target, action = request["client_id"], request["target"], request["action"]
        if target not in self.coach.entities:
            raise CommandError("unknown_target", "Unknown target")
        if action not in self.policy.definition(target).get("actions", {}):
            raise CommandError("unknown_action", "Unknown action")

        grant = self.policy.grant(client, target, action)
        validate_params(request["params"], self.policy.schema(target, action, grant))
        entity = self.coach.entities[target]
        if (entity.get("confirm") or grant.get("confirm")) and not request["confirmed"]:
            raise CommandError("confirmation_required", "Deliberate user confirmation is required")
        try:
            section = self.coach.resolve_config_section(entity, target)
            frames = self.coach.prepare_source_actions(target, action, request["params"])
        except (KeyError, ValueError, PermissionError) as error:
            raise CommandError("invalid_params", "Configured action cannot be preflighted") from error
        if not frames:
            raise CommandError("invalid_request", "Action has no transport operations")
        if availability:
            self.check_source(section)
        plan: list[ActionStep] = []
        duration = 0.0
        for wait, prepared in frames:
            duration += wait + prepared.dispatch_budget_seconds
            plan.append(ActionStep(target, section, wait, prepared))
            if len(plan) > 64 or duration > 30:
                raise CommandError("invalid_request", "Action plan exceeds 64 steps or 30 seconds")
        return plan

    def prepare_state(self, request: dict[str, Any], *, routine: bool = False) -> PreparedState:
        """Preflight a routine or whole state before writes or source enablement changes."""
        params = request["params"]
        key = "routine" if routine else "state"
        allowed = {key} if routine else {key, "reapply"}
        if set(params) - allowed or key not in params or type(params.get("reapply", False)) is not bool:
            raise CommandError("invalid_params", f"Expected {key}" + ("" if routine else " and optional boolean reapply"))
        name = params[key]
        states = self.coach.operating_states
        definitions = self.coach.routines.definitions if routine else states.definitions
        if not isinstance(name, str) or name not in definitions:
            raise CommandError("unknown_target", f"Unknown {key}")
        grant = self.policy.recipe_grant(request["client_id"], name, routine=routine)
        definition = definitions[name]
        if (grant.get("confirm") or definition.get("confirm")) and not request["confirmed"]:
            raise CommandError("confirmation_required", "Deliberate user confirmation is required")
        snapshot = states.snapshot()
        if not routine and snapshot["current"] == name and snapshot["application_status"] == "applied" and not params.get("reapply", False):
            return PreparedState(name, (), unchanged=True)
        actions: list[dict[str, Any]] = []
        for routine_name in [name] if routine else definition["routines"]:
            routine_grant = self.policy.recipe_grant(request["client_id"], routine_name, routine=True)
            routine_definition = self.coach.routines.definitions[routine_name]
            if (routine_grant.get("confirm") or routine_definition.get("confirm")) and not request["confirmed"]:
                raise CommandError("confirmation_required", "Deliberate user confirmation is required")
            actions.extend(routine_definition["actions"])
            if len(actions) > 64:
                raise CommandError("invalid_request", "Recipe exceeds 64 actions")
        steps: list[ActionStep | PreparedSourceControl] = []
        desired: dict[str, bool] = {}
        duration = 0.0
        for step in actions:
            if "source" in step:
                try:
                    section = self.coach.resolve_config_section({"target": step["source"]}, step["source"])
                except ValueError as error:
                    raise CommandError("source_unavailable", "Routine source binding is unavailable") from error
                member = dict(request, action=f"source_{step['action']}", params={"source": section, "reason": f"{key}:{name}"})
                prepared = self.prepare_source_control(member)
                desired[section] = prepared.enabled
                steps.append(prepared)
                duration += SOURCE_SETTLE_SECONDS
            else:
                member = dict(request, target=step["entity"], action=step["action"], params=step.get("params", {}))
                physical = self.plan(member, availability=False)
                for operation in physical:
                    self.check_write_gates(operation.section)
                    source_class = Source.source_class(self.config[operation.section].get("type", ""))
                    if not issubclass(source_class, WritableSource):
                        raise CommandError("source_unavailable", "Required source does not support writes")
                    if desired.get(operation.section) is False:
                        raise CommandError("source_unavailable", "Recipe uses a source after disabling it")
                    if not desired.get(operation.section, False):
                        self.check_source(operation.section)
                    duration += operation.wait + operation.action.dispatch_budget_seconds
                steps.extend(physical)
            if len(steps) > 64 or duration > 30:
                raise CommandError("invalid_request", "State plan exceeds 64 steps or 30 seconds")
        return PreparedState(name, tuple(steps), routine=routine)

    def prepare_source_control(self, request: dict[str, Any]) -> PreparedSourceControl:
        """Authorize and validate an enable/disable request without touching sources."""
        controller = self.source_controller
        if controller is None or not controller.active:
            raise CommandError("forbidden", "Source control is not configured")
        params = request["params"]
        if set(params) - {"source", "duration_s", "reason"} or "source" not in params:
            raise CommandError("invalid_params", "Source control requires source and optional duration_s and reason")
        section = params["source"]
        if not isinstance(section, str) or section not in controller.controllable:
            raise CommandError("unknown_target", "Source is not controllable")
        grant = self.policy.source_grant(request["client_id"], section, SOURCE_VERBS[request["action"]])
        if grant.get("confirm") and not request["confirmed"]:
            raise CommandError("confirmation_required", "Deliberate user confirmation is required")
        duration = params.get("duration_s")
        if duration is not None and (type(duration) is not int or not 1 <= duration <= MAX_DURATION_S):
            raise CommandError("invalid_params", f"duration_s must be an integer from 1 to {MAX_DURATION_S}")
        reason = params.get("reason", "")
        if not isinstance(reason, str) or len(reason) > MAX_REASON or not reason.isprintable():
            raise CommandError("invalid_params", f"reason must be printable text of at most {MAX_REASON} characters")
        return PreparedSourceControl(section, SOURCE_ACTIONS[request["action"]], duration, reason)

    def check_write_gates(self, section: str) -> None:
        """Check command, MQTT and source write gates even for a not-yet-started source."""
        if (
            not self.commands_enabled
            or not self.config[self.mqtt_section].getboolean("write_enabled", fallback=False)
            or not self.config[section].getboolean("write_enabled", fallback=False)
        ):
            raise CommandError("writes_disabled", "Physical writes are disabled")

    def check_source(self, section: str) -> WritableSource:
        """Recheck global and source write gates plus readiness before each operation."""
        self.check_write_gates(section)
        source = self.sources.get(section)
        if not isinstance(source, WritableSource) or not source.command_ready():
            raise CommandError("source_unavailable", "Required source is unavailable")
        return source

    def _validate_query(self, request: dict[str, Any], expires: float) -> list[str]:
        """Validate query grants, selected entities, deadlines and refresh limits."""
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
            if action == "request_status" and (
                not isinstance(params["request_id"], str)
                or not IDENTIFIER.fullmatch(params["request_id"])
                or params["request_id"] == request["request_id"]
            ):
                raise CommandError("invalid_params", "Lookup requires a different valid request ID")
            return []

        allowed = {"entities", "timeout_s"} if action == "refresh_states" else {"entities"}
        if set(params) - allowed:
            raise CommandError("invalid_params", "Unexpected query parameters")
        known = self.queries.entities()
        entities = params.get("entities", list(known))
        if (
            not isinstance(entities, list)
            or not 1 <= len(entities) <= 200
            or any(not isinstance(item, str) or item not in known for item in entities)
            or len(set(entities)) != len(entities)
        ):
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
            if (
                now - self._last_refresh.get(request["client_id"], -math.inf) < 10
                or any(now - self._last_source_refresh.get(section, -math.inf) < 5 for section in sections)
            ):
                raise CommandError("rate_limited", "Refresh rate limit exceeded")
        return entities

    def record_refresh(self, client: str, sections: set[str], now: float) -> None:
        """Start the per-client and per-source cooldown when refresh intent is journaled."""
        self._last_refresh[client] = now
        for section in sections:
            self._last_source_refresh[section] = now


class CoachCommandService:
    """Validate MQTT requests and execute one bounded plan/query outside callbacks."""

    def __init__(
        self, config: ConfigParser, coach: Coach, tracker: EntityStateTracker,
        sources: dict[str, Source], publish: PublishResult,
        connected: Callable[[], bool], clock: Callable[[], float] = utc_now,
        source_controller: SourceController | None = None,
        publish_state: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        """Validate configuration, load policy, and recover the durable journal."""
        self.config, self.coach, self.sources = config, coach, sources
        self.publish, self.connected, self.clock = publish, connected, clock
        self.queries = CoachQueries(coach, tracker, sources)
        section = config["coach_commands"]
        self.enabled = section.getboolean("enabled", fallback=False)
        self.queries_enabled = section.getboolean("queries_enabled", fallback=False)
        self.mqtt_section, self.policy, journal_path, limit = self.validate_config(config, coach, tracker)
        self.journal = RequestJournal(journal_path, limit, clock())
        self._admission = threading.Lock()
        self._busy = threading.Lock()
        self._closing = False
        self._worker: threading.Thread | None = None
        self.planner = CoachRequestPlanner(
            config, coach, self.policy, self.queries, sources, self.mqtt_section,
            self.enabled, self.queries_enabled, clock,
        )
        self.planner.source_controller = source_controller
        self.started = time.monotonic()
        self._publish_state = publish_state
        self._state_published: dict[str, Any] | None = None
        self._state_connected = False
        self._states_started = False
        self._startup_request_id: str | None = None
        self._status_lock = threading.RLock()
        self._execution: dict[str, Any] = {"version": 1, "online": True, "status": "idle"}
        self._execution_published: dict[str, Any] | None = None

    def start_operating_states(self) -> None:
        """Publish the startup selection and optionally admit its actions after sources attach."""
        if self._states_started:
            return
        self._states_started = True
        states = self.coach.operating_states
        self.tick()
        if not states.apply_on_startup:
            return
        now = self.clock()
        self._startup_request_id = str(uuid.uuid4())
        request = {
            "version": 1, "client_id": self.config["coach_commands"]["startup_client_id"],
            "request_id": self._startup_request_id, "issued_at": timestamp(now),
            "expires_at": timestamp(now + 15), "target": "coach", "action": "set_state",
            "params": {"state": states.default}, "confirmed": False,
        }
        self.receive("command", json.dumps(request).encode())

    def tick(self) -> None:
        """Publish retained operating-state changes and refresh them after broker reconnects."""
        if self._publish_state is None or not (self.coach.operating_states.definitions or self.coach.routines.definitions):
            return
        with self._status_lock:
            connected = self.connected()
            reconnected = connected and not self._state_connected
            self._state_connected = connected
            if not connected:
                return
            snapshot = self.coach.operating_states.snapshot()
            try:
                if self.coach.operating_states.definitions and (snapshot != self._state_published or reconnected):
                    self._publish_state(STATE_TOPIC, snapshot)
                    self._state_published = snapshot
                if self._execution != self._execution_published or reconnected:
                    self._publish_state(EXECUTION_TOPIC, dict(self._execution))
                    self._execution_published = dict(self._execution)
            except Exception:
                LOGGER.exception("Unable to publish coach operating-state/execution status")
                self._state_connected = False

    def execution_snapshot(self) -> dict[str, Any]:
        """Return detached progress and correlation metadata for the latest routine/state request."""
        with self._status_lock:
            return dict(self._execution)

    def _execution_update(self, **values: Any) -> None:
        """Serialize execution progress publication with supervision reconnect publication."""
        with self._status_lock:
            self._execution.update(values)
            self.tick()

    @property
    def enabled(self) -> bool:
        """Return whether physical coach commands are admitted."""
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        """Keep planner write gates aligned with the service's command gate."""
        self._enabled = value
        planner = getattr(self, "planner", None)
        if planner is not None:
            planner.commands_enabled = value

    @staticmethod
    def validate_config(
        config: ConfigParser, coach: Coach, tracker: EntityStateTracker,
    ) -> tuple[str, ControllerPolicy, Path, int]:
        """Validate command configuration and policy without opening the durable journal."""
        from .targets.base import Target

        section = config["coach_commands"]
        enabled = section.getboolean("enabled", fallback=False)
        section.getboolean("queries_enabled", fallback=False)
        mqtt_sections = [name for name, target in Target.enabled_targets(config) if target.target_name == "mqtt"]
        if len(mqtt_sections) != 1:
            raise ValueError("Coach API requires exactly one enabled MQTT target")
        mqtt_section = mqtt_sections[0]
        if enabled and not config[mqtt_section].getboolean("write_enabled", fallback=False):
            raise ValueError("Enabled coach commands require MQTT write_enabled")
        config_dir = Path(config.get("_meta", "path", fallback="config.ini")).resolve().parent
        policy_path = section.get("controllers_file", "").strip()
        if not policy_path:
            raise ValueError("Coach API requires controllers_file")
        policy = ControllerPolicy(
            coach, config_dir / policy_path, CoachQueries(coach, tracker, {}).entities(),
            controllable_sections(config),
        )
        if coach.operating_states.apply_on_startup:
            client = section.get("startup_client_id", "").strip()
            if not enabled or not IDENTIFIER.fullmatch(client):
                raise ValueError("apply_on_startup requires enabled coach commands and startup_client_id")
            default = coach.operating_states.default
            if default is None:
                raise ValueError("apply_on_startup requires a default state")
            policy.recipe_grant(client, default)
        limit = section.getint("journal_max_entries", fallback=1024)
        if not 1 <= limit <= 10000:
            raise ValueError("journal_max_entries must be between 1 and 10000")
        return mqtt_section, policy, config_dir / section.get("journal_file", "data/coach-commands.sqlite3"), limit

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
                # Serialize identity checks with shutdown and claim the single
                # execution slot before starting asynchronous work.
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
                    prepared = self.planner.prepare(kind, request, expires)
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
        if self._startup_request_id is not None and request.get("request_id") == self._startup_request_id:
            self.coach.operating_states.fail(request["params"]["state"], code, attempted=False)
        LOGGER.warning("Coach request rejected: code=%s client=%s request=%s", code, request.get("client_id") if isinstance(request.get("client_id"), str) and IDENTIFIER.fullmatch(request["client_id"]) else "invalid", request.get("request_id") if isinstance(request.get("request_id"), str) and IDENTIFIER.fullmatch(request["request_id"]) else "invalid")
        if all(isinstance(request.get(key), str) and IDENTIFIER.fullmatch(request[key]) for key in ("client_id", "request_id")):
            safe = {key: request[key] for key in ("client_id", "request_id")}
            safe.update({key: request[key] if isinstance(request.get(key), str) and IDENTIFIER.fullmatch(request[key]) else "" for key in ("target", "action")})
            try:
                self._publish(self.result(safe, "rejected", code, message))
            except (OSError, RuntimeError, ValueError):
                LOGGER.exception("Unable to publish coach rejection")

    def _plan(self, request: dict[str, Any], *, availability: bool = True) -> list[ActionStep]:
        """Preserve the service helper while delegating all planning to its planner."""
        return self.planner.plan(request, availability=availability)

    def prepare_query(self, request: dict[str, Any], expires: float) -> list[str]:
        """Preserve the service query helper while delegating to the request planner."""
        return self.planner.validate_query(request, expires)

    def _execute(
        self, kind: str, request: dict[str, Any], expires: float,
        prepared: PreparedOperation, accepted: dict[str, Any],
    ) -> None:
        """Publish admission, persist intent and dispatch; never automatically retry."""
        completed = 0
        attempted = False
        progress: dict[str, int] = {}
        result = accepted
        start = time.monotonic()
        try:
            if isinstance(prepared, PreparedState):
                with self._status_lock:
                    self._execution = {
                        "version": 1, "online": True, "status": "accepted",
                        "client_id": request["client_id"], "request_id": request["request_id"],
                        "action": request["action"], "name": prepared.name,
                        "steps_total": len(prepared.steps), "steps_completed": 0, "code": None,
                    }
                self.tick()
            self._publish(accepted, wait=True)
            if self.clock() >= expires:
                raise CommandError("expired", "Request expired before dispatch")
            if isinstance(prepared, PreparedQuery):
                result = self._query(request, list(prepared.entities), expires)
            elif isinstance(prepared, PreparedSourceControl):
                controller = self.planner.source_controller
                if controller is None:
                    raise CommandError("source_unavailable", "Source control is unavailable")
                attempted = True
                data = controller.request(
                    prepared.section, prepared.enabled, prepared.duration_s, prepared.reason, request["client_id"],
                )
                completed = 1
                result = self.result(request, "completed", "executed", "Source control recorded; the supervisor applies it within about one second")
                result.update(outcome="dispatched", steps_completed=completed, data=data)
            elif isinstance(prepared, PreparedState) and prepared.unchanged:
                result = self.result(request, "completed", "unchanged", "Operating state is already applied; use reapply to execute it again")
            else:
                if isinstance(prepared, PreparedState) and not prepared.routine:
                    self.coach.operating_states.begin(prepared.name)
                if isinstance(prepared, PreparedState):
                    self._execution_update(status="running")
                for step in prepared.steps:
                    if isinstance(step, ActionStep):
                        if step.wait:
                            self.coach.sleep(step.wait)
                        self.planner.check_source(step.section)
                    if not attempted:
                        if self.clock() >= expires:
                            raise CommandError("expired", "Request expired before the first write")
                        self.journal.save(request, accepted, intent=True)
                        # Intent must be durable before entering the hardware path;
                        # after this point a crash can leave physical outcome unknown.
                        if self.clock() >= expires:
                            raise CommandError("expired", "Request expired while recording execution intent")
                    attempted = True
                    if isinstance(step, PreparedSourceControl):
                        self._apply_state_source(step, request["client_id"])
                    else:
                        source = self.planner.check_source(step.section)
                        source.execute_action(step.action.name, step.action.params)
                        progress[step.entity] = progress.get(step.entity, 0) + 1
                    completed += 1
                    if isinstance(prepared, PreparedState) and not prepared.routine:
                        self.coach.operating_states.progress(completed)
                    if isinstance(prepared, PreparedState):
                        self._execution_update(steps_completed=completed)
                if isinstance(prepared, PreparedState) and not prepared.routine:
                    self.coach.operating_states.finish(prepared.name)
                result = self.result(request, "completed", "executed", "Configured action dispatched; physical state is not confirmed")
                result.update(outcome="dispatched", steps_completed=completed, data={"entities": [{"entity": name, "steps_completed": count} for name, count in progress.items()]})
        except CommandError as error:
            result = self.result(request, "failed" if attempted else "rejected", error.code, str(error))
            result.update(outcome="partial" if completed else ("unknown" if attempted else "not_started"), steps_completed=completed)
        except Exception as error:
            LOGGER.error("Coach request execution failed: client=%s request=%s error_type=%s", request["client_id"], request["request_id"], type(error).__name__)
            result = self.result(request, "failed" if attempted or kind == "query" else "rejected", "execution_failed", "Dispatch failed; inspect telemetry before any new request")
            result.update(outcome="unknown" if attempted else "not_started", steps_completed=completed)
        finally:
            if kind == "command" and completed and result["status"] != "completed":
                result["data"] = {"entities": [{"entity": name, "steps_completed": count} for name, count in progress.items()]}
            if isinstance(prepared, PreparedState) and not prepared.routine:
                if result["status"] != "completed":
                    self.coach.operating_states.fail(prepared.name, result["code"], attempted=attempted)
                result.setdefault("data", {})["operating_state"] = self.coach.operating_states.snapshot()
            if isinstance(prepared, PreparedState):
                self._execution_update(status=result["status"], code=result["code"], steps_completed=completed)
                result.setdefault("data", {})["execution"] = self.execution_snapshot()
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

    def _apply_state_source(self, step: PreparedSourceControl, client: str) -> None:
        """Record a source decision and wait a bounded time for the supervisor to apply it."""
        controller = self.planner.source_controller
        if controller is None:
            raise CommandError("source_unavailable", "Source control is unavailable")
        controller.request(step.section, step.enabled, reason=step.reason, client_id=client)
        deadline = time.monotonic() + SOURCE_SETTLE_SECONDS
        while time.monotonic() < deadline:
            if self._closing:
                raise CommandError("source_unavailable", "Service is shutting down")
            entries = controller.snapshot()["sources"]
            state = next(entry["state"] for entry in entries if entry["source"] == step.section)
            if not step.enabled and state == "disabled":
                return
            source = self.sources.get(step.section)
            if step.enabled and state == "running" and (
                not isinstance(source, WritableSource) or source.command_ready()
            ):
                return
            time.sleep(0.05)
        raise CommandError("source_unavailable", "Source did not reach the requested lifecycle state within five seconds")

    def _query(self, request: dict[str, Any], entities: list[str], expires: float) -> dict[str, Any]:
        """Run only authorized safe queries; never call the coach action executor."""
        action = request["action"]
        if action in ENTITY_QUERIES:
            if action == "refresh_states":
                if expires - self.clock() < request["params"].get("timeout_s", 5):
                    raise CommandError("expired", "Insufficient lifetime for refresh")
                now = time.monotonic()
                self.planner.record_refresh(
                    request["client_id"], self.queries.source_sections(entities), now,
                )
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
        elif action == "states":
            data = self._state_capabilities(request["client_id"])
            data["operating_state"] = self.coach.operating_states.snapshot()
            data["execution"] = self.execution_snapshot()
        elif action == "routines":
            data = {"routines": self._recipe_capabilities(request["client_id"], routine=True),
                    "execution": self.execution_snapshot()}
        elif action == "actions":
            capabilities = self._capabilities(request["client_id"])
            data = {"targets": capabilities["targets"], "sources": capabilities["sources"]}
        elif action == "sources":
            controller = self.planner.source_controller
            if controller is None:
                raise CommandError("source_unavailable", "Source status is unavailable")
            data = controller.snapshot()
        else:
            data = {
                "uptime_s": round(time.monotonic() - self.started, 3),
                "ready": not self._closing and self.connected() and bool(self.sources),
                "broker_connected": self.connected(), "busy": False,
                "sources": [{"source": section, "type": source.source_name, "available": source.is_alive(), "commands_available": source.command_ready(), "monitoring_supported": source.supports_status_requests} for section, source in self.sources.items()],
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
                definition = self.coach.entities[target]
                requires_confirmation |= bool(definition.get("confirm"))
                try:
                    section = self.coach.resolve_config_section(definition, target)
                    self.planner.check_source(section)
                except CommandError as error:
                    reasons.add(error.code)
                except (ValueError, KeyError):
                    reasons.add("source_unavailable")
                actions[action] = {"params": schema, "confirm": requires_confirmation, "available": not reasons, "reasons": sorted(reasons)}
            definition = self.policy.definition(target)
            targets[target] = {
                "name": definition.get("name", target), "description": definition.get("description", ""),
                "kind": definition.get("kind", ""),
                "actions": actions,
            }
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
        controller = self.planner.source_controller
        controllable = controller.controllable if controller is not None and self.enabled else ()
        source_controls = {
            section: {
                verb: {"confirm": bool(grant.get("confirm")), "available": section in controllable}
                for verb, grant in verbs.items()
            }
            for section, verbs in policy.get("sources", {}).items()
        }
        return {"targets": targets, "monitoring": monitoring, "queries": policy.get("queries", {}), "sources": source_controls,
                "operating_states": self._state_capabilities(client),
                "routines": self._recipe_capabilities(client, routine=True)}

    def _state_capabilities(self, client: str) -> dict[str, Any]:
        """List UI state metadata with caller-specific authorization and preflight availability."""
        states = self.coach.operating_states
        return {
            "default": states.default, "apply_on_startup": states.apply_on_startup,
            "states": self._recipe_capabilities(client, routine=False),
        }

    def _recipe_capabilities(self, client: str, *, routine: bool) -> dict[str, Any]:
        """Describe logical recipes, exact invocation schemas, permissions and inherited confirmation."""
        catalog = self.coach.routines.catalog() if routine else self.coach.operating_states.catalog()["states"]
        kind = "routines" if routine else "states"
        key = "routine" if routine else "state"
        action = "execute_routine" if routine else "set_state"
        for name, metadata in catalog.items():
            reasons: list[str] = []
            selectable = name in self.policy.controllers[client].get(kind, {})
            try:
                self.planner.prepare_state({
                    "client_id": client, "target": "coach", "action": action,
                    "params": {key: name} if routine else {key: name, "reapply": True}, "confirmed": True,
                }, routine=routine)
                if not self.enabled:
                    reasons.append("writes_disabled")
            except CommandError as error:
                reasons.append(error.code)
            confirm = bool(metadata["confirm"] or self.policy.controllers[client].get(kind, {}).get(name, {}).get("confirm"))
            routines = [name] if routine else metadata["routines"]
            if selectable:
                for routine_name in routines:
                    definition = self.coach.routines.definitions[routine_name]
                    confirm |= bool(definition.get("confirm") or self.policy.controllers[client].get("routines", {}).get(routine_name, {}).get("confirm"))
                    for step in definition["actions"]:
                        if "source" in step:
                            section = self.coach.resolve_config_section({"target": step["source"]}, step["source"])
                            confirm |= bool(self.policy.controllers[client].get("sources", {}).get(section, {}).get(step["action"], {}).get("confirm"))
                        else:
                            entity, member_action = step["entity"], step["action"]
                            confirm |= bool(self.coach.entities[entity].get("confirm") or self.policy.controllers[client].get("actions", {}).get(entity, {}).get(member_action, {}).get("confirm"))
            schema: dict[str, Any] = {key: {"type": "string", "enum": [name], "required": True}}
            if not routine:
                schema["reapply"] = {"type": "boolean", "required": False, "default": False}
            metadata.update(
                authorized=selectable, available=not reasons, reasons=reasons, confirm=confirm,
                command={"target": "coach", "action": action, "params": {key: name}, "schema": schema},
            )
            if not selectable and routine:
                metadata.pop("actions")
        return catalog

    def close(self) -> None:
        """Stop admission and finish the bounded worker before closing its journal."""
        with self._admission:
            self._closing = True
        if self._worker:
            self._worker.join()
        try:
            if self._publish_state is not None and self.connected():
                if self.coach.operating_states.definitions:
                    self._publish_state(STATE_TOPIC, dict(self.coach.operating_states.snapshot(), online=False))
                if self.coach.routines.definitions:
                    self._publish_state(EXECUTION_TOPIC, dict(self.execution_snapshot(), online=False))
        finally:
            self.journal.close()


def api_enabled(config: ConfigParser) -> bool:
    """Return whether either explicitly enabled semantic API requires setup."""
    return config.getboolean("coach_commands", "enabled", fallback=False) or config.getboolean("coach_commands", "queries_enabled", fallback=False)
