from __future__ import annotations

from pathlib import Path
from typing import Any
import pytest

from rv_control.coach import Coach, find_coach_file, load_coach_yaml
from rv_control.config import load_config


def test_find_coach_file_defaults() -> None:
    """Verify find_coach_file resolves default and named YAML files."""
    default_file = find_coach_file()
    assert default_file.exists()
    assert default_file.name == "thor-magnitude-bh35-2020.yml"

    named_file = find_coach_file("thor-magnitude-bh35-2020")
    assert named_file == default_file

    with pytest.raises(FileNotFoundError):
        find_coach_file("nonexistent-coach-model")


def test_load_coach_yaml(tmp_path: Path) -> None:
    """Verify load_coach_yaml parses valid YAML mappings."""
    yaml_file = tmp_path / "coach.yml"
    yaml_file.write_text("coach:\n  model: Test\ncommands: {}\n")
    data = load_coach_yaml(yaml_file)
    assert data["coach"]["model"] == "Test"

    with pytest.raises(FileNotFoundError):
        load_coach_yaml(tmp_path / "missing.yml")


class FakeBus:
    """Collect CAN messages instead of sending them."""

    def __init__(self) -> None:
        """Start with no recorded messages."""
        self.sent: list[Any] = []

    def send(self, msg: Any) -> None:
        """Record one outgoing CAN message."""
        self.sent.append(msg)


@pytest.fixture
def coach_env(config_file: Path, monkeypatch: Any) -> tuple[Coach, FakeBus, list[dict[str, Any]], list[float]]:
    """Return a coach wired to a fake CAN bus, a fake WLED source, and a fake sleep."""
    config = load_config(str(config_file))
    config["rv_c_bus"] = {"type": "rvc", "interface": "can0", "write_enabled": "true"}
    config["wled_patio"] = {"type": "wled", "base_url": "http://wled.local", "write_enabled": "true"}
    wled_calls: list[dict[str, Any]] = []

    from rv_control.sources.base import Source

    class FakeWledSource(Source, source_name="wled"):
        def __init__(self, _cfg: Any, publisher: Any = None, stop_event: Any = None, section_name: str = "") -> None:
            self.section_name = section_name

        def handle_command(self, payload: dict[str, Any]) -> None:
            wled_calls.append(payload)

        def comms_check(self) -> dict[str, Any]:
            return {"ok": True}

        def run(self) -> None:
            pass

    monkeypatch.setitem(Source._registry, "wled", FakeWledSource)
    coach = Coach.load(config)
    bus = FakeBus()
    coach.get_source("rv_c_bus").bus = bus
    sleeps: list[float] = []
    coach.sleep = sleeps.append
    return coach, bus, wled_calls, sleeps


def frames(bus: FakeBus) -> list[tuple[int, str]]:
    """Return (CAN ID, hex data) pairs for every message sent on the fake bus."""
    return [(msg.arbitration_id, bytes(msg.data).hex(" ").upper()) for msg in bus.sent]


def test_entity_dimmer_switch(coach_env: Any) -> None:
    """A dimmer_switch entity sends one panel-format DC_DIMMER_COMMAND_2 frame."""
    coach, bus, _wled, _sleeps = coach_env
    results = coach.execute("kitchen_fan", "on")
    assert results[0]["status"] == "ok"
    assert results[0]["config_section"] == "rv_c_bus"
    assert frames(bus) == [(0x19FEDB00, "14 FF C8 01 FF 00 FF FF")]


def test_entity_brightness_requires_level(coach_env: Any) -> None:
    """Dimmable lights take the runtime level parameter and reject a missing one."""
    coach, bus, _wled, _sleeps = coach_env
    with pytest.raises(ValueError, match="level"):
        coach.execute("living_ceiling_light", "brightness")
    assert bus.sent == []
    coach.execute("living_ceiling_light", "brightness", level=30)
    assert frames(bus) == [(0x19FEDB00, "01 FF 3C 00 FF 00 FF FF")]


