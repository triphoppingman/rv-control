#!/usr/bin/env python3
"""Discover likely Renogy and Hughes BLE devices."""

from __future__ import annotations

import asyncio

import click
from bleak import BLEDevice, BleakScanner


RENOGY_PREFIXES = ("BT-TH", "RNGRBP", "BTRIC", "RTMShunt", "RNGRIU")
HUGHES_PREFIXES = ("PMD", "PWS", "PMS", "WD_V5", "WD_E5")


def device_kind(name: str | None) -> str | None:
    """Return the likely device family for a BLE advertised name."""
    advertised_name = (name or "").strip()
    if advertised_name.startswith(RENOGY_PREFIXES):
        return "Renogy"
    if advertised_name.startswith(HUGHES_PREFIXES):
        return "Hughes Power Watchdog"
    return None


async def discover(adapter: str, timeout: float) -> list[tuple[BLEDevice, int | None, str]]:
    """Scan one Bluetooth adapter and return likely device advertisements."""
    discovered = await BleakScanner.discover(timeout=timeout, adapter=adapter, return_adv=True)
    matches = []
    for address, (device, advertisement) in discovered.items():
        kind = device_kind(device.name or advertisement.local_name)
        if kind:
            matches.append((device, advertisement.rssi, kind))
    return sorted(matches, key=lambda item: (item[2], item[0].address))


@click.command()
@click.option("--adapter", default="hci0", show_default=True, help="Bluetooth adapter to scan.")
@click.option("--timeout", default=10.0, show_default=True, type=click.FloatRange(min=1.0), help="Scan duration in seconds.")
def main(adapter: str, timeout: float) -> None:
    """Scan for likely Renogy and Hughes devices and print their details."""
    try:
        matches = asyncio.run(discover(adapter, timeout))
    except (OSError, RuntimeError) as error:
        raise click.ClickException(f"Bluetooth scan failed on {adapter}: {error}") from error

    if not matches:
        click.echo(f"No likely Renogy or Hughes devices found on {adapter}.")
        return

    click.echo(f"Found {len(matches)} likely device(s) on {adapter}:")
    for device, rssi, kind in matches:
        signal = f" RSSI={rssi} dBm" if rssi is not None else ""
        name = device.name or "<unnamed>"
        click.echo(f"{kind}: address={device.address} name={name!r}{signal}")


if __name__ == "__main__":
    main()
