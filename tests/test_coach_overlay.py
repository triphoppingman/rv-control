"""Additive installation overlays must preserve effective factory behavior."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
from ruamel.yaml import YAML

from rv_control.coach import Coach, load_coach_yaml, merge_coach_overlay
from rv_control.config import load_config


def load_overlay(config_file: Path, definition: dict[str, object]) -> Coach:
    """Write one local overlay beside its INI and load through the public entry point."""
    path = config_file.parent / "overlay.yml"
    with path.open("w") as handle:
        YAML().dump(definition, handle)
    config = load_config(str(config_file))
    config["coach"]["overlay"] = "overlay.yml"
    return Coach.load(config)


def test_overlay_optional_preserves_factory(config_file: Path) -> None:
    """Missing or blank overlay settings keep the original factory definition."""
    config = load_config(str(config_file))
    original = Coach.load(config)
    config["coach"]["overlay"] = " "
    assert Coach.load(config).data == original.data
    assert "patio_light" not in original.entities
    assert "evening_scene" not in original.command_groups
    original._validate_overlay()


def test_overlay_adds_wled_actions_without_mutating_factory(config_file: Path) -> None:
    """The shareable example adds inherited actions with unchanged base payloads."""
    path = Path(__file__).parents[1] / "src/rv_control/coaches/wled-overlay-example.yml"
    config = load_config(str(config_file))
    factory = Coach.load(config)
    before = copy.deepcopy(factory.data)
    with config_file.open("a") as handle:
        handle.write("\n[wled_patio]\ntype = wled\n")
    merged = load_overlay(config_file, load_coach_yaml(path))
    assert merged.prepare_action("patio_light", "color", {
        "segment": 0, "red": 1, "green": 2, "blue": 3,
    }) == [(0.0, {"seg": [{"id": 0, "col": [[1, 2, 3]]}]})]
    for action in ("on", "off", "brightness"):
        assert merged.entities["patio_light"]["actions"][action] == factory.templates["wled_light"]["actions"][action]
    assert "evening_scene" in merged.command_groups
    assert factory.data == before


def test_overlay_adds_entities_groups_and_reservations(config_file: Path) -> None:
    """New entities, scenes, metadata and safety exclusions are additive."""
    coach = load_overlay(config_file, {
        "coach": {"installation": "custom"},
        "entities": {"custom_strip": {"template": "wled_light", "target": "custom_strip"}},
        "command_groups": {"custom_scene": {"members": ["custom_strip"], "actions": ["off"]}},
        "reserved": [{"dgn": 0x1FEDB, "instances": [100]}],
    })
    assert coach.info["installation"] == "custom"
    assert 43 in coach.reserved[0x1FEDB] and 100 in coach.reserved[0x1FEDB]
    assert coach.entities["custom_strip"]["transport"] == "wled"


@pytest.mark.parametrize("definition", [
    {"coach": {"model": "replacement"}},
    {"entities": {"kitchen_fan": {"template": "replacement"}}},
    {"entities": {"kitchen_fan": {"transport": "wled"}}},
    {"entities": {"kitchen_fan": {"actions": {"on": [{"on": False}]}}}},
    {"templates": {"wled_light": {"actions": {"on": [{"on": False}]}}}},
    {"templates": {"wled_light": {"action_schemas": {"brightness": {"bri": {"max": 999}}}}}},
    {"templates": {"wled_light": {"action_schemas": {"brightness": {"bri": {"enum": [1]}}}}}},
    {"entities": {"living_ceiling_light": {"action_schemas": {"brightness": {"level": {"enum": [1]}}}}}},
    {"command_groups": {"all_fans": {"members": ["kitchen_fan"]}}},
    {"entities": {"fresh_tank": {"state": {"fields": {"level": "other"}}}}},
])
def test_overlay_rejects_direct_and_inherited_overrides(
    config_file: Path, definition: dict[str, object],
) -> None:
    """Raw collisions and effective template/entity overrides both fail closed."""
    with pytest.raises(ValueError, match="overlay.*(replace|inherited)"):
        load_overlay(config_file, definition)


@pytest.mark.parametrize("definition", [
    {"unknown": {}},
    {"templates": []},
    {"entities": {"bad": None}},
    {"templates": {"loop": {"extends": "loop"}}},
    {"entities": {"bad": {"template": "missing"}}},
    {"entities": {"bad": {"transport": "wled", "state": {"dgn": 1}}}},
    {"entities": {"bad": {"transport": "wled", "actions": {"x": {}}, "action_schemas": {"x": {"p": {"type": "invalid"}}}}}},
    {"entities": {"bad": {"transport": "wled", "actions": {"x": {"wait": 100}}}}},
    {"entities": {"bad": {"transport": "wled", "actions": {"x": {}}}}},
    {"command_groups": {"bad": {"members": ["missing"], "actions": ["on"]}}},
    {"command_groups": {"bad": {"members": ["bad"], "actions": ["on"]}}},
    {"command_groups": {"bad": {"members": ["patio_light"], "actions": ["missing"]}}},
    {"reserved": [{"dgn": 0x1FEDB, "instances": [1]}]},
    {"reserved": [{"dgn": 0x1FEDB, "instances": [256]}]},
])
def test_invalid_overlay_fails_before_hardware(
    config_file: Path, definition: dict[str, object],
) -> None:
    """Invalid structures, cycles, schemas, state and reserved actions are rejected."""
    with pytest.raises((ValueError, PermissionError), match="."):
        load_overlay(config_file, definition)


def test_overlay_relative_to_ini_and_explicit_base(
    config_file: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Overlay location follows the INI even with a different cwd or explicit base."""
    load_overlay(config_file, {"coach": {"installation": "custom"}})
    config = load_config(str(config_file))
    config["coach"]["overlay"] = "overlay.yml"
    monkeypatch.chdir(config_file.parent)
    assert Coach.load(config, "thor-magnitude-bh35-2020").info["installation"] == "custom"


def test_overlay_missing_and_malformed_files(config_file: Path) -> None:
    """An explicitly configured overlay must exist and parse successfully."""
    config = load_config(str(config_file))
    config["coach"]["overlay"] = "absent.yml"
    with pytest.raises(FileNotFoundError, match="absent.yml"):
        Coach.load(config)
    (config_file.parent / "absent.yml").write_text("templates: [")
    with pytest.raises(ValueError, match="Invalid coach overlay"):
        Coach.load(config)


def test_merge_never_mutates_inputs_or_accepts_duplicate_values() -> None:
    """Even identical redefinitions conflict, and callers retain original inputs."""
    base = {"templates": {"a": {"actions": {"on": [{"on": True}]}}}}
    before = copy.deepcopy(base)
    merged = merge_coach_overlay(base, {"coach": {"custom": True}}, "test")
    assert base == before and merged is not base
    with pytest.raises(ValueError, match="templates.a.actions.on"):
        merge_coach_overlay(base, before, "test")
