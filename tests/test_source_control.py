"""Hardware-free regressions for MQTT source enable/disable and status reporting."""

from __future__ import annotations

import json
import threading
import time
from configparser import ConfigParser
from pathlib import Path
from typing import Any

import pytest

from rv_control.coach import Coach
from rv_control.coach_commands import CoachCommandService, ControllerPolicy, timestamp
from rv_control.coach_state import EntityStateTracker
from rv_control.source_control import SourceController, SourceOverrides, controllable_sections
from rv_control.sources.base import Source


class IdleSource(Source, source_name="test_idle"):
    """Inert registered source that runs until its stop event is set."""

    def run(self) -> None:
        """Block on the stop event, like a real polling source."""
        self.stop_event.wait()

    def comms_check(self) -> dict[str, Any]:
        """Report success without probing hardware."""
        return {"ok": True}


def make_config(tmp_path: Path, policy: str | None = None, source_control: str = "leveler, spare") -> ConfigParser:
    """Build a configuration with one fixed, one controllable and one on-demand source."""
    config = ConfigParser()
    config.read_dict({
        "_meta": {"path": str(tmp_path / "config.ini")},
        "source": {"enabled-sources": "fixed, leveler"},
        "fixed": {"type": "test_idle"},
        "leveler": {"type": "test_idle"},
        "spare": {"type": "test_idle"},
        "target": {"enabled-targets": "mqtt"},
        "mqtt": {"type": "mqtt", "write_enabled": "true"},
        "coach": {"rvc": "rv_c_bus"},
        "rv_c_bus": {"type": "rvc"},
        "coach_commands": {
            "enabled": "true", "queries_enabled": "true", "source_control": source_control,
            "controllers_file": "controllers.yaml", "journal_file": str(tmp_path / "journal.sqlite3"),
        },
    })
    if policy is not None:
        (tmp_path / "controllers.yaml").write_text(policy)
    return config


class Harness:
    """Start real source threads under a controller exactly as the CLI does."""

    def __init__(self, config: ConfigParser, overrides: SourceOverrides | None = None) -> None:
        """Start enabled sources with private events for controllable sections."""
        self.published: list[dict[str, Any]] = []
        self.started: list[str] = []
        self.stop_event = threading.Event()
        self.controller = SourceController(
            config, overrides or SourceOverrides(), controllable_sections(config),
            self.published.append, lambda: True, lambda section, _source: self.started.append(section),
        )
        self.sources, self.source_map = Source.start_enabled(
            config, None, self.stop_event, isolated=self.controller.controllable, skip=self.controller.skipped(),
        )
        self.controller.attach(None, self.stop_event, self.sources, self.source_map)

    def settle(self, predicate: Any) -> None:
        """Tick until the predicate holds, failing after a short bound."""
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            self.controller.tick()
            if predicate():
                return
            time.sleep(0.01)
        raise AssertionError("source control did not settle")

    def state(self, section: str) -> str:
        """Return the reported lifecycle state for one section."""
        return {item["source"]: item for item in self.controller.snapshot()["sources"]}[section]["state"]

    def close(self) -> None:
        """Stop every source thread, including ones still shutting down."""
        Source.stop_and_join(self.sources + self.controller.stopping_sources(), self.stop_event, 2)
        self.controller.close()


@pytest.fixture
def harness(tmp_path: Path) -> Any:
    """Provide a running harness and always join its threads."""
    instance = Harness(make_config(tmp_path))
    yield instance
    instance.close()


def test_source_control_allowlist_is_validated(tmp_path: Path) -> None:
    """Allowlisted sections must exist, be unique and name a registered type."""
    assert controllable_sections(make_config(tmp_path)) == ("leveler", "spare")
    for value, message in (("leveler, leveler", "duplicate"), ("missing", "not found"), ("bad/name", "Invalid")):
        with pytest.raises(ValueError, match=message):
            controllable_sections(make_config(tmp_path, source_control=value))
    config = make_config(tmp_path)
    config["spare"]["type"] = "no_such_source"
    with pytest.raises(ValueError, match="Unknown source"):
        controllable_sections(config)


