"""Hardware-free socket tests for the temporary ELM327 TCP debug bridge."""

from __future__ import annotations

import socket
import threading
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from test_obd import INIT, FakeElmSocket, make_source
from test_obd_tool import load_tool
from rv_control.sources.obd import Elm327


def receive_until(client: socket.socket, suffix: bytes) -> bytes:
    """Receive a bounded test response until its expected protocol terminator."""
    data = bytearray()
    while not data.endswith(suffix):
        chunk = client.recv(1024)
        assert chunk, bytes(data)
        data.extend(chunk)
        assert len(data) < 10000
    return bytes(data)


def test_raw_command_preserves_echo_spacing_and_prompt() -> None:
    """Ensure the bridge path preserves responses while the existing parser stays normalized."""
    elm = Elm327(FakeElmSocket({"010C": "41 0C 1A F8"}))
    assert elm.command_raw("010C") == b"010C\r41 0C 1A F8\r\r>"
    assert elm.command("010C") == ["410C1AF8"]


@pytest.mark.parametrize("unsafe", [False, True])
def test_bridge_auth_commands_and_cleanup(unsafe: bool) -> None:
    """Authenticate over a real socket and verify filtering, raw replies, and session cleanup."""
    tool = load_tool()
    source, sockets = make_source({**INIT, "ATI": "ELM327 v1.5", "010C": "41 0C 1A F8"})
    client, peer = socket.socketpair()
    client.settimeout(3)
    failures: list[BaseException] = []

    def serve() -> None:
        """Run one client session and retain unexpected thread failures for the assertion."""
        try:
            with peer:
                tool.serve_client(peer, source, "test-token", unsafe, 2)
        except BaseException as error:
            failures.append(error)

    thread = threading.Thread(target=serve)
    thread.start()
    try:
        assert receive_until(client, b"\r\n") == b"AUTH required\r\n"
        assert not sockets
        client.sendall(b"AUTH test-token\r\n")
        assert receive_until(client, b">").startswith(b"OK authenticated")
        client.sendall(b"ATI\r")
        assert receive_until(client, b">") == b"ATI\rELM327 v1.5\r\r>"
        client.sendall(b"01 0c\n")
        assert receive_until(client, b">") == b"010C\r41 0C 1A F8\r\r>"
        client.sendall(b"ATSH7E1\n")
        header_reply = receive_until(client, b">")
        if unsafe:
            assert header_reply == b"ATSH7E1\r?\r\r>"
            assert "ATSH7E1" in sockets[0].sent
        else:
            assert header_reply.startswith(b"ERROR AT command not allowed")
            assert "ATSH7E1" not in sockets[0].sent
        client.sendall(b"04\n")
        reply = receive_until(client, b">")
        assert reply.startswith(b"04\r" if unsafe else b"ERROR")
        assert ("04" in sockets[0].sent) == unsafe
        client.sendall(b"QUIT\n")
    finally:
        client.close()
        thread.join(3)
    assert not thread.is_alive()
    assert not failures
    assert sockets[0].closed


def test_wrong_token_never_opens_bluetooth() -> None:
    """Reject authentication without opening a hardware session."""
    tool = load_tool()
    source, sockets = make_source(INIT)
    client, peer = socket.socketpair()
    with client, peer:
        client.sendall(b"AUTH wrong-token\n")
        tool.serve_client(peer, source, "test-token", False, 1)
        assert b"ERROR authentication failed" in client.recv(1024)
    assert not sockets


@pytest.mark.parametrize("disconnect", [True, False])
def test_authenticated_disconnect_or_idle_timeout_closes_adapter(disconnect: bool) -> None:
    """Release Bluetooth on client EOF or an authenticated client's idle deadline."""
    tool = load_tool()
    source, sockets = make_source({**INIT, "0100": "NO DATA"})
    client, peer = socket.socketpair()
    client.settimeout(3)
    failures: list[BaseException] = []

    def serve() -> None:
        """Retain the expected timeout while ensuring the session context is closed."""
        try:
            with peer:
                tool.serve_client(peer, source, "test-token", False, 0.05)
        except TimeoutError as error:
            failures.append(error)

    thread = threading.Thread(target=serve)
    thread.start()
    try:
        receive_until(client, b"\r\n")
        client.sendall(b"AUTH test-token\n")
        receive_until(client, b">")
        if disconnect:
            client.shutdown(socket.SHUT_WR)
        thread.join(3)
    finally:
        client.close()
        thread.join(3)
    assert not thread.is_alive()
    assert bool(failures) == (not disconnect)
    assert sockets[0].closed


