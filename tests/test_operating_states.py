"""Hardware-free routine discovery, state transitions and startup application regressions."""

from __future__ import annotations

import copy
import json
import threading
from configparser import ConfigParser
from pathlib import Path
from typing import Any

import pytest
from ruamel.yaml import YAML

from rv_control.coach import Coach
from rv_control.coach_commands import CoachCommandService, ControllerPolicy
from rv_control.coach_state import EntityStateTracker
from rv_control.source_control import SourceController, SourceOverrides
from test_coach_commands import FakeSource, dispatch, envelope


DEFINITION = {
    "routines": {
        "lights_on": {
            "name": "Lights on", "description": "Switch on the kitchen light",
            "actions": [{"entity": "kitchen_light", "action": "on"}],
        },
        "lights_off": {"actions": [{"entity": "kitchen_light", "action": "off"}]},
        "enable_leveler": {"actions": [{"source": "leveler", "action": "enable"}]},
        "disable_leveler": {"actions": [{"source": "leveler", "action": "disable"}]},
    },
    "operating_states": {
        "default": "parked", "apply_on_startup": False,
        "states": {
            "parked": {"name": "Parked", "routines": ["lights_off"]},
            "levelling": {"name": "Levelling", "routines": ["enable_leveler", "lights_on"]},
            "driving": {"routines": ["lights_off", "disable_leveler"]},
            "stored": {"routines": []},
        },
    },
}
POLICY = {
    "controllers": {
        "rv-cli": {
            "actions": {"kitchen_light": {"on": {}, "off": {}}},
            "sources": {"leveler": {"enable": {}, "disable": {}}},
            "routines": {name: {} for name in DEFINITION["routines"]},
            "states": {name: {} for name in DEFINITION["operating_states"]["states"]},
            "queries": {"states": {}, "routines": {}, "actions": {}, "capabilities": {}},
        },
        "viewer": {"queries": {"states": {}, "routines": {}, "actions": {}}},
    },
}


def write_yaml(path: Path, data: dict[str, Any]) -> None:
    """Write a detached YAML fixture through the repository's configured parser."""
    with path.open("w") as handle:
        YAML().dump(data, handle)


@pytest.fixture
def state_env(tmp_path: Path) -> Any:
    """Build the real model, policy, journal and service over a fake writable source."""
    write_yaml(tmp_path / "overlay.yml", DEFINITION)
    write_yaml(tmp_path / "controllers.yml", POLICY)
    config = ConfigParser()
    config.read_dict({
        "_meta": {"path": str(tmp_path / "config.ini")},
        "source": {"enabled-sources": "rv_c_bus"},
        "target": {"enabled-targets": "mqtt"},
        "mqtt": {"type": "mqtt", "write_enabled": "true"},
        "coach": {"rvc": "rv_c_bus", "overlay": "overlay.yml", "leveler": "leveler"},
        "rv_c_bus": {"type": "rvc", "write_enabled": "true"},
        "leveler": {"type": "beechlane"},
        "coach_commands": {
            "enabled": "true", "queries_enabled": "true", "source_control": "leveler",
            "controllers_file": "controllers.yml", "journal_file": "journal.sqlite3",
        },
    })
    coach = Coach.load(config)
    source = FakeSource(config)
    tracker = EntityStateTracker(coach, lambda *_args: None)
    results: list[dict[str, Any]] = []
    clock = [1_792_257_600.0]
    controller = SourceController(config, SourceOverrides(), ("leveler",))
    service = CoachCommandService(
        config, coach, tracker, {"rv_c_bus": source},
        lambda _topic, result, _wait: results.append(copy.deepcopy(result)),
        lambda: True, lambda: clock[0], source_controller=controller,
    )
    yield service, source, results, clock, tracker
    service.close()


def request(env: Any, action: str, params: dict[str, Any], **updates: Any) -> dict[str, Any]:
    """Send a unique coach-level command through normal asynchronous admission."""
    return envelope(env[3], target="coach", action=action, params=params,
                    request_id=f"request-{len(env[2])}", **updates)