def test_controllable_sources_get_private_stop_events(harness: Harness) -> None:
    """Only allowlisted sources can be stopped without stopping their peers."""
    assert harness.source_map["leveler"].stop_event is not harness.stop_event
    assert harness.source_map["fixed"].stop_event is harness.stop_event


def test_disable_stops_only_that_source_and_reenable_starts_a_new_instance(harness: Harness) -> None:
    """Disabling removes the source from routing; enabling starts a fresh thread."""
    original = harness.source_map["leveler"]
    result = harness.controller.request("leveler", False, client_id="ui")
    assert result["enabled"] is False
    harness.settle(lambda: harness.state("leveler") == "disabled")
    assert not original.is_alive()
    assert "leveler" not in harness.source_map
    assert original not in harness.sources
    assert harness.source_map["fixed"].is_alive()

    harness.controller.request("leveler", True, client_id="ui")
    harness.settle(lambda: harness.state("leveler") == "running")
    assert harness.source_map["leveler"] is not original
    assert harness.started == ["leveler"]


def test_on_demand_source_starts_and_status_is_published(harness: Harness) -> None:
    """A section outside enabled-sources can be enabled and is reported retained."""
    assert harness.state("spare") == "disabled"
    harness.controller.request("spare", True, reason="leveling", client_id="ui")
    harness.settle(lambda: harness.state("spare") == "running")
    latest = {item["source"]: item for item in harness.published[-1]["sources"]}
    assert latest["spare"]["configured_enabled"] is False
    assert latest["spare"]["enabled"] is True
    assert latest["spare"]["override"]["reason"] == "leveling"
    assert latest["fixed"]["controllable"] is False
    count = len(harness.published)
    harness.controller.tick()
    assert len(harness.published) == count
    harness.controller.close()
    assert harness.published[-1] == {"version": 1, "online": False, "sources": []}


def test_timed_override_expires_back_to_configuration(tmp_path: Path) -> None:
    """A duration restores configured enablement without another request."""
    now = [1000.0]
    overrides = SourceOverrides(lambda: now[0])
    harness = Harness(make_config(tmp_path), overrides)
    try:
        result = harness.controller.request("leveler", False, duration_s=60)
        assert result["until"] is not None
        harness.settle(lambda: harness.state("leveler") == "disabled")
        now[0] += 61
        harness.settle(lambda: harness.state("leveler") == "running")
        assert overrides.get("leveler") is None
    finally:
        harness.close()


def test_matching_configuration_clears_override(harness: Harness) -> None:
    """Requesting the configured state without a duration removes the override."""
    harness.controller.request("leveler", False)
    assert harness.controller.overrides.get("leveler") is not None
    harness.controller.request("leveler", True)
    assert harness.controller.overrides.get("leveler") is None


def test_overrides_carry_across_generations_and_skip_startup(tmp_path: Path) -> None:
    """A reload keeps disabled sources stopped and drops no-longer-allowed overrides."""
    overrides = SourceOverrides()
    first = Harness(make_config(tmp_path), overrides)
    first.controller.request("leveler", False)
    first.controller.request("spare", True)
    first.close()
    second = Harness(make_config(tmp_path, source_control="leveler"), overrides)
    try:
        assert "leveler" not in second.source_map
        assert overrides.get("spare") is None
        assert second.state("leveler") == "disabled"
    finally:
        second.close()


def test_supervisor_does_not_restart_a_disabled_source() -> None:
    """A stopped source whose private event is set is not resurrected."""
    shared = threading.Event()
    private = threading.Event()
    private.set()
    config = ConfigParser()
    config.read_dict({"leveler": {"type": "test_idle"}})
    source = IdleSource(config, None, private, "leveler")
    sources = [source]
    timer = threading.Timer(0.05, shared.set)
    timer.start()
    try:
        Source.run_until_stopped(config, None, shared, sources, {"leveler": source})
    finally:
        timer.cancel()
    assert sources == [source]
    assert not source.is_alive()


def test_stop_and_join_sets_private_events() -> None:
    """Shutdown reaches sources that do not share the session event."""
    config = ConfigParser()
    config.read_dict({"leveler": {"type": "test_idle"}})
    shared = threading.Event()
    source = IdleSource(config, None, threading.Event(), "leveler")
    source.start()
    Source.stop_and_join([source], shared, 2)
    assert not source.is_alive()


