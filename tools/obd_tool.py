#!/usr/bin/env python3
"""Monitor an ELM327 Bluetooth OBD-II adapter directly using its configured INI section."""

from __future__ import annotations

import json
import re
import socket
import threading
import time
from typing import Any, Callable

import click

from rv_control.config import load_config
from rv_control.debug_tcp import authenticate, auth_token as make_auth_token, client_line
from rv_control.sources.obd import ObdError, ObdNoData, ObdPid, ObdSource, extract_data, validate_request


READ_ONLY_AT = frozenset({"ATI", "AT@1", "AT@2", "ATRV", "ATDP", "ATDPN"})


@click.group()
@click.option("--config", "config_path", default="config.ini", type=click.Path(exists=True, dir_okay=False), show_default=True, help="INI configuration file.")
@click.option("--section", default="obd_engine", show_default=True, help="OBD configuration section.")
@click.pass_context
def cli(context: click.Context, config_path: str, section: str) -> None:
    """Inspect a configured ELM327 adapter; server passthrough requires explicit opt-in."""
    config = load_config(config_path)
    if not config.has_section(section):
        raise click.UsageError(f"Configuration section not found: [{section}]")
    if config[section].get("type", "").strip().lower() != "obd":
        raise click.UsageError(f"Configuration section [{section}] must have type = obd")
    context.ensure_object(dict)
    context.obj["source"] = ObdSource(config, None, threading.Event(), section)


def emit(value: Any) -> None:
    """Print a JSON-compatible value in a readable stable format."""
    click.echo(json.dumps(value, indent=2, sort_keys=True, default=str))


def source_from(context: click.Context) -> ObdSource:
    """Return the configured OBD source stored by the command group."""
    return context.obj["source"]


def run_operation(operation: Callable[[], Any]) -> None:
    """Run one adapter operation and convert expected failures into CLI errors."""
    try:
        emit(operation())
    except (OSError, ValueError, ObdError) as error:
        raise click.ClickException(str(error)) from error


@cli.command()
@click.pass_context
def info(context: click.Context) -> None:
    """Show adapter version, battery voltage, protocol, and ECU status."""
    def operation() -> dict[str, Any]:
        """Open a session and return its initialization details."""
        with source_from(context).session() as (_elm, details):
            return details
    run_operation(operation)


@cli.command()
@click.pass_context
def supported(context: click.Context) -> None:
    """List mode 01 PIDs the ECU reports as supported."""
    def operation() -> dict[str, Any]:
        """Read the ECU's supported-PID bitmaps."""
        source = source_from(context)
        with source.session() as (elm, details):
            if not details["ecu_online"]:
                raise ObdError(details["message"])
            return {"mode": "01", "pids": source.supported_pids(elm)}
    run_operation(operation)