def test_default_selected_without_actions_and_explicit_apply(state_env: Any) -> None:
    """A default is not applied until selected, and repeat/reapply semantics are explicit."""
    service, source, _results, _clock, _tracker = state_env
    service.start_operating_states()
    assert service.coach.operating_states.snapshot()["current"] == "parked"
    assert service.coach.operating_states.snapshot()["application_status"] == "not_applied"
    assert source.calls == []
    first = request(state_env, "set_state", {"state": "parked"})
    assert dispatch(state_env, first)["code"] == "executed"
    assert len(source.calls) == 1
    assert dispatch(state_env, first)["code"] == "executed"
    assert len(source.calls) == 1
    assert dispatch(state_env, request(state_env, "set_state", {"state": "parked"}))["code"] == "unchanged"
    assert dispatch(state_env, request(state_env, "set_state", {"state": "parked", "reapply": True}))["code"] == "executed"
    assert len(source.calls) == 2


def test_routine_does_not_change_operating_state(state_env: Any) -> None:
    """A standalone routine uses normal dispatch while leaving the selected state untouched."""
    service, source, *_rest = state_env
    before = service.coach.operating_states.snapshot()
    result = dispatch(state_env, request(state_env, "execute_routine", {"routine": "lights_on"}))
    assert result["status"] == "completed"
    assert len(source.calls) == 1
    assert service.coach.operating_states.snapshot() == before
    assert result["data"]["execution"]["name"] == "lights_on"
    assert result["data"]["execution"]["steps_total"] == 1


@pytest.mark.parametrize("query", ["states", "routines", "actions", "capabilities"])
def test_introspection_queries(state_env: Any, query: str) -> None:
    """UI metadata exposes invocations, permissions, action schemas and ordered composition."""
    result = dispatch(state_env, request(state_env, query, {}), kind="query")
    assert result["status"] == "completed"
    data = result["data"]
    if query == "routines":
        routine = data["routines"]["lights_on"]
        assert routine["actions"] == [{"entity": "kitchen_light", "action": "on"}]
        assert routine["authorized"] and routine["available"]
        assert routine["command"]["action"] == "execute_routine"
    if query == "states":
        assert data["states"]["parked"]["routines"] == ["lights_off"]
        assert data["states"]["parked"]["command"]["schema"]["reapply"]["default"] is False
        assert data["operating_state"]["application_status"] == "not_applied"
    if query == "actions":
        assert data["targets"]["kitchen_light"]["actions"]["on"]["params"] == {}
    if query == "capabilities":
        assert "routines" in data and "operating_states" in data


def test_viewer_sees_unavailable_buttons_but_cannot_execute(state_env: Any) -> None:
    """Discovery-only clients see metadata and explicit denial without gaining write permissions."""
    result = dispatch(state_env, request(state_env, "routines", {}, client_id="viewer"), kind="query")
    routine = result["data"]["routines"]["lights_on"]
    assert routine["authorized"] is False
    assert routine["available"] is False
    assert routine["reasons"] == ["forbidden"]
    denied = dispatch(state_env, request(state_env, "execute_routine", {"routine": "lights_on"}, client_id="viewer"))
    assert denied["code"] == "forbidden"
    assert state_env[1].calls == []


def test_whole_state_preflight_prevents_partial_writes(state_env: Any) -> None:
    """Unavailable later members prevent all earlier routine actions from executing."""
    service, source, *_rest = state_env
    service.coach.routines.definitions["bad"] = {"actions": [{"entity": "patio_light", "action": "on"}]}
    service.coach.operating_states.definitions["bad"] = {"routines": ["lights_on", "bad"]}
    service.policy.controllers["rv-cli"]["states"]["bad"] = {}
    service.policy.controllers["rv-cli"]["routines"]["bad"] = {}
    result = dispatch(state_env, request(state_env, "set_state", {"state": "bad"}))
    assert result["status"] == "rejected"
    assert source.calls == []


