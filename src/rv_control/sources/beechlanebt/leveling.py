"""Leveling math for the BeechLane leveler (derived from pyrvleveler, MIT).

Pure functions that replicate the vendor app, including its Java-style
rounding (``floor(x + 0.5)``) and unit conversion constants.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .codec import Orientation, Unit, VehicleGeometry

Xyz = tuple[float, float, float]

CM_PER_INCH = 2.54
INCH_PER_CM = 0.393701  # constant used by the vendor app


def _java_round(value: float) -> int:
    """Round half up, matching Java ``Math.round``."""
    return math.floor(value + 0.5)


def _round_quarter(value: float) -> float:
    """Round to the nearest 0.25."""
    return _java_round(value * 4) / 4


def _round_half(value: float) -> float:
    """Round to the nearest 0.5."""
    return _java_round(value * 2) / 2


def _angles_raw(xyz: Xyz) -> tuple[float, float]:
    """Return sensor-frame (pitch, roll) in degrees."""
    x, y, z = xyz
    pitch = math.degrees(math.atan2(-x, math.sqrt(y * y + z * z)))
    roll = math.degrees(math.atan2(y, z))
    return pitch, roll


def angle_a(xyz: Xyz, orientation: Orientation) -> float:
    """Return the vehicle cross-width angle for a mounting orientation."""
    pitch, roll = _angles_raw(xyz)
    return {
        Orientation.FRONT: pitch,
        Orientation.REAR: -pitch,
        Orientation.DRIVER_SIDE: roll + 90.0,
        Orientation.PASSENGER_SIDE: -(roll + 90.0),
    }[orientation]


def angle_b(xyz: Xyz, orientation: Orientation) -> float:
    """Return the vehicle along-length angle for a mounting orientation."""
    pitch, roll = _angles_raw(xyz)
    return {
        Orientation.FRONT: roll + 90.0,
        Orientation.REAR: -(roll + 90.0),
        Orientation.DRIVER_SIDE: -pitch,
        Orientation.PASSENGER_SIDE: pitch,
    }[orientation]


@dataclass(frozen=True)
class Calibration:
    """Reference angles captured while the vehicle was level."""

    u: float
    v: float

    @classmethod
    def from_offset_xyz(cls, xyz: Xyz, orientation: Orientation) -> Calibration:
        """Derive reference angles from the stored calibration offset."""
        if orientation is Orientation.NOT_SET:
            return cls(0.0, 0.0)
        return cls(angle_a(xyz, orientation), angle_b(xyz, orientation))


@dataclass(frozen=True)
class LevelingState:
    """Height differences in ``unit``.

    ``corners`` is (front_left, front_right, rear_left, rear_right); each value
    is how far that corner sits below the highest corner (all <= 0).
    """

    roll: float
    pitch: float
    corners: tuple[float, float, float, float]
    unit: Unit
    roll_degrees: float
    pitch_degrees: float


def convert_dimensions(geometry: VehicleGeometry, display_unit: Unit) -> tuple[float, float]:
    """Return (length, width) converted from the stored unit to ``display_unit``."""
    length, width = float(geometry.length), float(geometry.width)
    if geometry.unit is not display_unit:
        factor = CM_PER_INCH if display_unit is Unit.CENTIMETERS else INCH_PER_CM
        length *= factor
        width *= factor
    return length, width


def compute_state(
    xyz: Xyz,
    orientation: Orientation,
    calibration: Calibration,
    geometry: VehicleGeometry,
    display_unit: Unit | None = None,
) -> LevelingState:
    """Compute live leveling values from a normalized XYZ sample."""
    if orientation is Orientation.NOT_SET:
        raise ValueError("device orientation is not set")
    unit = display_unit or geometry.unit
    length, width = convert_dimensions(geometry, unit)

    roll_degrees = angle_a(xyz, orientation) - calibration.u
    pitch_degrees = calibration.v - angle_b(xyz, orientation)
    roll = _round_quarter(math.sin(math.radians(roll_degrees)) * width)
    pitch = _round_quarter(math.sin(math.radians(pitch_degrees)) * length)
    if unit is Unit.CENTIMETERS:
        roll, pitch = _round_half(roll), _round_half(pitch)

    a = abs(roll) if roll < 0 else 0.0
    b = max(roll, 0.0)
    c = min(pitch, 0.0)
    d = -pitch if pitch >= 0 else 0.0
    corners = (d - a, d - b, c - a, c - b)
    if unit is Unit.CENTIMETERS:
        corners = tuple(_round_half(value) for value in corners)

    return LevelingState(
        roll=roll, pitch=pitch, corners=corners, unit=unit,
        roll_degrees=roll_degrees, pitch_degrees=pitch_degrees,
    )
