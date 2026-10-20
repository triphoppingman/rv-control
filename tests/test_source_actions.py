"""Source-owned action contracts and unchanged coach YAML translation."""

from __future__ import annotations

import json
import threading
from configparser import ConfigParser
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from click.testing import CliRunner

from rv_control.cli import cli
from rv_control.coach import Coach
from rv_control.sources.base import Source
from rv_control.sources.rvc import RvcSource
from rv_control.sources.wled import WledSource
from rv_control.sources.writable import WritableSource


@pytest.mark.parametrize("source_type", [RvcSource, WledSource])
def test_action_catalog_is_independent_and_serializable(source_type: type[WritableSource]) -> None:
    """Discovery needs no source instance and callers cannot mutate future catalogs."""
    catalog = source_type.describe_actions()
    assert json.loads(json.dumps(catalog)) == catalog
    assert catalog
    catalog.clear()
    assert source_type.describe_actions()
    definitions = source_type.supported_actions()
    first = next(iter(definitions))
    definitions[first].params["injected"] = {"type": "string"}
    assert "injected" not in source_type.describe_actions()[first]["params"]


@pytest.mark.parametrize(("action", "params", "payload"), [
    ("on", {}, {"on": True}),
    ("off", {}, {"on": False}),
    ("brightness", {"bri": 255}, {"on": True, "bri": 255}),
    ("color", {"segment": 1, "red": 255, "green": 2, "blue": 3},
     {"seg": [{"id": 1, "col": [[255, 2, 3]]}]}),
    ("effect", {"segment": 0, "effect_id": 4}, {"seg": [{"id": 0, "fx": 4}]}),
    ("palette", {"segment": 0, "palette_id": 5}, {"seg": [{"id": 0, "pal": 5}]}),
    ("transition", {"duration": 10}, {"transition": 10}),
    ("preset", {"preset_id": 250}, {"ps": 250}),
    ("state", {"state": {"on": True, "seg": [{"id": 1, "sx": 42}]}},
     {"on": True, "seg": [{"id": 1, "sx": 42}]}),
])
def test_wled_catalog_actions_round_trip(
    action: str, params: dict[str, Any], payload: dict[str, Any],
) -> None:
    """Each declared action encodes and translates back without losing JSON fields."""
    prepared = WledSource.prepare_action(action, params, {"timeout": "2"})
    assert prepared.payload == payload
    assert prepared.dispatch_budget_seconds == 6
    translated, translated_params = WledSource.translate_frame(payload)
    assert translated == action
    assert WledSource.prepare_action(translated, translated_params, {}).payload == payload


@pytest.mark.parametrize(("source_type", "action", "params"), [
    (WledSource, "missing", {}),
    (WledSource, "on", {"bri": 1}),
    (WledSource, "brightness", {}),
    (WledSource, "brightness", {"bri": True}),
    (WledSource, "brightness", {"bri": "1"}),
    (WledSource, "brightness", {"bri": 256}),
    (WledSource, "color", {"segment": 0, "red": -1, "green": 0, "blue": 0}),
    (WledSource, "preset", {"preset_id": 0}),
    (WledSource, "transition", {"duration": 65536}),
    (WledSource, "palette", {"segment": 256, "palette_id": 1}),
    (WledSource, "effect", {"segment": 0, "effect_id": 256}),
    (WledSource, "state", {"state": {}}),
    (WledSource, "state", {"state": []}),
    (WledSource, "state", {"state": {"bri": float("nan")}}),
    (WledSource, "on", {"base_url": "http://other.invalid"}),
    (RvcSource, "dc_dimmer", {"instance": True, "command": "on"}),
    (RvcSource, "dc_dimmer", {"instance": 256, "command": "on"}),
    (RvcSource, "dc_dimmer", {"instance": 1, "command": "unsupported"}),
    (RvcSource, "dc_dimmer", {"instance": 1, "command": "on", "level": float("inf")}),
    (RvcSource, "frame", {"dgn": 0x20000, "payload": "00"}),
    (RvcSource, "frame", {"dgn": 1, "payload": "GG"}),
    (RvcSource, "frame", {"dgn": 1, "payload": "00" * 9}),
    (RvcSource, "frame", {"dgn": 1, "payload": "0"}),
])
def test_source_action_rejections(
    source_type: type[WritableSource], action: str, params: dict[str, Any],
) -> None:
    """Invalid contracts and malformed transport data fail during hardware-free preparation."""
    with pytest.raises(ValueError):
        source_type.prepare_action(action, params, {})


