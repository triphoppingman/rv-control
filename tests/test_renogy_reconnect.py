from __future__ import annotations

import asyncio
import importlib
import logging
from configparser import ConfigParser
from types import SimpleNamespace
from typing import Any

import pytest

from rv_control.sources.renogybt.BaseClient import BaseClient
from rv_control.sources.renogybt.BLEManager import BLEManager


@pytest.mark.parametrize("stage", ["connect", "subscribe", "disconnected"])
def test_setup_disconnects_report_only_connection_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, stage: str
) -> None:
    """Leave setup retries to the connection result, not transient callbacks."""
    failures: list[Exception] = []
    disconnects: list[str] = []
    failure = TimeoutError("BLE setup timed out")

    async def on_data(_data: bytes | bytearray) -> None:
        """Accept unused notifications without accessing hardware."""

    def on_disconnect() -> None:
        """Record an established-connection disconnect."""
        disconnects.append("disconnect")

    manager = BLEManager(
        "", "test", on_data, failures.append, on_disconnect, "service", "notify", "write"
    )
    manager.device = SimpleNamespace(name="test", address="00:00:00:00:00:00")

    class FakeClient:
        def __init__(self, _device: Any, disconnected_callback: Any) -> None:
            """Retain the real manager callback for simulated backend events."""
            self.callback = disconnected_callback
            self.address = "00:00:00:00:00:00"
            self.is_connected = False
            self.services = [
                SimpleNamespace(
                    uuid="service",
                    characteristics=[SimpleNamespace(uuid="notify")],
                )
            ]

        async def connect(self) -> None:
            """Simulate repeated transient disconnects during connection setup."""
            for _ in range(5):
                self.callback(self)
                await asyncio.sleep(0)
            if stage == "connect":
                raise failure
            self.is_connected = True

        async def start_notify(self, _characteristic: Any, _callback: Any) -> None:
            """Fail subscription or drop the link before setup completes."""
            self.is_connected = False
            self.callback(self)
            await asyncio.sleep(0)
            if stage == "subscribe":
                raise failure

    module = importlib.import_module("rv_control.sources.renogybt.BLEManager")
    monkeypatch.setattr(module, "BleakClient", FakeClient)
    with caplog.at_level(logging.WARNING):
        asyncio.run(manager.connect())
        manager._on_disconnected(manager.client)

    assert disconnects == []
    assert len(failures) == 1
    if stage != "disconnected":
        assert failures[0] is failure
    else:
        assert isinstance(failures[0], ConnectionError)
    assert "Unexpected disconnect" not in caplog.text


