from __future__ import annotations

import logging
import time
from typing import Any

from .source import Source


LOGGER = logging.getLogger(__name__)


def decode_dgn(dgn: str, data: str, spec: dict[str, Any], parameterized: bool) -> dict[str, Any]:
    """Decode an RV-C DGN payload using its specification into named fields."""
    decoder = spec.get(dgn)
    result = {"dgn": dgn, "data": data, "name": "UNKNOWN-" + dgn}
    if not decoder:
        return result
    result["name"] = decoder.get("name", result["name"])
    params = list(spec.get(decoder.get("alias"), {}).get("parameters", [])) + list(decoder.get("parameters", []))
    for parameter in params:
        name = parameter.get("name", "field")
        if parameterized:
            name = name.translate(str.maketrans(" /", "__", "() ")).lower()
        byte_range = parameter.get("byte")
        try:
            if isinstance(byte_range, int):
                raw = data[byte_range * 2:(byte_range + 1) * 2]
            else:
                first, last = (int(part) for part in str(byte_range).split("-"))
                raw = "".join(data[index * 2:index * 2 + 2] for index in range(last, first - 1, -1))
            value = int(raw, 16)
            bit_range = parameter.get("bit")
            if bit_range is not None:
                if isinstance(bit_range, int):
                    value = (value >> bit_range) & 1
                else:
                    low, high = (int(part) for part in str(bit_range).split("-"))
                    value = (value >> low) & ((1 << (high - low + 1)) - 1)
            unit = str(parameter.get("unit", "")).lower()
            kind = parameter.get("type", "")
            if unit == "pct" and value != 255:
                value /= 2
            elif unit == "v" and kind == "uint16":
                value = round(value * 0.05, 2)
            elif unit == "a" and kind == "uint16":
                value = round(value * 0.05 - 1600, 2)
            elif unit == "deg c" and kind == "uint8":
                value -= 40
            result[name] = value
        except (TypeError, ValueError, IndexError):
            continue
    return result


class RvcSource(Source, source_name="rvc"):
    source_name = "rvc"
    config_section = "rv_c"

    def __init__(self, config: Any, publisher: Any, stop_event: Any) -> None:
        """Initialize the RV-C source without opening the CAN bus yet."""
        super().__init__(config, publisher, stop_event)
        self.bus = None

    def handle_command(self, payload: dict[str, Any]) -> None:
        """Validate and send a configured RV-C CAN command when writes are enabled."""
        section = self.section
        if not section.getboolean("write_enabled", fallback=False) or self.bus is None:
            LOGGER.warning("RV-C write ignored because it is disabled or unavailable")
            return
        try:
            can_id = int(str(payload["can_id"]), 0)
            data = bytes.fromhex(str(payload["data"]))
            if not 0 <= can_id <= 0x1FFFFFFF or len(data) > 8:
                raise ValueError("CAN ID or data length is out of range")
            import can
            self.bus.send(can.Message(arbitration_id=can_id, data=data, is_extended_id=True))
        except (KeyError, TypeError, ValueError) as error:
            LOGGER.warning("Invalid RV-C command: %s", error)

    def interrogate(self) -> dict[str, Any]:
        """Listen briefly and return the latest decoded state for each DGN."""
        import can
        from ruamel.yaml import YAML

        section = self.section
        with open(section["specfile"], encoding="utf-8") as handle:
            spec = YAML(typ="safe").load(handle)
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
                can_id = f"{message.arbitration_id:029b}"
                dgn = f"{int(can_id[4:21], 2):05X}"
                decoded = decode_dgn(dgn, bytes(message.data).hex().upper(), spec, section.getboolean("parameterized_strings", fallback=True))
                source = can_id[21:29]
                latest[f"{dgn}:{source}"] = decoded
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
            from ruamel.yaml import YAML
            with open(section["specfile"], encoding="utf-8") as handle:
                YAML(typ="safe").load(handle)
            bus = can.Bus(interface="socketcan", channel=section.get("interface", "can0"))
            bus.shutdown()
            return {"ok": True, "message": f"SocketCAN {section.get('interface', 'can0')} is available"}
        except (OSError, KeyError, ImportError, ValueError) as error:
            return {"ok": False, "message": f"RV-C check failed: {error}"}

    def run(self) -> None:
        """Receive RV-C frames, decode them, and publish readings until stopped."""
        try:
            import can
            from ruamel.yaml import YAML
            section = self.section
            with open(section["specfile"], encoding="utf-8") as handle:
                spec = YAML(typ="safe").load(handle)
            self.bus = can.Bus(interface="socketcan", channel=section.get("interface", "can0"))
            while not self.stop_event.is_set():
                message = self.bus.recv(timeout=1)
                if message is None:
                    continue
                can_id = f"{message.arbitration_id:029b}"
                dgn = f"{int(can_id[4:21], 2):05X}"
                decoded = decode_dgn(dgn, bytes(message.data).hex().upper(), spec, section.getboolean("parameterized_strings", fallback=True))
                self.publisher.publish(f"rvc/{decoded['name']}", decoded)
        except Exception:
            LOGGER.exception("RV-C source stopped")