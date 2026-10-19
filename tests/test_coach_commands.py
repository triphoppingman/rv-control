"""Hardware-free admission, dispatch, policy and crash-recovery regressions."""

from __future__ import annotations

import json
import threading
from configparser import ConfigParser
from pathlib import Path
from typing import Any

import pytest

from rv_control.coach import Coach
from rv_control.coach_commands import (
    CoachCommandService, CommandError, ControllerPolicy, RequestJournal,
    parse_json, timestamp, validate_envelope,
)
from rv_control.coach_state import EntityStateTracker
from rv_control.sources.base import Source


class FakeSource(Source):
    """Capture reliable commands without hardware and expose controllable readiness."""

    supports_status_requests = True

    def __init__(self, config: ConfigParser) -> None:
        """Initialize an inert source with deterministic readiness."""
        super().__init__(config, None, threading.Event(), "rv_c_bus")
        self.calls: list[dict[str, Any]] = []
        self.ready = True
        self.failure: Exception | None = None
        self.before_send: Any = None

    def run(self) -> None:
        """Never start hardware collectors in these tests."""

    def comms_check(self) -> dict[str, Any]:
        """Provide the required source interface without probing."""
        return {"ok": self.ready}

    def is_alive(self) -> bool:
        """Report fake daemon liveness."""
        return self.ready

    def command_ready(self) -> bool:
        """Report fake command availability."""
        return self.ready

    def execute_command(self, payload: dict[str, Any]) -> None:
        """Capture dispatch or simulate an uncertain transport failure."""
        if self.before_send:
            self.before_send()
        if self.failure:
            raise self.failure
        self.calls.append(payload)

    def request_status(self, dgns: list[int]) -> None:
        """Do not touch a bus from query tests."""


POLICY = """
controllers:
  rv-cli:
    actions:
      kitchen_light: {on: {}, off: {}, toggle: {}}
      living_ceiling_light:
        on: {}
        off: {}
        brightness:
          params:
            level: {type: number, min: 10, max: 80}
      kitchen_fan_lid: {open: {confirm: true}, close: {confirm: true}}
      generator: {start: {confirm: true}, stop: {confirm: true}}
      all_fans: {on: {}, off: {}}
      kitchen_fan: {on: {}, off: {}}
      mid_bath_fan: {on: {}, off: {}}
      rear_bath_fan: {on: {}, off: {}}
      patio_light: {on: {}, off: {}, brightness: {}, color: {}, effect: {}, palette: {}, transition: {}, preset: {}}
    queries:
      snapshot: {entities: ["*"]}
      republish_states: {entities: ["*"]}
      refresh_states: {entities: ["*"]}
      capabilities: {}
      health: {}
      request_status: {}
  limited:
    queries:
      snapshot: {entities: [kitchen_light]}
      request_status: {}
"""


@pytest.fixture
def service_env(tmp_path: Path) -> Any:
    """Build a real coach service with only fake sources, clock and result transport."""
    policy = tmp_path / "controllers.yaml"
    policy.write_text(POLICY)
    config = ConfigParser()
    config.read_dict({
        "_meta": {"path": str(tmp_path / "config.ini")},
        "target": {"enabled-targets": "mqtt"},
        "mqtt": {"type": "mqtt", "write_enabled": "true"},
        "coach": {
            "rvc": "rv_c_bus", "wled": "wled_patio",
            "overlay": str(Path(__file__).parents[1] / "src/rv_control/coaches/wled-overlay-example.yml"),
        },
        "rv_c_bus": {"type": "rvc", "write_enabled": "true"},
        "wled_patio": {"type": "wled", "write_enabled": "false"},
        "coach_commands": {"enabled": "true", "queries_enabled": "true", "controllers_file": str(policy), "journal_file": str(tmp_path / "journal.sqlite3")},
    })
    coach = Coach.load(config)
    tracker = EntityStateTracker(coach, lambda *_args: None)
    source = FakeSource(config)
    results: list[dict[str, Any]] = []
    clock = [1_792_257_600.0]

    def publish(_topic: str, result: dict[str, Any], _wait: bool) -> None:
        """Record detached results, as a broker would."""
        results.append(json.loads(json.dumps(result)))

    service = CoachCommandService(config, coach, tracker, {"rv_c_bus": source}, publish, lambda: True, lambda: clock[0])
    yield service, source, results, clock, tracker
    service.close()


