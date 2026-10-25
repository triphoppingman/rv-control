from __future__ import annotations

import asyncio
import threading
from configparser import ConfigParser
from pathlib import Path
from typing import Any

import pytest

from rv_control.sources.renogybt.BaseClient import BaseClient


class LifecycleClient(BaseClient):
    """Exercise the real client lifecycle without accessing Bluetooth hardware."""

    def __init__(self) -> None:
        """Configure a minimal client and collect lifecycle observations."""
        config = ConfigParser()
        config["device"] = {"device_id": "255", "alias": "test", "mac_addr": ""}
        config["data"] = {"enable_polling": "false"}
        super().__init__(config)
        self.on_error_callback = self.record_error
        self.errors: list[Any] = []
        self.loops: list[asyncio.AbstractEventLoop] = []
        self.pending: list[asyncio.Task[None]] = []
        self.failure: Exception | None = None

    def record_error(self, _client: BaseClient, error: Any) -> None:
        """Capture errors surfaced through the normal client callback."""
        self.errors.append(error)

    async def connect(self) -> None:
        """Record the loop, leave background work pending, and end the session."""
        self.loops.append(asyncio.get_running_loop())
        self.pending.append(asyncio.create_task(asyncio.sleep(3600)))
        self.read_timeout = self.loop.call_later(3600, self.on_read_timeout)
        if self.failure is not None:
            raise self.failure
        self.stop()
        self.stop()


@pytest.mark.parametrize("fail", [False, True])
def test_repeated_client_runs_release_loops_and_descriptors(fail: bool) -> None:
    """Verify worker restarts close loops and tasks without leaking descriptors."""
    client = LifecycleClient()
    if fail:
        client.failure = OSError("discovery failed")
    fd_directory = Path("/proc/self/fd")
    before = len(list(fd_directory.iterdir())) if fd_directory.exists() else None

    for _ in range(30):
        worker = threading.Thread(target=client.start)
        worker.start()
        worker.join(timeout=5)
        assert not worker.is_alive()

    assert len(client.loops) == 30
    assert all(loop.is_closed() for loop in client.loops)
    assert all(task.cancelled() for task in client.pending)
    assert client.loop is None
    assert client.read_timeout.cancelled()
    assert client.errors == ([client.failure] * 30 if fail else [])
    if before is not None:
        assert len(list(fd_directory.iterdir())) == before


def test_stop_without_start_does_not_create_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify stopping an inactive client does not allocate event-loop resources."""
    client = LifecycleClient()

    def fail_loop_creation() -> asyncio.AbstractEventLoop:
        """Reject any unexpected loop allocation during shutdown."""
        raise AssertionError("stop must not create a loop")

    monkeypatch.setattr(asyncio, "new_event_loop", fail_loop_creation)
    client.stop()
    assert client.loop is None


def test_start_stops_when_source_event_is_set() -> None:
    """Verify the client event loop observes its source's shared shutdown event."""
    client = LifecycleClient()
    client_started = threading.Event()
    stop_event = threading.Event()

    async def connect() -> None:
        """Mark connection readiness and leave session completion to shutdown."""
        client.loops.append(asyncio.get_running_loop())
        client_started.set()

    client.connect = connect
    worker = threading.Thread(target=client.start, args=(stop_event,))
    worker.start()
    assert client_started.wait(timeout=1)
    stop_event.set()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert client.loops[0].is_closed()
    assert client.loop is None


def test_loop_creation_failure_preserves_original_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify descriptor exhaustion is reported without another loop allocation."""
    client = LifecycleClient()
    failure = OSError(24, "Too many open files")
    calls = []

    def fail_loop_creation() -> asyncio.AbstractEventLoop:
        """Simulate exhausting descriptors before the client can start."""
        calls.append("create")
        raise failure

    monkeypatch.setattr(asyncio.events, "new_event_loop", fail_loop_creation)
    monkeypatch.setattr(asyncio, "new_event_loop", fail_loop_creation)
    client.start()
    assert calls == ["create"]
    assert client.errors == [failure]
    assert client.loop is None


@pytest.mark.parametrize("fail_disconnect", [False, True])
def test_session_disconnects_before_closing_loop(fail_disconnect: bool) -> None:
    """Verify BLE teardown runs on the live loop, including failing disconnects."""
    client = LifecycleClient()
    calls = []
    failure = OSError("disconnect failed")

    class FakeManager:
        async def disconnect(self) -> None:
            """Record cleanup while the client loop is still available."""
            assert client.loop is asyncio.get_running_loop()
            assert all(task.cancelled() for task in client.pending)
            calls.append("disconnect")
            if fail_disconnect:
                raise failure

    client.ble_manager = FakeManager()
    client.start()

    assert calls == ["disconnect"]
    assert client.loops[0].is_closed()
    assert client.loop is None
    assert client.ble_manager is None
    assert client.errors == ([failure] if fail_disconnect else [])
