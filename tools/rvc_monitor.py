#!/usr/bin/env python3
"""Monitor SocketCAN traffic and display human-readable RV-C messages."""

from __future__ import annotations

import can
import click
import json
import select
import socket
from typing import Any

from rv_control.debug_tcp import authenticate, auth_token as make_auth_token, client_line
from rv_control.rvc_util import RVC_SPECFILE, decode_message, format_message, load_spec


@click.group(invoke_without_command=True)
@click.option("--interface", "interface_name", default="can0", show_default=True, help="SocketCAN interface to monitor.")
@click.option("--specfile", default=RVC_SPECFILE, show_default=True, type=click.Path(exists=True, dir_okay=False), help="RV-C YAML specification file.")
@click.option("--parameterized/--no-parameterized", default=True, show_default=True, help="Normalize parameter names for readable output.")
@click.pass_context
def main(context: click.Context, interface_name: str, specfile: str, parameterized: bool) -> None:
    """Monitor locally by default, or serve authenticated raw and decoded CAN traffic."""
    context.ensure_object(dict)
    context.obj.update(interface=interface_name, specfile=specfile, parameterized=parameterized)
    if context.invoked_subcommand is not None:
        return
    try:
        spec = load_spec(specfile)
        bus = can.Bus(interface="socketcan", channel=interface_name)
    except (OSError, ValueError, can.CanError) as error:
        raise click.ClickException(str(error)) from error

    click.echo(f"Monitoring {interface_name}; press Ctrl-C to stop.")
    try:
        while True:
            message = bus.recv(timeout=1.0)
            if message is not None:
                decoded = decode_message(message, spec, parameterized)
                click.echo(format_message(message, decoded))
    except KeyboardInterrupt:
        click.echo("Stopping")
    finally:
        bus.shutdown()


def frame_record(message: can.Message, spec: dict[str, Any], parameterized: bool) -> dict[str, Any]:
    """Preserve raw CAN evidence alongside canonical RV-C decoding where applicable."""
    decoded = None
    if not (message.is_error_frame or message.is_remote_frame or message.is_fd):
        decoded = decode_message(message, spec, parameterized)
    return {
        "timestamp": message.timestamp,
        "can_id": f"{message.arbitration_id:08X}" if message.is_extended_id else f"{message.arbitration_id:03X}",
        "data": bytes(message.data).hex(" ").upper(),
        "dlc": message.dlc,
        "is_extended_id": message.is_extended_id,
        "is_remote_frame": message.is_remote_frame,
        "is_error_frame": message.is_error_frame,
        "is_fd": message.is_fd,
        "bitrate_switch": message.bitrate_switch,
        "error_state_indicator": message.error_state_indicator,
        "channel": str(message.channel) if message.channel is not None else None,
        "decoded": decoded,
    }


def stream_client(
    client: socket.socket, token: str, interface_name: str,
    spec: dict[str, Any], parameterized: bool, write_timeout: float,
) -> None:
    """Authenticate before opening CAN, then stream JSON frames without any send capability."""
    if not authenticate(client, token):
        return
    client.settimeout(write_timeout)
    client.sendall(b"OK authenticated; streaming JSON lines\r\n")
    with can.Bus(interface="socketcan", channel=interface_name) as bus:
        while True:
            readable, _, _ = select.select([client], [], [], 0)
            if readable:
                line = client_line(client, 10)
                if line is None or line.strip().upper() == "QUIT":
                    return
                raise ValueError("stream is read-only; only QUIT is accepted")
            message = bus.recv(timeout=0.25)
            if message is not None:
                record = frame_record(message, spec, parameterized)
                client.settimeout(write_timeout)
                client.sendall((json.dumps(record, sort_keys=True) + "\n").encode("utf-8"))


def serve(
    interface_name: str, spec: dict[str, Any], parameterized: bool,
    host: str, port: int, token: str, write_timeout: float,
) -> None:
    """Serve one CAN subscriber at a time until interrupted, isolating failed client sessions."""
    with socket.create_server((host, port), backlog=1) as listener:
        click.echo(f"Listening on {listener.getsockname()[0]}:{listener.getsockname()[1]}", err=True)
        click.echo(f"Auth token: {token}", err=True)
        while True:
            client, _address = listener.accept()
            with client:
                try:
                    stream_client(client, token, interface_name, spec, parameterized, write_timeout)
                except (OSError, ValueError, can.CanError) as error:
                    click.echo(f"Client session failed: {error}", err=True)
                    try:
                        client.settimeout(1)
                        client.sendall(f"ERROR session failed: {error}\r\n".encode("utf-8"))
                    except OSError as send_error:
                        click.echo(f"Could not notify disconnected client: {send_error}", err=True)


@main.command()
@click.option("--host", default="127.0.0.1", show_default=True, help="IPv4 bind address; choose the Pi LAN address for remote clients.")
@click.option("--port", type=click.IntRange(1, 65535), default=35001, show_default=True)
@click.option("--auth-token", default=None, envvar="RVC_SERVER_AUTH_TOKEN", help="Override the generated token, or set RVC_SERVER_AUTH_TOKEN.")
@click.option("--write-timeout", type=click.FloatRange(min=0, min_open=True), default=10.0, show_default=True, help="Seconds allowed for each client socket write.")
@click.pass_context
def server(context: click.Context, host: str, port: int, auth_token: str | None, write_timeout: float) -> None:
    """Stream raw CAN fields and decoded RV-C messages over authenticated plain TCP."""
    try:
        token = make_auth_token(auth_token)
    except ValueError as error:
        raise click.BadParameter(str(error), param_hint="--auth-token") from error
    try:
        spec = load_spec(context.obj["specfile"])
        serve(context.obj["interface"], spec, context.obj["parameterized"], host, port, token, write_timeout)
    except KeyboardInterrupt:
        click.echo("Stopping server", err=True)
    except (OSError, ValueError, can.CanError) as error:
        raise click.ClickException(str(error)) from error


if __name__ == "__main__":
    main()