def test_failure_marks_state_unknown_and_never_rolls_back(state_env: Any) -> None:
    """A transport exception can leave physical outcome unknown even before any completed step."""
    service, source, *_rest = state_env
    source.failure = OSError("disconnected")
    result = dispatch(state_env, request(state_env, "set_state", {"state": "parked"}))
    assert result["outcome"] == "unknown"
    status = service.coach.operating_states.snapshot()
    assert status["current"] is None
    assert status["requested"] == "parked"
    assert status["application_status"] == "failed"
    assert result["data"]["execution"]["status"] == "failed"


def test_startup_application_uses_normal_policy_and_journal(state_env: Any) -> None:
    """Explicit startup execution is journaled and applied exactly once per service generation."""
    service, source, *_rest = state_env
    service.config["coach_commands"]["startup_client_id"] = "rv-cli"
    service.coach.operating_states.apply_on_startup = True
    service.start_operating_states()
    assert service._worker is not None
    service._worker.join(3)
    assert not service._worker.is_alive()
    service.start_operating_states()
    assert len(source.calls) == 1
    assert service.coach.operating_states.snapshot()["application_status"] == "applied"
    assert service.journal.lookup("rv-cli", service._startup_request_id) is not None


def test_startup_confirmation_is_not_bypassed(state_env: Any) -> None:
    """Unattended startup cannot impersonate deliberate confirmation."""
    service, source, *_rest = state_env
    service.config["coach_commands"]["startup_client_id"] = "rv-cli"
    service.coach.operating_states.apply_on_startup = True
    service.policy.controllers["rv-cli"]["routines"]["lights_off"]["confirm"] = True
    service.start_operating_states()
    assert source.calls == []
    assert service.coach.operating_states.snapshot()["error"]["code"] == "confirmation_required"


def test_retained_progress_reconnect_and_offline(state_env: Any) -> None:
    """Dashboards receive running/terminal status, reconnect refresh and offline markers."""
    service = state_env[0]
    publications: list[tuple[str, dict[str, Any]]] = []
    service._publish_state = lambda topic, payload: publications.append((topic, copy.deepcopy(payload)))
    connected = [True]
    service.connected = lambda: connected[0]
    service.start_operating_states()
    dispatch(state_env, request(state_env, "set_state", {"state": "parked"}))
    assert any(topic == "coach/execution" and payload["status"] == "running" for topic, payload in publications)
    assert publications[-1][1]["status"] == "completed"
    before = len(publications)
    service.tick()
    assert len(publications) == before
    connected[0] = False
    service.tick()
    connected[0] = True
    service.tick()
    assert len(publications) == before + 2
    service.close()
    assert all(not payload["online"] for _topic, payload in publications[-2:])
    service._publish_state = None


@pytest.mark.parametrize("query", ["actions", "routines", "states"])
def test_discovery_client_parameter_contract(query: str) -> None:
    """Installed and standalone MQTT clients accept the new no-parameter UI queries."""
    from rv_control.coach_mqtt_client import _query_params

    assert _query_params(query, (), (), None, None, "query-id") == {}


@pytest.mark.parametrize("update", [
    {"operating_states": {"default": "missing", "states": {"parked": {"routines": []}}}},
    {"operating_states": {"default": "parked", "states": {"parked": {"routines": ["missing"]}}}},
    {"routines": {"bad": {"actions": [{"routine": "nested", "action": "execute"}]}}},
    {"routines": {"bad": {"actions": [{"source": "leveler", "action": "restart"}]}}},
    {"routines": {"bad": {"actions": [{"entity": "kitchen_light", "action": "missing"}]}}},
    {"routines": {"bad": {"actions": [{"entity": "kitchen_light", "action": "on", "params": {"unknown": 1}}]}}},
])
def test_invalid_overlay_definitions_rejected(state_env: Any, update: dict[str, Any]) -> None:
    """Malformed definitions fail during hardware-free coach loading, independent of grants."""
    service = state_env[0]
    write_yaml(Path(service.config["_meta"]["path"]).parent / "overlay.yml", update)
    with pytest.raises(ValueError):
        Coach.load(service.config)


