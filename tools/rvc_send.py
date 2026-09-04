#!/usr/bin/env python3
"""Send generic and device-specific messages on an RV-C CAN bus."""

from __future__ import annotations

import time
from typing import Any

import can
import click

from rv_control.rvc_util import (
    AC_LOAD_COMMAND_DGN,
    CHARGER_COMMAND_DGN,
    CIRCULATION_PUMP_COMMAND_DGN,
    DC_LOAD_COMMAND_DGN,
    GENERATOR_COMMAND_DGN,
    GENERIC_INDICATOR_COMMAND_DGN,
    INVERTER_COMMAND_DGN,
    RVC_SPECFILE,
    REQUEST_DGN,
    RV_C_PRIORITY,
    SOURCE_ADDRESS,
    address_claim_request_payload,
    address_claim_message,
    charger_payload,
    circulation_pump_payload,
    dc_dimmer_payload,
    generator_payload,
    indicator_payload,
    inverter_payload,
    load_payload,
    load_spec,
    normalize_can_payload,
    rvc_name_details,
    send_can_message,
    send_spec_message,
)


def parse_integer(value: str | int) -> int:
    """Parse a decimal or hexadecimal command-line integer."""
    if isinstance(value, int):
        return value
    try:
        return int(value, 0)
    except ValueError as error:
        raise click.BadParameter("must be a decimal or hexadecimal integer") from error


def parse_source_address(_context: click.Context, _parameter: click.Parameter, value: str) -> int:
    """Parse the shared source-address option as decimal or hexadecimal."""
    return parse_integer(value)


def parse_payload(value: str) -> bytes:
    """Parse and validate a hexadecimal classic-CAN payload."""
    try:
        return normalize_can_payload(bytes.fromhex(value))
    except ValueError as error:
        raise click.BadParameter(f"invalid CAN payload: {error}") from error


def open_bus(interface: str) -> Any:
    """Open the selected SocketCAN interface and convert failures to Click errors."""
    try:
        return can.Bus(interface="socketcan", channel=interface)
    except (OSError, can.CanError) as error:
        raise click.ClickException(f"Unable to open {interface}: {error}") from error


def show_sent(command: str, interface: str, message: Any) -> None:
    """Print one concise candump-style representation of a sent message."""
    click.echo(f"Sent {command} on {interface}: {message.arbitration_id:08X}#" + bytes(message.data).hex().upper())


def send_payload(options: dict[str, Any], dgn: int, payload: bytes, command: str) -> None:
    """Open the selected bus, send one prepared RV-C payload, and close it."""
    bus = open_bus(options["interface"])
    try:
        message = send_can_message(bus, options["priority"], dgn, options["source_address"], payload)
        show_sent(command, options["interface"], message)
    except (OSError, ValueError, can.CanError) as error:
        raise click.ClickException(str(error)) from error
    finally:
        bus.shutdown()


@click.group()
@click.option("--interface", default="can0", show_default=True, help="SocketCAN interface to use.")
@click.option("--priority", default=RV_C_PRIORITY, show_default=True, type=click.IntRange(0, 7), help="RV-C message priority.")
@click.option("--source", "source_address", default=SOURCE_ADDRESS, show_default=True, callback=parse_source_address, help="Source address, decimal or hexadecimal.")
@click.pass_context
def cli(context: click.Context, interface: str, priority: int, source_address: int) -> None:
    """Send RV-C messages with shared CAN addressing options."""
    context.ensure_object(dict)
    context.obj.update(interface=interface, priority=priority, source_address=source_address)


@cli.command("generic")
@click.option("--dgn", "identifier", required=True, help="DGN number or specification name.")
@click.option("--data", "payload", required=True, help="CAN payload as hexadecimal bytes.")
@click.option("--specfile", default=RVC_SPECFILE, show_default=True, type=click.Path(exists=True, dir_okay=False), help="RV-C YAML specification file.")
@click.pass_context
def generic(context: click.Context, identifier: str, payload: str, specfile: str) -> None:
    """Send a raw payload for any DGN defined in the specification."""
    options = context.obj
    bus = open_bus(options["interface"])
    try:
        message = send_spec_message(bus, load_spec(specfile), identifier, options["priority"], options["source_address"], parse_payload(payload))
        show_sent(identifier, options["interface"], message)
    except (OSError, ValueError, can.CanError) as error:
        raise click.ClickException(str(error)) from error
    finally:
        bus.shutdown()


