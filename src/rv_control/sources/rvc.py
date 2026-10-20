from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterable, Mapping
from typing import Any

from ..rvc_util import (
    ADDRESS_CLAIM_DGN,
    DATE_TIME_STATUS_DGN,
    DC_DIMMER_COMMAND_DGN,
    DC_DIMMER_COMMANDS,
    AC_LOAD_COMMAND_DGN,
    CHARGER_COMMAND_DGN,
    CIRCULATION_PUMP_COMMAND_DGN,
    DC_LOAD_COMMAND_DGN,
    GENERATOR_COMMAND_DGN,
    GENERIC_INDICATOR_COMMAND_DGN,
    INVERTER_COMMAND_DGN,
    GLOBAL_ADDRESS,
    REQUEST_DGN,
    RVC_SPECFILE,
    RV_C_PRIORITY,
    SET_DATE_TIME_DGN,
    SOURCE_ADDRESS,
    TIME_ZONE_CODES,
    RvcName,
    address_claim_message,
    address_claim_payload,
    address_claim_request_payload,
    build_address_claim_message,
    build_address_claim_request_message,
    build_can_id,
    build_can_message,
    build_dc_dimmer_message,
    build_request_message,
    build_spec_message,
    command_can_id,
    datetime_payload,
    charger_payload,
    circulation_pump_payload,
    decode_datetime,
    decode_dgn,
    decode_message,
    decode_rvc_name,
    dc_dimmer_payload,
    encode_rvc_name,
    format_datetime,
    format_message,
    generator_payload,
    indicator_payload,
    inverter_payload,
    is_address_claim,
    load_spec,
    normalize_can_payload,
    rvc_name_details,
    send_can_message,
    send_spec_message,
    load_payload,
    spec_dgn,
    spec_payload_length,
)
from .writable import ActionDefinition, WritableSource, action_integer


LOGGER = logging.getLogger(__name__)


