"""Tests for RV-C status Request frames sent by the RV-C source."""

from __future__ import annotations

import configparser
import threading
from typing import Any

import pytest

from rv_control.rvc_util import build_request_message, request_payload
from rv_control.sources.rvc import RvcSource


class FakeBus:
    """CAN bus double recording sent messages."""

    def __init__(self) -> None:
        """Create an empty send record."""
        self.sent: list[Any] = []

    def send(self, message: Any) -> None:
        """Record a sent message."""
        self.sent.append(message)


def test_request_message_is_global_request_for_dgn() -> None:
    """A Request for DC_DIMMER_STATUS_3 is DGN EAFF with the DGN little-endian in the data."""
    message = build_request_message(0x1FEDA, 0x00)
    assert message.arbitration_id == 0x18EAFF00
    assert message.is_extended_id
    assert bytes(message.data) == bytes.fromhex("DAFE01")


def test_request_payload_rejects_out_of_range() -> None:
    """DGNs wider than 17 bits are rejected."""
    with pytest.raises(ValueError):
        request_payload(0x20000)


def test_source_sends_queued_requests_once() -> None:
    """Queued requests are deduplicated, sent once, and not gated by write_enabled."""
    config = configparser.ConfigParser()
    config.read_dict({"rv_c": {"type": "rvc", "write_enabled": "false"}})
    source = RvcSource(config, None, threading.Event(), "rv_c")
    source.request_status([0x1FEDA, 0x1FFB7, 0x1FEDA])
    bus = FakeBus()
    source._send_pending_requests(bus)
    source._send_pending_requests(bus)
    assert [bytes(message.data) for message in bus.sent] == [bytes.fromhex("DAFE01"), bytes.fromhex("B7FF01")]


def test_source_rejects_invalid_request_dgn() -> None:
    """Invalid DGNs are rejected when queued."""
    config = configparser.ConfigParser()
    config.read_dict({"rv_c": {"type": "rvc"}})
    source = RvcSource(config, None, threading.Event(), "rv_c")
    with pytest.raises(ValueError):
        source.request_status([0x20000])