def envelope(clock: list[float], **updates: Any) -> dict[str, Any]:
    """Return a valid request with replaceable test fields."""
    return dict({
        "version": 1, "client_id": "rv-cli", "request_id": "request-1",
        "issued_at": timestamp(clock[0]), "expires_at": timestamp(clock[0] + 15),
        "target": "kitchen_light", "action": "on", "params": {}, "confirmed": False,
    }, **updates)


def dispatch(env: Any, request: dict[str, Any], kind: str = "command", retained: bool = False) -> dict[str, Any]:
    """Submit and join a bounded service worker without sleeps."""
    service, _source, results, _clock, _tracker = env
    service.receive(kind, json.dumps(request).encode(), retained)
    if service._worker:
        service._worker.join(timeout=3)
        assert not service._worker.is_alive()
    return results[-1]


@pytest.mark.parametrize(("action", "params", "payload"), [
    ("color", {"segment": 0, "red": 0, "green": 128, "blue": 255},
     {"seg": [{"id": 0, "col": [[0, 128, 255]]}]}),
    ("effect", {"segment": 0, "effect_id": 0}, {"seg": [{"id": 0, "fx": 0}]}),
    ("palette", {"segment": 1, "palette_id": 255}, {"seg": [{"id": 1, "pal": 255}]}),
    ("transition", {"duration": 65535}, {"transition": 65535}),
    ("preset", {"preset_id": 250}, {"ps": 250}),
])
def test_wled_extended_semantic_actions(
    service_env: Any, action: str, params: dict[str, int], payload: dict[str, Any],
) -> None:
    """New actions pass full policy preflight and dispatch exact state payloads."""
    service, _source, _results, clock, _tracker = service_env
    wled = FakeSource(service.config)
    service.sources["wled_patio"] = wled
    service.config["wled_patio"]["write_enabled"] = "true"
    result = dispatch(service_env, envelope(clock, target="patio_light", action=action, params=params))
    assert result["status"] == "completed"
    assert wled.calls == [payload]


@pytest.mark.parametrize(("action", "params"), [
    ("color", {"segment": 0, "red": 256, "green": 0, "blue": 0}),
    ("color", {"segment": -1, "red": 0, "green": 0, "blue": 0}),
    ("color", {"segment": 0, "red": True, "green": 0, "blue": 0}),
    ("color", {"segment": 0, "red": 0, "green": 0}),
    ("effect", {"segment": 0, "effect_id": -1}),
    ("effect", {"segment": 0, "effect_id": 256}),
    ("palette", {"segment": 256, "palette_id": 0}),
    ("palette", {"segment": 0, "palette_id": "1"}),
    ("transition", {"duration": -1}),
    ("transition", {"duration": 65536}),
    ("transition", {"duration": 1.5}),
    ("preset", {"preset_id": 0}),
    ("preset", {"preset_id": 251}),
    ("preset", {"preset_id": 1, "psave": 2}),
])
def test_wled_extended_semantic_params_rejected(
    service_env: Any, action: str, params: dict[str, Any],
) -> None:
    """Invalid types, bounds, missing fields and overrides cannot reach WLED."""
    service, _source, _results, clock, _tracker = service_env
    wled = FakeSource(service.config)
    service.sources["wled_patio"] = wled
    service.config["wled_patio"]["write_enabled"] = "true"
    result = dispatch(service_env, envelope(clock, target="patio_light", action=action, params=params))
    assert result["code"] == "invalid_params"
    assert wled.calls == []


def test_kitchen_light_dispatch_and_duplicate(service_env: Any) -> None:
    """Real semantic kitchen-light frames dispatch once under duplicate QoS delivery."""
    service, source, results, clock, _tracker = service_env
    request = envelope(clock)
    result = dispatch(service_env, request)
    assert [item["status"] for item in results] == ["accepted", "completed"]
    assert result["outcome"] == "dispatched"
    assert result["steps_completed"] == 1
    assert source.calls == [{"can_id": "0x19FEDB00", "data": "10FFC801FF00FFFF"}]
    dispatch(service_env, request)
    assert len(source.calls) == 1
    assert results[-1] == result
    clock[0] += 20
    assert dispatch(service_env, request) == result
    changed = dict(request, action="off")
    assert dispatch(service_env, changed)["code"] == "request_id_conflict"