SOURCE_POLICY = """
controllers:
  rv-dashboard:
    sources:
      leveler: {enable: {}, disable: {confirm: true}}
    queries:
      sources: {}
      capabilities: {}
"""


@pytest.fixture
def service(tmp_path: Path) -> Any:
    """Build a coach service bound to a running source controller."""
    config = make_config(tmp_path, SOURCE_POLICY)
    harness = Harness(config)
    coach = Coach.load(config)
    tracker = EntityStateTracker(coach, lambda *_args: None)
    results: list[dict[str, Any]] = []
    instance = CoachCommandService(
        config, coach, tracker, harness.source_map,
        lambda _topic, result, _wait: results.append(json.loads(json.dumps(result))),
        lambda: True, source_controller=harness.controller,
    )
    yield instance, harness, results
    instance.close()
    harness.close()


def request(kind: str, action: str, params: dict[str, Any], request_id: str, confirmed: bool = False) -> bytes:
    """Encode a fresh, valid coach envelope targeting the coach itself."""
    now = time.time()
    return json.dumps({
        "version": 1, "client_id": "rv-dashboard", "request_id": request_id,
        "issued_at": timestamp(now), "expires_at": timestamp(now + 10),
        "target": "coach", "action": action, "params": params, "confirmed": confirmed,
    }).encode()


def send(instance: CoachCommandService, results: list[dict[str, Any]], kind: str, payload: bytes) -> dict[str, Any]:
    """Submit one request and return its terminal result."""
    instance.receive(kind, payload)
    if instance._worker is not None:
        instance._worker.join(3)
    return results[-1]


def test_mqtt_source_disable_requires_confirmation_and_applies(service: Any) -> None:
    """Disable follows policy confirmation and the supervisor stops the source."""
    instance, harness, results = service
    rejected = send(instance, results, "command", request("command", "source_disable", {"source": "leveler"}, "d1"))
    assert rejected["code"] == "confirmation_required"
    done = send(instance, results, "command", request("command", "source_disable", {"source": "leveler", "duration_s": 600, "reason": "parked"}, "d2", True))
    assert done["status"] == "completed"
    assert done["data"]["enabled"] is False and done["data"]["until"]
    harness.settle(lambda: harness.state("leveler") == "disabled")
    enabled = send(instance, results, "command", request("command", "source_enable", {"source": "leveler"}, "e1"))
    assert enabled["status"] == "completed"
    harness.settle(lambda: harness.state("leveler") == "running")


@pytest.mark.parametrize(("params", "code"), [
    ({"source": "spare"}, "forbidden"),
    ({"source": "fixed"}, "unknown_target"),
    ({}, "invalid_params"),
    ({"source": "leveler", "duration_s": 0}, "invalid_params"),
    ({"source": "leveler", "duration_s": True}, "invalid_params"),
    ({"source": "leveler", "reason": "x" * 121}, "invalid_params"),
    ({"source": "leveler", "extra": 1}, "invalid_params"),
])
def test_mqtt_source_control_rejects_invalid_requests(service: Any, params: dict[str, Any], code: str) -> None:
    """Ungranted, unlisted and malformed requests never change source state."""
    instance, harness, results = service
    result = send(instance, results, "command", request("command", "source_enable", params, "bad"))
    assert result["status"] == "rejected" and result["code"] == code
    assert harness.controller.overrides.get("spare") is None


def test_sources_query_and_capabilities_describe_controls(service: Any) -> None:
    """Dashboards can read source status and discover permitted controls."""
    instance, _harness, results = service
    status = send(instance, results, "query", request("query", "sources", {}, "q1"))
    assert status["status"] == "completed"
    states = {item["source"]: item for item in status["data"]["sources"]}
    assert states["leveler"]["state"] == "running" and states["leveler"]["controllable"] is True
    assert states["spare"]["state"] == "disabled"
    capabilities = send(instance, results, "query", request("query", "capabilities", {}, "q2"))
    assert capabilities["data"]["sources"] == {
        "leveler": {"enable": {"confirm": False, "available": True}, "disable": {"confirm": True, "available": True}},
    }
    misplaced = send(instance, results, "query", request("query", "source_enable", {"source": "leveler"}, "q3"))
    assert misplaced["code"] == "invalid_request"


