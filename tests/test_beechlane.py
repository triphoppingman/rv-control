from __future__ import annotations

import asyncio
import math
import threading
from configparser import ConfigParser
from typing import Any, Callable

import pytest

from rv_control.sources import beechlane as beechlane_module
from rv_control.sources.base import Source
from rv_control.sources.beechlane import BeechlaneSource
from rv_control.sources.beechlanebt import codec, uuids
from rv_control.sources.beechlanebt.codec import Orientation, Unit, VehicleGeometry, VehicleType
from rv_control.sources.beechlanebt.leveling import Calibration, angle_a, angle_b, compute_state, convert_dimensions

GEOM = VehicleGeometry(VehicleType.CLASS_A, Unit.INCHES, 360, 96)
FLAT = (0.0, 0.0, 1.0)


def tilted(pitch_deg: float, roll_deg: float) -> tuple[float, float, float]:
    """Build a normalized XYZ vector with the given sensor pitch and roll."""
    x = -math.sin(math.radians(pitch_deg))
    c = math.cos(math.radians(pitch_deg))
    return (x, c * math.sin(math.radians(roll_deg)), c * math.cos(math.radians(roll_deg)))


def xyz_bytes(xyz: tuple[float, float, float], divisor: int = 16383) -> bytes:
    """Encode a normalized XYZ vector as the device's int16 little-endian frame."""
    return b"".join(round(value * divisor).to_bytes(2, "little", signed=True) for value in xyz)


# --- codec ---------------------------------------------------------------

def test_codec_scalars() -> None:
    """Verify battery, temperature, orientation, and sensitivity decoding."""
    assert codec.parse_battery(b"\x2a") == 42
    assert codec.parse_temperature(b"\x28\x0a") == 26.0
    assert codec.parse_temperature(b"\x85\xff") == -1.2
    assert codec.celsius_to_fahrenheit(26.0) == 78.8
    assert codec.parse_orientation(b"\x03") is Orientation.DRIVER_SIDE
    assert codec.parse_orientation(b"\x00") is Orientation.NOT_SET
    assert [codec.parse_sensitivity(bytes([code])) for code in (0, 1, 2)] == [16383, 32766, 49149]
    for bad in (lambda: codec.parse_battery(b""), lambda: codec.parse_sensitivity(b"\x05")):
        with pytest.raises(ValueError):
            bad()


def test_codec_geometry_and_xyz() -> None:
    """Verify geometry framing and raw/normalized XYZ decoding."""
    geometry = VehicleGeometry(VehicleType.TRAVEL_TRAILER, Unit.INCHES, 360, 96)
    assert codec.parse_geometry(bytes.fromhex("00010003000101680060")) == geometry
    assert codec.parse_geometry(b"") is None
    with pytest.raises(ValueError):
        codec.parse_geometry(bytes.fromhex("00020003000101680060"))
    data = (1000).to_bytes(2, "little") + (-2000).to_bytes(2, "little", signed=True) + (16383).to_bytes(2, "little")
    assert codec.parse_xyz_raw(data) == (1000, -2000, 16383)
    assert codec.parse_xyz(data, 16383)[2] == pytest.approx(1.0)


# --- leveling math (parity with pyrvleveler / vendor app) ------------------

def test_angle_remapping() -> None:
    """Verify each mounting orientation remaps sensor axes like the vendor app."""
    assert angle_b(FLAT, Orientation.FRONT) == pytest.approx(90.0)
    xyz = tilted(10.0, 5.0)
    assert angle_a(xyz, Orientation.FRONT) == pytest.approx(10.0)
    assert angle_a(xyz, Orientation.REAR) == pytest.approx(-10.0)
    assert angle_b(xyz, Orientation.DRIVER_SIDE) == pytest.approx(-10.0)
    assert angle_b(xyz, Orientation.PASSENGER_SIDE) == pytest.approx(10.0)
    assert angle_a(xyz, Orientation.PASSENGER_SIDE) == pytest.approx(-95.0)


@pytest.mark.parametrize(
    ("orientation", "sample", "roll", "pitch", "corners"),
    [
        (Orientation.FRONT, tilted(5.0, 0.0), 8.25, 0.0, (0.0, -8.25, 0.0, -8.25)),
        (Orientation.FRONT, tilted(0.0, 5.0), 0.0, -31.5, (0.0, 0.0, -31.5, -31.5)),
        (Orientation.DRIVER_SIDE, tilted(5.0, 0.0), 0.0, 31.5, (-31.5, -31.5, 0.0, 0.0)),
    ],
)
def test_compute_state_matches_vendor_rounding(
    orientation: Orientation, sample: tuple[float, float, float], roll: float, pitch: float, corners: tuple[float, ...],
) -> None:
    """Verify roll, pitch, and corner outputs match the vendor app's math."""
    state = compute_state(sample, orientation, Calibration.from_offset_xyz(FLAT, orientation), GEOM)
    assert (state.roll, state.pitch, state.corners) == (roll, pitch, corners)


