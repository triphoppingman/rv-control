#!/usr/bin/env python3
"""Monitor SocketCAN traffic and display human-readable RV-C messages."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import can
import click
from ruamel.yaml import YAML

from rv_control.rvc import decode_dgn


DEFAULT_SPECFILE = "src/rv_control/data/rvc-spec.yml"


def load_spec(specfile: str) -> dict[str, Any]:
    """Load and return the RV-C decoder specification from a YAML file."""
    with Path(specfile).open(encoding="utf-8") as handle:
        spec = YAML(typ="safe").load(handle)
    if not isinstance(spec, dict):
        raise ValueError(f"RV-C specification is not a mapping: {specfile}")
    return spec


def decode_message(message: can.Message, spec: dict[str, Any], parameterized: bool) -> dict[str, Any] | None:
    """Decode an extended CAN message into RV-C metadata and human-readable values."""
    if not message.is_extended_id:
        return None
    can_id = int(message.arbitration_id)
    id_bits = f"{can_id:029b}"
    dgn = f"{int(id_bits[4:21], 2):05X}"
    source = f"{int(id_bits[21:29], 2):02X}"
    decoded = decode_dgn(dgn, bytes(message.data).hex().upper(), spec, parameterized)
    return {"dgn": dgn, "source": source, **decoded}


def format_message(message: can.Message, decoded: dict[str, Any] | None) -> str:
    """Format one CAN message with candump-style framing and decoded RV-C values."""
    timestamp = f"{message.timestamp:.6f}"
    can_id = f"{message.arbitration_id:08X}" if message.is_extended_id else f"{message.arbitration_id:03X}"
    data = bytes(message.data).hex(" ").upper()
    prefix = f"({timestamp}) {can_id} [{message.dlc}] {data}"
    if decoded is None:
        return f"{prefix} | non-RV-C frame"
    values = {
        key: value
        for key, value in decoded.items()
        if key not in {"dgn", "data", "name"}
    }
    fields = " ".join(f"{key}={value}" for key, value in values.items())
    suffix = f"{decoded['dgn']} {decoded['name']}"
    if fields:
        suffix += f" | {fields}"
    return f"{prefix} | {suffix}"


@click.command()
@click.option("--interface", "interface_name", default="can0", show_default=True, help="SocketCAN interface to monitor.")
@click.option("--specfile", default=DEFAULT_SPECFILE, show_default=True, type=click.Path(exists=True, dir_okay=False), help="RV-C YAML specification file.")
@click.option("--parameterized/--no-parameterized", default=True, show_default=True, help="Normalize parameter names for readable output.")
def main(interface_name: str, specfile: str, parameterized: bool) -> None:
    """Listen continuously for CAN traffic and print decoded RV-C messages."""
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


if __name__ == "__main__":
    main()