@pytest.mark.parametrize("updates,code", [
    ({"target": "missing"}, "unknown_target"),
    ({"action": "start"}, "unknown_action"),
    ({"client_id": "unlisted"}, "forbidden"),
    ({"params": {"can_id": "0x123"}}, "invalid_params"),
    ({"confirmed": "true"}, "invalid_request"),
    ({"version": True}, "unsupported_version"),
    ({"extra": 1}, "invalid_request"),
    ({"target": "coach", "action": "snapshot"}, "invalid_request"),
    ({"target": "generator", "action": "start"}, "confirmation_required"),
    ({"target": "kitchen_fan_lid", "action": "open"}, "confirmation_required"),
])
def test_rejections_do_not_write(service_env: Any, updates: dict[str, Any], code: str) -> None:
    """Every rejected request leaves the fake hardware untouched."""
    _service, source, _results, clock, _tracker = service_env
    assert dispatch(service_env, envelope(clock, **updates))["code"] == code
    assert source.calls == []


@pytest.mark.parametrize("value", [True, -1, 9, 81, 101, "50", None])
def test_typed_and_narrowed_brightness(service_env: Any, value: Any) -> None:
    """Reject booleans, coercions, nonfinite numbers and policy-bound violations."""
    _service, source, _results, clock, _tracker = service_env
    result = dispatch(service_env, envelope(clock, target="living_ceiling_light", action="brightness", params={"level": value}))
    assert result["status"] == "rejected"
    assert not source.calls


def test_brightness_and_confirmed_sequences(service_env: Any) -> None:
    """Typed brightness and confirmed multi-step motor/generator actions work."""
    _service, source, _results, clock, _tracker = service_env
    assert dispatch(service_env, envelope(clock, target="living_ceiling_light", action="brightness", params={"level": 30}))["steps_completed"] == 1
    assert source.calls[0]["data"].startswith("01FF3C")
    for index, (target, action) in enumerate([("kitchen_fan_lid", "open"), ("kitchen_fan_lid", "close"), ("generator", "start"), ("generator", "stop")]):
        result = dispatch(service_env, envelope(clock, request_id=f"sequence-{index}", target=target, action=action, confirmed=True))
        assert result["status"] == "completed"
        assert result["steps_completed"] == 2


def test_wled_semantic_params_remain_transport_specific(service_env: Any) -> None:
    """Loaded WLED schemas accept bri but never an RV-C level or payload override."""
    service, rvc_source, _results, clock, _tracker = service_env
    wled = FakeSource(service.config)
    service.sources["wled_patio"] = wled
    service.config["wled_patio"]["write_enabled"] = "true"
    request = envelope(clock, target="patio_light", action="brightness", params={"bri": 32})
    assert dispatch(service_env, request)["status"] == "completed"
    assert wled.calls == [{"on": True, "bri": 32}]
    assert not rvc_source.calls
    assert dispatch(service_env, dict(request, request_id="wrong-param", params={"level": 32}))["code"] == "invalid_params"
    assert dispatch(service_env, dict(request, request_id="out-of-range", params={"bri": 256}))["code"] == "invalid_params"
    assert len(wled.calls) == 1


def test_group_preflight_is_all_or_nothing(service_env: Any) -> None:
    """A later malformed or forbidden group member prevents every earlier write."""
    service, source, _results, clock, _tracker = service_env
    request = envelope(clock, target="all_fans", action="on")
    service.coach.entities["rear_bath_fan"]["actions"]["on"][0]["instance"] = 999
    assert dispatch(service_env, request)["status"] == "rejected"
    assert source.calls == []
    service.coach.entities["rear_bath_fan"]["actions"]["on"][0]["instance"] = 22
    del service.policy.controllers["rv-cli"]["actions"]["rear_bath_fan"]["on"]
    assert dispatch(service_env, request)["code"] == "forbidden"
    assert source.calls == []


