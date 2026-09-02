from __future__ import annotations

import asyncio
import logging
import struct
from typing import Any, Callable

from .source import Source


LOGGER = logging.getLogger(__name__)


class HughesSource(Source, source_name="hughes"):
    """Hughes BLE telemetry source with optional daemon reconnects."""
    source_name = "hughes"
    config_section = "hughes"
    LEGACY_HEADER = b"\x01\x03\x20"
    MODERN_HEADER = b"$yw@"

    @staticmethod
    def attribute_inventory(device_name: str | None) -> list[dict[str, str]]:
        """Describe telemetry fields exposed by the detected Hughes protocol."""
        modern = (device_name or "").strip().startswith(("WD_V5", "WD_E5"))
        attributes = [
            {"attribute": "voltage_line_1", "unit": "V", "access": "read"},
            {"attribute": "current_line_1", "unit": "A", "access": "read"},
            {"attribute": "power_line_1", "unit": "W", "access": "read"},
            {"attribute": "total_power", "unit": "kWh", "access": "read"},
            {"attribute": "error_code", "access": "read"},
            {"attribute": "error_text", "access": "read"},
        ]
        if modern:
            attributes.extend([
                {"attribute": "voltage_line_2", "unit": "V", "access": "read"},
                {"attribute": "current_line_2", "unit": "A", "access": "read"},
                {"attribute": "power_line_2", "unit": "W", "access": "read"},
                {"attribute": "combined_power", "unit": "W", "access": "read"},
            ])
        return attributes

    def comms_check(self) -> dict[str, Any]:
        """Check Hughes Bluetooth availability and return discovered attributes."""
        return asyncio.run(self._comms_check())

    def interrogate(self) -> dict[str, Any]:
        """Connect once and return the first complete power measurement."""
        address = self.section.get("address", "").strip()
        if not address:
            raise ValueError("Hughes address is required")
        return asyncio.run(self._interrogate_ble(address))

    async def _comms_check(self) -> dict[str, Any]:
        """Discover the configured Hughes device and report protocol capabilities."""
        section = self.section
        try:
            from bleak import BleakScanner
            address = section.get("address", "").strip()
            expected_name = section.get("name", "").strip()
            attributes = self.attribute_inventory(expected_name)
            if not address:
                return {"ok": False, "message": "Hughes address is required", "attributes": attributes}
            devices = await BleakScanner.discover(
                timeout=5, adapter=section.get("adapter", "hci0")
            )
            match = next((item for item in devices if item.address.lower() == address.lower()), None)
            if match is None:
                return {"ok": False, "message": f"Hughes device not found: {address}", "attributes": attributes}
            attributes = self.attribute_inventory(match.name)
            if expected_name and (match.name or "").strip() != expected_name.strip():
                return {"ok": False, "message": f"Hughes address found as {match.name!r}, expected {expected_name!r}", "attributes": attributes}
            return {"ok": True, "message": f"Hughes device matched: {match.name or 'unnamed'} ({address})", "attributes": attributes}
        except (OSError, ImportError, ValueError) as error:
            return {"ok": False, "message": f"Hughes Bluetooth check failed: {error}"}

    async def _interrogate_ble(self, address: str) -> dict[str, Any]:
        """Connect to one Hughes device and await its first complete measurement."""
        from bleak import BleakClient

        section = self.section
        packet = asyncio.get_running_loop().create_future()
        legacy_buffer = bytearray()
        async with BleakClient(address) as client:
            services = client.services
            modern = any(str(service.uuid).lower() == "000000ff-0000-1000-8000-00805f9b34fb" for service in services)
            tx = "0000ff01-0000-1000-8000-00805f9b34fb" if modern else "0000ffe2-0000-1000-8000-00805f9b34fb"

            def callback(_sender: Any, payload: bytearray) -> None:
                """Decode notification bytes and complete the pending measurement."""
                nonlocal legacy_buffer
                data = bytes(payload)
                decoded = self.decode(data)
                if decoded is None and not modern:
                    legacy_buffer.extend(data)
                    if len(legacy_buffer) >= 40:
                        decoded = self.decode(bytes(legacy_buffer[:40]))
                        legacy_buffer.clear()
                if decoded is not None and not packet.done():
                    packet.set_result(decoded)

            await client.start_notify(tx, callback)
            try:
                if modern:
                    await client.write_gatt_char(tx, b"!%!%,protocol,open,")
                return await asyncio.wait_for(packet, timeout=15)
            finally:
                await client.stop_notify(tx)

    @staticmethod
    def decode(data: bytes) -> dict | None:
        """Decode a legacy or modern Hughes packet, or return None if incomplete."""
        if len(data) >= 40 and data[:3] == HughesSource.LEGACY_HEADER:
            values = [struct.unpack(">i", data[offset:offset + 4])[0] / 10000 for offset in (3, 7, 11, 15)]
            line = 1 if data[37:40] == b"\0\0\0" else 2
            return {"line": line, "voltage": values[0], "current": values[1], "power": values[2], "energy": values[3], "error_code": data[19]}
        if len(data) >= 25 and data[:4] == HughesSource.MODERN_HEADER and data[6] == 1:
            return {"line": 1, "voltage": int.from_bytes(data[9:13], "big") / 10000, "current": int.from_bytes(data[13:17], "big") / 10000, "power": int.from_bytes(data[17:21], "big") / 10000, "energy": int.from_bytes(data[21:25], "big") / 10000}
        return None

    def run(self) -> None:
        """Run the Hughes collector and log failures that stop its thread."""
        try:
            from bleak import BleakClient
            address = self.section.get("address")
            if not address:
                raise ValueError("[hughes] address is required")
            asyncio.run(self._run_ble(BleakClient, address))
        except Exception:
            LOGGER.exception("Hughes source stopped")

    async def _run_ble(self, client_class: Callable[..., Any], address: str) -> None:
        """Maintain a Hughes session, reconnecting with bounded exponential backoff."""
        section = self.section
        persistent = section.get("persistent_connection", "").strip()
        if not persistent:
            persistent = self.config["service"].get("daemon_mode", "true")
        persistent = persistent.lower() == "true"
        reconnect_delay = self.config["service"].getfloat("reconnect_delay", fallback=10)
        max_reconnect_delay = self.config["service"].getfloat("max_reconnect_delay", fallback=300)
        max_retry = self.config["service"].getint("max_retry", fallback=0)
        retry_count = 0
        while not self.stop_event.is_set():
            try:
                await self._run_ble_session(client_class, address, section)
                if not persistent or self.stop_event.is_set():
                    return
                retry_count = 0
                LOGGER.warning("Hughes session ended; reconnecting to %s", address)
            except Exception:
                if self.stop_event.is_set() or not persistent:
                    raise
                retry_count += 1
                if max_retry and retry_count > max_retry:
                    raise RuntimeError(f"Hughes reconnect limit reached ({max_retry})")
                LOGGER.exception("Hughes connection dropped; reconnecting to %s", address)
            delay = min(reconnect_delay * (2 ** max(retry_count - 1, 0)), max_reconnect_delay)
            await asyncio.sleep(delay)

    async def _run_ble_session(self, client_class: Callable[..., Any], address: str, section: Any) -> None:
        """Run one Hughes notification session until the shared stop event is set."""
        async with client_class(address) as client:
            def callback(_sender: Any, payload: bytearray) -> None:
                """Decode a notification and publish it under the Hughes topic."""
                decoded = self.decode(bytes(payload))
                if decoded:
                    self.publisher.publish(section.get("topic", "hughes"), decoded)
            services = client.services
            modern = any(str(service.uuid).lower() == "000000ff-0000-1000-8000-00805f9b34fb" for service in services)
            tx = "0000ff01-0000-1000-8000-00805f9b34fb" if modern else "0000ffe2-0000-1000-8000-00805f9b34fb"
            await client.start_notify(tx, callback)
            if modern:
                await client.write_gatt_char(tx, b"!%!%,protocol,open,")
            while not self.stop_event.is_set():
                await asyncio.sleep(1)
            await client.stop_notify(tx)