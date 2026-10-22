#!/usr/bin/env python3
"""Discover BLE advertisements and explicitly inspect unknown GATT devices."""

from __future__ import annotations

import asyncio
import json
import math
import time
from collections.abc import AsyncGenerator, Callable, Coroutine
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

import click
from bleak import BleakClient, BleakScanner
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.exc import BleakError


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


def finite_seconds(_context: click.Context, parameter: click.Parameter, value: float) -> float:
    """Reject NaN and infinity in addition to Click's numeric range validation."""
    if not math.isfinite(value):
        raise click.BadParameter("must be finite", param=parameter)
    return value


def run_operation(operation: Coroutine[Any, Any, Any]) -> Any:
    """Run a bounded BLE operation and surface actionable CLI failures."""
    try:
        return asyncio.run(operation)
    except (OSError, RuntimeError, ValueError, TimeoutError, asyncio.TimeoutError, BleakError) as error:
        raise click.ClickException(f"BLE operation failed: {type(error).__name__}: {error}") from error


async def scan_records(adapter: str, timeout: float, include_all: bool) -> list[dict[str, Any]]:
    """Return the last advertisement snapshot for each matching address."""
    discovered = await asyncio.wait_for(
        BleakScanner.discover(timeout=timeout, adapter=adapter, return_adv=True),
        timeout=timeout + 5,
    )
    records = []
    for device, advertisement in discovered.values():
        name = advertisement.local_name or device.name
        kind = device_kind(name)
        if not include_all and not kind:
            continue
        records.append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "address": device.address, "name": name, "kind": kind,
            "rssi": advertisement.rssi, "tx_power": advertisement.tx_power,
            "service_uuids": list(advertisement.service_uuids),
            "manufacturer_data": {
                str(key): bytes(value).hex() for key, value in advertisement.manufacturer_data.items()
            },
            "service_data": {
                key: bytes(value).hex() for key, value in advertisement.service_data.items()
            },
        })
    return sorted(records, key=lambda item: (item["kind"] or "Unknown", item["address"]))


@asynccontextmanager
async def connected_device(
    address: str, adapter: str, timeout: float,
    disconnected: Callable[[BleakClient], None] | None = None,
) -> AsyncGenerator[BleakClient, None]:
    """Resolve on the chosen adapter, connect without pairing, and always disconnect."""
    device = await asyncio.wait_for(
        BleakScanner.find_device_by_address(address, timeout=timeout, adapter=adapter),
        timeout=timeout + 5,
    )
    if device is None:
        raise ValueError(f"Device {address!r} not found on {adapter}; wake it and close its phone app")
    client = BleakClient(device, adapter=adapter, timeout=timeout, disconnected_callback=disconnected)
    try:
        await asyncio.wait_for(client.connect(), timeout=timeout)
        yield client
    finally:
        await asyncio.wait_for(client.disconnect(), timeout=timeout)


def select_characteristic(client: BleakClient, selector: str) -> BleakGATTCharacteristic:
    """Select an unambiguous characteristic UUID or decimal/hex handle."""
    try:
        key: str | int = int(selector, 0)
    except ValueError:
        key = selector
    characteristic = client.services.get_characteristic(key)
    if characteristic is None:
        raise ValueError(f"Characteristic {selector!r} not found; inspect the device first")
    return characteristic


async def inspect_device(address: str, adapter: str, timeout: float) -> dict[str, Any]:
    """Enumerate GATT metadata without reading values or sending application commands."""
    async with connected_device(address, adapter, timeout) as client:
        return {
            "address": address, "adapter": adapter,
            "services": [{
                "uuid": service.uuid, "handle": service.handle, "description": service.description,
                "characteristics": [{
                    "uuid": characteristic.uuid, "handle": characteristic.handle,
                    "description": characteristic.description,
                    "properties": list(characteristic.properties),
                    "descriptors": [{
                        "uuid": descriptor.uuid, "handle": descriptor.handle,
                        "description": descriptor.description,
                    } for descriptor in characteristic.descriptors],
                } for characteristic in service.characteristics],
            } for service in client.services],
        }


