from __future__ import annotations

import asyncio
import logging
from typing import Any

from .source import Source


LOGGER = logging.getLogger(__name__)


class RenogySource(Source, source_name="renogy"):
    source_name = "renogy"
    config_section = "renogy"
    CLIENTS = {
        "RNG_CTRL": ("rv_control.renogybt.RoverClient", "RoverClient"),
        "RNG_CTRL_HIST": ("rv_control.renogybt.RoverHistoryClient", "RoverHistoryClient"),
        "RNG_BATT": ("rv_control.renogybt.BatteryClient", "BatteryClient"),
        "RNG_INVT": ("rv_control.renogybt.InverterClient", "InverterClient"),
        "RNG_INVT_HF": ("rv_control.renogybt.HFInverterClient", "HFInverterClient"),
        "RNG_DCC": ("rv_control.renogybt.DCChargerClient", "DCChargerClient"),
        "RNG_SHNT": ("rv_control.renogybt.ShuntClient", "ShuntClient"),
    }

    def __init__(self, config: Any, publisher: Any, stop_event: Any, section_name: str | None = None) -> None:
        """Initialize the Renogy source and defer BLE client creation until run time."""
        super().__init__(config, publisher, stop_event, section_name)
        self.client = None

    def handle_command(self, payload: dict[str, Any]) -> None:
        """Validate and forward an enabled Renogy write command to the BLE client."""
        section = self.section
        if not section.getboolean("write_enabled", fallback=False) or self.client is None or self.client.loop is None:
            LOGGER.warning("Renogy write ignored because it is disabled or unavailable")
            return
        try:
            if "bytes" in payload:
                request = list(bytes.fromhex(str(payload["bytes"])))
            else:
                request = self.client.create_generic_read_request(
                    int(payload.get("device_id", self.client.device_id)),
                    int(payload.get("function", 3)),
                    int(payload["register"]),
                    int(payload.get("words", 1)),
                )
            asyncio.run_coroutine_threadsafe(
                self.client.ble_manager.characteristic_write_value(request), self.client.loop
            )
        except (KeyError, TypeError, ValueError) as error:
            LOGGER.warning("Invalid Renogy command: %s", error)

    def comms_check(self) -> dict[str, Any]:
        """Check Renogy Bluetooth availability and return register metadata."""
        return asyncio.run(self._comms_check())

    def interrogate(self) -> dict[str, Any]:
        """Connect once, read all configured registers, and return the payload."""
        renogy_config = self._client_config(persistent_connection=False)
        client_info = self.CLIENTS.get(renogy_config["device"].get("device_type", ""))
        if not client_info:
            raise ValueError(f"Unknown Renogy device type: {renogy_config['device'].get('device_type')}")
        module_name, class_name = client_info
        module = __import__(module_name, fromlist=[class_name])
        result = {}

        def received(client: Any, data: dict[str, Any]) -> None:
            """Capture one response and stop the one-shot client."""
            result.update(data)
            client.stop()

        client = getattr(module, class_name)(renogy_config, received, lambda _client, error: LOGGER.error("Renogy: %s", error))
        client.start()
        if not result:
            raise RuntimeError("Renogy returned no data")
        return result

    async def _comms_check(self) -> dict[str, Any]:
        """Discover the configured Renogy device and report its communication status."""
        section = self.section
        try:
            from bleak import BleakScanner
            from configparser import ConfigParser
            renogy_config = self._client_config(persistent_connection=False)
            device = renogy_config["device"]
            address = device.get("mac_addr", "").strip()
            alias = device.get("alias", "").strip()
            registers = self._register_inventory(renogy_config)
            if not address or not alias:
                return {"ok": False, "message": "Renogy mac_addr and alias are required", "registers": registers}
            scanner = BleakScanner(adapter=section.get("adapter", "hci0"))
            devices = await scanner.discover(timeout=5)
            match = next((item for item in devices if item.address.lower() == address.lower()), None)
            if match is None:
                return {"ok": False, "message": f"Renogy device not found: {alias} ({address})", "registers": registers}
            if match.name != alias:
                return {"ok": False, "message": f"Renogy address found as {match.name!r}, expected {alias!r}", "registers": registers}
            return {"ok": True, "message": f"Renogy device matched: {alias} ({address})", "registers": registers}
        except (OSError, KeyError, ImportError, ValueError) as error:
            return {"ok": False, "message": f"Renogy Bluetooth check failed: {error}"}

    def _client_config(self, persistent_connection: bool | None = None) -> Any:
        """Build the BLE client configuration from source and service settings."""
        from configparser import ConfigParser

        section = self.section
        renogy_config = ConfigParser(inline_comment_prefixes=("#",))
        if persistent_connection is None:
            persistent_connection = section.get("persistent_connection", "").strip()
            if not persistent_connection:
                persistent_connection = self.config["service"].get("daemon_mode", "true")
        else:
            persistent_connection = str(persistent_connection).lower()
        renogy_config["device"] = {
            "adapter": section.get("adapter", "hci0"),
            "mac_addr": section.get("mac_addr", ""),
            "alias": section.get("alias", ""),
            "device_type": section.get("device-type", "RNG_CTRL"),
            "device_id": section.get("device_id", "255"),
            "max_retry": section.get("max_retry", "3"),
            "persistent_connection": persistent_connection,
        }
        renogy_config["data"] = {
            "enable_polling": str(
                section.getboolean("enable_polling", fallback=False)
                or persistent_connection == "true"
            ).lower(),
            "poll_interval": section.get("poll_interval", "60"),
            "temperature_unit": section.get("temperature_unit", "F"),
            "fields": section.get("fields", ""),
        }
        return renogy_config

    def _register_inventory(self, renogy_config: Any) -> list[dict[str, Any]]:
        """Describe the selected client’s readable registers and write commands."""
        device_type = renogy_config["device"].get("device_type", "")
        client_info = self.CLIENTS.get(device_type)
        if not client_info:
            return []
        module_name, class_name = client_info
        module = __import__(module_name, fromlist=[class_name])
        from_client = getattr(module, class_name)(renogy_config)
        inventory = []
        for section in from_client.sections:
            parser = section.get("parser")
            inventory.append({
                "register": section.get("register"),
                "words": section.get("words"),
                "attribute": parser.__name__.removeprefix("parse_") if parser else "unknown",
            })
        for name, command in vars(from_client).items():
            if name.startswith("set_") and isinstance(command, dict) and "register" in command:
                inventory.append({
                    "register": command["register"],
                    "words": None,
                    "attribute": name.removeprefix("set_"),
                    "access": "write",
                })
        return inventory

    def run(self) -> None:
        """Start Renogy collection and log failures that terminate the source thread."""
        try:
            section = self.section
            renogy_config = self._client_config()
            client_info = self.CLIENTS.get(renogy_config["device"].get("device_type", ""))
            if not client_info:
                raise ValueError(f"Unknown Renogy device type: {renogy_config['device'].get('device_type')}")
            module_name, class_name = client_info
            child = __import__(module_name, fromlist=[class_name])

            def received(client: Any, data: dict[str, Any]) -> None:
                """Publish a reading and stop the client when polling is disabled."""
                self.publisher.publish(section.get("topic", "renogy"), data)
                if not renogy_config["data"].getboolean("enable_polling", fallback=False):
                    client.stop()

            self.client = getattr(child, class_name)(
                renogy_config, received, lambda _client, error: LOGGER.error("Renogy: %s", error)
            )
            self.client.start()
        except Exception:
            LOGGER.exception("Renogy source stopped")