def test_valid_group_and_cycle(service_env: Any) -> None:
    """Allow-listed members dispatch together; runtime cycles fail closed."""
    service, source, _results, clock, _tracker = service_env
    request = envelope(clock, target="all_fans", action="off")
    assert dispatch(service_env, request)["steps_completed"] == 3
    service.coach.command_groups["all_fans"]["members"].append("all_fans")
    assert dispatch(service_env, dict(request, request_id="cycle"))["status"] == "rejected"
    assert len(source.calls) == 3


def test_nested_group_grants_are_not_bypassed(service_env: Any) -> None:
    """Leaf grants do not implicitly authorize an unlisted nested macro."""
    service, source, _results, clock, _tracker = service_env
    service.coach.command_groups["nested_fans"] = {"members": ["kitchen_fan"], "actions": ["on"]}
    service.coach.command_groups["all_fans"]["members"] = ["nested_fans"]
    request = envelope(clock, target="all_fans")
    assert dispatch(service_env, request)["code"] == "forbidden"
    assert not source.calls
    service.policy.controllers["rv-cli"]["actions"]["nested_fans"] = {"on": {"confirm": True}}
    assert dispatch(service_env, request)["code"] == "confirmation_required"
    assert not source.calls
    assert dispatch(service_env, dict(request, confirmed=True))["status"] == "completed"
    assert len(source.calls) == 1


@pytest.mark.parametrize("gate", ["api", "mqtt", "source", "readiness"])
def test_all_write_gates(service_env: Any, gate: str) -> None:
    """API enablement, global writes, source writes and readiness are independent."""
    service, source, _results, clock, _tracker = service_env
    if gate == "api":
        service.enabled = False
    elif gate == "readiness":
        source.ready = False
    else:
        service.config["mqtt" if gate == "mqtt" else "rv_c_bus"]["write_enabled"] = "false"
    assert dispatch(service_env, envelope(clock))["status"] == "rejected"
    assert source.calls == []


def test_gates_and_expiry_rechecked_after_admission(service_env: Any) -> None:
    """Broker acknowledgment delays cannot bypass expiration or changed write flags."""
    service, source, _results, clock, _tracker = service_env
    original = service.publish

    def expire(topic: str, result: dict[str, Any], wait: bool) -> None:
        """Expire the request while its accepted acknowledgment is being delivered."""
        original(topic, result, wait)
        if result["status"] == "accepted":
            clock[0] += 16

    service.publish = expire
    assert dispatch(service_env, envelope(clock))["code"] == "expired"
    assert not source.calls


def test_result_publish_failure_prevents_dispatch(service_env: Any) -> None:
    """Do not write when accepted cannot be acknowledged by the broker."""
    service, source, _results, clock, _tracker = service_env
    original = service.publish

    def fail_accepted(topic: str, result: dict[str, Any], wait: bool) -> None:
        """Simulate broker publication failure only for admission."""
        if result["status"] == "accepted":
            raise ConnectionError("offline")
        original(topic, result, wait)

    service.publish = fail_accepted
    assert dispatch(service_env, envelope(clock))["status"] == "rejected"
    assert source.calls == []


def test_worker_start_failure_is_durably_rejected(service_env: Any, monkeypatch: Any) -> None:
    """Resource exhaustion cannot leave a request accepted forever or touch hardware."""
    service, source, results, clock, _tracker = service_env

    def fail_start(_thread: threading.Thread) -> None:
        """Simulate failure to allocate a worker thread."""
        raise RuntimeError("cannot start thread")

    monkeypatch.setattr(threading.Thread, "start", fail_start)
    request = envelope(clock)
    service.receive("command", json.dumps(request).encode())
    assert results[-1]["code"] == "service_error"
    assert service.journal.lookup("rv-cli", "request-1")[1]["status"] == "rejected"
    assert not service._busy.locked()
    assert not source.calls


def test_transport_failure_is_unknown_not_success(service_env: Any) -> None:
    """A throwing reliable transport cannot produce a completed action result."""
    _service, source, _results, clock, _tracker = service_env
    source.failure = OSError("simulated CAN error")
    result = dispatch(service_env, envelope(clock))
    assert result["status"] == "failed"
    assert result["outcome"] == "unknown"
    assert result["steps_completed"] == 0


