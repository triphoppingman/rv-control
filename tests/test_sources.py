from __future__ import annotations

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