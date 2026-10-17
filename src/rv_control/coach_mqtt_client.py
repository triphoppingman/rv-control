from __future__ import annotations

import json
import math
import re
import threading
import time
import uuid
from configparser import ConfigParser
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import click
import paho.mqtt.client as mqtt

from .targets.base import Target

IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
QUERY_ACTIONS = {
    "refresh_states",
    "snapshot",
    "republish_states",
    "capabilities",
    "health",
    "request_status",
}
TERMINAL_STATUSES = {"completed", "rejected", "failed"}
MAX_RESULT_BYTES = 256 * 1024
MAX_REQUEST_BYTES = 8192


class CoachMqttClientError(RuntimeError):
    """Describe an MQTT exchange failure suitable for presentation by Click."""


def validate_identifier(value: str, name: str) -> str:
    """Validate and return an application-level identifier from the protocol."""
    if not IDENTIFIER_PATTERN.fullmatch(value):
        raise ValueError(f"{name} must match [A-Za-z0-9_-]{{1,64}}")
    return value


def validate_entity_id(value: str) -> str:
    """Reject empty entity IDs while leaving coach-specific naming to the controller."""
    if not value:
        raise ValueError("entity identifiers must not be empty")
    return value


def utc_timestamp(value: datetime) -> str:
    """Format an aware datetime as a UTC RFC 3339 timestamp."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("request timestamp clock must return an aware datetime")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def build_envelope(
    client_id: str,
    request_id: str,
    target: str,
    action: str,
    params: dict[str, Any],
    confirmed: bool,
    ttl: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build a version-one command or query request envelope with UTC expiry."""
    validate_identifier(client_id, "client_id")
    validate_identifier(request_id, "request_id")
    if not 1 <= ttl <= 15:
        raise ValueError("ttl must be from 1 through 15 seconds")
    if not target or not action:
        raise ValueError("target and action must not be empty")
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    if not isinstance(confirmed, bool):
        raise ValueError("confirmed must be a JSON boolean")
    issued = now or datetime.now(timezone.utc)
    expires = issued + timedelta(seconds=ttl)
    envelope = {
        "version": 1,
        "client_id": client_id,
        "request_id": request_id,
        "issued_at": utc_timestamp(issued),
        "expires_at": utc_timestamp(expires),
        "target": target,
        "action": action,
        "params": params,
        "confirmed": confirmed,
    }
    return envelope


def parse_scalar_parameter(value: str) -> Any:
    """Parse one JSON scalar parameter while rejecting containers and non-finite numbers."""
    def reject_constant(constant: str) -> None:
        """Reject JavaScript-style NaN and infinity tokens that are not valid JSON."""
        raise ValueError(f"{constant} is not a valid JSON scalar")

    try:
        parsed = json.loads(value, parse_constant=reject_constant)
    except (json.JSONDecodeError, ValueError) as error:
        raise ValueError(f"parameter value must be a JSON scalar: {error}") from error
    if isinstance(parsed, (dict, list)):
        raise ValueError("parameter value must be a JSON scalar, not an object or array")
    if isinstance(parsed, float) and not math.isfinite(parsed):
        raise ValueError("parameter value must be finite")
    return parsed


def parse_parameters(pairs: tuple[tuple[str, str], ...], *, command: bool) -> dict[str, Any]:
    """Parse unique key/value command-line pairs into a JSON-safe parameter mapping."""
    params: dict[str, Any] = {}
    for key, raw_value in pairs:
        if not key or key in params:
            raise ValueError(f"parameter key {key!r} is empty or duplicated")
        if command and key == "confirm":
            raise ValueError("use --confirm instead of a confirm parameter")
        params[key] = parse_scalar_parameter(raw_value)
    return params


def _configured_mqtt(config: ConfigParser) -> tuple[str, str, int, str, str]:
    """Resolve the enabled MQTT target and return its topic, credentials, host, and port."""
    section_name = Target.section_for_type(config, "mqtt")
    if section_name is None:
        raise ValueError("no MQTT target is enabled")
    section = config[section_name]
    host = section.get("host", "localhost").strip()
    port = section.getint("port", fallback=1883)
    username = section.get("username", "")
    password = section.get("password", "")
    base_topic = section.get("base_topic", "rv").strip("/")
    if not host:
        raise ValueError(f"MQTT target [{section_name}] has an empty host")
    if not 1 <= port <= 65535:
        raise ValueError(f"MQTT target [{section_name}] port must be from 1 through 65535")
    if any(char in base_topic for char in ("+", "#", "\x00")):
        raise ValueError(f"MQTT target [{section_name}] has an invalid base_topic")
    return base_topic, host, port, username, password