def test_policy_rejects_grants_outside_the_allowlist(tmp_path: Path) -> None:
    """Policy may never grant control of a section the installation did not list."""
    config = make_config(tmp_path)
    coach = Coach.load(config)
    path = tmp_path / "policy.yaml"
    for body, message in (
        ("controllers:\n  ui:\n    sources:\n      fixed: {enable: {}}\n", "not listed"),
        ("controllers:\n  ui:\n    sources:\n      leveler: {restart: {}}\n", "enable and/or disable"),
        ("controllers:\n  ui:\n    sources:\n      leveler: {enable: {confirm: 1}}\n", "Invalid source grant"),
    ):
        path.write_text(body)
        with pytest.raises(ValueError, match=message):
            ControllerPolicy(coach, path, {}, ("leveler",))


def test_daemon_session_applies_mqtt_source_control(monkeypatch: Any, tmp_path: Path) -> None:
    """The CLI wires private events, the controller tick and retained status together."""
    from click.testing import CliRunner

    import rv_control.cli as cli_module
    from rv_control.targets.mqtt import MqttTarget

    (tmp_path / "controllers.yaml").write_text(SOURCE_POLICY)
    path = tmp_path / "config.ini"
    path.write_text(
        "[source]\nenabled-sources = fixed, leveler\n[fixed]\ntype = test_idle\n[leveler]\ntype = test_idle\n"
        "[target]\nenabled-targets = mqtt\n[mqtt]\ntype = mqtt\nwrite_enabled = true\n"
        "[coach]\nrvc = rv_c_bus\n[rv_c_bus]\ntype = rvc\n"
        "[coach_commands]\nenabled = true\nqueries_enabled = true\nsource_control = leveler\n"
        f"controllers_file = controllers.yaml\njournal_file = {tmp_path / 'journal.sqlite3'}\n"
        "[service]\nlog_level = INFO\n",
    )
    retained: list[tuple[str, dict[str, Any]]] = []
    results: list[dict[str, Any]] = []
    mqtt = object.__new__(MqttTarget)
    mqtt.connected = threading.Event()
    mqtt.connect = mqtt.connected.set
    mqtt.close = lambda: None
    mqtt.publish = lambda *_args: None
    mqtt.publish_result = lambda _topic, payload, _wait=False: results.append(payload)
    mqtt.publish_retained = lambda topic, payload: retained.append((topic, payload))
    monkeypatch.setattr(cli_module.Target, "build", lambda *_args: mqtt)
    observed: dict[str, Any] = {}

    def supervise(_config: Any, _publisher: Any, stop: threading.Event, sources: list[Source], source_map: dict[str, Source], on_tick: Any) -> None:
        """Disable the leveler over the coach API and drive supervision ticks."""
        leveler = source_map["leveler"]
        observed["private"] = leveler.stop_event is not stop and source_map["fixed"].stop_event is stop
        mqtt.coach_service.receive("command", request("command", "source_disable", {"source": "leveler"}, "cli1", True))
        mqtt.coach_service._worker.join(3)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and (leveler.is_alive() or "leveler" in source_map):
            on_tick()
            time.sleep(0.01)
        on_tick()
        observed["stopped"] = not leveler.is_alive() and "leveler" not in source_map and len(sources) == 1

    monkeypatch.setattr(cli_module.Source, "run_until_stopped", supervise)
    result = CliRunner().invoke(cli_module.cli, ["--config", str(path), "run"])
    assert result.exit_code == 0, result.output
    assert observed == {"private": True, "stopped": True}
    assert results[-1]["status"] == "completed"
    assert {topic for topic, _payload in retained} == {"coach/sources"}
    states = {item["source"]: item["state"] for item in retained[-2][1]["sources"]}
    assert states == {"fixed": "running", "leveler": "disabled"}
    assert retained[-1][1]["online"] is False
