"""BeechLane wireless RV leveler BLE source (read-only).

Protocol constants, decoding, and leveling math live in ``beechlanebt``; this
module owns connection lifecycle, retries, and publishing.
"""

from __future__ import annotations

import asyncio
import logging
import math
import struct
import threading
import time
from collections import deque
from configparser import ConfigParser
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any, Callable

from ..bluetooth import BluetoothAdapterRegistry, release_stale_connection
from .base import Source
from .beechlanebt import codec, leveling, uuids
from .beechlanebt.codec import UNIT_SYMBOLS, Orientation, Unit, VehicleGeometry
from .beechlanebt.leveling import Calibration


LOGGER = logging.getLogger(__name__)

# The vendor app allows at most 5 connection attempts per 30 seconds.
CONNECT_RATE_LIMIT = 5
CONNECT_RATE_WINDOW = 30.0

DISPLAY_UNITS = {
    "in": Unit.INCHES, "inch": Unit.INCHES, "inches": Unit.INCHES,
    "cm": Unit.CENTIMETERS, "centimeter": Unit.CENTIMETERS, "centimeters": Unit.CENTIMETERS,
}

# Float settings: (default, allow zero).
FLOAT_SETTINGS: dict[str, tuple[float, bool]] = {
    "publish_interval": (1.0, True),
    "status_interval": (300.0, False),
    "poll_interval": (60.0, False),
    "notification_timeout": (60.0, True),
    "sample_timeout": (15.0, False),
    "connect_timeout": (20.0, False),
    "connect_settle_delay": (1.0, True),
    "discovery_timeout": (5.0, False),
}


@dataclass(frozen=True)
class LevelerSetup:
    """Device configuration needed to turn XYZ samples into leveling values."""

    orientation: Orientation
    divisor: int
    geometry: VehicleGeometry | None
    calibration: Calibration

    @property
    def configured(self) -> bool:
        """Report whether orientation and geometry have been set on the device."""
        return self.orientation is not Orientation.NOT_SET and self.geometry is not None


def leveling_payload(xyz: tuple[float, float, float], setup: LevelerSetup, display_unit: Unit | None) -> dict[str, Any]:
    """Convert a normalized XYZ sample into the published leveling payload."""
    if setup.geometry is None:
        raise ValueError("vehicle geometry not set")
    state = leveling.compute_state(xyz, setup.orientation, setup.calibration, setup.geometry, display_unit)
    front_left, front_right, rear_left, rear_right = (abs(value) for value in state.corners)
    return {
        "roll": state.roll,
        "pitch": state.pitch,
        "roll_degrees": round(state.roll_degrees, 2),
        "pitch_degrees": round(state.pitch_degrees, 2),
        "front_left_raise": front_left,
        "front_right_raise": front_right,
        "rear_left_raise": rear_left,
        "rear_right_raise": rear_right,
        "level": state.roll == 0 and state.pitch == 0,
        "unit": UNIT_SYMBOLS[state.unit],
    }


def status_payload(battery: int, temperature_c: float, name: str | None, setup: LevelerSetup) -> dict[str, Any]:
    """Build the published device status payload from decoded reads."""
    geometry = setup.geometry
    return {
        "battery": battery,
        "temperature_c": temperature_c,
        "temperature_f": codec.celsius_to_fahrenheit(temperature_c),
        "name": name,
        "orientation": setup.orientation.name.lower(),
        "sensitivity_divisor": setup.divisor,
        "vehicle_type": geometry.vehicle_type.name.lower() if geometry else None,
        "length": geometry.length if geometry else None,
        "width": geometry.width if geometry else None,
        "geometry_unit": UNIT_SYMBOLS[geometry.unit] if geometry else None,
        "configured": setup.configured,
    }


