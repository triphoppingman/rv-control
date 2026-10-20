"""Discoverable source actions, independent of coach entity action names."""

from __future__ import annotations

import copy
import json
import math
from abc import abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .base import Source


def action_integer(value: Any, field_name: str) -> int:
    """Parse a legacy YAML integer or decimal/hex string without accepting booleans."""
    if type(value) is int:
        return value
    if isinstance(value, str):
        try:
            return int(value, 0)
        except ValueError:
            pass
    raise ValueError(f"{field_name} must be an integer: {value!r}")


@dataclass(frozen=True)
class SourceAction:
    """Snapshot one validated source action and its compatibility payload."""

    name: str
    parameters_json: str
    payload_json: str
    dispatch_budget_seconds: float

    @property
    def params(self) -> dict[str, Any]:
        """Return an independent copy of the validated action parameters."""
        return json.loads(self.parameters_json)

    @property
    def payload(self) -> dict[str, Any]:
        """Return an independent copy of the legacy transport payload."""
        return json.loads(self.payload_json)


@dataclass(frozen=True)
class ActionDefinition:
    """Describe required/optional parameters for a source-owned action."""

    description: str
    params: dict[str, dict[str, Any]]

    def validate(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """Reject unknown, missing, mistyped and out-of-range action parameters."""
        unknown = params.keys() - self.params.keys()
        if unknown:
            raise ValueError(f"Unknown action parameters: {', '.join(sorted(unknown))}")
        result = copy.deepcopy(dict(params))
        for name, rule in self.params.items():
            if name not in result:
                if "default" not in rule:
                    raise ValueError(f"Missing action parameter {name!r}")
                result[name] = copy.deepcopy(rule["default"])
            value = result[name]
            kinds = rule["type"] if isinstance(rule["type"], list) else [rule["type"]]
            valid = (
                ("integer" in kinds and type(value) is int)
                or ("number" in kinds and type(value) in (int, float))
                or ("boolean" in kinds and type(value) is bool)
                or ("string" in kinds and isinstance(value, str))
                or ("object" in kinds and isinstance(value, dict))
            )
            if not valid:
                raise ValueError(f"Invalid type for action parameter {name!r}")
            if type(value) in (int, float):
                if not math.isfinite(value) or value < rule.get("min", -math.inf) or value > rule.get("max", math.inf):
                    raise ValueError(f"Out-of-range action parameter {name!r}")
            if "enum" in rule and value not in rule["enum"]:
                raise ValueError(f"Unsupported action parameter {name!r}")
        return result


class WritableSource(Source):
    """Declare, translate, prepare and execute source-owned actions."""

    @abstractmethod
    def command_ready(self) -> bool:
        """Report whether reliable dispatch can be attempted without probing hardware."""

    @abstractmethod
    def execute_command(self, payload: dict[str, Any]) -> None:
        """Dispatch a transport payload once or raise; never log and return on failure."""

    @classmethod
    @abstractmethod
    def supported_actions(cls) -> dict[str, ActionDefinition]:
        """Return fresh action definitions without discovery or hardware access."""

    @classmethod
    def describe_actions(cls) -> dict[str, Any]:
        """Return JSON-compatible action contracts, not permissions or live readiness."""
        return {
            name: {"description": definition.description, "params": copy.deepcopy(definition.params)}
            for name, definition in cls.supported_actions().items()
        }

    @classmethod
    @abstractmethod
    def translate_frame(cls, frame: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        """Translate an existing coach YAML frame into a source action."""

    @classmethod
    def prepare_action(
        cls, action: str, params: Mapping[str, Any], section: Mapping[str, str],
        *, reserved: Mapping[int, set[int]] | None = None,
    ) -> SourceAction:
        """Validate and encode an action without constructing a source or touching hardware."""
        definition = cls.supported_actions().get(action)
        if definition is None:
            raise ValueError(f"{cls.source_name} does not support action {action!r}")
        validated = definition.validate(params)
        payload = cls._encode_action(action, validated, section, reserved=reserved)
        budget = cls.dispatch_budget(section)
        if not math.isfinite(budget) or budget <= 0:
            raise ValueError("Source dispatch budget must be positive and finite")
        return SourceAction(
            action, json.dumps(validated, allow_nan=False),
            json.dumps(payload, allow_nan=False), budget,
        )

    @classmethod
    @abstractmethod
    def _encode_action(
        cls, action: str, params: dict[str, Any], section: Mapping[str, str],
        *, reserved: Mapping[int, set[int]] | None = None,
    ) -> dict[str, Any]:
        """Encode validated parameters and enforce protocol/installation constraints."""

    @classmethod
    @abstractmethod
    def dispatch_budget(cls, section: Mapping[str, str]) -> float:
        """Return the configured planning budget, not a hard wall-clock deadline."""

    def execute_action(self, action: str, params: Mapping[str, Any]) -> None:
        """Revalidate and dispatch once through the reliable source command path."""
        if not self.section.getboolean("write_enabled", fallback=False):
            raise PermissionError(f"[{self.section_name}] writes are disabled")
        if not self.command_ready():
            raise RuntimeError(f"[{self.section_name}] source is unavailable")
        prepared = self.prepare_action(action, params, self.section)
        self.execute_command(prepared.payload)

    def handle_action(self, action: str, params: Mapping[str, Any]) -> None:
        """Adapt local coach actions to the existing legacy command behavior."""
        prepared = self.prepare_action(action, params, self.section)
        self.handle_command(prepared.payload)