def test_rvc_action_encoding_and_reservations() -> None:
    """Named/raw actions retain exact padding, address settings and safety exclusions."""
    params = {"instance": 20, "command": "on"}
    prepared = RvcSource.prepare_action("dc_dimmer", params, {})
    assert prepared.payload == {"can_id": "0x19FEDB00", "data": "14FFC801FF00FFFF"}
    assert prepared.params["group"] == 255
    assert prepared.dispatch_budget_seconds == 1
    assert RvcSource.prepare_action(
        "frame", {"dgn": 0x1FEF9, "payload": "02F0"},
        {"priority": "3", "source": "128"},
    ).payload == {"can_id": "0x0DFEF980", "data": "02F0FFFFFFFFFFFF"}
    for action, action_params in [
        ("dc_dimmer", params), ("frame", {"dgn": 0x1FEDB, "payload": "14"}),
    ]:
        with pytest.raises(PermissionError, match="reserved"):
            RvcSource.prepare_action(action, action_params, {}, reserved={0x1FEDB: {20}})


def test_prepared_action_snapshots_nested_parameters() -> None:
    """Neither input mutation nor property mutation changes a prepared operation."""
    params = {"state": {"seg": [{"id": 1, "sx": 42}]}}
    prepared = WledSource.prepare_action("state", params, {})
    params["state"]["seg"][0]["sx"] = 99
    prepared.params["state"]["seg"][0]["sx"] = 100
    prepared.payload["seg"][0]["sx"] = 101
    assert prepared.payload == {"seg": [{"id": 1, "sx": 42}]}


@pytest.mark.parametrize("timeout", ["0", "-1", "nan", "inf"])
def test_wled_action_budget_rejects_unsafe_timeouts(timeout: str) -> None:
    """Preparation rejects invalid timeouts while the executor limits whole-plan duration."""
    with pytest.raises(ValueError):
        WledSource.prepare_action("on", {}, {"timeout": timeout})


def test_wled_large_local_timeout_remains_supported() -> None:
    """Local action preparation preserves timeouts beyond semantic plan limits."""
    assert WledSource.prepare_action("on", {}, {"timeout": "11"}).dispatch_budget_seconds == 33


@pytest.mark.parametrize(("source_type", "action", "params", "payload"), [
    (RvcSource, "dc_dimmer", {"instance": 20, "command": "on"},
     {"can_id": "0x19FEDB00", "data": "14FFC801FF00FFFF"}),
    (WledSource, "preset", {"preset_id": 7}, {"ps": 7}),
])
def test_execute_action_checks_gates_and_propagates_failure(
    monkeypatch: pytest.MonkeyPatch, source_type: type[WritableSource],
    action: str, params: dict[str, Any], payload: dict[str, Any],
) -> None:
    """Both sources use the same checked action entry point and dispatch exactly once."""
    config = ConfigParser()
    config["device"] = {"write_enabled": "false"}
    source = source_type(config, None, threading.Event(), "device")
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(source, "command_ready", lambda: True)
    monkeypatch.setattr(source, "execute_command", calls.append)
    try:
        with pytest.raises(PermissionError):
            source.execute_action(action, params)
        config["device"]["write_enabled"] = "true"
        monkeypatch.setattr(source, "command_ready", lambda: False)
        with pytest.raises(RuntimeError, match="unavailable"):
            source.execute_action(action, params)
        monkeypatch.setattr(source, "command_ready", lambda: True)
        source.execute_action(action, params)
        assert calls == [payload]

        def fail(_payload: dict[str, Any]) -> None:
            """Simulate an uncertain send failure without retrying."""
            calls.append(_payload)
            raise OSError("dispatch failed")

        monkeypatch.setattr(source, "execute_command", fail)
        with pytest.raises(OSError, match="dispatch failed"):
            source.execute_action(action, params)
        assert calls == [payload, payload]
    finally:
        if isinstance(source, WledSource):
            source.session.close()