def test_partial_gate_closure_between_steps(service_env: Any) -> None:
    """Report known progress without rollback when a gate closes between motor frames."""
    service, source, _results, clock, _tracker = service_env
    source.before_send = lambda: service.config["rv_c_bus"].update(write_enabled="false")
    result = dispatch(service_env, envelope(clock, target="generator", action="start", confirmed=True))
    assert result["status"] == "failed"
    assert result["outcome"] == "partial"
    assert result["steps_completed"] == 1
    assert len(source.calls) == 1


def test_source_replacement_is_used_without_reconstructing_sources(service_env: Any) -> None:
    """Supervision replacements remain visible to preflight and physical dispatch."""
    service, old_source, _results, clock, _tracker = service_env
    replacement = FakeSource(service.config)
    service.sources["rv_c_bus"] = replacement
    assert dispatch(service_env, envelope(clock))["status"] == "completed"
    assert len(replacement.calls) == 1
    assert not old_source.calls


def test_reserved_frame_and_excessive_waits_fail_before_writes(service_env: Any) -> None:
    """Reserved channels and excessive complete-plan duration cannot reach a source."""
    service, source, _results, clock, _tracker = service_env
    frame = service.coach.entities["kitchen_light"]["actions"]["on"][0]
    frame["instance"] = 32
    assert dispatch(service_env, envelope(clock))["status"] == "rejected"
    frame["instance"] = 16
    frame["wait"] = 30
    assert dispatch(service_env, envelope(clock))["status"] == "rejected"
    assert not source.calls


def test_persistence_failure_prevents_first_write(service_env: Any, monkeypatch: Any) -> None:
    """A failed durable intent never becomes a physical dispatch."""
    service, source, _results, clock, _tracker = service_env
    original = service.journal.save

    def fail_intent(request: dict[str, Any], result: dict[str, Any], *, intent: bool = False) -> None:
        """Fail only persistence needed to authorize a physical write."""
        if intent:
            raise OSError("disk failure")
        original(request, result)

    monkeypatch.setattr(service.journal, "save", fail_intent)
    assert dispatch(service_env, envelope(clock))["status"] == "rejected"
    assert not source.calls


def test_expiry_is_rechecked_after_durable_intent(service_env: Any, monkeypatch: Any) -> None:
    """Slow journal fsync cannot allow an expired request to reach hardware."""
    service, source, _results, clock, _tracker = service_env
    original = service.journal.save

    def slow_intent(request: dict[str, Any], result: dict[str, Any], *, intent: bool = False) -> None:
        """Simulate an fsync stall after successfully recording the execution intent."""
        original(request, result, intent=intent)
        if intent:
            clock[0] += 16

    monkeypatch.setattr(service.journal, "save", slow_intent)
    assert dispatch(service_env, envelope(clock))["code"] == "expired"
    assert not source.calls


def test_retained_expired_and_future_requests(service_env: Any) -> None:
    """Never dispatch retained, stale or future-dated requests."""
    _service, source, _results, clock, _tracker = service_env
    assert dispatch(service_env, envelope(clock), retained=True)["code"] == "invalid_request"
    assert dispatch(service_env, envelope(clock, issued_at=timestamp(clock[0] - 20), expires_at=timestamp(clock[0] - 5)))["code"] == "expired"
    assert dispatch(service_env, envelope(clock, issued_at=timestamp(clock[0] + 3), expires_at=timestamp(clock[0] + 15)))["code"] == "clock_skew"
    assert not source.calls