@cli.command("dc-dimmer")
@click.option("--instance", default=0, show_default=True, type=click.IntRange(0, 255), help="Dimmer instance.")
@click.option("--group", "dimmer_group", default=1, show_default=True, type=click.IntRange(0, 255), help="Dimmer group bitmap.")
@click.option("--level", "dimmer_level", default=0.0, show_default=True, type=click.FloatRange(0, 100), help="Dimmer level as a percentage.")
@click.option("--command", "dimmer_command", required=True, help="Dimmer command name or numeric value.")
@click.option("--delay", "delay_duration", default=0, show_default=True, type=click.IntRange(0, 255), help="Command delay or duration.")
@click.option("--interlock", default=0, show_default=True, type=click.IntRange(0, 3), help="Interlock value.")
@click.pass_context
def dc_dimmer(context: click.Context, instance: int, dimmer_group: int, dimmer_level: float, dimmer_command: str, delay_duration: int, interlock: int) -> None:
    """Send a DC_DIMMER_COMMAND_2 message using its specific fields."""
    options = context.obj
    send_payload(options, 0x1FEDB, dc_dimmer_payload(instance, dimmer_group, dimmer_level, dimmer_command, delay_duration, interlock), "DC_DIMMER_COMMAND_2")


@cli.command("generator")
@click.option("--command", required=True, help="Generator command name or numeric value.")
@click.pass_context
def generator(context: click.Context, command: str) -> None:
    """Send a GENERATOR_COMMAND message."""
    send_payload(context.obj, GENERATOR_COMMAND_DGN, generator_payload(command), "GENERATOR_COMMAND")


@cli.command("circulation-pump")
@click.option("--instance", default=0, show_default=True, type=click.IntRange(0, 255), help="Pump instance.")
@click.option("--mode", "output_mode", required=True, help="Pump output mode, such as on or off.")
@click.pass_context
def circulation_pump(context: click.Context, instance: int, output_mode: str) -> None:
    """Send a CIRCULATION_PUMP_COMMAND message."""
    send_payload(context.obj, CIRCULATION_PUMP_COMMAND_DGN, circulation_pump_payload(instance, output_mode), "CIRCULATION_PUMP_COMMAND")


@cli.command("inverter")
@click.option("--instance", default=0, show_default=True, type=click.IntRange(0, 255), help="Inverter instance.")
@click.option("--enable/--disable", default=False, help="Enable or disable the inverter.")
@click.option("--load-sense/--no-load-sense", default=False, help="Enable or disable load sensing.")
@click.option("--pass-through/--no-pass-through", default=False, help="Enable or disable pass-through.")
@click.option("--enable-on-startup/--no-enable-on-startup", default=False)
@click.option("--load-sense-on-startup/--no-load-sense-on-startup", default=False)
@click.option("--pass-through-on-startup/--no-pass-through-on-startup", default=False)
@click.pass_context
def inverter(context: click.Context, instance: int, enable: bool, load_sense: bool, pass_through: bool, enable_on_startup: bool, load_sense_on_startup: bool, pass_through_on_startup: bool) -> None:
    """Send an INVERTER_COMMAND message."""
    payload = inverter_payload(instance, enable, load_sense, pass_through, enable_on_startup, load_sense_on_startup, pass_through_on_startup)
    send_payload(context.obj, INVERTER_COMMAND_DGN, payload, "INVERTER_COMMAND")


@cli.command("charger")
@click.option("--instance", default=0, show_default=True, type=click.IntRange(0, 255), help="Charger instance.")
@click.option("--status", required=True, help="Charger status: disable, enable, or equalize.")
@click.option("--default-on/--default-off", default=False)
@click.option("--auto-recharge/--no-auto-recharge", default=False)
@click.option("--force-charge", default="cancel", show_default=True, help="Force mode: cancel, bulk, or float.")
@click.pass_context
def charger(context: click.Context, instance: int, status: str, default_on: bool, auto_recharge: bool, force_charge: str) -> None:
    """Send a CHARGER_COMMAND message."""
    send_payload(context.obj, CHARGER_COMMAND_DGN, charger_payload(instance, status, default_on, auto_recharge, force_charge), "CHARGER_COMMAND")


