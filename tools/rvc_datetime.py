#!/usr/bin/env python3
"""Read or set the RV-C network date and time."""

from __future__ import annotations

from datetime import datetime
import time

import can
import click

from rv_control.rvc import (RVC_SPECFILE, RV_C_PRIORITY, SET_DATE_TIME_DGN,
                            SOURCE_ADDRESS, datetime_payload, decode_datetime,
                            format_datetime, load_spec, send_can_message)


DEFAULT_INTERFACE = "can0"
READ_TIMEOUT = 15.0


@click.command()
@click.option("-s", "set_time", is_flag=True, help="Set the controller time from the host clock.")
@click.option("-i", "--interface", default=DEFAULT_INTERFACE, show_default=True, help="SocketCAN interface.")
@click.argument("specfile", required=False, default=RVC_SPECFILE, type=click.Path(exists=True, dir_okay=False))
def main(set_time: bool, interface: str, specfile: str) -> None:
    """Read the controller date/time, or set it with the host date/time."""
    try:
        bus = can.Bus(interface="socketcan", channel=interface)
    except (OSError, ValueError, can.CanError) as error:
        raise click.ClickException(f"Unable to open {interface}: {error}") from error

    try:
        if set_time:
            now = datetime.now().astimezone()
            payload = datetime_payload(now)
            send_can_message(bus, RV_C_PRIORITY, SET_DATE_TIME_DGN, SOURCE_ADDRESS, payload)
            click.echo(f"Set controller date/time to {now:%Y-%m-%d %H:%M:%S} on {interface}")
            return

        spec = load_spec(specfile)
        deadline = time.monotonic() + READ_TIMEOUT
        while time.monotonic() < deadline:
            message = bus.recv(timeout=min(1.0, deadline - time.monotonic()))
            if message is None:
                continue
            decoded = decode_datetime(message, spec)
            if decoded is not None:
                click.echo(f"Controller date/time: {format_datetime(decoded)}")
                return
        raise click.ClickException(f"No controller date/time received on {interface} within {READ_TIMEOUT:g} seconds")
    except (OSError, ValueError, can.CanError) as error:
        raise click.ClickException(str(error)) from error
    finally:
        bus.shutdown()


if __name__ == "__main__":
    main()