def value_record(
    address: str, characteristic: BleakGATTCharacteristic, data: bytes | bytearray,
) -> dict[str, Any]:
    """Represent raw bytes losslessly with UTC and monotonic capture timestamps."""
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "monotonic_ns": time.monotonic_ns(),
        "address": address, "characteristic": characteristic.uuid,
        "handle": characteristic.handle, "length": len(data), "hex": bytes(data).hex(),
    }


async def read_value(address: str, adapter: str, timeout: float, selector: str) -> dict[str, Any]:
    """Read only the selected readable characteristic, with an operation timeout."""
    async with connected_device(address, adapter, timeout) as client:
        characteristic = select_characteristic(client, selector)
        if "read" not in characteristic.properties:
            raise ValueError(f"Characteristic {selector!r} does not advertise read support")
        data = await asyncio.wait_for(client.read_gatt_char(characteristic), timeout=timeout)
        return value_record(address, characteristic, data)


async def capture_notifications(
    address: str, adapter: str, timeout: float, selector: str,
    duration: float, max_packets: int, stream: TextIO,
) -> dict[str, Any]:
    """Stream bounded JSONL notifications and fail explicitly on disconnect or output errors."""
    loop = asyncio.get_running_loop()
    finished: asyncio.Future[str] = loop.create_future()
    count = 0
    failure: Exception | None = None

    def disconnected(_client: BleakClient) -> None:
        """Wake the capture if the device disconnects before the requested stop."""
        if not finished.done():
            finished.set_result("disconnected")

    async with connected_device(address, adapter, timeout, disconnected) as client:
        characteristic = select_characteristic(client, selector)
        if not {"notify", "indicate"}.intersection(characteristic.properties):
            raise ValueError(f"Characteristic {selector!r} does not advertise notifications or indications")

        def received(_sender: Any, data: bytearray) -> None:
            """Write each packet immediately without accumulating an unbounded buffer."""
            nonlocal count, failure
            if finished.done():
                return
            try:
                stream.write(json.dumps(value_record(address, characteristic, data)) + "\n")
                stream.flush()
            except (OSError, ValueError) as error:
                failure = error
                finished.set_result("output_error")
                return
            count += 1
            if count >= max_packets:
                finished.set_result("packet_limit")

        subscribed = False
        try:
            await asyncio.wait_for(client.start_notify(characteristic, received), timeout=timeout)
            subscribed = True
            reason = await asyncio.wait_for(asyncio.shield(finished), timeout=duration)
        except (TimeoutError, asyncio.TimeoutError):
            if not subscribed:
                raise
            reason = "duration"
            if not finished.done():
                finished.set_result(reason)
        finally:
            if subscribed and client.is_connected:
                await asyncio.wait_for(client.stop_notify(characteristic), timeout=timeout)
        if failure is not None:
            raise failure
        if reason == "disconnected":
            raise RuntimeError("Device disconnected during capture; saved packets are partial")
        if count == 0:
            raise RuntimeError("No notifications received; subscription is not proof of telemetry support")
        return {"packets": count, "stop_reason": reason}


def emit_json(value: Any, output: Path | None) -> None:
    """Print JSON or save it without overwriting an existing evidence file."""
    text = json.dumps(value, indent=2) + "\n"
    if output is None:
        click.echo(text, nl=False)
    else:
        try:
            with output.open("x", encoding="utf-8") as stream:
                stream.write(text)
        except OSError as error:
            raise click.ClickException(f"Unable to save {output}: {error}") from error


