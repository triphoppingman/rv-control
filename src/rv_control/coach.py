"""Coach abstraction manager and command orchestrator."""

from __future__ import annotations

import logging
import threading
from configparser import ConfigParser
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from .rvc_util import (
    RV_C_PRIORITY,
    SOURCE_ADDRESS,
    build_can_id,
    build_can_message,
    dc_dimmer_payload,
    normalize_can_payload,
)
from .source import Source

LOGGER = logging.getLogger(__name__)
DEFAULT_COACH_SPEC = str(Path(__file__).resolve().parent / "coaches/thor-magnitude-bh35-2020.yml")


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
    """Manage coach semantic abstraction and orchestrate command and group execution."""

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
        self.commands: dict[str, Any] = data.get("commands", {})
        self.command_groups: dict[str, Any] = data.get("command_groups", {}) or data.get("groups", {})

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

    def resolve_config_section(self, cmd_def: dict[str, Any], command_name: str) -> str:
        """Resolve the config.ini section for a command.

        Checks candidates (target, config_section, transport) against the [coach]
        section in config.ini, then falls back to matching section names in config.ini.

        Parameters
        ----------
        cmd_def : dict[str, Any]
            Command definition dictionary.
        command_name : str
            Command name string.

        Returns
        -------
        str
            Resolved section name in config.ini.
        """
        candidates = []
        for key in ("target", "config_section", "transport"):
            val = cmd_def.get(key)
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
            f"Unable to resolve configuration section for command {command_name!r} "
            f"(candidates: {candidates})"
        )

    def list_commands(self) -> dict[str, dict[str, Any]]:
        """Return a dictionary of available individual commands and their actions."""
        res = {}
        for name, cmd in self.commands.items():
            try:
                resolved_sec = self.resolve_config_section(cmd, name)
            except ValueError:
                resolved_sec = cmd.get("config_section", "")
            res[name] = {
                "description": cmd.get("description", ""),
                "transport": cmd.get("transport", "unknown"),
                "config_section": resolved_sec,
                "actions": list(cmd.get("actions", {}).keys()),
            }
        return res

    def list_command_groups(self) -> dict[str, dict[str, Any]]:
        """Return a dictionary of available command groups and their actions."""
        return {
            name: {
                "description": group.get("description", ""),
                "actions": list(group.get("actions", {}).keys()),
            }
            for name, group in self.command_groups.items()
        }

    def execute(self, target: str, action: str, **kwargs: Any) -> list[dict[str, Any]]:
        """Orchestrate execution of a single command or command group.

        Parameters
        ----------
        target : str
            Name of a command (e.g. 'livingroom_lights') or group (e.g. 'all_lights').
        action : str
            Action name to perform (e.g. 'on', 'off', 'toggle', 'brightness').
        **kwargs : Any
            Additional keyword arguments passed to the action.

        Returns
        -------
        list[dict[str, Any]]
            List of execution result dictionaries from each step performed.
        """
        if target in self.command_groups:
            return self.execute_group(target, action, **kwargs)
        if target in self.commands:
            return [self.execute_command(target, action, **kwargs)]
        raise KeyError(f"Unknown command or command group: {target!r}")

    def execute_group(self, group_name: str, action_name: str, **kwargs: Any) -> list[dict[str, Any]]:
        """Orchestrate execution of all commands configured in a command group action.

        Parameters
        ----------
        group_name : str
            Name of the command group.
        action_name : str
            Action name configured for the group.
        **kwargs : Any
            Additional parameter overrides passed to group steps.

        Returns
        -------
        list[dict[str, Any]]
            List of result dictionaries for every step executed in the group.
        """
        group_def = self.command_groups.get(group_name)
        if not group_def:
            raise KeyError(f"Command group not found: {group_name!r}")

        actions = group_def.get("actions", {})
        steps = actions.get(action_name)
        if steps is None:
            raise ValueError(f"Action {action_name!r} not supported by group {group_name!r}")

        if not isinstance(steps, list):
            raise ValueError(f"Group action {action_name!r} in {group_name!r} must be a list of steps")

        results = []
        for step in steps:
            if not isinstance(step, dict) or "command" not in step:
                continue
            cmd = step["command"]
            act = step.get("action", action_name)
            step_params = dict(step.get("params", {}))
            step_params.update(kwargs)
            results.extend(self.execute(cmd, act, **step_params))

        return results

    def execute_command(self, command_name: str, action_name: str, **kwargs: Any) -> dict[str, Any]:
        """Execute a single semantic command mapped in the coach specification.

        Parameters
        ----------
        command_name : str
            Name of the command.
        action_name : str
            Action name.
        **kwargs : Any
            Action parameter overrides.

        Returns
        -------
        dict[str, Any]
            Execution status and response details.
        """
        cmd_def = self.commands.get(command_name)
        if not cmd_def:
            raise KeyError(f"Command not found: {command_name!r}")

        actions = cmd_def.get("actions", {})
        action_def = actions.get(action_name)
        if action_def is None:
            raise ValueError(f"Action {action_name!r} not supported by command {command_name!r}")

        transport = cmd_def.get("transport", "").lower()
        config_section = self.resolve_config_section(cmd_def, command_name)

        source = self.get_source(config_section)

        if transport == "rvc":
            return self._execute_rvc(source, command_name, action_name, cmd_def, action_def, config_section, **kwargs)
        elif transport == "wled":
            return self._execute_wled(source, command_name, action_name, cmd_def, action_def, config_section, **kwargs)
        elif transport == "bluetooth":
            return self._execute_bluetooth(source, command_name, action_name, cmd_def, action_def, config_section, **kwargs)
        else:
            payload = self._build_generic_payload(action_def, **kwargs)
            source.handle_command(payload)
            return {
                "status": "ok",
                "target": command_name,
                "action": action_name,
                "transport": transport,
                "config_section": config_section,
                "payload": payload,
            }

    def _execute_rvc(
        self,
        source: Source,
        command_name: str,
        action_name: str,
        cmd_def: dict[str, Any],
        action_def: dict[str, Any] | str,
        config_section: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Execute an RV-C transport command via Source interface.

        Parameters
        ----------
        source : Source
            Instantiated Source object for the RVC section.
        command_name : str
            Command identifier.
        action_name : str
            Action identifier.
        cmd_def : dict[str, Any]
            Full command definition dictionary.
        action_def : dict[str, Any] | str
            Specific action definition from YAML.
        config_section : str
            Section in config.ini defining the CAN interface.
        **kwargs : Any
            Runtime parameter overrides.

        Returns
        -------
        dict[str, Any]
            Execution summary dictionary.
        """
        if not self.config.has_section(config_section):
            raise ValueError(f"Config section [{config_section}] not found in configuration")

        section = self.config[config_section]
        priority = section.getint("priority", fallback=RV_C_PRIORITY)
        source_addr = section.getint("source", fallback=SOURCE_ADDRESS)

        rvc_block = cmd_def.get("rvc", {})
        protocol_cmd = rvc_block.get("command", "").lower()
        dgn = rvc_block.get("dgn")
        if isinstance(dgn, str):
            dgn = int(dgn, 0)
        instance = rvc_block.get("instance", 0)
        group = rvc_block.get("group", 1)
        defaults = rvc_block.get("defaults", {})

        action_dict = action_def if isinstance(action_def, dict) else {"command": str(action_def)}
        rvc_cmd = kwargs.get("command") or action_dict.get("command") or action_name

        if protocol_cmd == "dc_dimmer_command_2" or dgn == 0x1FEDB:
            level = kwargs.get("level") if "level" in kwargs else kwargs.get("brightness")
            if level is None:
                level = action_dict.get("level", defaults.get("level", 100))
            delay = kwargs.get("delay", action_dict.get("delay", defaults.get("delay", 0)))
            interlock = kwargs.get("interlock", action_dict.get("interlock", defaults.get("interlock", 0)))

            payload_bytes = dc_dimmer_payload(instance, group, level, rvc_cmd, delay, interlock)
        else:
            payload_data = kwargs.get("payload") or action_dict.get("payload") or defaults.get("payload")
            if payload_data is not None:
                payload_bytes = normalize_can_payload(bytes.fromhex(str(payload_data)) if isinstance(payload_data, str) else payload_data)
            else:
                payload_bytes = bytes([instance, group])

        can_id = build_can_id(priority, dgn or 0x1FEDB, source_addr)
        cmd_payload: dict[str, Any] = {
            "can_id": f"0x{can_id:08X}",
            "data": payload_bytes.hex().upper(),
        }

        source.handle_command(cmd_payload)

        return {
            "status": "ok",
            "target": command_name,
            "action": action_name,
            "transport": "rvc",
            "config_section": config_section,
            "can_id": f"0x{can_id:08X}",
            "payload": payload_bytes.hex().upper(),
        }

    def _execute_wled(
        self,
        source: Source,
        command_name: str,
        action_name: str,
        cmd_def: dict[str, Any],
        action_def: dict[str, Any] | str,
        config_section: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Execute a WLED transport command via Source interface.

        Parameters
        ----------
        source : Source
            Instantiated Source object for the WLED section.
        command_name : str
            Command identifier.
        action_name : str
            Action identifier.
        cmd_def : dict[str, Any]
            Full command definition dictionary.
        action_def : dict[str, Any] | str
            Action definition from YAML.
        config_section : str
            Section in config.ini defining the WLED URL.
        **kwargs : Any
            Runtime parameter overrides.

        Returns
        -------
        dict[str, Any]
            Execution summary dictionary.
        """
        action_dict = action_def if isinstance(action_def, dict) else {}

        payload: dict[str, Any] = {}
        if "state" in action_dict and isinstance(action_dict["state"], dict):
            payload.update(action_dict["state"])
        elif action_name == "on":
            payload["on"] = True
        elif action_name == "off":
            payload["on"] = False
        elif action_name in ("brightness", "level"):
            bri = kwargs.get("brightness") if "brightness" in kwargs else kwargs.get("level", 255)
            payload["bri"] = bri

        payload.update({k: v for k, v in kwargs.items() if k in ("on", "bri", "seg", "transition")})

        source.handle_command(payload)

        return {
            "status": "ok",
            "target": command_name,
            "action": action_name,
            "transport": "wled",
            "config_section": config_section,
            "payload": payload,
        }

    def _execute_bluetooth(
        self,
        source: Source,
        command_name: str,
        action_name: str,
        cmd_def: dict[str, Any],
        action_def: dict[str, Any] | str,
        config_section: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Execute a Bluetooth transport command via Source interface.

        Parameters
        ----------
        source : Source
            Instantiated Source object for the Bluetooth section.
        command_name : str
            Command identifier.
        action_name : str
            Action identifier.
        cmd_def : dict[str, Any]
            Full command definition dictionary.
        action_def : dict[str, Any] | str
            Action definition from YAML.
        config_section : str
            Section in config.ini.
        **kwargs : Any
            Runtime parameter overrides.

        Returns
        -------
        dict[str, Any]
            Execution summary dictionary.
        """
        action_dict = action_def if isinstance(action_def, dict) else {"payload": str(action_def)}
        payload_hex = kwargs.get("payload") or action_dict.get("payload", "")
        payload = {"payload": payload_hex}

        source.handle_command(payload)

        return {
            "status": "ok",
            "target": command_name,
            "action": action_name,
            "transport": "bluetooth",
            "config_section": config_section,
            "payload": payload_hex,
        }

    def _build_generic_payload(self, action_def: dict[str, Any] | str, **kwargs: Any) -> dict[str, Any]:
        """Build a generic payload dictionary from action definition and kwargs."""
        payload: dict[str, Any] = {}
        if isinstance(action_def, dict):
            payload.update(action_def)
        payload.update(kwargs)
        return payload