def test_entity_timed_pair_sequence(coach_env: Any) -> None:
    """The kitchen lid turns the opposite motor side off before running its own side."""
    coach, bus, _wled, _sleeps = coach_env
    coach.execute("kitchen_fan_lid", "open")
    assert frames(bus) == [
        (0x19FEDB00, "34 FF 00 03 FF 00 FF FF"),
        (0x19FEDB00, "33 FF C8 01 0F 00 FF FF"),
    ]


def test_entity_confirm_and_wait(coach_env: Any) -> None:
    """Confirm-only entities refuse to run unconfirmed, and frame waits are honoured."""
    coach, bus, _wled, sleeps = coach_env
    with pytest.raises(PermissionError):
        coach.execute("awning", "extend")
    assert bus.sent == []
    coach.execute("awning", "extend", confirm=True)
    assert frames(bus) == [
        (0x19FEDB00, "06 FF 00 03 FF 00 FF FF"),
        (0x19FEDB00, "05 FF C8 01 01 00 FF FF"),
        (0x19FEDB00, "05 FF 00 03 FF 00 FF FF"),
    ]
    assert sleeps == [1.0]


def test_entity_raw_payload_climate(coach_env: Any) -> None:
    """Thermostat actions send their captured THERMOSTAT_COMMAND_1 payloads in order."""
    coach, bus, _wled, _sleeps = coach_env
    coach.execute("front_ac", "cool")
    assert frames(bus) == [
        (0x19FEF900, "02 F0 FF FF FF FF FF FF"),
        (0x19FEF900, "00 F1 FF FF FF FF FF FF"),
    ]


def test_entity_wled(coach_env: Any) -> None:
    """WLED entities pass their JSON state frames to the WLED source."""
    coach, _bus, wled_calls, _sleeps = coach_env
    results = coach.execute("patio_light", "on")
    assert results[0]["transport"] == "wled"
    assert wled_calls == [{"on": True}]


@pytest.mark.parametrize(("action", "params", "payload"), [
    ("color", {"segment": 0, "red": 255, "green": 80, "blue": 0},
     {"seg": [{"id": 0, "col": [[255, 80, 0]]}]}),
    ("effect", {"segment": 1, "effect_id": 2}, {"seg": [{"id": 1, "fx": 2}]}),
    ("palette", {"segment": 1, "palette_id": 3}, {"seg": [{"id": 1, "pal": 3}]}),
    ("transition", {"duration": 10}, {"transition": 10}),
    ("preset", {"preset_id": 7}, {"ps": 7}),
])
def test_entity_wled_advanced_actions(
    coach_env: Any, action: str, params: dict[str, int], payload: dict[str, Any],
) -> None:
    """Expanded coach actions preserve typed nested WLED fields without powering on."""
    coach, _bus, wled_calls, _sleeps = coach_env
    coach.execute("patio_light", action, **params)
    assert wled_calls == [payload]


def test_wled_entities_select_distinct_controllers(coach_env: Any) -> None:
    """Logical entity bindings select separate WLED sources sharing one template."""
    coach, _bus, wled_calls, _sleeps = coach_env
    coach.config["wled_roof"] = {"type": "wled", "write_enabled": "true"}
    coach.config["coach"]["patio_strip"] = "wled_patio"
    coach.config["coach"]["roof_strip"] = "wled_roof"
    coach.entities["patio_light"]["target"] = "patio_strip"
    coach.entities["roof_light"] = dict(coach.entities["patio_light"], target="roof_strip")
    patio = coach.execute("patio_light", "effect", segment=0, effect_id=1)[0]
    roof = coach.execute("roof_light", "palette", segment=0, palette_id=2)[0]
    assert patio["config_section"] == "wled_patio"
    assert roof["config_section"] == "wled_roof"
    assert coach.get_source("wled_patio") is not coach.get_source("wled_roof")
    assert wled_calls == [{"seg": [{"id": 0, "fx": 1}]}, {"seg": [{"id": 0, "pal": 2}]}]