def test_calibration_and_units() -> None:
    """Verify calibration cancels offsets and centimeter output rounds to 0.5."""
    cal = Calibration.from_offset_xyz(tilted(3.0, 2.0), Orientation.FRONT)
    state = compute_state(tilted(3.0, 2.0), Orientation.FRONT, cal, GEOM)
    assert (state.roll, state.pitch) == (0.0, 0.0)
    assert convert_dimensions(GEOM, Unit.CENTIMETERS)[0] == pytest.approx(360 * 2.54)
    flat_cal = Calibration.from_offset_xyz(FLAT, Orientation.FRONT)
    assert compute_state(tilted(5.0, 0.0), Orientation.FRONT, flat_cal, GEOM, Unit.CENTIMETERS).roll == 21.5
    with pytest.raises(ValueError):
        compute_state(FLAT, Orientation.NOT_SET, Calibration(0.0, 0.0), GEOM)


# --- source --------------------------------------------------------------

class Recorder:
    """Collect publications in order."""

    def __init__(self) -> None:
        """Start with no publications."""
        self.items: list[tuple[str, dict[str, Any]]] = []

    def publish(self, topic: str, payload: dict[str, Any]) -> None:
        """Record one publication."""
        self.items.append((topic, payload))


class FakeCharacteristic:
    """Stand in for a bleak characteristic addressed by service."""

    def __init__(self, uuid: str) -> None:
        """Remember the characteristic's synthetic UUID."""
        self.uuid = uuid


class FakeService:
    """Expose one characteristic, as the leveler's single-value services do."""

    def __init__(self, uuid: str) -> None:
        """Create the service's first characteristic."""
        self.characteristics = [FakeCharacteristic(f"{uuid}#0")]


class FakeServices:
    """Return fake services by UUID."""

    def get_service(self, uuid: str) -> FakeService:
        """Return a service exposing a first characteristic."""
        return FakeService(uuid)


def make_client_class(
    frames: list[bytes], *, geometry: bytes = bytes.fromhex("00010001000101680060"),
    orientation: bytes = b"\x01", on_notify: Callable[[], None] | None = None, fail_first: int = 0,
) -> tuple[type, dict[str, Any]]:
    """Build a fake BleakClient class that serves a configured leveler."""
    state: dict[str, Any] = {"constructed": 0, "kwargs": [], "stopped_notify": 0, "exited": 0}
    reads = {
        f"{uuids.BATTERY_SERVICE}#0": b"\x55",
        f"{uuids.TEMPERATURE_SERVICE}#0": (2150).to_bytes(2, "little"),
        f"{uuids.RENAME_SERVICE}#0": b"Leveler",
        uuids.DEVICE_ORIENTATION: orientation,
        uuids.SENSITIVITY: b"\x00",
        uuids.VEHICLE_GEOMETRY: geometry,
        uuids.XYZ_CALIBRATE_OFFSET: xyz_bytes(FLAT),
    }

    class FakeClient:
        services = FakeServices()

        def __init__(self, address: str, **kwargs: Any) -> None:
            """Record construction arguments."""
            state["constructed"] += 1
            state["kwargs"].append(kwargs)

        async def __aenter__(self) -> FakeClient:
            """Fail the first N connections, then connect."""
            if state["constructed"] <= fail_first:
                raise OSError("temporary failure")
            return self

        async def __aexit__(self, *_args: Any) -> bool:
            """Record disconnects."""
            state["exited"] += 1
            return False

        async def read_gatt_char(self, characteristic: Any) -> bytearray:
            """Return canned characteristic values."""
            key = characteristic.uuid if isinstance(characteristic, FakeCharacteristic) else characteristic
            return bytearray(reads[key])

        async def start_notify(self, uuid: str, callback: Callable[[Any, bytearray], None]) -> None:
            """Deliver queued XYZ frames on the running loop."""
            assert uuid == uuids.XYZ_OUT
            loop = asyncio.get_running_loop()
            for frame in frames:
                loop.call_soon(callback, None, bytearray(frame))
            if on_notify:
                on_notify()

        async def stop_notify(self, _uuid: str) -> None:
            """Record unsubscribe."""
            state["stopped_notify"] += 1

    return FakeClient, state


@pytest.fixture
def leveler_config() -> ConfigParser:
    """Return a configuration with one BeechLane source and fast timings."""
    config = ConfigParser()
    config.read_string(
        """[service]
daemon_mode = true
reconnect_delay = 0

[leveler]
type = beechlane
adapter = hci1
address = AA:BB:CC:DD:EE:FF
topic = leveler
connect_settle_delay = 0
publish_interval = 0
sample_timeout = 1
"""
    )
    return config


