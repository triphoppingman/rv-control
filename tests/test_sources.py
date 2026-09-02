from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any

from rv_control.config import load_config
from rv_control.hughes import HughesSource
from rv_control.renogy import RenogySource
from rv_control.source import Source


def test_source_registry_contains_concrete_sources() -> None:
    """Verify concrete telemetry sources are present in the source registry."""
    assert set(Source.list_sources()) >= {"hughes", "renogy", "rvc"}
    assert Source.source_class("renogy") is RenogySource


def test_renogy_persistent_mode_enables_polling(config_file: Path) -> None:
    """Verify persistent Renogy mode enables client polling."""
    config = load_config(str(config_file))
    source = RenogySource(config, None, threading.Event())
    client_config = source._client_config()
    assert client_config["device"]["persistent_connection"] == "true"
    assert client_config["data"]["enable_polling"] == "true"


def test_hughes_protocol_attribute_inventory() -> None:
    """Verify Hughes protocol detection exposes the expected attributes."""
    legacy = HughesSource.attribute_inventory("PMD 082CF8E30C")
    modern = HughesSource.attribute_inventory("WD_V5_device")
    assert {item["attribute"] for item in legacy} >= {"voltage_line_1", "error_code"}
    assert {item["attribute"] for item in modern} >= {"voltage_line_2", "combined_power"}


def test_hughes_legacy_packet_decode() -> None:
    """Verify a legacy Hughes packet decodes into line telemetry."""
    packet = bytearray(40)
    packet[:3] = b"\x01\x03\x20"
    packet[3:7] = (1200000).to_bytes(4, "big", signed=True)
    packet[7:11] = (250000).to_bytes(4, "big", signed=True)
    packet[11:15] = (30000000).to_bytes(4, "big", signed=True)
    packet[15:19] = (12345).to_bytes(4, "big", signed=True)
    packet[37:40] = b"\x01\x01\x01"
    decoded = HughesSource.decode(bytes(packet))
    assert decoded["line"] == 2
    assert decoded["voltage"] == 120
    assert decoded["current"] == 25
    assert decoded["energy"] == 1.2345


def test_hughes_daemon_buffers_fragmented_legacy_packet(config_file: Path) -> None:
    """Verify fragmented legacy notifications are published as one measurement."""
    config = load_config(str(config_file))
    published = []
    source = HughesSource(config, type("Publisher", (), {"publish": lambda _self, topic, data: published.append((topic, data))})(), threading.Event(), "hughes")
    packet = bytearray(40)
    packet[:3] = b"\x01\x03\x20"
    packet[3:7] = (1200000).to_bytes(4, "big", signed=True)
    packet[7:11] = (250000).to_bytes(4, "big", signed=True)
    packet[11:15] = (30000000).to_bytes(4, "big", signed=True)
    packet[15:19] = (12345).to_bytes(4, "big", signed=True)
    packet[37:40] = b"\x01\x01\x01"

    class FakeClient:
        """Provide a notification session that fragments one legacy packet."""

        def __init__(self, _address: str) -> None:
            """Accept the BLE address used to create the client."""

        async def __aenter__(self) -> "FakeClient":
            """Enter the fake BLE session."""
            return self

        async def __aexit__(self, *_args: Any) -> bool:
            """Exit the fake BLE session without suppressing errors."""
            return False

        @property
        def services(self) -> list[Any]:
            """Select the legacy Hughes protocol."""
            return []

        async def start_notify(self, _tx: str, callback: Any) -> None:
            """Deliver one legacy packet in two notification fragments."""
            callback(None, packet[:17])
            callback(None, packet[17:])
            source.stop_event.set()

        async def stop_notify(self, _tx: str) -> None:
            """Accept notification cleanup."""

    asyncio.run(source._run_ble_session(FakeClient, "AA:BB:CC:DD:EE:FF", source.section))
    assert published[0][0] == "hughes"
    assert published[0][1]["voltage"] == 120