def test_coach_execute_group(coach_env: Any) -> None:
    """Member groups apply one action to every member; step groups run explicit steps."""
    coach, bus, wled_calls, _sleeps = coach_env
    results = coach.execute("all_fans", "off")
    assert [r["target"] for r in results] == ["kitchen_fan", "mid_bath_fan", "rear_bath_fan"]
    assert len(bus.sent) == 3

    results = coach.execute("evening_scene", "activate")
    assert [r["target"] for r in results] == ["living_ceiling_light", "bedroom_ceiling_light", "patio_light"]
    assert frames(bus)[3] == (0x19FEDB00, "01 FF 3C 00 FF 00 FF FF")
    assert wled_calls == [{"on": True}]


def test_coach_error_handling(config_file: Path) -> None:
    """Unknown targets, unknown actions, and read-only entities raise errors."""
    coach = Coach.load(load_config(str(config_file)))
    with pytest.raises(KeyError):
        coach.execute("nonexistent_target", "on")
    with pytest.raises(ValueError):
        coach.execute("kitchen_fan", "nonexistent_action")
    with pytest.raises(ValueError):
        coach.execute("fresh_tank", "on")


def test_reserved_instances_rejected(config_file: Path) -> None:
    """Entities that address a reserved instance are rejected when the coach loads."""
    data = {
        "templates": {"switch": {"transport": "rvc", "actions": {"on": [{"dgn": 0x1FEDB, "instance": "{instance}", "command": "on"}]}}},
        "entities": {"slide": {"template": "switch", "instance": 43}},
        "reserved": [{"dgn": 0x1FEDB, "instances": [43]}],
    }
    with pytest.raises(PermissionError):
        Coach(load_config(str(config_file)), data)


def test_template_extends_loop_rejected(config_file: Path) -> None:
    """Templates that extend each other in a loop are rejected."""
    data = {
        "templates": {"a": {"extends": "b"}, "b": {"extends": "a"}},
        "entities": {"x": {"template": "a"}},
    }
    with pytest.raises(ValueError, match="loop"):
        Coach(load_config(str(config_file)), data)


def test_list_entities(config_file: Path) -> None:
    """list_entities reports kind, area, actions and confirmation for each entity."""
    entities = Coach.load(load_config(str(config_file))).list_entities()
    assert entities["kitchen_fan_lid"]["actions"] == ["open", "close"]
    assert entities["generator"]["confirm"] is True
    assert entities["fresh_tank"]["kind"] == "tank"
    assert entities["fresh_tank"]["actions"] == []


def test_coach_load_from_config_section(config_file: Path) -> None:
    """Verify Coach.load uses the [coach] section specfile from configuration."""
    config = load_config(str(config_file))
    config["coach"] = {"specfile": "thor-magnitude-bh35-2020", "rvc": "rv_c_bus", "wled": "wled_patio"}
    coach = Coach.load(config)
    assert coach.info["model"] == "Magnitude BH35"


def test_coach_transport_mapping_via_coach_section(config_file: Path) -> None:
    """Verify Coach resolves transport names to custom config.ini section names via [coach]."""
    config = load_config(str(config_file))
    config["coach"] = {
        "specfile": "thor-magnitude-bh35-2020",
        "rvc": "my_custom_can_section",
        "wled": "wled_patio",
    }
    config["my_custom_can_section"] = {"type": "rvc", "interface": "can1", "write_enabled": "true"}
    coach = Coach.load(config)
    bus = FakeBus()
    coach.get_source("my_custom_can_section").bus = bus
    results = coach.execute("kitchen_fan", "on")
    assert results[0]["config_section"] == "my_custom_can_section"
    assert len(bus.sent) == 1