def test_coach_prepares_named_actions_without_constructing_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Existing frame YAML maps to source actions with reserved checks before dispatch."""
    config = ConfigParser()
    config.read_dict({
        "coach": {"rvc": "bus", "wled": "strip"},
        "bus": {"type": "rvc"}, "strip": {"type": "wled"},
    })
    coach = Coach(config, {
        "entities": {
            "fan": {"transport": "rvc", "actions": {
                "on": [{"instance": "{instance}", "command": "on"}],
            }},
            "light": {"transport": "wled", "actions": {"saved": [{"ps": "{id}", "wait": 1}]}},
        },
        "reserved": [{"dgn": 0x1FEDB, "instances": [43]}],
    })

    def fail_create(*_args: Any, **_kwargs: Any) -> Source:
        """Make accidental source construction observable."""
        raise AssertionError("Preparation must not create sources")

    monkeypatch.setattr(Source, "create", fail_create)
    assert coach.prepare_source_actions("fan", "on", {"instance": 20})[0][1].name == "dc_dimmer"
    wait, action = coach.prepare_source_actions("light", "saved", {"id": 3})[0]
    assert (wait, action.name, action.params) == (1, "preset", {"preset_id": 3})
    with pytest.raises(PermissionError):
        coach.prepare_source_actions("fan", "on", {"instance": 43})


def test_named_rvc_action_reaches_can_send(monkeypatch: pytest.MonkeyPatch) -> None:
    """The public action entry point encodes and sends exactly one bounded CAN frame."""
    config = ConfigParser()
    config["bus"] = {"write_enabled": "true"}
    source = RvcSource(config, None, threading.Event(), "bus")
    sent: list[tuple[Any, float]] = []

    def send(message: Any, timeout: float) -> None:
        """Capture the actual CAN message rather than replacing the command interface."""
        sent.append((message, timeout))

    source.bus = SimpleNamespace(send=send)
    monkeypatch.setattr(source, "is_alive", lambda: True)
    source.execute_action("dc_dimmer", {"instance": 20, "command": "on"})
    assert len(sent) == 1
    message, timeout = sent[0]
    assert message.arbitration_id == 0x19FEDB00
    assert bytes(message.data) == bytes.fromhex("14FFC801FF00FFFF")
    assert timeout == 1


def test_named_wled_action_reaches_http_post(monkeypatch: pytest.MonkeyPatch) -> None:
    """Named source actions use only the configured controller's state endpoint."""
    config = ConfigParser()
    config["strip"] = {
        "write_enabled": "true", "base_url": "http://strip.invalid", "timeout": "2",
    }
    source = WledSource(config, None, threading.Event(), "strip")
    calls: list[tuple[str, str, dict[str, Any], float]] = []

    def request(method: str, url: str, json: dict[str, Any], timeout: float) -> Any:
        """Capture HTTP dispatch and supply the expected object response."""
        calls.append((method, url, json, timeout))
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"success": True})

    monkeypatch.setattr(source, "is_alive", lambda: True)
    monkeypatch.setattr(source.session, "request", request)
    try:
        source.execute_action("color", {"segment": 0, "red": 1, "green": 2, "blue": 3})
        assert calls == [(
            "POST", "http://strip.invalid/json/state",
            {"seg": [{"id": 0, "col": [[1, 2, 3]]}]}, 2,
        )]
    finally:
        source.session.close()


@pytest.mark.parametrize("source_type", ["rvc", "wled"])
def test_source_actions_cli(config_file: Path, source_type: str) -> None:
    """Expose the exact source contract as JSON without starting sources."""
    result = CliRunner().invoke(cli, ["--config", str(config_file), "source-actions", source_type])
    assert result.exit_code == 0, result.output
    source_class = RvcSource if source_type == "rvc" else WledSource
    assert json.loads(result.output) == source_class.describe_actions()


@pytest.mark.parametrize("source_type", ["obd", "unknown"])
def test_source_actions_cli_unsupported(config_file: Path, source_type: str) -> None:
    """Read-only and unknown sources report explicit errors, not empty catalogs."""
    result = CliRunner().invoke(cli, ["--config", str(config_file), "source-actions", source_type])
    assert result.exit_code == 1
    assert "does not support actions" in result.output or "Unknown source" in result.output
