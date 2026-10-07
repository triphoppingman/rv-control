#!/usr/bin/env python3
"""Monitor an ELM327 Bluetooth OBD-II adapter directly using its configured INI section."""

from __future__ import annotations

import json
import threading
from typing import Any, Callable

import click

from rv_control.config import load_config
from rv_control.sources.obd import ObdError, ObdSource, extract_data, validate_request


@click.group()
@click.option("--config", "config_path", default="config.ini", type=click.Path(exists=True, dir_okay=False), show_default=True, help="INI configuration file.")
@click.option("--section", default="obd_engine", show_default=True, help="OBD configuration section.")
@click.pass_context
def cli(context: click.Context, config_path: str, section: str) -> None:
    """Read OBD-II data from a configured ELM327 adapter (read-only)."""
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


if __name__ == "__main__":
    cli()
