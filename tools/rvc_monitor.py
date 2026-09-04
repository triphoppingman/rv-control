#!/usr/bin/env python3
"""Monitor SocketCAN traffic and display human-readable RV-C messages."""

from __future__ import annotations

import can
import click

from rv_control.rvc import RVC_SPECFILE, decode_message, format_message, load_spec


@click.command()
@click.option("--interface", "interface_name", default="can0", show_default=True, help="SocketCAN interface to monitor.")
@click.option("--specfile", default=RVC_SPECFILE, show_default=True, type=click.Path(exists=True, dir_okay=False), help="RV-C YAML specification file.")
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