@pytest.mark.parametrize("line", ["ATZ", "ATSH7E0", "2EF47800", "04", "010C\rATZ", "xyz", "010"])
def test_read_only_filter_rejects_unchecked_commands(line: str) -> None:
    """Reject state-changing adapter commands, write services, and malformed input."""
    with pytest.raises(ValueError):
        load_tool().bridge_command(line, False)


def test_line_limits_and_auth_deadline() -> None:
    """Enforce bounds before hardware access, including unterminated client input."""
    tool = load_tool()
    client, peer = socket.socketpair()
    with client, peer:
        client.sendall(b"x" * 257)
        with pytest.raises(ValueError, match="exceeds"):
            tool.client_line(peer, 1)
    client, peer = socket.socketpair()
    with client, peer:
        client.sendall(b"AUTH incomplete")
        with pytest.raises(TimeoutError):
            tool.client_line(peer, 0.02)
    client, peer = socket.socketpair()
    with client, peer:
        client.sendall(b"\xff\n")
        with pytest.raises(UnicodeDecodeError):
            tool.client_line(peer, 1)


def test_listener_recovers_after_bad_auth_and_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise a real localhost listener with sequential rejected and authenticated clients."""
    tool = load_tool()
    source, sockets = make_source(INIT)
    listener = socket.create_server(("127.0.0.1", 0))
    address = listener.getsockname()
    monkeypatch.setattr(tool.socket, "create_server", lambda *_args, **_kwargs: listener)
    ready = threading.Event()
    messages: list[str] = []
    failures: list[BaseException] = []

    def echo(message: str, **_kwargs: Any) -> None:
        """Capture local startup output and signal that the listener is ready."""
        messages.append(message)
        if message.startswith("Auth token:"):
            ready.set()

    def serve() -> None:
        """Run the real listener until the source stop event is set."""
        try:
            tool.run_server(source, "127.0.0.1", address[1], "test-token", False, 1)
        except BaseException as error:
            failures.append(error)

    monkeypatch.setattr(tool.click, "echo", echo)
    thread = threading.Thread(target=serve)
    thread.start()
    try:
        assert ready.wait(2)
        for token in ("wrong", "test-token"):
            with socket.create_connection(address, timeout=3) as client:
                receive_until(client, b"\r\n")
                client.sendall(f"AUTH {token}\n".encode("ascii"))
                if token == "wrong":
                    assert receive_until(client, b"\r\n") == b"ERROR authentication failed\r\n"
                else:
                    receive_until(client, b">")
                    client.sendall(b"QUIT\n")
    finally:
        source.stop_event.set()
        thread.join(3)
    assert not thread.is_alive()
    assert not failures
    assert len(sockets) == 1
    assert sockets[0].closed
    assert listener.fileno() == -1
    assert any(message.startswith("Listening on 127.0.0.1:") for message in messages)


@pytest.mark.parametrize("override", [None, "configured-test-token"])
def test_server_cli_options(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, override: str | None,
) -> None:
    """Verify generated or configured tokens and explicit unsafe and network options."""
    tool = load_tool()
    source, _sockets = make_source(INIT)
    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(tool, "run_server", lambda *args: calls.append(args))
    monkeypatch.delenv("OBD_SERVER_AUTH_TOKEN", raising=False)
    config_path = tmp_path / "config.ini"
    with config_path.open("w", encoding="utf-8") as handle:
        source.config.write(handle)
    arguments = [
        "--config", str(config_path), "server",
        "--host", "0.0.0.0", "--port", "36000", "--allow-unsafe", "--idle-timeout", "30",
    ]
    if override is not None:
        monkeypatch.setenv("OBD_SERVER_AUTH_TOKEN", override)

    result = CliRunner().invoke(tool.cli, arguments)

    assert result.exit_code == 0, result.output
    _source, host, port, token, unsafe, timeout = calls[0]
    assert (host, port, unsafe, timeout) == ("0.0.0.0", 36000, True, 30)
    if override is None:
        assert len(token) == 43
    else:
        assert token == override
