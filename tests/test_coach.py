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


def test_coach_execution_single_rvc_command(config_file: Path) -> None:
    """Verify executing a single RV-C command constructs and dispatches the CAN message."""
    config = load_config(str(config_file))
    config["source"]["enabled-sources"] = "rv_c_bus"
    config["rv_c_bus"] = {"type": "rvc", "interface": "can0", "write_enabled": "true"}

    sent_messages = []

    class FakeBus:
        def send(self, msg: Any) -> None:
            sent_messages.append(msg)

    coach = Coach.load(config)
    coach.get_source("rv_c_bus").bus = FakeBus()
    results = coach.execute("livingroom_lights", "on")

    assert len(results) == 1
    assert results[0]["status"] == "ok"
    assert results[0]["target"] == "livingroom_lights"
    assert results[0]["action"] == "on"
    assert results[0]["transport"] == "rvc"
    assert len(sent_messages) == 1
    assert sent_messages[0].arbitration_id == 0x19FEDB00


def test_coach_execution_wled_command(config_file: Path, monkeypatch: Any) -> None:
    """Verify executing a WLED command delegates to WledSource."""
    config = load_config(str(config_file))
    config["wled_patio"] = {"type": "wled", "base_url": "http://wled.local", "write_enabled": "true"}

    wled_calls = []

    from rv_control.source import Source

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
    results = coach.execute("patio_lights", "on")
    assert len(results) == 1
    assert results[0]["transport"] == "wled"
    assert wled_calls == [{"on": True}]


def test_coach_execute_group(config_file: Path, monkeypatch: Any) -> None:
    """Verify execute_group runs all step commands in the group."""
    config = load_config(str(config_file))
    config["rv_c_bus"] = {"type": "rvc", "interface": "can0", "write_enabled": "true"}
    config["wled_patio"] = {"type": "wled", "base_url": "http://wled.local", "write_enabled": "true"}

    from rv_control.source import Source

    class FakeWledSource(Source, source_name="wled"):
        def __init__(self, _cfg: Any, publisher: Any = None, stop_event: Any = None, section_name: str = "") -> None:
            self.section_name = section_name

        def handle_command(self, payload: dict[str, Any]) -> None:
            pass

        def comms_check(self) -> dict[str, Any]:
            return {"ok": True}

        def run(self) -> None:
            pass

    monkeypatch.setitem(Source._registry, "wled", FakeWledSource)

    sent_messages = []

    class FakeBus:
        def send(self, msg: Any) -> None:
            sent_messages.append(msg)

    coach = Coach.load(config)
    coach.get_source("rv_c_bus").bus = FakeBus()

    # Execute all_interior_lights -> toggle
    results = coach.execute("all_interior_lights", "toggle")
    assert len(results) == 2
    targets = [r["target"] for r in results]
    assert targets == ["livingroom_lights", "bedroom_lights"]
    assert len(sent_messages) == 2

    # Execute evening_scene -> activate
    sent_messages.clear()
    results_scene = coach.execute("evening_scene", "activate")
    assert len(results_scene) == 3
    targets_scene = [r["target"] for r in results_scene]
    assert targets_scene == ["livingroom_lights", "bedroom_lights", "patio_lights"]


def test_coach_error_handling(config_file: Path) -> None:
    """Verify Coach raises appropriate errors for invalid targets and actions."""
    config = load_config(str(config_file))
    coach = Coach.load(config)

    with pytest.raises(KeyError):
        coach.execute("nonexistent_target", "on")

    with pytest.raises(ValueError):
        coach.execute("livingroom_lights", "nonexistent_action")


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
    config["wled_patio"] = {"type": "wled", "base_url": "http://wled.local", "write_enabled": "true"}

    sent_messages = []

    class FakeBus:
        def send(self, msg: Any) -> None:
            sent_messages.append(msg)

    coach = Coach.load(config)
    coach.get_source("my_custom_can_section").bus = FakeBus()
    results = coach.execute("livingroom_lights", "on")

    assert len(results) == 1
    assert results[0]["config_section"] == "my_custom_can_section"
    assert len(sent_messages) == 1
