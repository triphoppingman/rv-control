"""Declarative coach operating states and observable, non-persistent application status."""

from __future__ import annotations

import copy
import threading
from typing import TYPE_CHECKING, Any

from .routines import validate_metadata

if TYPE_CHECKING:
    from .coach import Coach

STATE_TOPIC = "coach/operating_state"


class OperatingStates:
    """Validate entry recipes and track one selected state independently of telemetry."""

    def __init__(self, coach: Coach, definition: Any) -> None:
        """Compile optional YAML definitions without selecting or touching hardware."""
        self._lock = threading.RLock()
        self.default: str | None = None
        self.apply_on_startup = False
        self.definitions: dict[str, dict[str, Any]] = {}
        self._status: dict[str, Any] = {
            "version": 1, "online": True, "current": None, "requested": None,
            "application_status": "not_applied", "steps_completed": 0, "error": None,
        }
        if definition is None:
            return
        if not isinstance(definition, dict) or set(definition) - {"default", "apply_on_startup", "states"}:
            raise ValueError("operating_states requires default, apply_on_startup and states mappings")
        states = definition.get("states")
        if not isinstance(states, dict) or not states:
            raise ValueError("operating_states.states must be a nonempty mapping")
        for name, state in states.items():
            validate_metadata(name, state, {"routines"}, "Operating state")
            routines = state.get("routines")
            if not isinstance(routines, list) or len(routines) > 64:
                raise ValueError(f"Operating state {name!r} routines must be a list of at most 64 IDs")
            if any(not isinstance(item, str) or item not in coach.routines.definitions for item in routines):
                raise ValueError(f"Operating state {name!r} references an unknown routine")
            self.definitions[name] = copy.deepcopy(state)
        default = definition.get("default")
        if not isinstance(default, str) or default not in states:
            raise ValueError("operating_states.default must name a configured state")
        if type(definition.get("apply_on_startup", False)) is not bool:
            raise ValueError("operating_states.apply_on_startup must be boolean")
        self.default = default
        self.apply_on_startup = definition.get("apply_on_startup", False)
        self._status["current"] = default

    def catalog(self) -> dict[str, Any]:
        """Return detached UI metadata and entry actions, without installation secrets."""
        return {
            "default": self.default, "apply_on_startup": self.apply_on_startup,
            "states": {
                name: dict(name=state.get("name", name), description=state.get("description", ""),
                           confirm=state.get("confirm", False), routines=list(state["routines"]))
                for name, state in self.definitions.items()
            },
        }

    def snapshot(self) -> dict[str, Any]:
        """Return the selected state and the last transition's detached application status."""
        with self._lock:
            return copy.deepcopy(self._status)

    def begin(self, name: str) -> None:
        """Mark a transition in progress while retaining the previously selected state."""
        with self._lock:
            self._status.update(requested=name, application_status="applying", steps_completed=0, error=None)

    def progress(self, completed: int) -> None:
        """Record completed dispatch/lifecycle steps for UI progress reporting."""
        with self._lock:
            self._status["steps_completed"] = completed

    def finish(self, name: str) -> None:
        """Select a state after every entry action has been dispatched successfully."""
        with self._lock:
            self._status.update(current=name, requested=None, application_status="applied", error=None)

    def fail(self, name: str, code: str, *, attempted: bool) -> None:
        """Report failure, invalidating the selected state if side effects may have occurred."""
        with self._lock:
            self._status.update(requested=name, application_status="failed", error={"code": code})
            if attempted:
                self._status["current"] = None
