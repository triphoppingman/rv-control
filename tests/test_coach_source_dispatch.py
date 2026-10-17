"""Prove semantic commands use failure-reporting source methods, not legacy logs."""

from __future__ import annotations

import threading
from configparser import ConfigParser
from types import SimpleNamespace
from typing import Any

import pytest
import requests

from rv_control.sources.rvc import RvcSource
from rv_control.sources.wled import WledSource


def test_rvc_reliable_dispatch_has_timeout_and_raises(monkeypatch: Any) -> None:
    """A successful send has a bound; unavailable, disabled and failed sends raise."""
    config = ConfigParser()
    config["rvc"] = {"write_enabled": "true"}
    source = RvcSource(config, None, threading.Event(), "rvc")
    monkeypatch.setattr(source, "is_alive", lambda: True)
    sent = []

    def send(message: Any, timeout: float) -> None:
        """Capture a real built CAN frame without touching SocketCAN."""
        sent.append((message, timeout))

    source.bus = SimpleNamespace(send=send)
    command = {"can_id": "0x19FEDB00", "data": "10FFC801FF00FFFF"}
    source.execute_command(command)
    assert sent[0][1] == 1.0
    assert sent[0][0].arbitration_id == 0x19FEDB00
    assert bytes(sent[0][0].data) == bytes.fromhex(command["data"])
    config["rvc"]["write_enabled"] = "false"
    with pytest.raises(PermissionError):
        source.execute_command(command)
    config["rvc"]["write_enabled"] = "true"
    source.bus = None
    with pytest.raises(RuntimeError):
        source.execute_command(command)

    def fail(*_args: Any, **_kwargs: Any) -> None:
        """Simulate a send failure with uncertain physical outcome."""
        raise OSError("send failed")

    source.bus = SimpleNamespace(send=fail)
    with pytest.raises(OSError):
        source.execute_command(command)


def test_rvc_failure_clears_and_closes_bus(monkeypatch: Any) -> None:
    """A failed collector cannot leave a stale command-ready bus behind."""
    import can
    import rv_control.sources.rvc as rvc_module

    config = ConfigParser()
    config["rvc"] = {"specfile": "unused.yml"}
    closed: list[bool] = []

    def fail_receive(**_kwargs: Any) -> None:
        """End the collector without accessing SocketCAN."""
        raise OSError("receive failed")

    bus = SimpleNamespace(recv=fail_receive, shutdown=lambda: closed.append(True))
    monkeypatch.setattr(can, "Bus", lambda **_kwargs: bus)
    monkeypatch.setattr(rvc_module, "load_spec", lambda _path: {})
    source = RvcSource(config, None, threading.Event(), "rvc")
    source.run()
    assert source.bus is None
    assert not source.command_ready()
    assert closed == [True]


def test_wled_reliable_dispatch_surfaces_http_errors(monkeypatch: Any) -> None:
    """The semantic WLED path cannot swallow a disabled-write or HTTP failure."""
    config = ConfigParser()
    config["wled"] = {"write_enabled": "false", "base_url": "http://wled.invalid"}
    source = WledSource(config, None, threading.Event(), "wled")
    try:
        with pytest.raises(PermissionError):
            source.execute_command({"on": True})
        config["wled"]["write_enabled"] = "true"

        def fail(*_args: Any, **_kwargs: Any) -> Any:
            """Avoid HTTP traffic and simulate a bounded request timeout."""
            raise requests.Timeout("timed out")

        monkeypatch.setattr(source.session, "request", fail)
        with pytest.raises(requests.Timeout):
            source.execute_command({"on": True})
    finally:
        source.session.close()


def test_wled_command_lock_wait_is_bounded() -> None:
    """A slow collector cannot leave semantic dispatch waiting indefinitely for its session."""
    config = ConfigParser()
    config["wled"] = {"write_enabled": "true", "base_url": "http://wled.invalid", "timeout": "0.001"}
    source = WledSource(config, None, threading.Event(), "wled")
    source._request_lock.acquire()
    try:
        with pytest.raises(TimeoutError, match="busy"):
            source.execute_command({"on": True})
    finally:
        source._request_lock.release()
        source.session.close()
