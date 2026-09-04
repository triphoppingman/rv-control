from __future__ import annotations

import logging
import time
from typing import Any

from .rvc_util import (
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
from .source import Source


LOGGER = logging.getLogger(__name__)


class RvcSource(Source, source_name="rvc"):
    """RV-C source lifecycle and integration with the shared utility library."""

    source_name = "rvc"
    config_section = "rv_c"

    def __init__(self, config: Any, publisher: Any, stop_event: Any, section_name: str | None = None) -> None:
        """Initialize the RV-C source without opening the CAN bus yet."""
        super().__init__(config, publisher, stop_event, section_name)
        self.bus = None

    def handle_command(self, payload: dict[str, Any]) -> None:
        """Validate and send a configured RV-C CAN command when writes are enabled."""
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
                import can

                interface = section.get("interface", "can0")
                transient_bus = can.Bus(interface="socketcan", channel=interface)
                try:
                    send_can_message(transient_bus, (can_id >> 26) & 0x7, (can_id >> 8) & 0x1FFFF, can_id & 0xFF, data)
                finally:
                    transient_bus.shutdown()
        except (KeyError, TypeError, ValueError, OSError, can.CanError) as error:
            LOGGER.warning("Invalid RV-C command: %s", error)

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
                message = self.bus.recv(timeout=1)
                if message is None:
                    continue
                decoded = decode_message(message, spec, section.getboolean("parameterized_strings", fallback=True))
                if decoded is not None:
                    self.publisher.publish(f"rvc/{decoded['name']}", decoded)
        except Exception:
            LOGGER.exception("RV-C source stopped")