class BeechlaneSource(Source, source_name="beechlane"):
    """Read-only BeechLane leveler telemetry with daemon reconnects or polling."""

    source_name = "beechlane"
    default_topic = "leveler"
    config_section = "beechlane"
    _session_received_telemetry = False

    def __init__(self, config: ConfigParser, publisher: Any, stop_event: threading.Event, section_name: str | None = None) -> None:
        """Create the source thread and its per-instance connection rate limiter."""
        super().__init__(config, publisher, stop_event, section_name)
        self._connect_attempts: deque[float] = deque()

    @staticmethod
    def attribute_inventory() -> list[dict[str, str]]:
        """Describe the telemetry fields published by this source."""
        return [
            {"attribute": "state.roll", "unit": "in|cm", "access": "read"},
            {"attribute": "state.pitch", "unit": "in|cm", "access": "read"},
            {"attribute": "state.roll_degrees", "unit": "deg", "access": "read"},
            {"attribute": "state.pitch_degrees", "unit": "deg", "access": "read"},
            {"attribute": "state.front_left_raise", "unit": "in|cm", "access": "read"},
            {"attribute": "state.front_right_raise", "unit": "in|cm", "access": "read"},
            {"attribute": "state.rear_left_raise", "unit": "in|cm", "access": "read"},
            {"attribute": "state.rear_right_raise", "unit": "in|cm", "access": "read"},
            {"attribute": "state.level", "access": "read"},
            {"attribute": "status.battery", "unit": "%", "access": "read"},
            {"attribute": "status.temperature_c", "unit": "C", "access": "read"},
            {"attribute": "status.temperature_f", "unit": "F", "access": "read"},
            {"attribute": "status.orientation", "access": "read"},
            {"attribute": "status.vehicle_type", "access": "read"},
            {"attribute": "status.length", "access": "read"},
            {"attribute": "status.width", "access": "read"},
            {"attribute": "status.configured", "access": "read"},
        ]

    @classmethod
    def validate_config(cls, config: ConfigParser, section_name: str) -> None:
        """Validate static leveler settings without contacting hardware."""
        section = config[section_name]
        if not section.get("address", "").strip():
            raise ValueError(f"[{section_name}] address is required")
        cls._display_unit(section, section_name)
        for name in FLOAT_SETTINGS:
            cls._float_setting(section, section_name, name)

    @staticmethod
    def _display_unit(section: Any, section_name: str) -> Unit | None:
        """Return the configured display unit, or None to use the device unit."""
        value = section.get("display_unit", "").strip().lower()
        if not value:
            return None
        if value not in DISPLAY_UNITS:
            raise ValueError(f"[{section_name}] display_unit must be blank, in, or cm")
        return DISPLAY_UNITS[value]

    @staticmethod
    def _float_setting(section: Any, section_name: str, name: str) -> float:
        """Return a finite non-negative duration, rejecting zero where unsupported."""
        default, allow_zero = FLOAT_SETTINGS[name]
        raw = section.get(name, "").strip()
        try:
            value = float(raw) if raw else default
        except ValueError:
            raise ValueError(f"[{section_name}] {name} must be a number") from None
        if not math.isfinite(value) or value < 0 or (value == 0 and not allow_zero):
            raise ValueError(f"[{section_name}] {name} must be {'non-negative' if allow_zero else 'positive'} and finite")
        return value

    def _setting(self, name: str) -> float:
        """Return a validated float setting for this instance."""
        return self._float_setting(self.section, self.section_name, name)

    def _retry_setting(self, name: str, default: int | float, value_type: Callable[[str], int | float]) -> int | float:
        """Return a source retry override or the corresponding service default."""
        value = self.section.get(name, "").strip()
        if value:
            return value_type(value)
        service = self.config["service"] if self.config.has_section("service") else {}
        return value_type(service.get(name, str(default)))

    def _persistent(self) -> bool:
        """Return whether the daemon streams on a held connection or polls."""
        value = self.section.get("persistent_connection", "").strip()
        if not value:
            value = self.config["service"].get("daemon_mode", "true") if self.config.has_section("service") else "true"
        return value.lower() == "true"

    @property
    def topic(self) -> str:
        """Return the configured topic root for leveler publications."""
        return self.section.get("topic", self.default_topic).strip().strip("/") or self.default_topic

    @property
    def adapter(self) -> str:
        """Return the configured Bluetooth adapter name."""
        return self.section.get("adapter", "hci0").strip() or "hci0"

    @property
    def address(self) -> str:
        """Return the configured leveler address or raise when absent."""
        address = self.section.get("address", "").strip()
        if not address:
            raise ValueError(f"[{self.section_name}] address is required")
        return address

    # --- one-shot operations ---------------------------------------------

    def comms_check(self) -> dict[str, Any]:
        """Scan for the configured leveler and report the published attributes."""
        return asyncio.run(self._comms_check())

    async def _comms_check(self) -> dict[str, Any]:
        """Discover levelers on the adapter and match the configured address."""
        attributes = self.attribute_inventory()
        try:
            from bleak import BleakScanner

            address = self.section.get("address", "").strip()
            timeout = self._setting("discovery_timeout")
            async with BluetoothAdapterRegistry.for_adapter(self.adapter).scan_slot():
                found = await asyncio.wait_for(
                    BleakScanner.discover(timeout=timeout, adapter=self.adapter, return_adv=True),
                    timeout=timeout + 5,
                )
            levelers = [
                device.address for device, advertisement in found.values()
                if uuids.ADV_SERVICE in [str(item).lower() for item in advertisement.service_uuids or []]
            ]
            if not address:
                found_text = ", ".join(levelers) or "none"
                return {"ok": False, "message": f"BeechLane address is required; levelers seen: {found_text}", "attributes": attributes}
            match = next((device for device, _adv in found.values() if device.address.lower() == address.lower()), None)
            if match is None:
                return {"ok": False, "message": f"BeechLane leveler not found: {address}", "attributes": attributes}
            if match.address not in levelers:
                return {"ok": True, "message": f"Device {address} found ({match.name or 'unnamed'}) but did not advertise the leveler service", "attributes": attributes}
            return {"ok": True, "message": f"BeechLane leveler matched: {match.name or 'unnamed'} ({address})", "attributes": attributes}
        except Exception as error:
            return {"ok": False, "message": f"BeechLane Bluetooth check failed: {type(error).__name__}: {error}", "attributes": attributes}

    def interrogate(self) -> dict[str, Any]:
        """Connect once, read status and one leveling sample, then disconnect."""
        from bleak import BleakClient

        return asyncio.run(self._poll_once(BleakClient, publish=False))

    # --- daemon ----------------------------------------------------------

    def run(self) -> None:
        """Run the leveler collector and log failures that stop its thread."""
        try:
            from bleak import BleakClient

            asyncio.run(self._run_ble(BleakClient))
        except Exception:
            LOGGER.exception("BeechLane source instance %s stopped", self.section_name)

    async def _run_ble(self, client_class: Callable[..., Any]) -> None:
        """Stream or poll the leveler, reconnecting with bounded exponential backoff."""
        persistent = self._persistent()
        reconnect_delay = float(self._retry_setting("reconnect_delay", 10, float))
        max_reconnect_delay = float(self._retry_setting("max_reconnect_delay", 300, float))
        max_retry = int(self._retry_setting("max_retry", 0, int))
        expected_errors: tuple[type[BaseException], ...] = (TimeoutError, asyncio.TimeoutError, OSError)
        try:
            from bleak.exc import BleakError
            expected_errors += (BleakError,)
        except ImportError:
            pass
        retry_count = 0
        while not self.stop_event.is_set():
            self._session_received_telemetry = False
            try:
                if persistent:
                    configured = await self._run_streaming_session(client_class)
                    # An unconfigured device is released so the phone app can set it up.
                    delay = reconnect_delay if configured else self._setting("status_interval")
                    if configured and not self.stop_event.is_set():
                        LOGGER.warning("BeechLane session ended; reconnecting to %s", self.address)
                else:
                    await self._poll_once(client_class, publish=True)
                    delay = self._setting("poll_interval")
                retry_count = 0
            except Exception as error:
                if self.stop_event.is_set():
                    return
                # A session that delivered telemetry before failing should not escalate backoff.
                retry_count = 1 if self._session_received_telemetry else retry_count + 1
                if max_retry and retry_count > max_retry:
                    raise RuntimeError(f"BeechLane reconnect limit reached ({max_retry})") from error
                delay = min(reconnect_delay * (2 ** max(retry_count - 1, 0)), max_reconnect_delay)
                if not persistent:
                    delay = max(delay, min(self._setting("poll_interval"), max_reconnect_delay))
                if isinstance(error, expected_errors):
                    LOGGER.warning("BeechLane %s unavailable (%s: %s); retrying in %gs", self.address, type(error).__name__, error, delay)
                else:
                    LOGGER.exception("BeechLane session failed; retrying %s in %gs", self.address, delay)
            if await asyncio.to_thread(self.stop_event.wait, delay):
                return

    async def _wait_for_connect_slot(self) -> None:
        """Delay connection attempts to respect the device's connection rate limit."""
        while True:
            now = time.monotonic()
            while self._connect_attempts and now - self._connect_attempts[0] >= CONNECT_RATE_WINDOW:
                self._connect_attempts.popleft()
            if len(self._connect_attempts) < CONNECT_RATE_LIMIT:
                self._connect_attempts.append(now)
                return
            wait = CONNECT_RATE_WINDOW - (now - self._connect_attempts[0])
            if await asyncio.to_thread(self.stop_event.wait, wait):
                raise RuntimeError("BeechLane source stopping")

    async def _connect(self, stack: AsyncExitStack, client_class: Callable[..., Any]) -> Any:
        """Release stale links, connect under the shared adapter lock, and settle."""
        address, adapter = self.address, self.adapter
        await self._wait_for_connect_slot()
        # An orphaned BlueZ link keeps the leveler invisible to this service and the phone app.
        await release_stale_connection(adapter, address)
        stack.push_async_callback(release_stale_connection, adapter, address)
        async with BluetoothAdapterRegistry.for_adapter(adapter).connect_slot():
            client = await stack.enter_async_context(
                client_class(address, adapter=adapter, timeout=self._setting("connect_timeout"))
            )
        settle = self._setting("connect_settle_delay")
        if settle > 0:
            await asyncio.sleep(settle)
        return client

    @staticmethod
    def _first_characteristic(client: Any, service_uuid: str) -> Any:
        """Return the first characteristic of a service, as the vendor app addresses it."""
        service = client.services.get_service(service_uuid)
        if service is None or not service.characteristics:
            raise RuntimeError(f"BeechLane service {service_uuid} not found on device")
        return service.characteristics[0]

    async def _read(self, client: Any, characteristic: Any) -> bytes:
        """Read a GATT characteristic and return its bytes."""
        return bytes(await client.read_gatt_char(characteristic))

    async def _read_setup(self, client: Any) -> LevelerSetup:
        """Read orientation, sensitivity, geometry, and calibration offset."""
        orientation = codec.parse_orientation(await self._read(client, uuids.DEVICE_ORIENTATION))
        divisor = codec.parse_sensitivity(await self._read(client, uuids.SENSITIVITY))
        geometry = codec.parse_geometry(await self._read(client, uuids.VEHICLE_GEOMETRY))
        offset = codec.parse_xyz(await self._read(client, uuids.XYZ_CALIBRATE_OFFSET), divisor)
        return LevelerSetup(orientation, divisor, geometry, Calibration.from_offset_xyz(offset, orientation))

    async def _read_status(self, client: Any, setup: LevelerSetup) -> dict[str, Any]:
        """Read battery, temperature, and name into a status payload."""
        battery = codec.parse_battery(await self._read(client, self._first_characteristic(client, uuids.BATTERY_SERVICE)))
        temperature = codec.parse_temperature(await self._read(client, self._first_characteristic(client, uuids.TEMPERATURE_SERVICE)))
        try:
            name: str | None = (await self._read(client, self._first_characteristic(client, uuids.RENAME_SERVICE))).decode(errors="replace").strip("\x00 ")
        except (RuntimeError, OSError) as error:
            LOGGER.debug("BeechLane name read failed: %r", error)
            name = None
        return status_payload(battery, temperature, name, setup)

    def _publish(self, subtopic: str, payload: dict[str, Any]) -> None:
        """Publish a payload beneath this source's topic root."""
        self.publisher.publish(f"{self.topic}/{subtopic}", payload)

    async def _poll_once(self, client_class: Callable[..., Any], publish: bool) -> dict[str, Any]:
        """Connect, read status and one leveling sample, and disconnect."""
        display_unit = self._display_unit(self.section, self.section_name)
        async with AsyncExitStack() as stack:
            client = await self._connect(stack, client_class)
            setup = await self._read_setup(client)
            status = await self._read_status(client, setup)
            if publish:
                self._publish("status", status)
            state = None
            if setup.configured:
                xyz = await self._first_sample(client, setup)
                state = leveling_payload(xyz, setup, display_unit)
                self._session_received_telemetry = True
                if publish:
                    self._publish("state", state)
            else:
                LOGGER.warning("BeechLane %s orientation/geometry not set; complete setup in the vendor app", self.address)
        return {"status": status, "state": state}

    async def _first_sample(self, client: Any, setup: LevelerSetup) -> tuple[float, float, float]:
        """Subscribe to XYZ indications and return the first decodable sample."""
        sample: asyncio.Future[tuple[float, float, float]] = asyncio.get_running_loop().create_future()

        def on_frame(_characteristic: Any, data: bytearray) -> None:
            """Resolve the pending sample with the first decodable XYZ frame."""
            if sample.done():
                return
            try:
                sample.set_result(codec.parse_xyz(bytes(data), setup.divisor))
            except (struct.error, ValueError) as error:
                LOGGER.debug("Ignoring malformed BeechLane XYZ frame %s: %r", bytes(data).hex(), error)

        await client.start_notify(uuids.XYZ_OUT, on_frame)
        try:
            return await asyncio.wait_for(sample, timeout=self._setting("sample_timeout"))
        finally:
            await self._stop_notify(client)

    async def _stop_notify(self, client: Any) -> None:
        """Unsubscribe from XYZ indications, tolerating a dropped link."""
        try:
            await client.stop_notify(uuids.XYZ_OUT)
        except Exception as error:
            LOGGER.debug("BeechLane stop_notify failed during teardown: %r", error)

    async def _run_streaming_session(self, client_class: Callable[..., Any]) -> bool:
        """Hold a connection, publishing throttled leveling state and periodic status.

        Returns False when the device has not been set up, after publishing its status.
        """
        display_unit = self._display_unit(self.section, self.section_name)
        publish_interval = self._setting("publish_interval")
        status_interval = self._setting("status_interval")
        notification_timeout = self._setting("notification_timeout")
        loop = asyncio.get_running_loop()
        async with AsyncExitStack() as stack:
            client = await self._connect(stack, client_class)
            setup = await self._read_setup(client)
            self._publish("status", await self._read_status(client, setup))
            if not setup.configured:
                LOGGER.warning("BeechLane %s orientation/geometry not set; releasing it for vendor app setup", self.address)
                return False
            latest: dict[str, Any] | None = None
            last_frame = loop.time()

            def on_frame(_characteristic: Any, data: bytearray) -> None:
                """Decode an XYZ indication into the latest leveling state."""
                nonlocal latest, last_frame
                try:
                    latest = leveling_payload(codec.parse_xyz(bytes(data), setup.divisor), setup, display_unit)
                except (struct.error, ValueError) as error:
                    LOGGER.debug("Ignoring malformed BeechLane XYZ frame %s: %r", bytes(data).hex(), error)
                    return
                last_frame = loop.time()
                self._session_received_telemetry = True

            await client.start_notify(uuids.XYZ_OUT, on_frame)
            stack.push_async_callback(self._stop_notify, client)
            published: dict[str, Any] | None = None
            last_published = last_status = loop.time()
            tick = min(publish_interval, 1.0) if publish_interval > 0 else 0.1
            while not self.stop_event.is_set():
                await asyncio.sleep(tick)
                now = loop.time()
                changed = latest != published and now - last_published >= publish_interval
                # Unchanged state is republished every status_interval as a liveness heartbeat.
                if latest is not None and (changed or now - last_published >= status_interval):
                    self._publish("state", latest)
                    published, last_published = latest, now
                if now - last_status >= status_interval:
                    # Re-read setup so recalibration or geometry changes take effect without reconnecting.
                    setup = await self._read_setup(client)
                    self._publish("status", await self._read_status(client, setup))
                    last_status = now
                    if not setup.configured:
                        return False
                if notification_timeout and now - last_frame >= notification_timeout:
                    raise TimeoutError(f"No BeechLane leveling data received for {notification_timeout:g} seconds")
        return True
