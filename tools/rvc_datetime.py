#!/usr/bin/env python3
"""Read or set the RV-C network date and time."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import time
from typing import Any

import can
import click
from ruamel.yaml import YAML

from rv_control.rvc import decode_dgn


DEFAULT_INTERFACE = "can0"
DEFAULT_SPECFILE = str(Path(__file__).resolve().parents[1] / "src/rv_control/data/rvc-spec.yml")
DATE_TIME_STATUS_DGN = 0x1FFFF
SET_DATE_TIME_DGN = 0x1FFFE
RV_C_PRIORITY = 6
SOURCE_ADDRESS = 0x00
READ_TIMEOUT = 15.0
TIME_ZONE_CODES = {
    "GMT": 0,
    "UTC": 0,
    "EDT": 4,
    "EST": 5,
    "PDT": 7,
    "PST": 8,
}


def load_spec(specfile: str) -> dict[str, Any]:
    """Load the RV-C specification used to decode date/time status frames."""
    with Path(specfile).open(encoding="utf-8") as handle:
        spec = YAML(typ="safe").load(handle)
    if not isinstance(spec, dict):
        raise ValueError(f"RV-C specification is not a mapping: {specfile}")
    return spec


def dgn_from_can_id(arbitration_id: int) -> int:
    """Extract the RV-C DGN from a 29-bit CAN identifier."""
    return (arbitration_id >> 8) & 0x1FFFF


def decode_datetime(message: can.Message, spec: dict[str, Any]) -> dict[str, Any] | None:
    """Decode an RV-C date/time status message, or return None for other frames."""
    if not message.is_extended_id or dgn_from_can_id(message.arbitration_id) != DATE_TIME_STATUS_DGN:
        return None
    data = bytes(message.data)
    if len(data) != 8:
        raise ValueError(f"date/time status must contain 8 bytes, received {len(data)}")
    return decode_dgn("1FFFF", data.hex().upper(), spec, True)


def format_datetime(decoded: dict[str, Any]) -> str:
    """Format decoded RV-C date/time fields for console output."""
    return (
        f"{int(decoded['year']) + 2000:04d}-{int(decoded['month']):02d}-"
        f"{int(decoded['date']):02d} {int(decoded['hour']):02d}:"
        f"{int(decoded['minute']):02d}:{int(decoded['second']):02d} "
        f"(timezone code {decoded.get('timezone', 'unknown')})"
    )


def timezone_code(now: datetime) -> int:
    """Return the RV-C timezone code for the host timezone when recognized."""
    return TIME_ZONE_CODES.get(now.tzname() or "", 0)


def datetime_payload(now: datetime) -> list[int]:
    """Encode the current host datetime as an RV-C date/time command payload."""
    year = now.year - 2000
    if not 0 <= year <= 255:
        raise ValueError(f"year is outside the RV-C range: {now.year}")
    return [year, now.month, now.day, (now.isoweekday() % 7) + 1, now.hour, now.minute, now.second, timezone_code(now)]


def command_can_id() -> int:
    """Build the extended CAN identifier for the RV-C date/time command."""
    return (RV_C_PRIORITY << 26) | (SET_DATE_TIME_DGN << 8) | SOURCE_ADDRESS


@click.command()
@click.option("-s", "set_time", is_flag=True, help="Set the controller time from the host clock.")
@click.option("-i", "--interface", default=DEFAULT_INTERFACE, show_default=True, help="SocketCAN interface.")
@click.argument("specfile", required=False, default=DEFAULT_SPECFILE, type=click.Path(exists=True, dir_okay=False))
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
            message = can.Message(
                arbitration_id=command_can_id(),
                data=payload,
                is_extended_id=True,
            )
            bus.send(message)
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