@cli.command("check-pids")
@click.option("--output", "output_path", type=click.Path(dir_okay=False, resolve_path=True), default=None, help="Also save the completed JSON report to FILE, replacing an existing file.", metavar="FILE")
@click.pass_context
def check_pids(context: click.Context, output_path: str | None) -> None:
    """Check configured PIDs once and report usable values, failures, and elapsed time."""
    def operation() -> dict[str, Any]:
        """Read one cycle using the source's header switching and decode dependencies."""
        started = time.monotonic()
        source = source_from(context)
        with source.session() as (elm, details):
            if not details["ecu_online"]:
                raise ObdError(details["message"])
            control: dict[str, Any] = {"request": "010C", "header": source._header() or None}
            try:
                control["rpm"] = source.query(
                    elm, ObdPid.parse("rpm", "01, 0C, (b0*256+b1)/4, RPM"),
                    diagnostics=control,
                )
                control["error"] = None
            except (ObdNoData, TimeoutError) as error:
                control["error"] = str(error)
            diagnostics: list[dict[str, Any]] = []
            values, errors = source.read_cycle(elm, diagnostics)
            results = [
                {
                    "name": pid.name,
                    "request": pid.request,
                    "header": pid.header or source._header() or None,
                    "unit": pid.unit,
                    "usable": pid.name not in errors,
                    "value": values[pid.name],
                    "error": errors.get(pid.name),
                    "diagnostics": evidence,
                }
                for pid, evidence in zip(source.pids, diagnostics, strict=True)
            ]
        report = {
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "checked": len(results),
            "usable": len(results) - len(errors),
            "failed": len(errors),
            "pids": results,
            "adapter": details,
            "control": control,
        }
        if output_path is not None:
            with click.open_file(output_path, "w", encoding="utf-8", atomic=True) as handle:
                handle.write(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n")
        return report
    run_operation(operation)


@cli.command()
@click.pass_context
def read(context: click.Context) -> None:
    """Read every configured PID once."""
    run_operation(source_from(context).interrogate)


@cli.command()
@click.option("--hz", type=click.FloatRange(min=0, min_open=True), default=None, help="Poll rate; defaults to poll_hz.")
@click.option("--count", type=click.IntRange(min=0), default=0, show_default=True, help="Cycles to read; 0 runs until interrupted.")
@click.pass_context
def monitor(context: click.Context, hz: float | None, count: int) -> None:
    """Continuously print configured PID values as they are read."""
    source = source_from(context)
    try:
        interval = 1.0 / hz if hz else source._poll_interval()
        units = {pid.name: pid.unit for pid in source.pids}
        with source.session() as (elm, details):
            if not details["ecu_online"]:
                raise ObdError(details["message"])
            click.echo(f"{details['adapter']} {details['protocol']} {details['voltage']}")
            cycle = 0
            while not count or cycle < count:
                values, _errors = source.read_cycle(elm)
                click.echo("  ".join(f"{name}={'N/A' if value is None else value}{units[name]}" for name, value in values.items()))
                cycle += 1
                source.stop_event.wait(interval)
    except KeyboardInterrupt:
        click.echo("Stopping")
    except (OSError, ValueError, ObdError) as error:
        raise click.ClickException(str(error)) from error


@cli.command()
@click.argument("request")
@click.pass_context
def query(context: click.Context, request: str) -> None:
    """Send one raw read-only OBD request such as 010C or 22F478."""
    try:
        mode, pid = validate_request(request)
    except ValueError as error:
        raise click.BadParameter(str(error)) from error

    def operation() -> dict[str, Any]:
        """Send the request and return raw lines and decoded data bytes."""
        with source_from(context).session() as (elm, _details):
            lines = elm.command(f"{mode:02X}{pid}")
            try:
                data: str | None = extract_data(lines, mode, pid).hex(" ").upper()
            except ObdError:
                data = None
            return {"request": f"{mode:02X}{pid}", "response": lines, "data": data}
    run_operation(operation)


def bridge_command(line: str, allow_unsafe: bool) -> str:
    """Validate one bridge command without forwarding unchecked input to the adapter."""
    command = line.strip().upper().replace(" ", "")
    if not command or len(command) > 256 or not command.isascii() or not command.isprintable():
        raise ValueError("expected a printable ASCII AT command or hexadecimal request")
    if command.startswith("AT"):
        if not allow_unsafe and command not in READ_ONLY_AT:
            raise ValueError("AT command not allowed in read-only mode; use --allow-unsafe")
        return command
    if allow_unsafe:
        if not re.fullmatch(r"(?:[0-9A-F]{2})+", command):
            raise ValueError("expected complete hexadecimal bytes")
        return command
    mode, pid = validate_request(command)
    return f"{mode:02X}{pid}"


def serve_client(
    client: socket.socket, source: ObdSource, token: str,
    allow_unsafe: bool, idle_timeout: float,
) -> None:
    """Authenticate a client before opening Bluetooth, then relay bounded commands serially."""
    if not authenticate(client, token):
        return
    client.sendall(b"OK authenticated; initializing adapter\r\n")
    with source.session() as (elm, _details):
        client.sendall(b">")
        while True:
            line = client_line(client, idle_timeout)
            if line is None or line.strip().upper() == "QUIT":
                return
            try:
                command = bridge_command(line, allow_unsafe)
            except ValueError as error:
                client.sendall(f"ERROR {error}\r\n>".encode("ascii"))
                continue
            try:
                response = elm.command_raw(command)
            except TimeoutError as error:
                client.sendall(f"ERROR {error}\r\n>".encode("ascii"))
                continue
            client.settimeout(idle_timeout)
            client.sendall(response)


def run_server(
    source: ObdSource, host: str, port: int, token: str,
    allow_unsafe: bool, idle_timeout: float,
) -> None:
    """Listen until interrupted, handling one authenticated client and adapter session at a time."""
    with socket.create_server((host, port), backlog=1) as listener:
        listener.settimeout(1)
        click.echo(f"Listening on {listener.getsockname()[0]}:{listener.getsockname()[1]}", err=True)
        click.echo(f"Auth token: {token}", err=True)
        if allow_unsafe:
            click.echo("WARNING: unrestricted AT/hex passthrough enabled; ECU writes are possible.", err=True)
        while not source.stop_event.is_set():
            try:
                client, _address = listener.accept()
            except TimeoutError:
                continue
            with client:
                try:
                    serve_client(client, source, token, allow_unsafe, idle_timeout)
                except (OSError, ValueError, ObdError) as error:
                    click.echo(f"Client session failed: {error}", err=True)
                    try:
                        client.settimeout(1)
                        client.sendall(f"ERROR session failed: {error}\r\n".encode("ascii", errors="replace"))
                    except OSError as send_error:
                        click.echo(f"Could not notify disconnected client: {send_error}", err=True)


@cli.command()
@click.option("--host", default="127.0.0.1", show_default=True, help="IPv4 bind address; use the Pi LAN address for remote clients.")
@click.option("--port", type=click.IntRange(1, 65535), default=35000, show_default=True)
@click.option("--auth-token", default=None, envvar="OBD_SERVER_AUTH_TOKEN", help="Token override; defaults to a new random token. May also use OBD_SERVER_AUTH_TOKEN.")
@click.option("--idle-timeout", type=click.FloatRange(min=0, min_open=True), default=300.0, show_default=True, help="Seconds to receive each complete client command.")
@click.option("--allow-unsafe", is_flag=True, help="Permit unrestricted AT/hex commands, including possible ECU writes.")
@click.pass_context
def server(
    context: click.Context, host: str, port: int, auth_token: str | None,
    idle_timeout: float, allow_unsafe: bool,
) -> None:
    """Temporarily expose the adapter over authenticated plain TCP for nc-style debugging."""
    try:
        token = make_auth_token(auth_token)
    except ValueError as error:
        raise click.BadParameter(str(error), param_hint="--auth-token") from error
    try:
        run_server(source_from(context), host, port, token, allow_unsafe, idle_timeout)
    except KeyboardInterrupt:
        click.echo("Stopping server", err=True)
    except (OSError, ValueError, ObdError) as error:
        raise click.ClickException(str(error)) from error


if __name__ == "__main__":
    cli()
