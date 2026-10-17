"""Shared bounded authentication primitives for temporary local TCP debug tools."""

from __future__ import annotations

import secrets
import socket
import time
from collections.abc import Callable

Trace = Callable[[str, bytes], None]


def auth_token(value: str | None) -> str:
    """Generate a debug token or validate an explicitly supplied printable ASCII token."""
    token = secrets.token_urlsafe(32) if value is None else value
    if not token or len(token) > 200 or not token.isascii() or any(char.isspace() or not char.isprintable() for char in token):
        raise ValueError("token must be 1 to 200 printable ASCII characters without whitespace")
    return token


def client_line(client: socket.socket, timeout: float, trace: Callable[[bytes], None] | None = None) -> str | None:
    """Read a bounded ASCII CR/LF-terminated line with a whole-line deadline.

    When ``trace`` is given it receives every raw byte consumed, including skipped
    CR/LF terminators, once the read finishes, fails, or times out.
    """
    deadline = time.monotonic() + timeout
    data = bytearray()
    raw = bytearray()
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("client line timed out")
            client.settimeout(remaining)
            byte = client.recv(1)
            if not byte:
                return None
            raw.extend(byte)
            if byte in (b"\r", b"\n"):
                if data:
                    return data.decode("ascii")
                continue
            data.extend(byte)
            if len(data) > 256:
                raise ValueError("client line exceeds 256 bytes")
    finally:
        if trace is not None and raw:
            trace(bytes(raw))


def authenticate(client: socket.socket, token: str, trace: Trace | None = None) -> bool:
    """Require the token within ten seconds without revealing it to unauthenticated clients.

    ``trace`` receives ``("TX"|"RX", raw_bytes)`` for the handshake, with the token redacted.
    """
    def send(data: bytes) -> None:
        """Send handshake bytes and report them to the optional trace hook."""
        if trace is not None:
            trace("TX", data)
        client.sendall(data)

    def received(raw: bytes) -> None:
        """Report received handshake bytes with the secret token redacted."""
        if trace is not None:
            trace("RX", raw.replace(token.encode("ascii"), b"<token>"))

    client.settimeout(10)
    send(b"AUTH required\r\n")
    line = client_line(client, 10, received)
    if line is None or not secrets.compare_digest(line, f"AUTH {token}"):
        send(b"ERROR authentication failed\r\n")
        return False
    return True