def test_base_coach_rejects_routines_and_states(tmp_path: Path) -> None:
    """Installation automation cannot accidentally become part of the factory coach vocabulary."""
    for key in ("routines", "operating_states"):
        path = tmp_path / "base.yml"
        write_yaml(path, {key: {}})
        with pytest.raises(ValueError, match="overlay"):
            Coach.load(ConfigParser(), path)


def test_policy_requires_routine_and_leaf_grants(state_env: Any, tmp_path: Path) -> None:
    """State permissions cannot bypass missing routine permissions or missing leaf permissions."""
    service = state_env[0]
    for key in ("routines", "actions"):
        policy = copy.deepcopy(POLICY)
        policy["controllers"]["rv-cli"].pop(key)
        path = tmp_path / "incomplete.yml"
        write_yaml(path, policy)
        with pytest.raises(ValueError, match="authorized"):
            ControllerPolicy(service.coach, path, {}, ("leveler",))


def test_write_gates_and_inherited_confirmation(state_env: Any) -> None:
    """Routine discovery and execution honor member confirmation and existing write gates."""
    service, source, *_rest = state_env
    service.policy.controllers["rv-cli"]["actions"]["kitchen_light"]["off"]["confirm"] = True
    data = dispatch(state_env, request(state_env, "states", {}), kind="query")["data"]
    assert data["states"]["parked"]["confirm"] is True
    result = dispatch(state_env, request(state_env, "set_state", {"state": "parked"}))
    assert result["code"] == "confirmation_required"
    service.config["rv_c_bus"]["write_enabled"] = "false"
    result = dispatch(state_env, request(state_env, "set_state", {"state": "parked"}, confirmed=True))
    assert result["code"] == "writes_disabled"
    assert source.calls == []


def test_busy_transition_does_not_overlap(state_env: Any) -> None:
    """A second routine or state cannot race with an executing transition."""
    service, source, *_rest = state_env
    entered, release = threading.Event(), threading.Event()

    def wait_before_send() -> None:
        """Hold the fake actuator until the competing command has been rejected."""
        entered.set()
        assert release.wait(3)

    source.before_send = wait_before_send
    try:
        service.receive("command", json.dumps(request(state_env, "set_state", {"state": "parked"})).encode())
        assert entered.wait(3)
        service.receive("command", json.dumps(request(state_env, "execute_routine", {"routine": "lights_on"})).encode())
        assert state_env[2][-1]["code"] == "busy"
    finally:
        release.set()
        if service._worker:
            service._worker.join(3)


def test_source_lifecycle_ordering(state_env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """State routines wait for source enablement before dispatching subsequent entity actions."""
    service, source, *_rest = state_env
    controller = service.planner.source_controller
    assert controller is not None
    controller.attach(None, threading.Event(), [], service.sources)
    applied: list[str] = []
    original_request = controller.request

    def enable(section: str, enabled: bool, **kwargs: Any) -> dict[str, Any]:
        """Record enablement and supply a deterministic fake running leveler."""
        applied.append("enable" if enabled else "disable")
        result = original_request(section, enabled, **kwargs)
        if enabled:
            service.sources[section] = source
        else:
            service.sources.pop(section, None)
        return result

    monkeypatch.setattr(controller, "request", enable)
    source.before_send = lambda: applied.append("light")
    result = dispatch(state_env, request(state_env, "set_state", {"state": "levelling"}))
    assert result["status"] == "completed"
    assert applied == ["enable", "light"]
    assert service.coach.operating_states.snapshot()["current"] == "levelling"


def test_source_timeout_reports_uncertain_side_effect(state_env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """A source lifecycle timeout stops the rest of the recipe and invalidates selected state."""
    monkeypatch.setattr("rv_control.coach_commands.SOURCE_SETTLE_SECONDS", 0.01)
    result = dispatch(state_env, request(state_env, "set_state", {"state": "levelling"}))
    assert result["code"] == "source_unavailable"
    assert result["outcome"] == "unknown"
    assert state_env[1].calls == []
