"""Shared bounded authentication primitives for temporary local TCP debug tools."""

from __future__ import annotations

import secrets
import socket
import time


def auth_token(value: str | None) -> str:
    """Generate a debug token or validate an explicitly supplied printable ASCII token."""
    token = secrets.token_urlsafe(32) if value is None else value
    if not token or len(token) > 200 or not token.isascii() or any(char.isspace() or not char.isprintable() for char in token):
        raise ValueError("token must be 1 to 200 printable ASCII characters without whitespace")
    return token


def client_line(client: socket.socket, timeout: float) -> str | None:
    """Read a bounded ASCII CR/LF-terminated line with a whole-line deadline."""
    deadline = time.monotonic() + timeout
    data = bytearray()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("client line timed out")
        client.settimeout(remaining)
        byte = client.recv(1)
        if not byte:
            return None
        if byte in (b"\r", b"\n"):
            if data:
                return data.decode("ascii")
            continue
        data.extend(byte)
        if len(data) > 256:
            raise ValueError("client line exceeds 256 bytes")


def authenticate(client: socket.socket, token: str) -> bool:
    """Require the token within ten seconds without revealing it to unauthenticated clients."""
    client.settimeout(10)
    client.sendall(b"AUTH required\r\n")
    line = client_line(client, 10)
    if line is None or not secrets.compare_digest(line, f"AUTH {token}"):
        client.sendall(b"ERROR authentication failed\r\n")
        return False
    return True
