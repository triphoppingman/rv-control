from __future__ import annotations

import asyncio
import importlib
import threading
from configparser import ConfigParser
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from rv_control.bluetooth import bluez_device_path, release_stale_connection
from rv_control.config import load_config
from rv_control.sources.hughes import HughesSource
from rv_control.sources.renogybt.BaseClient import BaseClient
from rv_control.sources.renogybt.BLEManager import BLEManager


def test_bluez_device_path_normalizes_adapter_and_address() -> None:
    """Map adapter names and colon addresses onto BlueZ object paths."""
    assert bluez_device_path("", "aa:bb:cc:dd:ee:ff") == "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF"
    assert bluez_device_path("hci1", "00:11:22:33:44:55") == "/org/bluez/hci1/dev_00_11_22_33_44_55"


def test_release_without_address_skips_dbus() -> None:
    """Return immediately when no device address is configured."""
    assert asyncio.run(release_stale_connection("hci0", "")) is False


def test_failed_setup_disconnects_client_and_releases_link(
    monkeypatch: pytest.MonkeyPatch, stub_bluez_link_release: list[tuple[str, str]]
) -> None:
    """Never leave a half-established BLE link held by BlueZ after setup fails."""
    failures: list[Any] = []
    disconnects: list[str] = []

    async def on_data(_data: bytes | bytearray) -> None:
        """Accept unused notifications."""

    manager = BLEManager(
        "00:00:00:00:00:01", "test", on_data, failures.append, lambda: None,
        "service", "notify", "write",
    )
    manager.device = SimpleNamespace(name="test", address="00:00:00:00:00:01")

    class FakeClient:
        def __init__(self, _device: Any, disconnected_callback: Any) -> None:
            """Expose a notify characteristic whose subscription fails."""
            self.is_connected = False
            self.services = [SimpleNamespace(uuid="service", characteristics=[SimpleNamespace(uuid="notify")])]

        async def connect(self) -> None:
            """Establish the fake link."""
            self.is_connected = True

        async def start_notify(self, _characteristic: Any, _callback: Any) -> None:
            """Fail during GATT setup while the link is still up."""
            raise TimeoutError("subscribe timed out")

        async def disconnect(self) -> None:
            """Record the cleanup disconnect."""
            disconnects.append("disconnect")
            self.is_connected = False

    module = importlib.import_module("rv_control.sources.renogybt.BLEManager")
    monkeypatch.setattr(module, "BleakClient", FakeClient)
    asyncio.run(manager.connect())

    assert disconnects == ["disconnect"]
    assert len(failures) == 1
    assert ("hci0", "00:00:00:00:00:01") in stub_bluez_link_release


def test_disconnect_releases_link_even_when_client_reports_closed(
    stub_bluez_link_release: list[tuple[str, str]],
) -> None:
    """Call bleak disconnect and BlueZ release even if bleak thinks the link is down."""
    calls: list[str] = []

    async def on_data(_data: bytes | bytearray) -> None:
        """Accept unused notifications."""

    class ClosedClient:
        is_connected = False

        async def disconnect(self) -> None:
            """Record the unconditional disconnect."""
            calls.append("disconnect")

    manager = BLEManager(
        "00:00:00:00:00:02", "test", on_data, lambda _e: None, lambda: None,
        "service", "notify", "write", adapter="hci1",
    )
    manager.client = ClosedClient()
    asyncio.run(manager.disconnect())
    assert calls == ["disconnect"]
    assert stub_bluez_link_release == [("hci1", "00:00:00:00:00:02")]


def _polling_config(persistent: bool) -> ConfigParser:
    """Build a minimal polling client configuration."""
    config = ConfigParser()
    config["device"] = {
        "device_id": "255", "alias": "test", "mac_addr": "",
        "persistent_connection": str(persistent).lower(),
    }
    config["data"] = {"enable_polling": "true", "poll_interval": "60", "request_interval": "0"}
    return config