@pytest.mark.parametrize("raw", [b"[]", b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":Infinity}', b'{"a":1e309}', b"\xff", b" " * 8193])
def test_strict_json(raw: bytes) -> None:
    """Reject malformed, duplicate-key, oversized and nonstandard JSON."""
    with pytest.raises(CommandError):
        parse_json(raw)


def test_invalid_correlation_is_not_reflected(service_env: Any) -> None:
    """Never reflect untrusted topic delimiters into a result topic."""
    service, source, results, clock, _tracker = service_env
    service.receive("command", json.dumps(envelope(clock, client_id="../+#")).encode())
    assert not results
    assert not source.calls


def test_monitoring_works_with_all_physical_writes_disabled(service_env: Any) -> None:
    """Snapshot and discovery require only query grants, never physical write flags."""
    service, source, _results, clock, tracker = service_env
    service.enabled = False
    service.config["mqtt"]["write_enabled"] = "false"
    service.config["rv_c_bus"]["write_enabled"] = "false"
    tracker.handle({"dgn": "1FEDA", "instance": 16, "loadstatus": 1})
    result = dispatch(service_env, envelope(clock, target="coach", action="snapshot", params={"entities": ["kitchen_light"]}), "query")
    assert result["status"] == "completed"
    assert result["data"]["entities"][0]["entity"] == "kitchen_light"
    result = dispatch(service_env, envelope(clock, request_id="discovery", target="coach", action="capabilities"), "query")
    action = result["data"]["targets"]["kitchen_light"]["actions"]["on"]
    assert not action["available"]
    assert action["reasons"] == ["writes_disabled"]
    assert not source.calls


def test_monitoring_authorization_and_result_lookup(service_env: Any) -> None:
    """Selections cannot silently narrow and journal lookup cannot cross controllers."""
    _service, source, _results, clock, _tracker = service_env
    request = envelope(clock)
    dispatch(service_env, request)
    query = envelope(clock, client_id="limited", request_id="query", target="coach", action="snapshot")
    assert dispatch(service_env, query, "query")["code"] == "forbidden"
    query.update(action="request_status", params={"request_id": "request-1"})
    assert dispatch(service_env, query, "query")["code"] == "request_not_found"
    query.update(client_id="rv-cli")
    result = dispatch(service_env, query, "query")
    assert result["data"]["result"]["code"] == "executed"
    assert len(source.calls) == 1


@pytest.mark.parametrize("params", [
    {"entities": []}, {"entities": ["*"]}, {"entities": ["unknown"]},
    {"entities": ["kitchen_light", "kitchen_light"]}, {"entities": "kitchen_light"},
    {"entities": ["kitchen_light"], "timeout_s": 1},
])
def test_invalid_snapshot_selections(service_env: Any, params: dict[str, Any]) -> None:
    """Reject malformed monitoring selections before cache access or publication."""
    _service, source, _results, clock, _tracker = service_env
    result = dispatch(service_env, envelope(clock, target="coach", action="snapshot", params=params), "query")
    assert result["code"] == "invalid_params"
    assert not source.calls


def test_query_endpoint_cannot_execute_physical_action(service_env: Any) -> None:
    """Read-only routing never interprets an entity action as a query."""
    _service, source, _results, clock, _tracker = service_env
    assert dispatch(service_env, envelope(clock), "query")["code"] == "invalid_request"
    assert not source.calls


@pytest.mark.parametrize("value,code", [("x" * (256 * 1024), "response_too_large"), (float("nan"), "execution_failed")])
def test_invalid_or_oversized_monitoring_results_are_explicit(
    service_env: Any, monkeypatch: Any, value: Any, code: str,
) -> None:
    """Never silently leave a request accepted when cached data cannot be returned."""
    service, source, _results, clock, _tracker = service_env
    monkeypatch.setattr(service.queries, "run", lambda *_args: {
        "status": "completed", "code": "queried", "data": {"value": value},
    })
    request = envelope(clock, target="coach", action="snapshot")
    result = dispatch(service_env, request, "query")
    assert result["code"] == code
    assert result["outcome"] == "not_started"
    assert service.journal.lookup("rv-cli", "request-1")[1] == result
    assert not source.calls


def test_concurrent_duplicate_precedes_busy(service_env: Any) -> None:
    """In-flight duplicate delivery replays acceptance instead of another execution."""
    service, source, results, clock, _tracker = service_env
    entered, release = threading.Event(), threading.Event()
    original = source.execute_command

    def block(payload: dict[str, Any]) -> None:
        """Hold a fake transport operation while callback delivery continues."""
        entered.set()
        assert release.wait(3)
        original(payload)

    source.execute_command = block
    request = envelope(clock)
    service.receive("command", json.dumps(request).encode())
    assert entered.wait(3)
    try:
        service.receive("command", json.dumps(request).encode())
        assert results[-1]["status"] == "accepted"
        service.receive("command", json.dumps(dict(request, request_id="other")).encode())
        assert results[-1]["code"] == "busy"
    finally:
        release.set()
        service._worker.join(3)
    assert len(source.calls) == 1


def test_journal_recovery_capacity_and_single_owner(tmp_path: Path) -> None:
    """Crash intent is never replayed and full/unavailable journals fail closed."""
    path = tmp_path / "journal.sqlite3"
    now = [1000.0]
    request = envelope(now)
    accepted = dict(request, status="accepted", code="accepted", outcome="not_started", steps_completed=0)
    journal = RequestJournal(path, 1, now[0])
    try:
        journal.reserve(request, "digest", 1015, accepted, now[0])
        journal.save(request, accepted, intent=True)
        with pytest.raises(CommandError, match="full"):
            journal.reserve(dict(request, request_id="second"), "other", 1015, accepted, now[0])
        with pytest.raises(OSError):
            RequestJournal(path, 1, now[0])
    finally:
        journal.close()
    journal = RequestJournal(path, 1, now[0] + 1)
    try:
        recovered = journal.lookup("rv-cli", "request-1")[1]
        assert recovered["code"] == "interrupted"
        assert recovered["outcome"] == "unknown"
    finally:
        journal.close()


@pytest.mark.parametrize("replacement", [
    ("max: 80", "max: 200"),
    ("type: number", "type: string"),
    ("kitchen_light: {on: {}, off: {}, toggle: {}}", "missing: {on: {}}"),
    ("queries:\n      snapshot:", "queries:\n      invalid_query: {}\n      snapshot:"),
])
def test_invalid_policy_fails_startup(service_env: Any, tmp_path: Path, replacement: tuple[str, str]) -> None:
    """Invalid target, query and widening grants fail before MQTT subscriptions."""
    service, _source, _results, _clock, _tracker = service_env
    path = tmp_path / "invalid.yaml"
    path.write_text(POLICY.replace(*replacement))
    with pytest.raises(ValueError):
        ControllerPolicy(service.coach, path, service.queries.entities())


def test_shareable_example_policy_matches_packaged_coach() -> None:
    """Keep the documented starter controller policy usable with the actual coach map."""
    root = Path(__file__).resolve().parents[1]
    config = ConfigParser()
    config.read(root / "config-example.ini")
    coach = Coach.load(config)
    tracker = EntityStateTracker(coach, lambda *_args: None)
    from rv_control.coach_queries import CoachQueries

    queries = CoachQueries(coach, tracker, {})
    policy = ControllerPolicy(coach, root / "config-example-controllers.yaml", queries.entities())
    assert set(policy.controllers) == {"rv-cli", "rv-dashboard"}


@pytest.mark.parametrize("target", ["patio_light", "evening_scene"])
def test_overlay_policy_target_missing_from_factory(tmp_path: Path, target: str) -> None:
    """Stale installation grants identify the missing target and configuration remedy."""
    config = ConfigParser()
    coach = Coach.load(config)
    path = tmp_path / "controllers.yaml"
    path.write_text(f"controllers:\n  rv-cli:\n    actions:\n      {target}: {{off: {{}}}}\n")

    with pytest.raises(ValueError) as error:
        ControllerPolicy(coach, path, {})

    assert f"Policy references unknown target {target!r}" in str(error.value)
    assert "[coach] specfile/overlay" in str(error.value)
    assert "remove the target's controller action grants" in str(error.value)


def test_expiry_lifetime_thresholds() -> None:
    """Verify exact 15-second TTL and two-second future-clock boundaries."""
    now = [1000.0]
    request = envelope(now)
    assert validate_envelope(request, 1000) == 1015
    with pytest.raises(CommandError):
        validate_envelope(dict(request, expires_at=timestamp(1015.001)), 1000)
    assert validate_envelope(dict(request, issued_at=timestamp(1002)), 1000) == 1015
    with pytest.raises(CommandError, match="future"):
        validate_envelope(dict(request, issued_at=timestamp(1002.001)), 1000)