class RvcSource(WritableSource, source_name="rvc"):
    """RV-C source lifecycle and integration with the shared utility library."""

    source_name = "rvc"
    config_section = "rv_c"
    supports_status_requests = True

    @classmethod
    def supported_actions(cls) -> dict[str, ActionDefinition]:
        """Describe named dimmer commands and arbitrary bounded RV-C payload frames."""
        byte = {"type": "integer", "min": 0, "max": 255}
        return {
            "dc_dimmer": ActionDefinition("Send DC_DIMMER_COMMAND_2 with percent brightness.", {
                "instance": dict(byte),
                "command": {"type": ["string", "integer"], "enum": [*DC_DIMMER_COMMANDS, *range(256)]},
                "group": {**byte, "default": 255},
                "level": {"type": "number", "min": 0, "max": 100, "default": 100},
                "delay": {**byte, "default": 255},
                "interlock": {"type": "integer", "min": 0, "max": 3, "default": 0},
            }),
            "frame": ActionDefinition("Send a DGN with up to eight hexadecimal payload bytes.", {
                "dgn": {"type": "integer", "min": 0, "max": 0x1FFFF},
                "payload": {"type": "string"},
            }),
        }

    @classmethod
    def translate_frame(cls, frame: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        """Translate existing named/raw coach frames without changing their YAML format."""
        dgn = action_integer(frame.get("dgn", DC_DIMMER_COMMAND_DGN), "dgn")
        if "payload" in frame:
            return "frame", {"dgn": dgn, "payload": frame["payload"]}
        if dgn != DC_DIMMER_COMMAND_DGN:
            raise ValueError(f"Frame for DGN 0x{dgn:05X} needs a hexadecimal 'payload'")
        params = {key: value for key, value in frame.items() if key != "dgn"}
        for name in ("instance", "group", "delay", "interlock"):
            if name in params:
                params[name] = action_integer(params[name], name)
        if isinstance(params.get("level"), str):
            params["level"] = float(params["level"])
        command = params.get("command")
        if isinstance(command, str):
            params["command"] = command.lower() if command.lower() in DC_DIMMER_COMMANDS else action_integer(command, "command")
        return "dc_dimmer", params

    @classmethod
    def _encode_action(
        cls, action: str, params: dict[str, Any], section: Mapping[str, str],
        *, reserved: Mapping[int, set[int]] | None = None,
    ) -> dict[str, Any]:
        """Encode using canonical RV-C helpers and enforce coach safety reservations."""
        if action == "frame":
            dgn = params["dgn"]
            data = normalize_can_payload(bytes.fromhex(params["payload"]))
        else:
            dgn = DC_DIMMER_COMMAND_DGN
            data = dc_dimmer_payload(
                params["instance"], params["group"], params["level"],
                params["command"], params["delay"], params["interlock"],
            )
        if data and data[0] in (reserved or {}).get(dgn, set()):
            raise PermissionError(f"Frame: instance {data[0]} on DGN 0x{dgn:05X} is reserved")
        priority = int(section.get("priority", str(RV_C_PRIORITY)))
        source = int(section.get("source", str(SOURCE_ADDRESS)))
        can_id = build_can_id(priority, dgn, source)
        return {"can_id": f"0x{can_id:08X}", "data": data.ljust(8, b"\xff").hex().upper()}

    @classmethod
    def dispatch_budget(cls, section: Mapping[str, str]) -> float:
        """Reserve the reliable CAN send timeout for each operation."""
        return 1.0

    def __init__(self, config: Any, publisher: Any, stop_event: Any, section_name: str | None = None) -> None:
        """Initialize the RV-C source without opening the CAN bus yet."""
        super().__init__(config, publisher, stop_event, section_name)
        self.bus = None
        self._pending_requests: list[int] = []
        self._requests_lock = threading.Lock()

    def request_status(self, dgns: Iterable[int]) -> None:
        """Queue RV-C Requests asking every node to send the given DGNs.

        The requests are broadcast from the daemon thread as soon as its CAN bus
        is open, so this may be called before or after the source starts. A
        Request asks devices to report status; it does not change any load, so
        it is not gated by ``write_enabled``.

        Parameters
        ----------
        dgns : Iterable[int]
            DGNs to request, such as ``0x1FEDA`` for DC dimmer status.
        """
        validated = []
        for dgn in dgns:
            if not 0 <= int(dgn) <= 0x1FFFF:
                raise ValueError(f"RV-C DGN must fit 17 bits: {dgn}")
            validated.append(int(dgn))
        with self._requests_lock:
            self._pending_requests.extend(dgn for dgn in validated if dgn not in self._pending_requests)

    def _send_pending_requests(self, bus: Any) -> None:
        """Broadcast queued status requests on an open bus, logging rather than raising send failures.

        Parameters
        ----------
        bus : Any
            Open python-can bus.
        """
        with self._requests_lock:
            pending, self._pending_requests = self._pending_requests, []
        if not pending:
            return
        import can

        section = self.section
        priority = section.getint("priority", fallback=RV_C_PRIORITY)
        source = section.getint("source", fallback=SOURCE_ADDRESS)
        for dgn in pending:
            try:
                bus.send(build_request_message(dgn, source, GLOBAL_ADDRESS, priority), timeout=1.0)
                LOGGER.info("Requested RV-C DGN %05X from all nodes", dgn)
            except (OSError, ValueError, can.CanError) as error:
                LOGGER.warning("RV-C status request for DGN %05X failed: %s", dgn, error)

    def handle_command(self, payload: dict[str, Any]) -> None:
        """Validate and send a configured RV-C CAN command when writes are enabled."""
        import can

        section = self.section
        if not section.getboolean("write_enabled", fallback=False):
            LOGGER.warning("RV-C write ignored because it is disabled")
            return
        try:
            can_id = int(str(payload["can_id"]), 0)
            data = bytes.fromhex(str(payload["data"]))
            if not 0 <= can_id <= 0x1FFFFFFF or len(data) > 8:
                raise ValueError("CAN ID or data length is out of range")
            bus = payload.get("bus") or self.bus
            if bus is not None:
                send_can_message(bus, (can_id >> 26) & 0x7, (can_id >> 8) & 0x1FFFF, can_id & 0xFF, data)
            else:
                interface = section.get("interface", "can0")
                transient_bus = can.Bus(interface="socketcan", channel=interface)
                try:
                    send_can_message(transient_bus, (can_id >> 26) & 0x7, (can_id >> 8) & 0x1FFFF, can_id & 0xFF, data)
                finally:
                    transient_bus.shutdown()
        except (KeyError, TypeError, ValueError, OSError, can.CanError) as error:
            LOGGER.warning("Invalid RV-C command: %s", error)

    def command_ready(self) -> bool:
        """Report whether the daemon currently owns an open CAN bus."""
        return self.is_alive() and self.bus is not None

    def execute_command(self, payload: dict[str, Any]) -> None:
        """Send a preflighted command on the running bus with bounded failure reporting."""
        if not self.section.getboolean("write_enabled", fallback=False):
            raise PermissionError("RV-C writes are disabled")
        if not self.command_ready():
            raise RuntimeError("RV-C source is unavailable")
        can_id = int(str(payload["can_id"]), 0)
        data = bytes.fromhex(str(payload["data"]))
        if not 0 <= can_id <= 0x1FFFFFFF or len(data) > 8:
            raise ValueError("CAN ID or data length is out of range")
        message = build_can_message((can_id >> 26) & 7, (can_id >> 8) & 0x1FFFF, can_id & 0xFF, data)
        self.bus.send(message, timeout=1.0)

    def interrogate(self) -> dict[str, Any]:
        """Listen briefly and return the latest decoded state for each DGN."""
        import can

        section = self.section
        spec = load_spec(section["specfile"])
        duration = section.getfloat("interrogate_seconds", fallback=10.0)
        if duration <= 0:
            raise ValueError("rv_c interrogate_seconds must be greater than zero")
        bus = can.Bus(interface="socketcan", channel=section.get("interface", "can0"))
        latest = {}
        frames = 0
        deadline = time.monotonic() + duration
        try:
            while time.monotonic() < deadline:
                message = bus.recv(timeout=min(1, max(0, deadline - time.monotonic())))
                if message is None:
                    continue
                decoded = decode_message(message, spec, section.getboolean("parameterized_strings", fallback=True))
                if decoded is None:
                    continue
                latest[f"{decoded['dgn']}:{decoded['source']}"] = decoded
                frames += 1
        finally:
            bus.shutdown()
        if not latest:
            raise RuntimeError(f"No RV-C frames received in {duration:g} seconds")
        return {"duration_seconds": duration, "frames": frames, "messages": list(latest.values())}

    def comms_check(self) -> dict[str, Any]:
        """Validate the RV-C specification and test access to the configured CAN bus."""
        section = self.section
        try:
            import can

            load_spec(section["specfile"])
            bus = can.Bus(interface="socketcan", channel=section.get("interface", "can0"))
            bus.shutdown()
            return {"ok": True, "message": f"SocketCAN {section.get('interface', 'can0')} is available"}
        except (OSError, KeyError, ImportError, ValueError) as error:
            return {"ok": False, "message": f"RV-C check failed: {error}"}

    def run(self) -> None:
        """Receive RV-C frames, decode them, and publish readings until stopped."""
        try:
            import can

            section = self.section
            spec = load_spec(section["specfile"])
            self.bus = can.Bus(interface="socketcan", channel=section.get("interface", "can0"))
            while not self.stop_event.is_set():
                self._send_pending_requests(self.bus)
                message = self.bus.recv(timeout=1)
                if message is None:
                    continue
                decoded = decode_message(message, spec, section.getboolean("parameterized_strings", fallback=True))
                if decoded is not None:
                    self.publisher.publish(f"rvc/{decoded['name']}", decoded)
        except Exception:
            LOGGER.exception("RV-C source stopped")
        finally:
            bus, self.bus = self.bus, None
            if bus is not None:
                bus.shutdown()