@pytest.mark.parametrize("persistent", [False, True])
def test_polling_disconnects_between_cycles_only_when_not_persistent(
    monkeypatch: pytest.MonkeyPatch, persistent: bool
) -> None:
    """Free the device between polls in non-persistent mode and keep it otherwise."""
    events: list[str] = []
    real_sleep = asyncio.sleep

    async def fast_sleep(_delay: float) -> None:
        """Skip production delays."""
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)

    class FakeManager:
        def __init__(self, **_kwargs: Any) -> None:
            """Report an established connection."""
            self.device = SimpleNamespace(name="test")
            self.client = SimpleNamespace(is_connected=True)
            self.discovered_devices: list[Any] = []

        async def discover(self) -> None:
            """Record discovery."""
            events.append("discover")

        async def connect(self) -> None:
            """Record connection setup."""
            events.append("connect")

        async def disconnect(self) -> None:
            """Record link release."""
            events.append("disconnect")

        async def characteristic_write_value(self, _request: Any) -> None:
            """Record a register read request."""
            events.append("read")

    module = importlib.import_module("rv_control.sources.renogybt.BaseClient")
    monkeypatch.setattr(module, "BLEManager", FakeManager)

    class PollClient(BaseClient):
        def __init__(self) -> None:
            """Use one register section and collect completed cycles."""
            super().__init__(_polling_config(persistent))
            self.sections = [{"register": 1, "words": 1, "parser": None}]
            self.cycles = 0
            self.on_data_callback = self.record

        def record(self, _client: BaseClient, _data: dict[str, Any]) -> None:
            """Stop after two complete polling cycles."""
            self.cycles += 1
            if self.cycles == 2:
                self.stop()

    client = PollClient()

    async def respond() -> None:
        """Answer each read like the device would, from a notification task."""
        while not client._stopping:
            if events and events[-1] == "read":
                events.append("response")
                await client.on_data_received(bytes([255, 3, 2, 0, 0, 0, 0]))
            await real_sleep(0)

    async def exercise() -> None:
        """Run a session with a simulated responder."""
        client.loop = asyncio.get_running_loop()
        client.future = client.loop.create_future()
        responder = asyncio.create_task(respond())
        try:
            await client.connect()
            await asyncio.wait_for(client.future, timeout=2)
        finally:
            client._stopping = True
            responder.cancel()
            if client.poll_timer:
                client.poll_timer.cancel()
            await asyncio.gather(responder, return_exceptions=True)

    asyncio.run(exercise())
    cycle = ["read", "response"] if persistent else ["discover", "connect", "read", "response", "disconnect"]
    expected = ["discover", "connect", "read", "response"] + cycle if persistent else cycle * 2
    assert client.cycles == 2
    assert events == expected

def test_stale_read_chain_is_discarded_after_reconnect(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent an old session's read chain from running alongside the new one."""
    reads: list[int] = []
    real_sleep = asyncio.sleep

    async def fast_sleep(_delay: float) -> None:
        """Skip production delays."""
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)
    client = BaseClient(_polling_config(True))
    client.sections = [
        {"register": 1, "words": 1, "parser": None},
        {"register": 2, "words": 1, "parser": None},
    ]

    async def record_read() -> None:
        """Record which section would be requested."""
        reads.append(client.section_index)

    monkeypatch.setattr(client, "read_section", record_read)

    async def exercise() -> None:
        """Deliver a response, then simulate a reconnect during the request interval."""
        client.loop = asyncio.get_running_loop()
        task = asyncio.create_task(client.on_data_received(bytes([255, 3, 2, 0, 0, 0, 0])))
        await real_sleep(0)
        client._generation += 1
        await task

    asyncio.run(exercise())
    assert reads == []


def test_hughes_releases_stale_link_around_each_session(
    config_file: Path, stub_bluez_link_release: list[tuple[str, str]]
) -> None:
    """Drop orphaned BlueZ links before connecting and after each Hughes session."""
    config = load_config(str(config_file))
    config["hughes"]["persistent_connection"] = "false"
    config["hughes"]["adapter"] = "hci1"
    source = HughesSource(config, None, threading.Event())

    class FakeClient:
        def __init__(self, _address: str, **_kwargs: Any) -> None:
            """Accept construction arguments."""

        async def __aenter__(self) -> Any:
            """Fail the connection attempt."""
            raise OSError("connect failed")

        async def __aexit__(self, *_args: Any) -> bool:
            """Leave exceptions visible."""
            return False

    with pytest.raises(OSError):
        asyncio.run(source._run_ble(FakeClient, "AA:BB:CC:DD:EE:FF"))
    assert stub_bluez_link_release == [("hci1", "AA:BB:CC:DD:EE:FF")] * 2


def test_hughes_stop_notify_failure_does_not_mask_session_end(config_file: Path) -> None:
    """Finish teardown cleanly when the link is already gone at stop_notify."""
    config = load_config(str(config_file))
    config["hughes"]["persistent_connection"] = "false"
    source = HughesSource(config, None, threading.Event())

    class FakeClient:
        def __init__(self, _address: str, **_kwargs: Any) -> None:
            """Accept construction arguments."""

        async def __aenter__(self) -> Any:
            """Connect successfully."""
            return self

        async def __aexit__(self, *_args: Any) -> bool:
            """Leave exceptions visible."""
            return False

        services: list[Any] = []

        async def start_notify(self, _tx: str, _callback: Any) -> None:
            """End the session once subscribed."""
            source.stop_event.set()

        async def stop_notify(self, _tx: str) -> None:
            """Fail like a dropped BLE link."""
            raise OSError("not connected")

    asyncio.run(source._run_ble(FakeClient, "AA:BB:CC:DD:EE:FF"))