def _topic(base_topic: str, request_type: str) -> str:
    """Return the configured command or query request topic."""
    if request_type not in {"command", "query"}:
        raise ValueError("request_type must be command or query")
    return "/".join(part for part in (base_topic, "coach", request_type) if part)


def _result_topic(base_topic: str, client_id: str, request_id: str) -> str:
    """Derive the only permitted result topic from validated application identifiers."""
    return "/".join(part for part in (
        base_topic, "coach", "result", validate_identifier(client_id, "client_id"),
        validate_identifier(request_id, "request_id"),
    ) if part)


class CoachMqttClient:
    """Perform one correlated Coach MQTT request without retrying a request."""

    def __init__(
        self,
        config: ConfigParser,
        client_id: str,
        request_id: str,
        timeout: float,
        client_factory: Callable[..., Any] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """Create a clean-session MQTT client and initialize callback coordination."""
        self.base_topic, self.host, self.port, username, password = _configured_mqtt(config)
        self.client_id = validate_identifier(client_id, "client_id")
        self.request_id = validate_identifier(request_id, "request_id")
        self.timeout = timeout
        self._monotonic = monotonic
        self.deadline = self._monotonic() + timeout
        factory = client_factory or mqtt.Client
        self.client = factory(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"rv-cli-{uuid.uuid4().hex}",
            clean_session=True,
            protocol=mqtt.MQTTv311,
        )
        self.client.connect_timeout = min(timeout, 10.0)
        if username:
            self.client.username_pw_set(username, password)
        self.connected = threading.Event()
        self.subscribed = threading.Event()
        self.result_received = threading.Event()
        self._state_lock = threading.Lock()
        self._subscribe_mid: int | None = None
        self._suback_results: dict[int, Any] = {}
        self._failure: str | None = None
        self._result: dict[str, Any] | None = None
        self._accepted: dict[str, Any] | None = None
        self._expected_target: str | None = None
        self._expected_action: str | None = None
        self.client.on_connect = self._on_connect
        self.client.on_subscribe = self._on_subscribe
        self.client.on_message = self._on_message
        self.client.on_disconnect = self._on_disconnect

    def _remaining(self) -> float:
        """Return the remaining wall-clock budget or fail when the request timed out."""
        remaining = self.deadline - self._monotonic()
        if remaining <= 0:
            raise CoachMqttClientError("timed out waiting for MQTT request result")
        return remaining

    def _on_connect(self, _client: Any, _userdata: Any, _flags: Any, reason_code: Any, _properties: Any = None) -> None:
        """Record broker connection success or its refusal reason."""
        if reason_code == 0:
            self.connected.set()
        else:
            self._set_failure(f"MQTT connection refused: {reason_code}")

    def _on_subscribe(
        self,
        _client: Any,
        _userdata: Any,
        mid: int,
        reason_codes: Any,
        _properties: Any = None,
    ) -> None:
        """Record SUBACK and wake the sender only after the exact subscription is accepted."""
        codes = list(reason_codes or [])
        failure = next(
            (
                code
                for code in codes
                if bool(getattr(code, "is_failure", False)) or (isinstance(code, int) and code >= 128)
            ),
            None,
        )
        with self._state_lock:
            self._suback_results[mid] = failure
            if mid == self._subscribe_mid:
                if failure is None:
                    self.subscribed.set()
                else:
                    self._failure = f"MQTT result subscription rejected: {failure}"
                    self.subscribed.set()

    def _on_message(self, _client: Any, _userdata: Any, message: Any) -> None:
        """Accept only bounded, correlated result payloads from the derived result topic."""
        expected_topic = _result_topic(self.base_topic, self.client_id, self.request_id)
        if (
            message.topic != expected_topic
            or bool(getattr(message, "retain", False))
            or len(message.payload) > MAX_RESULT_BYTES
        ):
            return
        try:
            result = json.loads(message.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if not isinstance(result, dict):
            return
        if (
            result.get("version") != 1
            or result.get("client_id") != self.client_id
            or result.get("request_id") != self.request_id
            or result.get("target") != self._expected_target
            or result.get("action") != self._expected_action
            or not isinstance(result.get("status"), str)
        ):
            return
        if result["status"] == "accepted":
            self._accepted = result
        elif result["status"] in TERMINAL_STATUSES:
            self._result = result
            self.result_received.set()

    def _on_disconnect(
        self,
        _client: Any,
        _userdata: Any,
        _disconnect_flags: Any,
        reason_code: Any,
        _properties: Any = None,
    ) -> None:
        """Wake pending work when the broker drops the connection unexpectedly."""
        if reason_code != 0:
            self._set_failure(f"MQTT disconnected: {reason_code}")

    def _set_failure(self, message: str) -> None:
        """Store a single asynchronous client failure and release pending waits."""
        self._failure = message
        self.connected.set()
        self.subscribed.set()
        self.result_received.set()

    def _check_failure(self) -> None:
        """Raise any error recorded asynchronously by an MQTT callback."""
        if self._failure:
            raise CoachMqttClientError(self._failure)

    def exchange(self, request_type: str, envelope: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """Subscribe before publishing once, then return the terminal result and any acceptance."""
        if request_type not in {"command", "query"}:
            raise ValueError("request_type must be command or query")
        self._expected_target = envelope["target"]
        self._expected_action = envelope["action"]
        topic = _topic(self.base_topic, request_type)
        result_topic = _result_topic(self.base_topic, self.client_id, self.request_id)
        connection_attempted = False
        loop_started = False
        try:
            connection_attempted = True
            self.client.connect(self.host, self.port, keepalive=30)
            self.client.loop_start()
            loop_started = True
            if not self.connected.wait(self._remaining()):
                raise CoachMqttClientError("timed out connecting to MQTT broker")
            self._check_failure()

            subscribe_rc, subscribe_mid = self.client.subscribe(result_topic, qos=1)
            if subscribe_rc != mqtt.MQTT_ERR_SUCCESS:
                raise CoachMqttClientError(f"MQTT result subscription failed: rc={subscribe_rc}")
            with self._state_lock:
                self._subscribe_mid = subscribe_mid
                if subscribe_mid in self._suback_results:
                    failure = self._suback_results[subscribe_mid]
                    if failure is None:
                        self.subscribed.set()
                    else:
                        self._failure = f"MQTT result subscription rejected: {failure}"
                        self.subscribed.set()
            if not self.subscribed.wait(self._remaining()):
                raise CoachMqttClientError("timed out waiting for MQTT result subscription acknowledgement")
            self._check_failure()

            payload = json.dumps(envelope, separators=(",", ":"), allow_nan=False)
            if len(payload.encode("utf-8")) > MAX_REQUEST_BYTES:
                raise CoachMqttClientError(f"request envelope exceeds {MAX_REQUEST_BYTES} bytes")
            published = self.client.publish(topic, payload, qos=1, retain=False)
            if published.rc != mqtt.MQTT_ERR_SUCCESS:
                raise CoachMqttClientError(f"MQTT request publish failed: rc={published.rc}")
            if hasattr(published, "wait_for_publish"):
                published.wait_for_publish(timeout=self._remaining())
                if hasattr(published, "is_published") and not published.is_published():
                    raise CoachMqttClientError("timed out waiting for MQTT request delivery")

            if not self.result_received.wait(self._remaining()):
                raise CoachMqttClientError("timed out waiting for terminal MQTT result")
            self._check_failure()
            if self._result is None:
                raise CoachMqttClientError("MQTT result was not received")
            return self._result, self._accepted
        finally:
            self.result_received.set()
            try:
                if loop_started:
                    self.client.loop_stop()
            finally:
                if connection_attempted:
                    self.client.disconnect()


def _run_request(
    config: ConfigParser,
    request_type: str,
    client_id: str,
    request_id: str,
    target: str,
    action: str,
    params: dict[str, Any],
    confirmed: bool,
    ttl: int,
    timeout: float,
) -> None:
    """Build, send, and print one command/query request with Click-friendly failures."""
    if timeout < ttl:
        raise click.BadParameter("--timeout must be greater than or equal to --ttl", param_hint="--timeout")
    try:
        envelope = build_envelope(client_id, request_id, target, action, params, confirmed, ttl)
        result, accepted = CoachMqttClient(config, client_id, request_id, timeout).exchange(request_type, envelope)
    except (CoachMqttClientError, OSError, RuntimeError, ValueError, TypeError, mqtt.MQTTException) as error:
        raise click.ClickException(str(error)) from error
    if accepted is not None:
        click.echo(json.dumps(accepted, indent=2, sort_keys=True, allow_nan=False), err=True)
    click.echo(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
    if result["status"] != "completed":
        raise click.ClickException(f"{result.get('code', 'request_failed')}: {result.get('message', 'request failed')}")


@click.command("mqtt-command")
@click.argument("target", type=str)
@click.argument("action", type=str)
@click.option("--client-id", default="rv-cli", show_default=True, help="Application-level controller identity.")
@click.option("--request-id", default=None, help="Request identifier; generated when omitted.")
@click.option("--ttl", default=15, type=click.IntRange(1, 15), show_default=True, help="Request lifetime in seconds.")
@click.option("--timeout", default=45.0, type=click.FloatRange(min=1.0, max=300.0), show_default=True, help="Maximum MQTT exchange time in seconds.")
@click.option("--confirm", is_flag=True, help="Assert that the user deliberately confirmed this action.")
@click.option("--param", "-p", "params", multiple=True, type=(str, str), help="Set a parameter as KEY JSON_SCALAR.")
@click.pass_obj
def mqtt_command(
    config: ConfigParser,
    target: str,
    action: str,
    client_id: str,
    request_id: str | None,
    ttl: int,
    timeout: float,
    confirm: bool,
    params: tuple[tuple[str, str], ...],
) -> None:
    """Send one semantic coach TARGET ACTION request over MQTT."""
    if not target or not action:
        raise click.UsageError("TARGET and ACTION must not be empty")
    request_id = request_id or str(uuid.uuid4())
    try:
        parsed_params = parse_parameters(params, command=True)
    except ValueError as error:
        raise click.BadParameter(str(error), param_hint="--param") from error
    _run_request(config, "command", client_id, request_id, target, action, parsed_params, confirm, ttl, timeout)


def _query_params(
    action: str,
    params: tuple[tuple[str, str], ...],
    entities: tuple[str, ...],
    refresh_timeout: int | None,
    lookup_request_id: str | None,
    envelope_request_id: str,
) -> dict[str, Any]:
    """Validate action-specific query arguments and return protocol parameters."""
    if action not in QUERY_ACTIONS:
        raise click.BadParameter(f"unsupported query action {action!r}", param_hint="ACTION")
    try:
        result = parse_parameters(params, command=False)
    except ValueError as error:
        raise click.BadParameter(str(error), param_hint="--param") from error

    allowed_keys = {
        "refresh_states": {"entities", "timeout_s"},
        "snapshot": {"entities"},
        "republish_states": {"entities"},
        "capabilities": set(),
        "health": set(),
        "request_status": {"request_id"},
    }[action]
    unknown = sorted(set(result) - allowed_keys)
    if unknown:
        raise click.BadParameter(f"parameters not valid for {action}: {', '.join(unknown)}", param_hint="--param")

    if entities and action not in {"refresh_states", "snapshot", "republish_states"}:
        raise click.BadParameter(f"--entity is not valid for {action}", param_hint="--entity")
    if refresh_timeout is not None and action != "refresh_states":
        raise click.BadParameter(f"--refresh-timeout is only valid for refresh_states", param_hint="--refresh-timeout")
    if lookup_request_id is not None and action != "request_status":
        raise click.BadParameter(f"--lookup-request-id is only valid for request_status", param_hint="--lookup-request-id")

    if entities:
        if len(entities) > 200:
            raise click.BadParameter("--entity may be specified at most 200 times", param_hint="--entity")
        for entity in entities:
            try:
                validate_entity_id(entity)
            except ValueError as error:
                raise click.BadParameter(str(error), param_hint="--entity") from error
        if len(set(entities)) != len(entities):
            raise click.BadParameter("--entity values must be unique", param_hint="--entity")
        if "entities" in result:
            raise click.BadParameter("entities cannot be set with both --entity and --param", param_hint="--param")
        result["entities"] = list(entities)

    if refresh_timeout is not None:
        if "timeout_s" in result:
            raise click.BadParameter("timeout_s cannot be set with both --refresh-timeout and --param", param_hint="--param")
        result["timeout_s"] = refresh_timeout
    if "timeout_s" in result:
        timeout_s = result["timeout_s"]
        if isinstance(timeout_s, bool) or not isinstance(timeout_s, int) or not 1 <= timeout_s <= 10:
            raise click.BadParameter("timeout_s must be an integer from 1 through 10", param_hint="--param")

    lookup = lookup_request_id
    if lookup is not None:
        try:
            validate_identifier(lookup, "lookup request_id")
        except ValueError as error:
            raise click.BadParameter(str(error), param_hint="--lookup-request-id") from error
        if "request_id" in result:
            raise click.BadParameter("request_id cannot be set with both --lookup-request-id and --param", param_hint="--param")
        result["request_id"] = lookup
    if action == "request_status":
        if not isinstance(result.get("request_id"), str):
            raise click.BadParameter("request_status requires --lookup-request-id or -p request_id JSON_STRING")
        try:
            validate_identifier(result["request_id"], "lookup request_id")
        except ValueError as error:
            raise click.BadParameter(str(error), param_hint="--lookup-request-id") from error
        if result["request_id"] == envelope_request_id:
            raise click.BadParameter("the looked-up request ID must differ from this request's --request-id")
    elif "request_id" in result:
        raise click.BadParameter(f"request_id is not valid for {action}", param_hint="--param")
    if "entities" in result:
        entity_values = result["entities"]
        if not isinstance(entity_values, list) or not entity_values:
            raise click.BadParameter("entities must be a non-empty list", param_hint="--param")
        if len(entity_values) > 200 or any(not isinstance(entity, str) for entity in entity_values):
            raise click.BadParameter("entities must contain 1 through 200 entity identifiers", param_hint="--param")
        for entity in entity_values:
            try:
                validate_entity_id(entity)
            except ValueError as error:
                raise click.BadParameter(str(error), param_hint="--param") from error
        if len(set(entity_values)) != len(entity_values):
            raise click.BadParameter("entities must be unique", param_hint="--param")
    return result


@click.command("mqtt-query")
@click.argument("action", type=str)
@click.option("--client-id", default="rv-cli", show_default=True, help="Application-level controller identity.")
@click.option("--request-id", default=None, help="Request identifier; generated when omitted.")
@click.option("--ttl", default=15, type=click.IntRange(1, 15), show_default=True, help="Request lifetime in seconds.")
@click.option("--timeout", default=45.0, type=click.FloatRange(min=1.0, max=300.0), show_default=True, help="Maximum MQTT exchange time in seconds.")
@click.option("--param", "-p", "params", multiple=True, type=(str, str), help="Set a query parameter as KEY JSON_SCALAR.")
@click.option("--entity", multiple=True, help="Select an entity; repeat for multiple entities.")
@click.option("--refresh-timeout", type=click.IntRange(1, 10), help="Refresh observation window in seconds.")
@click.option("--lookup-request-id", help="Request ID to retrieve with request_status.")
@click.pass_obj
def mqtt_query(
    config: ConfigParser,
    action: str,
    client_id: str,
    request_id: str | None,
    ttl: int,
    timeout: float,
    params: tuple[tuple[str, str], ...],
    entity: tuple[str, ...],
    refresh_timeout: int | None,
    lookup_request_id: str | None,
) -> None:
    """Send one read-only Coach monitoring ACTION request over MQTT."""
    request_id = request_id or str(uuid.uuid4())
    query_params = _query_params(action, params, entity, refresh_timeout, lookup_request_id, request_id)
    _run_request(config, "query", client_id, request_id, "coach", action, query_params, False, ttl, timeout)
