"""Hardware-free tests for local monitoring and authenticated RV-C raw streaming."""

from __future__ import annotations

import importlib.util
import json
import socket
import threading
from pathlib import Path
from typing import Any

import can
import pytest
from click.testing import CliRunner

from test_obd_server import receive_until


def load_monitor() -> Any:
    """Load the standalone monitor for CLI and real-socket tests."""
    path = Path(__file__).parents[1] / "tools" / "rvc_monitor.py"
    spec = importlib.util.spec_from_file_location("rvc_monitor", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeBus:
    """Simulate receive-only CAN access and capture lifecycle cleanup."""

    def __init__(self, messages: list[can.Message]) -> None:
        """Store scripted frames and a shutdown marker."""
        self.messages = messages
        self.closed = False

    def __enter__(self) -> FakeBus:
        """Return the bus as its context manager resource."""
        return self

    def __exit__(self, *_args: Any) -> None:
        """Release the resource on all exit paths."""
        self.shutdown()

    def shutdown(self) -> None:
        """Record that CAN access was released."""
        self.closed = True

    def recv(self, timeout: float) -> can.Message | None:
        """Deliver scripted frames or briefly wait for the next client action."""
        if self.messages:
            return self.messages.pop(0)
        threading.Event().wait(min(timeout, 0.01))
        return None


@pytest.mark.parametrize("token", ["wrong", "test-token"])
def test_authenticated_stream_preserves_raw_frames(
    monkeypatch: pytest.MonkeyPatch, token: str,
) -> None:
    """Authenticate over a real socket and verify raw data plus canonical decoded fields."""
    tool = load_monitor()
    messages = [
        can.Message(timestamp=123.5, arbitration_id=0x19FFFF01, data=b"\x00\xFF", is_extended_id=True),
        can.Message(timestamp=124, arbitration_id=0x123, data=b"\x42", is_extended_id=False),
    ]
    bus = FakeBus(messages.copy())
    opened: list[str] = []

    def factory(**kwargs: Any) -> FakeBus:
        """Capture the selected interface without connecting to CAN hardware."""
        opened.append(kwargs["channel"])
        return bus

    monkeypatch.setattr(tool.can, "Bus", factory)
    client, peer = socket.socketpair()
    client.settimeout(3)
    failures: list[BaseException] = []

    def stream() -> None:
        """Capture unexpected server-thread exceptions."""
        try:
            with peer:
                tool.stream_client(peer, "test-token", "can-test", {}, True, 1)
        except BaseException as error:
            failures.append(error)

    thread = threading.Thread(target=stream)
    thread.start()
    try:
        receive_until(client, b"\r\n")
        assert not opened
        client.sendall(f"AUTH {token}\n".encode("ascii"))
        with client.makefile("rb") as reader:
            greeting = reader.readline()
            if token == "wrong":
                assert greeting == b"ERROR authentication failed\r\n"
            else:
                assert greeting.startswith(b"OK authenticated")
                records = [json.loads(reader.readline()) for _ in messages]
                assert records[0]["can_id"] == "19FFFF01"
                assert records[0]["data"] == "00 FF"
                assert records[0]["timestamp"] == 123.5
                assert records[0]["dlc"] == 2
                assert records[0]["decoded"]["name"].startswith("UNKNOWN-")
                assert records[1]["can_id"] == "123"
                assert records[1]["decoded"] is None
                client.sendall(b"QUIT\n")
    finally:
        client.close()
        thread.join(3)
    assert not thread.is_alive()
    assert not failures
    assert opened == ([] if token == "wrong" else ["can-test"])
    assert bus.closed == (token != "wrong")


def test_special_frames_are_kept_raw() -> None:
    """Preserve remote, error, and FD frames without pretending to decode them as RV-C."""
    tool = load_monitor()
    for flags in ({"is_remote_frame": True}, {"is_error_frame": True}, {"is_fd": True}):
        message = can.Message(arbitration_id=0x123, **flags)
        record = tool.frame_record(message, {}, True)
        assert record["decoded"] is None
        assert all(record[key] for key in flags)


def test_existing_local_command_and_server_options(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep invocation without a subcommand as the original local monitor."""
    tool = load_monitor()
    bus = FakeBus([])

    def interrupt(_timeout: float = 1, **_kwargs: Any) -> None:
        """Stop the local monitor as a user interrupt would."""
        raise KeyboardInterrupt

    monkeypatch.setattr(bus, "recv", interrupt)
    monkeypatch.setattr(tool.can, "Bus", lambda **_kwargs: bus)
    monkeypatch.setattr(tool, "load_spec", lambda _path: {})
    result = CliRunner().invoke(tool.main, ["--interface", "can-test"])
    assert result.exit_code == 0, result.output
    assert "Monitoring can-test" in result.output
    assert bus.closed
    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(tool, "serve", lambda *args: calls.append(args))
    monkeypatch.delenv("RVC_SERVER_AUTH_TOKEN", raising=False)
    result = CliRunner().invoke(
        tool.main, ["--interface", "can-test", "server", "--host", "0.0.0.0", "--port", "36001"],
    )
    assert result.exit_code == 0, result.output
    interface, _spec, _parameterized, host, port, token, timeout = calls[0]
    assert (interface, host, port, timeout) == ("can-test", "0.0.0.0", 36001, 10)
    assert len(token) == 43


def test_listener_recovers_after_bad_auth_and_invalid_command(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify a responsive localhost listener, rejected commands, and complete socket cleanup."""
    tool = load_monitor()
    listener = socket.create_server(("127.0.0.1", 0))
    address = listener.getsockname()
    monkeypatch.setattr(tool.socket, "create_server", lambda *_args, **_kwargs: listener)
    buses: list[FakeBus] = []
    ready = threading.Event()
    stopping = threading.Event()
    failures: list[BaseException] = []
    logs: list[str] = []

    def factory(**_kwargs: Any) -> FakeBus:
        """Provide fresh receive-only bus sessions for authenticated clients."""
        bus = FakeBus([])
        buses.append(bus)
        return bus

    def echo(message: str, **_kwargs: Any) -> None:
        """Capture status/errors and signal server startup without printing tokens."""
        logs.append(message)
        if message.startswith("Auth token:"):
            ready.set()

    def serve() -> None:
        """Run a real listener, treating the test's explicit shutdown as expected."""
        try:
            tool.serve("can-test", {}, True, "127.0.0.1", address[1], "test-token", 1)
        except OSError as error:
            if not stopping.is_set():
                failures.append(error)
        except BaseException as error:
            failures.append(error)

    monkeypatch.setattr(tool.can, "Bus", factory)
    monkeypatch.setattr(tool.click, "echo", echo)
    thread = threading.Thread(target=serve)
    thread.start()
    try:
        assert ready.wait(2)
        for token, command in (("wrong", None), ("test-token", b"SEND 123\n"), ("test-token", b"QUIT\n")):
            with socket.create_connection(address, timeout=3) as client:
                receive_until(client, b"\r\n")
                client.sendall(f"AUTH {token}\n".encode("ascii"))
                greeting = receive_until(client, b"\r\n")
                if command is None:
                    assert greeting == b"ERROR authentication failed\r\n"
                else:
                    assert greeting.startswith(b"OK authenticated")
                    client.sendall(command)
                    if command.startswith(b"SEND"):
                        assert b"stream is read-only" in receive_until(client, b"\r\n")
    finally:
        stopping.set()
        listener.shutdown(socket.SHUT_RDWR)
        thread.join(3)
    assert not thread.is_alive()
    assert not failures
    assert len(buses) == 2
    assert all(bus.closed for bus in buses)
    assert listener.fileno() == -1
    assert any("stream is read-only" in line for line in logs)