@click.group(invoke_without_command=True)
@click.option("--adapter", default="hci0", show_default=True, help="Bluetooth adapter to scan.")
@click.option("--timeout", default=10.0, show_default=True, type=click.FloatRange(1, 120), callback=finite_seconds, help="Scan duration / per-operation timeout in seconds.")
@click.option("--all", "include_all", is_flag=True, help="Include unknown and unnamed BLE devices.")
@click.option("--details", is_flag=True, help="Show advertisement UUIDs and raw data.")
@click.option("--json", "json_output", is_flag=True, help="Print scan results as JSON.")
@click.option("--output", type=click.Path(path_type=Path, dir_okay=False), help="Save scan JSON to a new file.")
@click.pass_context
def main(
    context: click.Context, adapter: str, timeout: float, include_all: bool,
    details: bool, json_output: bool, output: Path | None,
) -> None:
    """Scan BLE (Renogy/Hughes by default), or explicitly inspect a selected device.

    Put shared --adapter and --timeout options before the subcommand.
    """
    context.obj = {"adapter": adapter, "timeout": timeout}
    if context.invoked_subcommand is not None:
        if include_all or details or json_output or output:
            raise click.UsageError("--all, --details, --json and global --output are scan-only options")
        return
    records = run_operation(scan_records(adapter, timeout, include_all))
    if json_output or output:
        emit_json(records, output)
        return
    if not records:
        label = "BLE" if include_all else "likely Renogy or Hughes"
        click.echo(f"No {label} devices found on {adapter}.")
        return
    click.echo(f"Found {len(records)} {'BLE' if include_all else 'likely'} device(s) on {adapter}:")
    for record in records:
        kind = record["kind"] or "Unknown"
        signal = f" RSSI={record['rssi']} dBm" if record["rssi"] is not None else ""
        name = record["name"] or "<unnamed>"
        device_address = record["address"]
        click.echo(f"{kind}: address={device_address} name={name!r}{signal}")
        if details:
            click.echo(json.dumps(record, indent=2))


@main.command("inspect")
@click.argument("address")
@click.option("--output", type=click.Path(path_type=Path, dir_okay=False))
@click.pass_obj
def inspect_command(options: dict[str, Any], address: str, output: Path | None) -> None:
    """Connect and print GATT metadata only; do not read values or pair."""
    emit_json(run_operation(inspect_device(address, **options)), output)


@main.command("read")
@click.argument("address")
@click.option("--characteristic", "selector", required=True, help="Characteristic UUID or decimal/0x handle.")
@click.option("--output", type=click.Path(path_type=Path, dir_okay=False))
@click.pass_obj
def read_command(options: dict[str, Any], address: str, selector: str, output: Path | None) -> None:
    """Read one explicitly selected characteristic and return raw hex JSON."""
    emit_json(run_operation(read_value(address, selector=selector, **options)), output)


@main.command("listen")
@click.argument("address")
@click.option("--characteristic", "selector", required=True, help="Characteristic UUID or decimal/0x handle.")
@click.option("--duration", default=30.0, show_default=True, type=click.FloatRange(0.1, 3600), callback=finite_seconds)
@click.option("--max-packets", default=10000, show_default=True, type=click.IntRange(1, 100000))
@click.option("--output", type=click.Path(path_type=Path, dir_okay=False), help="Stream JSONL to a new file instead of stdout.")
@click.pass_obj
def listen_command(
    options: dict[str, Any], address: str, selector: str, duration: float,
    max_packets: int, output: Path | None,
) -> None:
    """Subscribe explicitly; this writes BLE subscription descriptors, not application commands."""
    try:
        with click.open_file(str(output) if output else "-", mode="x" if output else "w", encoding="utf-8") as stream:
            summary = run_operation(capture_notifications(
                address, selector=selector, duration=duration,
                max_packets=max_packets, stream=stream, **options,
            ))
    except OSError as error:
        raise click.ClickException(f"Unable to write capture: {error}") from error
    click.echo(json.dumps(summary), err=True)


if __name__ == "__main__":
    main()
