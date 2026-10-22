"""Hardware-free coverage for BLE discovery, explicit GATT access and evidence capture."""

from __future__ import annotations

import asyncio
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from bleak.exc import BleakError
from click.testing import CliRunner


@pytest.fixture
def tool(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Load the standalone utility with an entirely synthetic BLE stack."""
    path = Path(__file__).parents[1] / "tools/bt_discovery.py"
    spec = importlib.util.spec_from_file_location("bt_discovery", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.calls = []
    module.device = SimpleNamespace(address="TEST-DEVICE", name=None)
    module.characteristic = SimpleNamespace(
        uuid="00002a19-0000-1000-8000-00805f9b34fb", handle=7,
        description="Test value", properties=["read", "notify"],
        descriptors=[SimpleNamespace(uuid="descriptor", handle=8, description="Subscription")],
    )
    service = SimpleNamespace(
        uuid="service", handle=1, description="Test service",
        characteristics=[module.characteristic],
    )

    class Services:
        """Provide iterable services and deterministic characteristic selection."""

        def __iter__(self) -> Any:
            """Iterate the single fake GATT service."""
            return iter([service])

        def get_characteristic(self, key: str | int) -> Any:
            """Resolve supported selectors and simulate an ambiguous UUID."""
            if key == "ambiguous":
                raise BleakError("Multiple Characteristics with this UUID")
            return module.characteristic if key in (7, module.characteristic.uuid) else None

    class Client:
        """Capture lifecycle operations and simulate notification delivery."""

        def __init__(self, device: Any, **kwargs: Any) -> None:
            """Record selected adapter, pairing policy and disconnect callback."""
            module.calls.append(("client", device, kwargs))
            self.callback = kwargs["disconnected_callback"]
            self.services = Services()
            self.is_connected = False

        async def connect(self) -> None:
            """Mark the synthetic connection active."""
            module.calls.append("connect")
            self.is_connected = True

        async def disconnect(self) -> None:
            """Record cleanup, including after failed GATT operations."""
            module.calls.append("disconnect")
            self.is_connected = False
            if self.callback:
                self.callback(self)

        async def read_gatt_char(self, characteristic: Any) -> bytearray:
            """Return a lossless binary value."""
            module.calls.append(("read", characteristic.handle))
            return bytearray(b"\x00\xff")

        async def start_notify(self, characteristic: Any, callback: Any) -> None:
            """Deliver one packet without waiting for real BLE hardware."""
            module.calls.append(("start", characteristic.handle))
            callback(characteristic, bytearray(b"\x01\xfe"))

        async def stop_notify(self, characteristic: Any) -> None:
            """Record explicit unsubscription."""
            module.calls.append(("stop", characteristic.handle))

    async def find(address: str, **kwargs: Any) -> Any:
        """Resolve the requested fake device on the explicitly selected adapter."""
        module.calls.append(("find", address, kwargs))
        return module.device

    async def scan(**kwargs: Any) -> dict[str, Any]:
        """Return known, unknown, unnamed and advertisement-name-only devices."""
        module.calls.append(("scan", kwargs))
        result = {}
        for index, (device_name, local_name) in enumerate([
            ("BT-TH-test", None), ("WD_V5_test", None), ("Beech-test", None),
            (None, None), (None, "PMD-test"),
        ]):
            device = SimpleNamespace(address=f"TEST-{index}", name=device_name)
            adv = SimpleNamespace(
                local_name=local_name, rssi=-40 - index, tx_power=None,
                service_uuids=["service"], manufacturer_data={123: b"\x00\xff"},
                service_data={"service": b"\xab\xcd"},
            )
            result[device.address] = (device, adv)
        return result

    monkeypatch.setattr(module, "BleakClient", Client)
    monkeypatch.setattr(module, "BleakScanner", SimpleNamespace(discover=scan, find_device_by_address=find))
    return module


def test_default_scan_preserves_filter_and_options(tool: Any) -> None:
    """Legacy invocation still lists only likely Renogy/Hughes devices without connecting."""
    result = CliRunner().invoke(tool.main, ["--adapter", "hci2", "--timeout", "15"])
    assert result.exit_code == 0, result.output
    assert "Found 3 likely device(s) on hci2" in result.output
    assert "Renogy" in result.output and "Hughes Power Watchdog" in result.output
    assert "Beech-test" not in result.output
    assert tool.calls == [("scan", {"timeout": 15, "adapter": "hci2", "return_adv": True})]


def test_scan_all_json_and_details(tool: Any, tmp_path: Path) -> None:
    """Unknown/unnamed devices and advertisement bytes are retained in machine-readable exports."""
    path = tmp_path / "scan.json"
    result = CliRunner().invoke(tool.main, ["--all", "--output", str(path)])
    assert result.exit_code == 0, result.output
    records = json.loads(path.read_text())
    assert len(records) == 5
    assert any(record["name"] is None for record in records)
    assert any(record["name"] == "PMD-test" for record in records)
    assert records[0]["manufacturer_data"] == {"123": "00ff"}
    assert records[0]["service_data"] == {"service": "abcd"}
    assert records[0]["timestamp"]
    detail = CliRunner().invoke(tool.main, ["--all", "--details"])
    assert "Beech-test" in detail.output and "<unnamed>" in detail.output
    assert "manufacturer_data" in detail.output
    repeated = CliRunner().invoke(tool.main, ["--all", "--output", str(path)])
    assert repeated.exit_code == 1
    assert json.loads(path.read_text()) == records


def test_scan_json_stdout(tool: Any) -> None:
    """JSON output contains only machine-readable records, without human headings."""
    result = CliRunner().invoke(tool.main, ["--all", "--json"])
    assert result.exit_code == 0, result.output
    assert len(json.loads(result.stdout)) == 5
    assert not result.stderr


def test_inspect_metadata_only(tool: Any) -> None:
    """Inspection enumerates properties/descriptors without reads, notifications or pairing."""
    result = CliRunner().invoke(tool.main, ["--adapter", "hci3", "inspect", "TEST-DEVICE"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    characteristic = data["services"][0]["characteristics"][0]
    assert characteristic["properties"] == ["read", "notify"]
    assert characteristic["descriptors"][0]["handle"] == 8
    assert tool.calls[-1] == "disconnect"
    client_call = next(call for call in tool.calls if isinstance(call, tuple) and call[0] == "client")
    assert client_call[2]["adapter"] == "hci3"
    assert "pair" not in client_call[2]
    assert not any(isinstance(call, tuple) and call[0] in ("read", "start") for call in tool.calls)


@pytest.mark.parametrize("selector", ["7", "0x7", "00002a19-0000-1000-8000-00805f9b34fb"])
def test_explicit_read(tool: Any, selector: str) -> None:
    """UUID and handle selection read exactly one value, preserving raw bytes."""
    result = CliRunner().invoke(tool.main, ["read", "TEST-DEVICE", "--characteristic", selector])
    assert result.exit_code == 0, result.output
    value = json.loads(result.output)
    assert (value["hex"], value["length"], value["handle"]) == ("00ff", 2, 7)
    assert value["timestamp"] and value["monotonic_ns"] > 0
    assert tool.calls.count(("read", 7)) == 1
    assert tool.calls[-1] == "disconnect"


@pytest.mark.parametrize(("selector", "properties", "message"), [
    ("unknown", ["read"], "not found"),
    ("ambiguous", ["read"], "Multiple Characteristics"),
    ("7", ["notify"], "does not advertise read"),
])
def test_read_rejections_disconnect(tool: Any, selector: str, properties: list[str], message: str) -> None:
    """Invalid or ambiguous selections cannot silently read other characteristics."""
    tool.characteristic.properties = properties
    result = CliRunner().invoke(tool.main, ["read", "TEST-DEVICE", "--characteristic", selector])
    assert result.exit_code == 1
    assert message in result.output
    assert tool.calls[-1] == "disconnect"
    assert ("read", 7) not in tool.calls


@pytest.mark.parametrize("property_name", ["notify", "indicate"])
def test_listen_jsonl_packet_limit_and_cleanup(tool: Any, tmp_path: Path, property_name: str) -> None:
    """Capture streams evidence into a new file, reports its stop reason and cleans up."""
    tool.characteristic.properties = [property_name]
    path = tmp_path / "packets.jsonl"
    args = ["listen", "TEST-DEVICE", "--characteristic", "7", "--max-packets", "1", "--output", str(path)]
    result = CliRunner().invoke(tool.main, args)
    assert result.exit_code == 0, result.output
    packet = json.loads(path.read_text())
    assert packet["hex"] == "01fe"
    assert packet["length"] == 2
    assert json.loads(result.stderr) == {"packets": 1, "stop_reason": "packet_limit"}
    assert tool.calls[-2:] == [("stop", 7), "disconnect"]
    tool.calls.clear()
    repeated = CliRunner().invoke(tool.main, args)
    assert repeated.exit_code == 1
    assert not tool.calls
    assert json.loads(path.read_text()) == packet


def test_listen_stdout_duration(tool: Any) -> None:
    """A finite duration also stops captures, keeping the stdout JSONL uncontaminated."""
    result = CliRunner().invoke(tool.main, [
        "listen", "TEST-DEVICE", "--characteristic", "7", "--duration", "0.1",
    ])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["hex"] == "01fe"
    assert json.loads(result.stderr) == {"packets": 1, "stop_reason": "duration"}


@pytest.mark.parametrize("mode", ["empty", "disconnect", "start_failure", "unsupported"])
def test_capture_failures_are_explicit(tool: Any, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    """Missing data, link loss and subscription failures never look like successful captures."""
    async def start(self: Any, _characteristic: Any, _callback: Any) -> None:
        """Simulate a particular unsuccessful subscription."""
        if mode == "disconnect":
            self.is_connected = False
            self.callback(self)
        if mode == "start_failure":
            raise BleakError("subscription failed")

    monkeypatch.setattr(tool.BleakClient, "start_notify", start)
    if mode == "unsupported":
        tool.characteristic.properties = ["read"]
    result = CliRunner().invoke(tool.main, [
        "listen", "TEST-DEVICE", "--characteristic", "7", "--duration", "0.1",
    ])
    assert result.exit_code == 1
    expected = {
        "empty": "No notifications", "disconnect": "disconnected during capture",
        "start_failure": "subscription failed", "unsupported": "does not advertise notifications",
    }
    assert expected[mode] in result.output
    assert tool.calls[-1] == "disconnect"


def test_output_failure_propagates_and_disconnects(tool: Any) -> None:
    """A failed output sink aborts subscription with explicit cleanup."""
    class FailedStream(io.StringIO):
        """Simulate storage exhaustion while recording evidence."""

        def write(self, _text: str) -> int:
            """Reject the packet rather than silently losing it."""
            raise OSError("disk full")

    with pytest.raises(OSError, match="disk full"):
        asyncio.run(tool.capture_notifications("TEST-DEVICE", "hci0", 1, "7", 1, 10, FailedStream()))
    assert tool.calls[-2:] == [("stop", 7), "disconnect"]


def test_connect_failure_still_disconnects(tool: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """A partially established connection is cleaned up when connection setup fails."""
    async def fail(_self: Any) -> None:
        """Fail after client construction."""
        raise BleakError("connect failed")

    monkeypatch.setattr(tool.BleakClient, "connect", fail)
    result = CliRunner().invoke(tool.main, ["inspect", "TEST-DEVICE"])
    assert result.exit_code == 1
    assert "connect failed" in result.output
    assert tool.calls[-1] == "disconnect"


def test_not_found_does_not_construct_client(tool: Any) -> None:
    """Missing advertisements produce a useful error without connecting."""
    tool.device = None
    result = CliRunner().invoke(tool.main, ["inspect", "TEST-DEVICE"])
    assert result.exit_code == 1
    assert "not found on hci0" in result.output
    assert len(tool.calls) == 1


def test_read_failure_still_disconnects(tool: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Device-side permission errors are surfaced rather than interpreted as empty data."""
    async def fail(_self: Any, _characteristic: Any) -> bytearray:
        """Simulate an authenticated-read requirement."""
        raise BleakError("authentication required")

    monkeypatch.setattr(tool.BleakClient, "read_gatt_char", fail)
    result = CliRunner().invoke(tool.main, ["read", "TEST-DEVICE", "--characteristic", "7"])
    assert result.exit_code == 1
    assert "authentication required" in result.output
    assert tool.calls[-1] == "disconnect"


@pytest.mark.parametrize("command", ["inspect", "read"])
def test_metadata_and_value_exports(tool: Any, tmp_path: Path, command: str) -> None:
    """Connected operations produce valid JSON evidence without overwriting earlier data."""
    path = tmp_path / f"{command}.json"
    args = [command, "TEST-DEVICE", "--output", str(path)]
    if command == "read":
        args += ["--characteristic", "7"]
    result = CliRunner().invoke(tool.main, args)
    assert result.exit_code == 0, result.output
    data = json.loads(path.read_text())
    assert data["address"] == "TEST-DEVICE"
    repeated = CliRunner().invoke(tool.main, args)
    assert repeated.exit_code == 1
    assert json.loads(path.read_text()) == data


@pytest.mark.parametrize("operation", ["connect", "read", "start", "stop", "disconnect"])
def test_operation_timeouts_are_bounded(
    tool: Any, monkeypatch: pytest.MonkeyPatch, operation: str,
) -> None:
    """Stalled BLE operations time out and still attempt disconnect cleanup."""
    async def stall(_self: Any, *_args: Any) -> None:
        """Wait indefinitely until the utility's per-operation timeout cancels us."""
        tool.calls.append(("stall", operation))
        await asyncio.Event().wait()

    methods = {
        "connect": "connect", "read": "read_gatt_char", "start": "start_notify",
        "stop": "stop_notify", "disconnect": "disconnect",
    }
    monkeypatch.setattr(tool.BleakClient, methods[operation], stall)
    with pytest.raises((TimeoutError, asyncio.TimeoutError)):
        if operation in ("start", "stop"):
            asyncio.run(tool.capture_notifications("TEST-DEVICE", "hci0", 0.001, "7", 1, 1, io.StringIO()))
        else:
            asyncio.run(tool.read_value("TEST-DEVICE", "hci0", 0.001, "7"))
    assert ("stall", operation) in tool.calls
    expected = ("stall", "disconnect") if operation == "disconnect" else "disconnect"
    assert tool.calls[-1] == expected


def test_cancelled_capture_cleans_up(tool: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Task cancellation unsubscribes and disconnects rather than leaving a live session."""
    async def exercise() -> None:
        """Cancel only after notification subscription has completed."""
        started = asyncio.Event()

        async def start(_self: Any, _characteristic: Any, _callback: Any) -> None:
            """Signal successful subscription without providing packets."""
            started.set()

        monkeypatch.setattr(tool.BleakClient, "start_notify", start)
        task = asyncio.create_task(tool.capture_notifications(
            "TEST-DEVICE", "hci0", 1, "7", 30, 10, io.StringIO(),
        ))
        await started.wait()
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())
    assert tool.calls[-2:] == [("stop", 7), "disconnect"]


@pytest.mark.parametrize("empty", [True, False])
def test_empty_scan_and_scan_errors(tool: Any, monkeypatch: pytest.MonkeyPatch, empty: bool) -> None:
    """No matching devices is distinct from an actual scanner failure."""
    async def scan(**_kwargs: Any) -> dict[str, Any]:
        """Return no advertisements or raise a BlueZ-style failure."""
        if empty:
            return {}
        raise BleakError("adapter unavailable")

    monkeypatch.setattr(tool.BleakScanner, "discover", scan)
    result = CliRunner().invoke(tool.main)
    assert result.exit_code == (0 if empty else 1)
    assert ("No likely Renogy or Hughes" if empty else "adapter unavailable") in result.output


@pytest.mark.parametrize("args", [
    ["--timeout", "nan"], ["--timeout", "inf"], ["--timeout", "0"],
    ["--all", "inspect", "TEST-DEVICE"],
    ["listen", "TEST-DEVICE", "--characteristic", "7", "--duration", "nan"],
    ["listen", "TEST-DEVICE", "--characteristic", "7", "--max-packets", "0"],
])
def test_invalid_options_fail_before_ble(tool: Any, args: list[str]) -> None:
    """Invalid durations and misplaced scan options do not access Bluetooth."""
    result = CliRunner().invoke(tool.main, args)
    assert result.exit_code == 2
    assert not tool.calls
