"""Reusable coach action sequences configured by an installation overlay."""

from __future__ import annotations

import copy
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .coach import Coach

RECIPE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")


def validate_metadata(name: Any, definition: Any, extra: set[str], label: str) -> None:
    """Validate shared stable IDs, human-facing labels and deliberate-confirmation flags."""
    if not isinstance(name, str) or not RECIPE_ID.fullmatch(name):
        raise ValueError(f"{label} IDs must contain 1-64 letters, digits, underscores or hyphens")
    if not isinstance(definition, dict) or set(definition) - ({"name", "description", "confirm"} | extra):
        raise ValueError(f"Invalid {label.lower()} {name!r}")
    for key in ("name", "description"):
        if key in definition and (not isinstance(definition[key], str) or not definition[key].strip()):
            raise ValueError(f"{label} {name!r} {key} must be nonempty text")
    if type(definition.get("confirm", False)) is not bool:
        raise ValueError(f"{label} {name!r} confirm must be boolean")


class Routines:
    """Compile reusable, ordered actions defined by an installation overlay."""

    def __init__(self, coach: Coach, definitions: Any) -> None:
        """Validate routine metadata and action references without opening sources."""
        self.definitions: dict[str, dict[str, Any]] = {}
        if not isinstance(definitions, dict):
            raise ValueError("routines must be a mapping")
        for name, routine in definitions.items():
            validate_metadata(name, routine, {"actions"}, "Routine")
            actions = routine.get("actions")
            if not isinstance(actions, list) or not 1 <= len(actions) <= 64:
                raise ValueError(f"Routine {name!r} actions must contain 1-64 steps")
            for step in actions:
                self._validate_step(coach, name, step)
            self.definitions[name] = copy.deepcopy(routine)

    @staticmethod
    def _validate_step(coach: Coach, name: str, step: Any) -> None:
        """Require explicit entity actions or logical-source enable/disable operations."""
        if not isinstance(step, dict) or not isinstance(step.get("action"), str):
            raise ValueError(f"Routine {name!r} step requires an action")
        if "source" in step:
            if set(step) != {"source", "action"} or not isinstance(step["source"], str) or not RECIPE_ID.fullmatch(step["source"]):
                raise ValueError(f"Routine {name!r} source step requires source and action only")
            if step["action"] not in {"enable", "disable"}:
                raise ValueError(f"Routine {name!r} source action must be enable or disable")
            return
        if set(step) - {"entity", "action", "params"} or not isinstance(step.get("entity"), str) or not isinstance(step.get("params", {}), dict):
            raise ValueError(f"Routine {name!r} step requires entity, action and optional params")
        target = step["entity"]
        entity = coach.entities.get(target)
        if entity is None or step["action"] not in entity.get("actions", {}):
            raise ValueError(f"Routine {name!r} references unknown action {target}.{step['action']}")
        from .coach_commands import validate_params, validate_schema

        schema = entity.get("action_schemas", {}).get(step["action"], {})
        validate_schema(schema)
        validate_params(step.get("params", {}), schema)

    def catalog(self) -> dict[str, dict[str, Any]]:
        """Return detached routine metadata and ordered action references."""
        return {
            name: dict(name=routine.get("name", name), description=routine.get("description", ""),
                       confirm=routine.get("confirm", False), actions=copy.deepcopy(routine["actions"]))
            for name, routine in self.definitions.items()
        }