def test_disconnect_callbacks_ignore_stale_and_duplicate_clients(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Report a lost active connection once and ignore replaced clients."""
    disconnects: list[str] = []

    async def on_data(_data: bytes | bytearray) -> None:
        """Accept unused notifications without accessing hardware."""

    def on_failure(_error: Any) -> None:
        """Reject unexpected connection failures in this callback-only test."""
        pytest.fail("No connection attempt is expected")

    def on_disconnect() -> None:
        """Record the manager's disconnect notification."""
        disconnects.append("disconnect")

    manager = BLEManager(
        "", "test", on_data, on_failure, on_disconnect, "service", "notify", "write"
    )
    stale = SimpleNamespace(address="00:00:00:00:00:00")
    active = SimpleNamespace(address="00:00:00:00:00:01")
    manager.client = active
    with caplog.at_level(logging.WARNING):
        manager._on_disconnected(stale)
        manager._on_disconnected(active)
        manager._on_disconnected(active)

    assert disconnects == ["disconnect"]
    assert caplog.text.count("Unexpected disconnect from device") == 1


@pytest.mark.parametrize("intentional", [False, True])
def test_successful_setup_reports_only_unexpected_connection_loss(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    intentional: bool,
) -> None:
    """Keep normal notification setup and intentional teardown free of warnings."""
    events: list[str] = []

    async def on_data(_data: bytes | bytearray) -> None:
        """Record a forwarded telemetry notification."""
        events.append("data")

    def on_failure(_error: Any) -> None:
        """Reject failures during a successful connection."""
        pytest.fail("Connection should succeed")

    def on_disconnect() -> None:
        """Record a lost established connection."""
        events.append("disconnect")

    manager = BLEManager(
        "", "test", on_data, on_failure, on_disconnect, "service", "notify", "write"
    )
    manager.device = SimpleNamespace(name="test", address="00:00:00:00:00:00")

    class FakeClient:
        def __init__(self, _device: Any, disconnected_callback: Any) -> None:
            """Provide fake notify and write characteristics."""
            self.callback = disconnected_callback
            self.address = "00:00:00:00:00:00"
            self.is_connected = False
            self.services = [
                SimpleNamespace(
                    uuid="service",
                    characteristics=[
                        SimpleNamespace(uuid="notify"),
                        SimpleNamespace(uuid="write", handle=42),
                    ],
                )
            ]

        async def connect(self) -> None:
            """Establish the fake connection."""
            self.is_connected = True

        async def start_notify(self, characteristic: Any, callback: Any) -> None:
            """Deliver a notification through the manager's actual callback."""
            await callback(characteristic, bytearray([1]))

        async def disconnect(self) -> None:
            """Deliver the normal backend disconnect callback."""
            self.is_connected = False
            self.callback(self)

    module = importlib.import_module("rv_control.sources.renogybt.BLEManager")
    monkeypatch.setattr(module, "BleakClient", FakeClient)

    async def exercise() -> None:
        """Complete setup before disconnecting the active client."""
        await manager.connect()
        assert manager.write_char_handle == 42
        assert manager.client.is_connected
        if intentional:
            await manager.disconnect()
        else:
            await manager.client.disconnect()
            manager._on_disconnected(manager.client)

    with caplog.at_level(logging.WARNING):
        asyncio.run(exercise())
    assert events == (["data"] if intentional else ["data", "disconnect"])
    assert caplog.text.count("Unexpected disconnect from device") == (0 if intentional else 1)


@pytest.mark.parametrize("failures_before_success", [2, 10])
@pytest.mark.parametrize(
    "failure_stage", ["connect", "discovery", "subscription", "discovery_error"]
)
def test_connection_failures_continue_until_success_or_retry_limit(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failures_before_success: int,
    failure_stage: str,
) -> None:
    """Retry failures occurring inside a retry without stalling or overlapping."""
    config = ConfigParser()
    config["device"] = {
        "device_id": "255",
        "alias": "test",
        "mac_addr": "",
        "max_retry": "3",
        "reconnect_delay": "10",
        "max_reconnect_delay": "15",
        "reconnect_jitter": "0",
    }
    config["data"] = {"enable_polling": "false"}
    errors: list[Any] = []
    attempts: list[str] = []
    cleanups: list[str] = []
    discoveries: list[str] = []
    delays: list[float] = []
    connecting = False
    failure = RuntimeError("Bluetooth discovery failed")
    real_sleep = asyncio.sleep

    async def skip_backoff(delay: float) -> None:
        """Record production backoff delays while keeping tests hardware-free and fast."""
        if delay:
            delays.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", skip_backoff)

    class RetryClient(BaseClient):
        async def read_section(self) -> None:
            """End the test session once the connection has recovered."""
            self.stop()

    class FakeManager:
        def __init__(self, **kwargs: Any) -> None:
            """Retain the real connection-failure callback."""
            self.on_failure = kwargs["on_connect_fail"]
            self.device = SimpleNamespace(name="test")
            self.client = SimpleNamespace(is_connected=False)
            self.discovered_devices: list[Any] = []

        async def discover(self) -> None:
            """Yield as real device discovery would."""
            discoveries.append("discover")
            if failure_stage == "discovery_error" and len(discoveries) == 2:
                raise failure
            if failure_stage == "discovery" and len(discoveries) <= failures_before_success:
                self.device = None
            await asyncio.sleep(0)

        async def connect(self) -> None:
            """Fail selected attempts, allowing the retry task to run mid-call."""
            nonlocal connecting
            assert not connecting
            connecting = True
            attempts.append("connect")
            if failure_stage != "discovery" and len(attempts) <= failures_before_success:
                self.client.is_connected = failure_stage == "subscription"
                self.on_failure(TimeoutError("BLE setup timed out"))
                await asyncio.sleep(0)
                await asyncio.sleep(0)
            else:
                self.client.is_connected = True
            connecting = False

        async def disconnect(self) -> None:
            """Verify teardown never interrupts a connection still in progress."""
            assert not connecting
            cleanups.append("disconnect")
            self.client.is_connected = False

    def on_error(_client: BaseClient, error: Any) -> None:
        """Collect terminal retry failures."""
        errors.append(error)

    client = RetryClient(config)
    client.on_error_callback = on_error
    module = importlib.import_module("rv_control.sources.renogybt.BaseClient")
    monkeypatch.setattr(module, "BLEManager", FakeManager)

    async def exercise() -> None:
        """Bound a real retry sequence and clean up its tasks on failure."""
        client.loop = asyncio.get_running_loop()
        client.future = client.loop.create_future()
        try:
            await client.connect()
            await asyncio.wait_for(client.future, timeout=1)
        finally:
            client._stopping = True
            pending = asyncio.all_tasks() - {asyncio.current_task()}
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            await client.disconnect()

    with caplog.at_level(logging.INFO):
        asyncio.run(exercise())

    if failure_stage == "discovery_error":
        assert len(discoveries) == 2
        assert len(attempts) == 1
        assert errors == [failure]
        assert len(cleanups) == 2
        assert delays == [10]
        assert not client._reconnecting
        return
    if failures_before_success == 2:
        assert len(discoveries) == 3
        assert len(attempts) == (1 if failure_stage == "discovery" else 3)
        assert errors == []
        assert client._retry_count == 0
        assert delays == [10, 15]
    else:
        assert len(discoveries) == 4
        assert len(attempts) == (0 if failure_stage == "discovery" else 4)
        assert len(errors) == 1
        assert "Max retries reached" in errors[0]
        assert delays == [10, 15, 15]
    assert len(cleanups) == len(discoveries)
    if failure_stage != "discovery":
        assert "TimeoutError" in caplog.text