def test_source_registered_and_config_validated(leveler_config: ConfigParser) -> None:
    """Verify registration and static configuration validation."""
    assert Source.source_class("beechlane") is BeechlaneSource
    BeechlaneSource.validate_config(leveler_config, "leveler")
    for key, value in (("display_unit", "furlongs"), ("status_interval", "0"), ("publish_interval", "nan"), ("address", "")):
        bad = ConfigParser()
        bad.read_dict({"leveler": {**leveler_config["leveler"], key: value}})
        with pytest.raises(ValueError):
            BeechlaneSource.validate_config(bad, "leveler")


def test_poll_once_publishes_status_and_state(leveler_config: ConfigParser, stub_bluez_link_release: list[tuple[str, str]]) -> None:
    """Verify a poll connects on the configured adapter and publishes both topics."""
    recorder = Recorder()
    client_class, state = make_client_class([b"\x01", xyz_bytes(tilted(5.0, 0.0))])
    source = BeechlaneSource(leveler_config, recorder, threading.Event(), "leveler")
    result = asyncio.run(source._poll_once(client_class, publish=True))
    assert [topic for topic, _ in recorder.items] == ["leveler/status", "leveler/state"]
    status, leveling = recorder.items[0][1], recorder.items[1][1]
    assert status["battery"] == 0x55 and status["temperature_c"] == 21.5 and status["name"] == "Leveler"
    assert status["orientation"] == "front" and status["vehicle_type"] == "class_a" and status["configured"]
    assert leveling["roll"] == 8.25 and leveling["front_right_raise"] == 8.25 and leveling["front_left_raise"] == 0.0
    assert leveling["level"] is False and leveling["unit"] == "in"
    assert result == {"status": status, "state": leveling}
    assert state["kwargs"][0]["adapter"] == "hci1" and state["exited"] == 1 and state["stopped_notify"] == 1
    assert stub_bluez_link_release == [("hci1", "AA:BB:CC:DD:EE:FF")] * 2


def test_unconfigured_device_publishes_status_only(leveler_config: ConfigParser) -> None:
    """Verify a device without geometry reports status and skips leveling math."""
    recorder = Recorder()
    client_class, _ = make_client_class([], geometry=b"", orientation=b"\x00")
    source = BeechlaneSource(leveler_config, recorder, threading.Event(), "leveler")
    result = asyncio.run(source._poll_once(client_class, publish=True))
    assert result["state"] is None and result["status"]["configured"] is False
    assert [topic for topic, _ in recorder.items] == ["leveler/status"]


def test_streaming_publishes_changes_only(leveler_config: ConfigParser) -> None:
    """Verify the daemon session suppresses duplicate states and cleans up on stop."""
    recorder = Recorder()
    stop = threading.Event()
    frames = [xyz_bytes(FLAT), xyz_bytes(FLAT), xyz_bytes(tilted(5.0, 0.0))]
    client_class, state = make_client_class(frames)
    source = BeechlaneSource(leveler_config, recorder, stop, "leveler")

    async def run() -> bool:
        """Stop the session shortly after frames are delivered."""
        asyncio.get_running_loop().call_later(0.4, stop.set)
        return await source._run_streaming_session(client_class)

    assert asyncio.run(run()) is True
    states = [payload for topic, payload in recorder.items if topic == "leveler/state"]
    assert len(states) == 1 and states[0]["roll"] == 8.25
    assert state["stopped_notify"] == 1 and state["exited"] == 1


def test_streaming_stall_raises_timeout(leveler_config: ConfigParser) -> None:
    """Verify a connected session without indications fails for reconnection."""
    leveler_config["leveler"]["notification_timeout"] = "0.2"
    client_class, state = make_client_class([])
    source = BeechlaneSource(leveler_config, Recorder(), threading.Event(), "leveler")
    with pytest.raises(TimeoutError):
        asyncio.run(source._run_streaming_session(client_class))
    assert state["exited"] == 1


def test_daemon_reconnects_after_failure(leveler_config: ConfigParser) -> None:
    """Verify a failed connection is retried and a later session streams."""
    stop = threading.Event()
    client_class, state = make_client_class([xyz_bytes(FLAT)], on_notify=stop.set, fail_first=1)
    source = BeechlaneSource(leveler_config, Recorder(), stop, "leveler")
    asyncio.run(source._run_ble(client_class))
    assert state["constructed"] == 2


def test_connect_rate_limit_waits(leveler_config: ConfigParser, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify a sixth connection within 30 seconds waits for the window."""
    source = BeechlaneSource(leveler_config, Recorder(), threading.Event(), "leveler")
    waits: list[float] = []

    def fake_wait(timeout: float) -> bool:
        """Record the wait and simulate shutdown."""
        waits.append(timeout)
        return True

    monkeypatch.setattr(source.stop_event, "wait", fake_wait)
    monkeypatch.setattr(beechlane_module.time, "monotonic", lambda: 100.0)
    for _ in range(5):
        asyncio.run(source._wait_for_connect_slot())
    with pytest.raises(RuntimeError):
        asyncio.run(source._wait_for_connect_slot())
    assert waits == [30.0]