@cli.command("dc-load")
@click.option("--instance", default=0, show_default=True, type=click.IntRange(0, 255))
@click.option("--group", "load_group", default=1, show_default=True, type=click.IntRange(0, 255))
@click.option("--level", "load_level", default=0.0, show_default=True, type=click.FloatRange(0, 100))
@click.option("--mode", default="automatic", show_default=True)
@click.option("--interlock", default=0, type=click.IntRange(0, 3))
@click.option("--command", "load_command", required=True)
@click.option("--delay", "delay_duration", default=0, type=click.IntRange(0, 255))
@click.pass_context
def dc_load(context: click.Context, instance: int, load_group: int, load_level: float, mode: str, interlock: int, load_command: str, delay_duration: int) -> None:
    """Send a DC_LOAD_COMMAND message."""
    send_payload(context.obj, DC_LOAD_COMMAND_DGN, load_payload(instance, load_group, load_level, mode, interlock, load_command, delay_duration), "DC_LOAD_COMMAND")


@cli.command("ac-load")
@click.option("--instance", default=0, show_default=True, type=click.IntRange(0, 255))
@click.option("--group", "load_group", default=1, show_default=True, type=click.IntRange(0, 255))
@click.option("--level", "load_level", default=0.0, show_default=True, type=click.FloatRange(0, 100))
@click.option("--mode", default="automatic", show_default=True)
@click.option("--interlock", default=0, type=click.IntRange(0, 3))
@click.option("--load-priority", default=0, type=click.IntRange(0, 13))
@click.option("--command", "load_command", required=True)
@click.option("--delay", "delay_duration", default=0, type=click.IntRange(0, 255))
@click.pass_context
def ac_load(context: click.Context, instance: int, load_group: int, load_level: float, mode: str, interlock: int, load_priority: int, load_command: str, delay_duration: int) -> None:
    """Send an AC_LOAD_COMMAND message."""
    send_payload(context.obj, AC_LOAD_COMMAND_DGN, load_payload(instance, load_group, load_level, mode, interlock, load_command, delay_duration, load_priority), "AC_LOAD_COMMAND")


@cli.command("indicator")
@click.option("--instance", default=0, show_default=True, type=click.IntRange(0, 255))
@click.option("--group", "indicator_group", default=1, show_default=True, type=click.IntRange(0, 255))
@click.option("--brightness", default=0.0, type=click.FloatRange(0, 100))
@click.option("--bank", default=0, type=click.IntRange(0, 15))
@click.option("--duration", default=0, type=click.IntRange(0, 255))
@click.option("--function", "indicator_function", required=True)
@click.pass_context
def indicator(context: click.Context, instance: int, indicator_group: int, brightness: float, bank: int, duration: int, indicator_function: str) -> None:
    """Send a GENERIC_INDICATOR_COMMAND message."""
    send_payload(context.obj, GENERIC_INDICATOR_COMMAND_DGN, indicator_payload(instance, indicator_group, brightness, bank, duration, indicator_function), "GENERIC_INDICATOR_COMMAND")


@cli.command("address-claim-request")
@click.pass_context
def address_claim_request(context: click.Context) -> None:
    """Request that all devices announce their dynamic addresses."""
    options = context.obj
    bus = open_bus(options["interface"])
    try:
        message = send_can_message(bus, options["priority"], REQUEST_DGN, options["source_address"], address_claim_request_payload())
        show_sent("global address-claim request", options["interface"], message)
    except (OSError, ValueError, can.CanError) as error:
        raise click.ClickException(str(error)) from error
    finally:
        bus.shutdown()


@cli.command("get-info")
@click.option("--timeout", default=5.0, show_default=True, type=click.FloatRange(min=1.0), help="Seconds to wait for address claims.")
@click.pass_context
def get_info(context: click.Context, timeout: float) -> None:
    """Request and display device information from RV-C address claims."""
    options = context.obj
    bus = open_bus(options["interface"])
    claims = {}
    try:
        send_can_message(bus, options["priority"], REQUEST_DGN, options["source_address"], address_claim_request_payload())
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            message = bus.recv(timeout=min(1.0, deadline - time.monotonic()))
            if message is None:
                continue
            try:
                claim = address_claim_message(message)
            except ValueError:
                continue
            if claim is not None:
                source, name = claim
                claims[source] = name
        if not claims:
            click.echo(f"No address claims received on {options['interface']} in {timeout:g} seconds.")
            return
        click.echo(f"Found {len(claims)} RV-C device(s) on {options['interface']}:")
        for source, name in sorted(claims.items()):
            click.echo(f"  source=0x{source:02X}")
            for field, value in rvc_name_details(name).items():
                click.echo(f"    {field}={value}")
    except (OSError, ValueError, can.CanError) as error:
        raise click.ClickException(str(error)) from error
    finally:
        bus.shutdown()


if __name__ == "__main__":
    cli()
