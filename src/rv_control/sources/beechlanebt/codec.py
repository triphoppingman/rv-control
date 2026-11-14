"""Byte-level decoding for the BeechLane leveler protocol (derived from pyrvleveler, MIT)."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum


class Orientation(IntEnum):
    """Sensor mounting position stored on the device."""

    NOT_SET = 0
    FRONT = 1
    REAR = 2
    DRIVER_SIDE = 3
    PASSENGER_SIDE = 4


class VehicleType(IntEnum):
    """Vehicle class stored with the device geometry."""

    CLASS_A = 1
    CLASS_B_C = 2
    TRAVEL_TRAILER = 3
    FIFTH_WHEEL = 4
    HYBRID_POP_UP = 5


class Unit(IntEnum):
    """Length unit for geometry and leveling values."""

    INCHES = 1
    CENTIMETERS = 2


UNIT_SYMBOLS = {Unit.INCHES: "in", Unit.CENTIMETERS: "cm"}
SENSITIVITY_DIVISORS = {0: 16383, 1: 32766, 2: 49149}
GEOMETRY_PREFIX = 1


@dataclass(frozen=True)
class VehicleGeometry:
    """Vehicle type and wheelbase/track dimensions in the stored unit."""

    vehicle_type: VehicleType
    unit: Unit
    length: int
    width: int


def parse_battery(data: bytes) -> int:
    """Return the battery percentage from a one-byte payload."""
    if not data:
        raise ValueError("empty battery payload")
    return data[0]


def parse_temperature(data: bytes) -> float:
    """Return Celsius from an int16 little-endian payload scaled by 0.01."""
    (raw,) = struct.unpack_from("<h", data)
    return round(raw * 0.01, 1)


def celsius_to_fahrenheit(celsius: float) -> float:
    """Convert Celsius to Fahrenheit, rounded to one decimal like the vendor app."""
    return round(celsius * 9 / 5 + 32, 1)


def parse_orientation(data: bytes) -> Orientation:
    """Return the mounting orientation from a one-byte payload."""
    if not data:
        raise ValueError("empty orientation payload")
    return Orientation(data[0])


def parse_sensitivity(data: bytes) -> int:
    """Return the accelerometer full-scale divisor for a sensitivity payload."""
    if not data:
        raise ValueError("empty sensitivity payload")
    try:
        return SENSITIVITY_DIVISORS[data[0]]
    except KeyError:
        raise ValueError(f"unknown sensitivity code {data[0]}") from None


def parse_geometry(data: bytes) -> VehicleGeometry | None:
    """Parse vehicle geometry; None means the device has never been set up."""
    if not data:
        return None
    prefix, vehicle_type, unit, length, width = struct.unpack_from(">HHHHH", data)
    if prefix != GEOMETRY_PREFIX:
        raise ValueError(f"unexpected geometry prefix {prefix:#06x}")
    return VehicleGeometry(VehicleType(vehicle_type), Unit(unit), length, width)


def parse_xyz_raw(data: bytes) -> tuple[int, int, int]:
    """Return raw accelerometer axes from three int16 little-endian values."""
    x, y, z = struct.unpack_from("<hhh", data)
    return x, y, z


def parse_xyz(data: bytes, divisor: int) -> tuple[float, float, float]:
    """Return accelerometer axes normalized by the sensitivity divisor."""
    x, y, z = parse_xyz_raw(data)
    return x / divisor, y / divisor, z / divisor
