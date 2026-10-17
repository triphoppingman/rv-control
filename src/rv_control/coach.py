"""Coach entity model: template resolution and entity/group action execution."""

from __future__ import annotations

import copy
import logging
import re
import threading
import time
from collections.abc import Callable
from configparser import ConfigParser
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from .rvc_util import (
    DC_DIMMER_COMMAND_DGN,
    RV_C_PRIORITY,
    SOURCE_ADDRESS,
    build_can_id,
    dc_dimmer_payload,
    normalize_can_payload,
)
from .sources.base import Source

LOGGER = logging.getLogger(__name__)
DEFAULT_COACH_SPEC = str(Path(__file__).resolve().parent / "coaches/thor-magnitude-bh35-2020.yml")
PLACEHOLDER_PATTERN = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
MAX_FRAME_WAIT_SECONDS = 60.0
CONFIRM_VALUES = {"1", "true", "yes", "y", "on"}


def substitute_placeholders(value: Any, context: dict[str, Any], strict: bool) -> Any:
    """Replace ``{name}`` placeholders in a nested YAML value from a context mapping.

    A string that is exactly one placeholder is replaced by the context value
    itself, preserving its type (so ``"{instance}"`` becomes the integer 20).
    Placeholders embedded in longer strings are replaced textually. Mapping
    keys are substituted as well, which lets templates name actions after
    entity keys such as ``"{first_action}"``.

    Parameters
    ----------
    value : Any
        Scalar, list, or mapping parsed from the coach YAML.
    context : dict[str, Any]
        Placeholder values keyed by placeholder name.
    strict : bool
        When True, any placeholder missing from ``context`` raises ``ValueError``;
        when False it is left untouched for a later substitution pass.

    Returns
    -------
    Any
        A new value with placeholders substituted.
    """
    if isinstance(value, dict):
        return {
            substitute_placeholders(key, context, strict): substitute_placeholders(item, context, strict)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [substitute_placeholders(item, context, strict) for item in value]
    if not isinstance(value, str):
        return value
    whole = PLACEHOLDER_PATTERN.fullmatch(value)
    if whole and whole.group(1) in context:
        return context[whole.group(1)]

    def replace(match: re.Match[str]) -> str:
        """Return the context text for one embedded placeholder match."""
        key = match.group(1)
        if key in context:
            return str(context[key])
        if strict:
            raise ValueError(f"missing parameter {key!r}")
        return match.group(0)

    return PLACEHOLDER_PATTERN.sub(replace, value)


def merge_definitions(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Merge an entity or child template over a parent template definition.

    Top-level keys in ``override`` replace those in ``base``, except
    ``actions``, whose entries are combined so an entity can add or replace
    individual actions while inheriting the rest.

    Parameters
    ----------
    base : dict[str, Any]
        Parent template definition.
    override : dict[str, Any]
        Child template or entity definition.

    Returns
    -------
    dict[str, Any]
        Newly allocated merged definition.
    """
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if key == "actions" and isinstance(value, dict) and isinstance(merged.get("actions"), dict):
            merged["actions"].update(copy.deepcopy(value))
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _as_int(value: Any, field_name: str) -> int:
    """Convert an integer or integer string (decimal or ``0x`` hex) from YAML or the CLI.

    Parameters
    ----------
    value : Any
        Value to convert.
    field_name : str
        Field name used in error messages.

    Returns
    -------
    int
        Parsed integer.
    """
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be an integer: {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value, 0)
        except ValueError:
            pass
    raise ValueError(f"{field_name} must be an integer: {value!r}")


def load_coach_yaml(path: str | Path) -> dict[str, Any]:
    """Load and validate a coach definition YAML file.

    Parameters
    ----------
    path : str | Path
        Path to the YAML specification file.

    Returns
    -------
    dict[str, Any]
        Parsed coach specification mapping.
    """
    path_obj = Path(path).resolve()
    if not path_obj.is_file():
        raise FileNotFoundError(f"Coach YAML specification file not found: {path}")
    with path_obj.open("r", encoding="utf-8") as handle:
        data = YAML(typ="safe").load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"Coach YAML specification is not a dictionary: {path}")
    return data


def find_coach_file(spec_identifier: str | Path | None = None) -> Path:
    """Resolve a coach specification path or model name to an existing YAML file.

    Parameters
    ----------
    spec_identifier : str | Path | None
        Path to a YAML file, or model name / filename stem (e.g. 'thor-magnitude-bh35-2020').
        If None, returns the default coach file path.

    Returns
    -------
    Path
        Resolved absolute path to the YAML file.
    """
    if spec_identifier is None:
        return Path(DEFAULT_COACH_SPEC).resolve()

    candidate = Path(spec_identifier)
    if candidate.is_file():
        return candidate.resolve()

    coaches_dir = Path(__file__).resolve().parent / "coaches"
    stem = candidate.stem if candidate.suffix == ".yml" else str(spec_identifier)
    named_file = coaches_dir / f"{stem}.yml"
    if named_file.is_file():
        return named_file.resolve()

    raise FileNotFoundError(f"Unable to resolve coach specification: {spec_identifier}")


class Coach:
    """Resolve coach entities from templates and execute entity and group actions."""

    def __init__(
        self,
        config: ConfigParser,
        data: dict[str, Any],
        coach_file: str | Path | None = None,
        sources: dict[str, Source] | None = None,
    ) -> None:
        """Initialize the coach manager.

        Parameters
        ----------
        config : ConfigParser
            Application configuration loaded from config.ini.
        data : dict[str, Any]
            Parsed coach specification mapping.
        coach_file : str | Path | None
            Optional path to the source YAML file.
        sources : dict[str, Source] | None
            Optional pre-instantiated Source objects keyed by configuration section name.
        """
        self.config = config
        self.data = data
        self.coach_file = Path(coach_file).resolve() if coach_file else None
        self._sources: dict[str, Source] = dict(sources) if sources else {}
        self._stop_event = threading.Event()
        self.info: dict[str, Any] = data.get("coach", {})
        self.command_groups: dict[str, Any] = data.get("command_groups", {}) or {}
        self.templates: dict[str, Any] = data.get("templates", {}) or {}
        self.reserved: dict[int, set[int]] = self._load_reserved(data.get("reserved", []))
        self.entities: dict[str, dict[str, Any]] = {
            name: self._resolve_entity(name, definition)
            for name, definition in (data.get("entities", {}) or {}).items()
        }
        self.sleep: Callable[[float], None] = time.sleep

    @classmethod
    def load(
        cls,
        config: ConfigParser,
        coach_spec: str | Path | None = None,
        sources: dict[str, Source] | None = None,
    ) -> Coach:
        """Construct a Coach instance from configuration or a spec identifier.

        Parameters
        ----------
        config : ConfigParser
            Loaded application configuration.
        coach_spec : str | Path | None
            File path or coach name. If None, checks config['service'].get('coach_spec').
        sources : dict[str, Source] | None
            Optional pre-instantiated Source objects keyed by section name.

        Returns
        -------
        Coach
            Initialized Coach instance.
        """
        if coach_spec is None and config.has_section("coach"):
            coach_spec = (
                config["coach"].get("specfile", "").strip()
                or config["coach"].get("spec", "").strip()
                or config["coach"].get("coach_spec", "").strip()
                or None
            )
        if coach_spec is None and config.has_section("service"):
            coach_spec = config["service"].get("coach_spec", "").strip() or None
        file_path = find_coach_file(coach_spec)
        data = load_coach_yaml(file_path)
        return cls(config=config, data=data, coach_file=file_path, sources=sources)

    def get_source(self, config_section: str) -> Source:
        """Get or construct a Source instance for a configuration section via source.py interface.

        Parameters
        ----------
        config_section : str
            Section name in config.ini.

        Returns
        -------
        Source
            Instantiated Source subclass.
        """
        if config_section not in self._sources:
            if not self.config.has_section(config_section):
                raise ValueError(f"Config section [{config_section}] not found in configuration")
            self._sources[config_section] = Source.create(
                config_section, self.config, publisher=None, stop_event=self._stop_event
            )
        return self._sources[config_section]

    def resolve_config_section(self, entity: dict[str, Any], entity_name: str) -> str:
        """Resolve the config.ini section for an entity.

        Checks candidates (target, config_section, transport) against the [coach]
        section in config.ini, then falls back to matching section names in config.ini.

        Parameters
        ----------
        entity : dict[str, Any]
            Resolved entity definition.
        entity_name : str
            Entity name.

        Returns
        -------
        str
            Resolved section name in config.ini.
        """
        candidates = []
        for key in ("target", "config_section", "transport"):
            val = entity.get(key)
            if val and isinstance(val, str) and val.strip():
                candidates.append(val.strip())

        if self.config.has_section("coach"):
            coach_sec = self.config["coach"]
            for cand in candidates:
                mapped = coach_sec.get(cand, "").strip()
                if mapped:
                    return mapped

        for cand in candidates:
            if self.config.has_section(cand):
                return cand

        raise ValueError(
            f"Unable to resolve configuration section for entity {entity_name!r} "
            f"(candidates: {candidates})"
        )

    def list_entities(self) -> dict[str, dict[str, Any]]:
        """Return a summary of every coach entity: kind, area, transport and actions."""
        result: dict[str, dict[str, Any]] = {}
        for name, entity in self.entities.items():
            try:
                section = self.resolve_config_section(entity, name)
            except ValueError:
                section = ""
            result[name] = {
                "name": entity.get("name", name),
                "kind": entity.get("kind", ""),
                "area": entity.get("area", ""),
                "transport": entity.get("transport", ""),
                "config_section": section,
                "actions": list(entity.get("actions", {})),
                "confirm": bool(entity.get("confirm", False)),
                "verified": bool(entity.get("verified", False)),
            }
        return result

    def list_command_groups(self) -> dict[str, dict[str, Any]]:
        """Return a summary of every command group: description, members and actions."""
        return {
            name: {
                "description": group.get("description", ""),
                "members": list(group.get("members", [])),
                "actions": list(group.get("actions", {})),
            }
            for name, group in self.command_groups.items()
        }

    def execute(self, target: str, action: str, **kwargs: Any) -> list[dict[str, Any]]:
        """Execute an action on an entity or a command group.

        Parameters
        ----------
        target : str
            Entity name (e.g. ``kitchen_fan``) or command group name (e.g. ``all_interior_lights``).
        action : str
            Action name (e.g. ``on``, ``off``, ``toggle``, ``brightness``).
        **kwargs : Any
            Runtime parameters such as ``level`` or ``confirm``.

        Returns
        -------
        list[dict[str, Any]]
            One result dictionary per entity action performed.
        """
        if target in self.command_groups:
            return self.execute_group(target, action, **kwargs)
        if target in self.entities:
            return [self.execute_entity(target, action, **kwargs)]
        raise KeyError(f"Unknown entity or command group: {target!r}")

    def execute_group(self, group_name: str, action_name: str, **kwargs: Any) -> list[dict[str, Any]]:
        """Execute a command group action.

        A group either lists ``members`` and the ``actions`` (names) applied to
        every member, or maps each action to explicit steps
        (``{entity, action, params}``).

        Parameters
        ----------
        group_name : str
            Command group name.
        action_name : str
            Group action name.
        **kwargs : Any
            Runtime parameters passed to every step; they override step params.

        Returns
        -------
        list[dict[str, Any]]
            Result dictionaries for every entity action performed.
        """
        group_def = self.command_groups.get(group_name)
        if not group_def:
            raise KeyError(f"Command group not found: {group_name!r}")
        actions = group_def.get("actions", {})
        if action_name not in actions:
            raise ValueError(f"Action {action_name!r} not supported by group {group_name!r}")

        if isinstance(actions, list):
            results: list[dict[str, Any]] = []
            for member in group_def.get("members", []):
                results.extend(self.execute(member, action_name, **kwargs))
            return results

        steps = actions[action_name]
        if not isinstance(steps, list):
            raise ValueError(f"Group action {action_name!r} in {group_name!r} must be a list of steps")
        results = []
        for step in steps:
            if not isinstance(step, dict) or "entity" not in step:
                raise ValueError(f"Group {group_name!r} action {action_name!r} has a step without 'entity'")
            step_params = dict(step.get("params", {}))
            step_params.update(kwargs)
            results.extend(self.execute(step["entity"], step.get("action", action_name), **step_params))
        return results

    def execute_entity(self, entity_name: str, action_name: str, **kwargs: Any) -> dict[str, Any]:
        """Execute one entity action by sending its frames in order.

        Every frame is built and validated (including reserved-instance checks)
        before the first one is sent, so a bad parameter cannot leave a motor
        pair half-commanded. Each frame's ``wait`` is slept before it is sent.

        Parameters
        ----------
        entity_name : str
            Entity name.
        action_name : str
            Action name.
        **kwargs : Any
            Runtime placeholder values (e.g. ``level``); ``confirm`` is required
            for entities marked ``confirm: true``.

        Returns
        -------
        dict[str, Any]
            Execution summary including every frame sent.
        """
        entity = self.entities.get(entity_name)
        if entity is None:
            raise KeyError(f"Entity not found: {entity_name!r}")
        frames = entity.get("actions", {}).get(action_name)
        if frames is None:
            raise ValueError(f"Action {action_name!r} not supported by entity {entity_name!r}")
        confirmed = str(kwargs.pop("confirm", "")).strip().lower() in CONFIRM_VALUES
        if entity.get("confirm") and not confirmed:
            raise PermissionError(f"{entity_name} {action_name} requires confirmation (--confirm)")
        try:
            frames = substitute_placeholders(frames, kwargs, strict=True)
        except ValueError as error:
            raise ValueError(f"{entity_name} {action_name}: {error}") from error

        transport = str(entity.get("transport", "")).lower()
        config_section = self.resolve_config_section(entity, entity_name)
        source = self.get_source(config_section)
        result: dict[str, Any] = {
            "status": "ok",
            "target": entity_name,
            "action": action_name,
            "transport": transport,
            "config_section": config_section,
        }

        if transport == "rvc":
            built = [self._build_rvc_frame(frame, config_section) for frame in frames]
            for wait, payload in built:
                if wait:
                    self.sleep(wait)
                source.handle_command(payload)
            result["frames"] = [{"can_id": payload["can_id"], "data": payload["data"], "wait": wait} for wait, payload in built]
            return result
        if transport == "wled":
            built_wled = [self._build_wled_frame(frame) for frame in frames]
            for wait, payload in built_wled:
                if wait:
                    self.sleep(wait)
                source.handle_command(payload)
            result["frames"] = [payload for _wait, payload in built_wled]
            return result
        raise ValueError(f"Entity {entity_name!r} uses unsupported transport {transport!r}")

    def _resolve_template(self, template_name: str, chain: tuple[str, ...] = ()) -> dict[str, Any]:
        """Return a template merged with the templates it ``extends``.

        Parameters
        ----------
        template_name : str
            Template name under ``templates``.
        chain : tuple[str, ...]
            Templates already visited, used to reject ``extends`` loops.

        Returns
        -------
        dict[str, Any]
            Fully merged template definition.
        """
        if template_name in chain:
            raise ValueError(f"Template extends loop: {' -> '.join(chain + (template_name,))}")
        template = self.templates.get(template_name)
        if not isinstance(template, dict):
            raise ValueError(f"Template not found: {template_name!r}")
        parent = template.get("extends")
        if not parent:
            return copy.deepcopy(template)
        return merge_definitions(self._resolve_template(str(parent), chain + (template_name,)), template)

    def _resolve_entity(self, entity_name: str, definition: Any) -> dict[str, Any]:
        """Merge an entity with its template and fill in entity-level placeholders.

        Parameters
        ----------
        entity_name : str
            Entity name under ``entities``.
        definition : Any
            Raw entity definition from YAML.

        Returns
        -------
        dict[str, Any]
            Resolved entity with ``actions`` mapping action names to frame lists.
        """
        if not isinstance(definition, dict):
            raise ValueError(f"Entity {entity_name!r} must be a mapping")
        template_name = definition.get("template")
        base = self._resolve_template(str(template_name)) if template_name else {}
        base.pop("description", None)
        merged = merge_definitions(base, definition)
        context = {key: value for key, value in definition.items() if not isinstance(value, (dict, list))}
        context["entity"] = entity_name

        actions = substitute_placeholders(merged.get("actions") or {}, context, strict=False)
        if not isinstance(actions, dict):
            raise ValueError(f"Entity {entity_name!r} actions must be a mapping")
        for action_name, frames in list(actions.items()):
            frames = frames if isinstance(frames, list) else [frames]
            if not all(isinstance(frame, dict) for frame in frames):
                raise ValueError(f"Entity {entity_name!r} action {action_name!r} frames must be mappings")
            for frame in frames:
                self._check_static_reserved(entity_name, frame)
            actions[action_name] = frames
        merged["actions"] = actions
        if "state" in merged:
            merged["state"] = substitute_placeholders(merged["state"], context, strict=False)
        return merged

    def _load_reserved(self, entries: Any) -> dict[int, set[int]]:
        """Parse the ``reserved`` section into a mapping of DGN to forbidden instances.

        Parameters
        ----------
        entries : Any
            Raw ``reserved`` list from YAML.

        Returns
        -------
        dict[int, set[int]]
            Reserved instance numbers keyed by DGN.
        """
        reserved: dict[int, set[int]] = {}
        for entry in entries or []:
            if not isinstance(entry, dict):
                raise ValueError("Reserved entries must be mappings with dgn and instances")
            dgn = _as_int(entry.get("dgn"), "reserved dgn")
            instances = entry.get("instances", [entry["instance"]] if "instance" in entry else [])
            reserved.setdefault(dgn, set()).update(_as_int(item, "reserved instance") for item in instances)
        return reserved

    def _check_reserved(self, dgn: int, instance: int | None, label: str) -> None:
        """Raise ``PermissionError`` when a frame addresses a reserved instance.

        Parameters
        ----------
        dgn : int
            Frame DGN.
        instance : int | None
            Instance addressed by the frame, if known.
        label : str
            Entity or frame description used in the error message.
        """
        if instance is not None and instance in self.reserved.get(dgn, set()):
            raise PermissionError(f"{label}: instance {instance} on DGN 0x{dgn:05X} is reserved")

    def _check_static_reserved(self, entity_name: str, frame: dict[str, Any]) -> None:
        """Reject, at load time, entity frames that hard-code a reserved instance.

        Parameters
        ----------
        entity_name : str
            Entity name used in the error message.
        frame : dict[str, Any]
            Frame definition after entity placeholder substitution.
        """
        if "dgn" not in frame:
            return
        dgn = _as_int(frame["dgn"], "dgn")
        instance = frame.get("instance")
        if isinstance(instance, int) and not isinstance(instance, bool):
            self._check_reserved(dgn, instance, f"Entity {entity_name!r}")
        payload = frame.get("payload")
        if isinstance(payload, str) and len(payload) >= 2:
            self._check_reserved(dgn, int(payload[:2], 16), f"Entity {entity_name!r}")

    def _build_rvc_frame(self, frame: dict[str, Any], config_section: str) -> tuple[float, dict[str, Any]]:
        """Build and validate one RV-C frame for ``RvcSource.handle_command``.

        A frame either carries a raw hexadecimal ``payload`` or, for
        DC_DIMMER_COMMAND_2, named fields (``instance``, ``group``, ``level``
        percent, ``command``, ``delay``, ``interlock``). Short payloads are
        padded with 0xFF to eight bytes, matching the coach's own panels.

        Parameters
        ----------
        frame : dict[str, Any]
            Frame definition with all placeholders substituted.
        config_section : str
            RV-C configuration section providing ``priority`` and ``source``.

        Returns
        -------
        tuple[float, dict[str, Any]]
            Seconds to wait before sending, and the source command payload.
        """
        if not self.config.has_section(config_section):
            raise ValueError(f"Config section [{config_section}] not found in configuration")
        section = self.config[config_section]
        priority = section.getint("priority", fallback=RV_C_PRIORITY)
        source_addr = section.getint("source", fallback=SOURCE_ADDRESS)
        dgn = _as_int(frame.get("dgn", DC_DIMMER_COMMAND_DGN), "dgn")

        if "payload" in frame:
            try:
                data = normalize_can_payload(bytes.fromhex(str(frame["payload"])))
            except ValueError as error:
                raise ValueError(f"Invalid RV-C payload {frame['payload']!r}: {error}") from error
            instance = data[0] if data else None
        elif dgn == DC_DIMMER_COMMAND_DGN:
            if "command" not in frame:
                raise ValueError("DC dimmer frame needs a 'command'")
            instance = _as_int(frame.get("instance"), "instance")
            level = frame.get("level", 100)
            if isinstance(level, str):
                level = float(level)
            data = dc_dimmer_payload(
                instance,
                _as_int(frame.get("group", 0xFF), "group"),
                level,
                frame["command"],
                _as_int(frame.get("delay", 0xFF), "delay"),
                _as_int(frame.get("interlock", 0), "interlock"),
            )
        else:
            raise ValueError(f"Frame for DGN 0x{dgn:05X} needs a hexadecimal 'payload'")

        self._check_reserved(dgn, instance, "Frame")
        data = data.ljust(8, b"\xff")
        can_id = build_can_id(priority, dgn, source_addr)
        return self._frame_wait(frame), {"can_id": f"0x{can_id:08X}", "data": data.hex().upper()}

    def _build_wled_frame(self, frame: dict[str, Any]) -> tuple[float, dict[str, Any]]:
        """Split a WLED frame into its pre-send wait and JSON state payload.

        Parameters
        ----------
        frame : dict[str, Any]
            WLED JSON state fields, optionally with ``wait``.

        Returns
        -------
        tuple[float, dict[str, Any]]
            Seconds to wait before sending, and the WLED state payload.
        """
        payload = {key: value for key, value in frame.items() if key != "wait"}
        return self._frame_wait(frame), payload

    @staticmethod
    def _frame_wait(frame: dict[str, Any]) -> float:
        """Return a frame's validated pre-send ``wait`` in seconds.

        Parameters
        ----------
        frame : dict[str, Any]
            Frame definition.

        Returns
        -------
        float
            Wait in seconds, between 0 and ``MAX_FRAME_WAIT_SECONDS``.
        """
        wait = float(frame.get("wait", 0))
        if not 0 <= wait <= MAX_FRAME_WAIT_SECONDS:
            raise ValueError(f"Frame wait must be between 0 and {MAX_FRAME_WAIT_SECONDS:g} seconds: {wait}")
        return wait
