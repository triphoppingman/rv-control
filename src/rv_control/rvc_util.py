"""Reusable RV-C specification, CAN, date/time, and address utilities."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

RVC_SPECFILE = str(Path(__file__).resolve().parent / "data/rvc-spec.yml")
DATE_TIME_STATUS_DGN = 0x1FFFF
SET_DATE_TIME_DGN = 0x1FFFE
RV_C_PRIORITY = 6
SOURCE_ADDRESS = 0x00
TIME_ZONE_CODES = {"GMT": 0, "UTC": 0, "EDT": 4, "EST": 5, "PDT": 7, "PST": 8}
ADDRESS_CLAIM_DGN = 0x0EE00
REQUEST_DGN = 0x0EA00
GLOBAL_ADDRESS = 0xFF
DC_DIMMER_COMMAND_DGN = 0x1FEDB
DC_DIMMER_COMMANDS = {
    "set-brightness": 0,
    "brightness": 0,
    "set-level": 0,
    "on-duration": 1,
    "on": 1,
    "on-delay": 2,
    "off": 3,
    "stop": 4,
    "toggle": 5,
    "memory-off": 6,
    "ramp-brightness": 17,
    "ramp-toggle": 18,
    "ramp-up": 19,
    "ramp-down": 20,
    "ramp-up-down": 21,
    "lock": 33,
    "unlock": 34,
    "flash": 49,
    "flash-momentarily": 50,
}
GENERATOR_COMMAND_DGN = 0x1FFDA
GENERATOR_COMMANDS = {"stop": 0, "start": 1, "manual-prime": 2, "manual-preheat": 3}
CIRCULATION_PUMP_COMMAND_DGN = 0x1FE96
PUMP_OUTPUT_MODES = {"off": 0, "on": 1}
INVERTER_COMMAND_DGN = 0x1FFD3
CHARGER_COMMAND_DGN = 0x1FFC5
DC_LOAD_COMMAND_DGN = 0x1FFBC
AC_LOAD_COMMAND_DGN = 0x1FFBE
LOAD_COMMANDS = {"set-level": 0, "on": 1, "on-delay": 2, "off": 3, "stop": 4, "toggle": 5, "memory-off": 6, "ramp-brightness": 17, "ramp-toggle": 18, "ramp-up": 19, "ramp-down": 20, "ramp-up-down": 21, "lock": 33, "unlock": 34, "flash": 49, "flash-momentarily": 50}
GENERIC_INDICATOR_COMMAND_DGN = 0x1FED9
INDICATOR_FUNCTIONS = {"set-brightness": 0, "off": 1, "on": 4, "ramp": 17, "flash": 51}
OPERATING_MODES = {"automatic": 0, "manual": 1}


@dataclass(frozen=True)
class RvcName:
    """Represent the 64-bit J1939/RV-C NAME carried by address claims."""

    identity_number: int
    manufacturer_code: int
    ecu_instance: int
    function_instance: int
    function: int
    vehicle_system: int
    vehicle_system_instance: int
    industry_group: int
    arbitrary_address_capable: bool


def dc_dimmer_payload(instance: int, group: int, level: float, command: str | int, delay_duration: int = 0, interlock: int = 0) -> bytes:
    """Build the six-byte ``DC_DIMMER_COMMAND_2`` payload from named fields.

    The specification represents brightness as a percentage in half-percent
    units, so a requested level of 50 percent is transmitted as byte value 100.
    Commands that do not use level or delay still receive those fields, as
    required by the fixed RV-C command layout.
    """
    _validate_field("instance", instance, 8)
    _validate_field("group", group, 8)
    _validate_field("delay_duration", delay_duration, 8)
    _validate_field("interlock", interlock, 2)
    encoded_level = _percent_byte(level)
    command_value = _command_value(command, DC_DIMMER_COMMANDS, "DC dimmer")
    return bytes((instance, group, encoded_level, command_value, delay_duration, interlock))


def generator_payload(command: str | int) -> bytes:
    """Build the one-byte generator command payload."""
    return bytes((_command_value(command, GENERATOR_COMMANDS, "generator"),))


def circulation_pump_payload(instance: int, output_mode: str | int) -> bytes:
    """Build the circulation-pump instance and output-mode payload."""
    _validate_field("instance", instance, 8)
    mode = _command_value(output_mode, PUMP_OUTPUT_MODES, "circulation pump output")
    return bytes((instance, mode))


def inverter_payload(instance: int, inverter_enable: bool, load_sense_enable: bool, pass_through_enable: bool, enable_on_startup: bool = False, load_sense_on_startup: bool = False, pass_through_on_startup: bool = False) -> bytes:
    """Build the eight-byte inverter command payload from boolean controls."""
    _validate_field("instance", instance, 8)
    active = 0
    active = _set_bits(active, "inverter_enable", int(inverter_enable), 0, 2)
    active = _set_bits(active, "load_sense_enable", int(load_sense_enable), 2, 2)
    active = _set_bits(active, "pass_through_enable", int(pass_through_enable), 4, 2)
    startup = 0
    startup = _set_bits(startup, "enable_on_startup", int(enable_on_startup), 0, 2)
    startup = _set_bits(startup, "load_sense_on_startup", int(load_sense_on_startup), 2, 2)
    startup = _set_bits(startup, "pass_through_on_startup", int(pass_through_on_startup), 4, 2)
    return bytes((instance, active, 0, 0, 0, 0, 0, startup))


def charger_payload(instance: int, status: str | int, default_on: bool = False, auto_recharge: bool = False, force_charge: str | int = 0) -> bytes:
    """Build the three-byte charger command payload from readable controls."""
    _validate_field("instance", instance, 8)
    statuses = {"disable": 0, "enable": 1, "equalize": 2}
    force_modes = {"cancel": 0, "bulk": 1, "float": 2}
    status_value = _command_value(status, statuses, "charger status")
    force_value = force_charge if isinstance(force_charge, int) else force_modes.get(force_charge.lower())
    if force_value is None:
        raise ValueError(f"unknown charger force mode: {force_charge}")
    _validate_field("force_charge", force_value, 4)
    flags = int(default_on) | (int(auto_recharge) << 2) | (force_value << 4)
    return bytes((instance, status_value, flags))


def load_payload(instance: int, group: int, level: float, operating_mode: str | int, interlock: int, command: str | int, delay_duration: int, priority: int | None = None) -> bytes:
    """Build a DC or AC load command payload with shared field semantics."""
    _validate_field("instance", instance, 8)
    _validate_field("group", group, 8)
    if isinstance(operating_mode, str):
        operating_mode = _command_value(operating_mode, OPERATING_MODES, "load operating mode")
    _validate_field("operating_mode", operating_mode, 2)
    _validate_field("interlock", interlock, 2)
    _validate_field("delay_duration", delay_duration, 8)
    command_value = _command_value(command, LOAD_COMMANDS, "load")
    control = operating_mode | (interlock << 2)
    if priority is not None:
        _validate_field("priority", priority, 4)
        control |= priority << 4
    values = [instance, group, _percent_byte(level), control, command_value, delay_duration]
    return bytes(values)


def indicator_payload(instance: int, group: int, brightness: float, bank: int, duration: int, function: str | int) -> bytes:
    """Build the seven-byte generic indicator command payload."""
    _validate_field("instance", instance, 8)
    _validate_field("group", group, 8)
    _validate_field("bank", bank, 4)
    _validate_field("duration", duration, 8)
    function_value = _command_value(function, INDICATOR_FUNCTIONS, "indicator")
    return bytes((instance, group, _percent_byte(brightness, "brightness"), bank, duration, 0, function_value))


def _validate_field(name: str, value: int, width: int) -> int:
    """Validate one unsigned bit-field before it is packed into a NAME."""
    if not isinstance(value, int) or not 0 <= value < (1 << width):
        raise ValueError(f"{name} must fit in {width} bits: {value}")
    return value


def _set_bits(value: int, field_name: str, field_value: int, offset: int, width: int) -> int:
    """Validate and insert one bit-field into an integer payload value."""
    return value | (_validate_field(field_name, field_value, width) << offset)


def _command_value(command: str | int, commands: dict[str, int], label: str) -> int:
    """Resolve a named or numeric command code for a protocol command."""
    if isinstance(command, str):
        if command.lower() in commands:
            return commands[command.lower()]
        try:
            command = int(command, 0)
        except ValueError as error:
            raise ValueError(f"unknown {label} command: {command}") from error
    return _validate_field(f"{label} command", command, 8)


def _percent_byte(level: float, field_name: str = "level") -> int:
    """Convert a percentage to the RV-C half-percent byte representation."""
    if not isinstance(level, (int, float)) or not 0 <= level <= 100:
        raise ValueError(f"{field_name} must be between 0 and 100 percent: {level}")
    return round(float(level) * 2)


def encode_rvc_name(name: RvcName) -> bytes:
    """Encode an RV-C/J1939 NAME into its eight-byte little-endian payload."""
    value = 0
    fields = (
        ("identity_number", name.identity_number, 0, 21),
        ("manufacturer_code", name.manufacturer_code, 21, 11),
        ("ecu_instance", name.ecu_instance, 32, 3),
        ("function_instance", name.function_instance, 35, 5),
        ("function", name.function, 40, 8),
        ("vehicle_system_instance", name.vehicle_system_instance, 56, 4),
        ("industry_group", name.industry_group, 60, 3),
    )
    for field_name, field_value, offset, width in fields:
        value |= _validate_field(field_name, field_value, width) << offset
    value |= _validate_field("vehicle_system", name.vehicle_system, 7) << 49
    if name.arbitrary_address_capable:
        value |= 1 << 63
    return value.to_bytes(8, "little")


def decode_rvc_name(payload: bytes | bytearray) -> RvcName:
    """Decode an eight-byte little-endian RV-C/J1939 NAME payload."""
    if len(payload) != 8:
        raise ValueError(f"RV-C NAME must contain 8 bytes: {len(payload)}")
    value = int.from_bytes(payload, "little")
    return RvcName(
        identity_number=value & 0x1FFFFF,
        manufacturer_code=(value >> 21) & 0x7FF,
        ecu_instance=(value >> 32) & 0x7,
        function_instance=(value >> 35) & 0x1F,
        function=(value >> 40) & 0xFF,
        vehicle_system=(value >> 49) & 0x7F,
        vehicle_system_instance=(value >> 56) & 0xF,
        industry_group=(value >> 60) & 0x7,
        arbitrary_address_capable=bool((value >> 63) & 1),
    )


def rvc_name_details(name: RvcName) -> dict[str, Any]:
    """Return address-claim NAME fields in a display- and JSON-friendly mapping."""
    return {
        "identity_number": name.identity_number,
        "manufacturer_code": name.manufacturer_code,
        "ecu_instance": name.ecu_instance,
        "function_instance": name.function_instance,
        "function": name.function,
        "vehicle_system": name.vehicle_system,
        "vehicle_system_instance": name.vehicle_system_instance,
        "industry_group": name.industry_group,
        "arbitrary_address_capable": name.arbitrary_address_capable,
    }


def build_can_id(priority: int, dgn: int, source: int) -> int:
    """Build and validate an RV-C extended 29-bit identifier."""
    if not 0 <= priority <= 0x7:
        raise ValueError(f"RV-C priority must be between 0 and 7: {priority}")
    if not 0 <= dgn <= 0x1FFFF:
        raise ValueError(f"RV-C DGN must fit 17 bits: {dgn}")
    if not 0 <= source <= 0xFF:
        raise ValueError(f"RV-C source address must fit 8 bits: {source}")
    return (priority << 26) | (dgn << 8) | source


def normalize_can_payload(payload: bytes | bytearray | list[int] | tuple[int, ...]) -> bytes:
    """Validate and normalize a classic CAN payload into immutable bytes."""
    try:
        data = bytes(payload)
    except (TypeError, ValueError) as error:
        raise ValueError("CAN payload must contain byte values") from error
    if len(data) > 8:
        raise ValueError(f"CAN payload cannot exceed 8 bytes: {len(data)}")
    return data


def build_can_message(priority: int, dgn: int, source: int, payload: bytes | bytearray | list[int] | tuple[int, ...]) -> Any:
    """Create a validated extended CAN message for an RV-C transmission."""
    import can

    return can.Message(arbitration_id=build_can_id(priority, dgn, source), data=normalize_can_payload(payload), is_extended_id=True)


def send_can_message(bus: Any, priority: int, dgn: int, source: int, payload: bytes | bytearray | list[int] | tuple[int, ...]) -> Any:
    """Build and send one validated RV-C message, returning the sent message."""
    message = build_can_message(priority, dgn, source, payload)
    bus.send(message)
    return message


def build_dc_dimmer_message(instance: int, group: int, level: float, command: str | int, delay_duration: int = 0, interlock: int = 0, priority: int = RV_C_PRIORITY, source: int = SOURCE_ADDRESS) -> Any:
    """Create a validated ``DC_DIMMER_COMMAND_2`` message from readable fields."""
    return build_can_message(priority, DC_DIMMER_COMMAND_DGN, source, dc_dimmer_payload(instance, group, level, command, delay_duration, interlock))


def load_spec(specfile: str = RVC_SPECFILE) -> dict[str, Any]:
    """Load and validate an RV-C YAML specification."""
    from ruamel.yaml import YAML

    with Path(specfile).open(encoding="utf-8") as handle:
        spec = YAML(typ="safe").load(handle)
    if not isinstance(spec, dict):
        raise ValueError(f"RV-C specification is not a mapping: {specfile}")
    return spec


def spec_dgn(spec: dict[str, Any], identifier: str) -> tuple[int, str, dict[str, Any]]:
    """Resolve a numeric DGN or specification name to its definition."""
    normalized = identifier.strip().upper()
    if normalized in spec and isinstance(spec[normalized], dict):
        return int(normalized, 16), normalized, spec[normalized]
    for key, definition in spec.items():
        if isinstance(definition, dict) and str(definition.get("name", "")).upper() == normalized:
            return int(str(key), 16), str(key).upper(), definition
    raise ValueError(f"RV-C DGN not found in specification: {identifier}")


def spec_payload_length(spec: dict[str, Any], dgn: str) -> int | None:
    """Return the minimum payload length implied by a DGN's byte definitions."""
    definition = spec.get(dgn)
    if not isinstance(definition, dict):
        raise ValueError(f"RV-C DGN not found in specification: {dgn}")
    parameters = list(spec.get(definition.get("alias"), {}).get("parameters", [])) + list(definition.get("parameters", []))
    byte_ends = []
    for parameter in parameters:
        byte_range = parameter.get("byte")
        if byte_range is None:
            continue
        byte_ends.extend([byte_range] if isinstance(byte_range, int) else [int(part) for part in str(byte_range).split("-")])
    return max(byte_ends) + 1 if byte_ends else None


def build_spec_message(spec: dict[str, Any], identifier: str, priority: int, source: int, payload: bytes | bytearray | list[int] | tuple[int, ...]) -> Any:
    """Build a validated RV-C message for a DGN defined by the specification."""
    dgn, dgn_text, _definition = spec_dgn(spec, identifier)
    data = normalize_can_payload(payload)
    required_length = spec_payload_length(spec, dgn_text)
    if required_length is not None and len(data) < required_length:
        raise ValueError(f"payload for DGN {dgn_text} requires at least {required_length} bytes: {len(data)}")
    return build_can_message(priority, dgn, source, data)


def send_spec_message(bus: Any, spec: dict[str, Any], identifier: str, priority: int, source: int, payload: bytes | bytearray | list[int] | tuple[int, ...]) -> Any:
    """Validate and send a specification-defined RV-C message."""
    message = build_spec_message(spec, identifier, priority, source, payload)
    bus.send(message)
    return message


def dgn_from_can_id(arbitration_id: int) -> int:
    """Extract the RV-C DGN from a 29-bit CAN identifier."""
    return (arbitration_id >> 8) & 0x1FFFF


def decode_dgn(dgn: str, data: str, spec: dict[str, Any], parameterized: bool = True) -> dict[str, Any]:
    """Decode an RV-C DGN payload using its specification into named fields."""
    decoder = spec.get(dgn)
    result = {"dgn": dgn, "data": data, "name": "UNKNOWN-" + dgn}
    if not decoder:
        return result
    result["name"] = decoder.get("name", result["name"])
    params = list(spec.get(decoder.get("alias"), {}).get("parameters", [])) + list(decoder.get("parameters", []))
    for parameter in params:
        name = parameter.get("name", "field")
        if parameterized:
            name = name.translate(str.maketrans(" /", "__", "() ")).lower()
        byte_range = parameter.get("byte")
        try:
            if isinstance(byte_range, int):
                raw = data[byte_range * 2:(byte_range + 1) * 2]
            else:
                first, last = (int(part) for part in str(byte_range).split("-"))
                raw = "".join(data[index * 2:index * 2 + 2] for index in range(last, first - 1, -1))
            value = int(raw, 16)
            bit_range = parameter.get("bit")
            if bit_range is not None:
                if isinstance(bit_range, int):
                    value = (value >> bit_range) & 1
                else:
                    low, high = (int(part) for part in str(bit_range).split("-"))
                    value = (value >> low) & ((1 << (high - low + 1)) - 1)
            unit = str(parameter.get("unit", "")).lower()
            kind = parameter.get("type", "")
            if unit == "pct" and value != 255:
                value /= 2
            elif unit == "v" and kind == "uint16":
                value = round(value * 0.05, 2)
            elif unit == "a" and kind == "uint16":
                value = round(value * 0.05 - 1600, 2)
            elif unit == "deg c" and kind == "uint8":
                value -= 40
            result[name] = value
        except (TypeError, ValueError, IndexError):
            continue
    return result


def decode_message(message: Any, spec: dict[str, Any], parameterized: bool = True) -> dict[str, Any] | None:
    """Decode an extended CAN message into RV-C metadata and values."""
    if not message.is_extended_id:
        return None
    dgn = f"{dgn_from_can_id(int(message.arbitration_id)):05X}"
    source = f"{int(message.arbitration_id) & 0xFF:02X}"
    decoded = decode_dgn(dgn, bytes(message.data).hex().upper(), spec, parameterized)
    return {"dgn": dgn, "source": source, **decoded}


def format_message(message: Any, decoded: dict[str, Any] | None) -> str:
    """Format one CAN message with candump-style framing and decoded values."""
    timestamp = f"{message.timestamp:.6f}"
    can_id = f"{message.arbitration_id:08X}" if message.is_extended_id else f"{message.arbitration_id:03X}"
    data = bytes(message.data).hex(" ").upper()
    prefix = f"({timestamp}) {can_id} [{message.dlc}] {data}"
    if decoded is None:
        return f"{prefix} | non-RV-C frame"
    values = {key: value for key, value in decoded.items() if key not in {"dgn", "data", "name"}}
    fields = " ".join(f"{key}={value}" for key, value in values.items())
    suffix = f"{decoded['dgn']} {decoded['name']}"
    if fields:
        suffix += f" | {fields}"
    return f"{prefix} | {suffix}"


def decode_datetime(message: Any, spec: dict[str, Any]) -> dict[str, Any] | None:
    """Decode an RV-C date/time status message, or return None for other frames."""
    if not message.is_extended_id or dgn_from_can_id(message.arbitration_id) != DATE_TIME_STATUS_DGN:
        return None
    data = bytes(message.data)
    if len(data) != 8:
        raise ValueError(f"date/time status must contain 8 bytes, received {len(data)}")
    return decode_dgn(f"{DATE_TIME_STATUS_DGN:05X}", data.hex().upper(), spec, True)


def format_datetime(decoded: dict[str, Any]) -> str:
    """Format decoded RV-C date/time fields for console output."""
    return (f"{int(decoded['year']) + 2000:04d}-{int(decoded['month']):02d}-{int(decoded['date']):02d} "
            f"{int(decoded['hour']):02d}:{int(decoded['minute']):02d}:{int(decoded['second']):02d} "
            f"(timezone code {decoded.get('timezone', 'unknown')})")


def datetime_payload(now: datetime) -> list[int]:
    """Encode a host datetime as an RV-C date/time command payload."""
    year = now.year - 2000
    if not 0 <= year <= 255:
        raise ValueError(f"year is outside the RV-C range: {now.year}")
    timezone = TIME_ZONE_CODES.get(now.tzname() or "", 0)
    return [year, now.month, now.day, (now.isoweekday() % 7) + 1, now.hour, now.minute, now.second, timezone]


def command_can_id() -> int:
    """Build the extended CAN identifier for the RV-C date/time command."""
    return build_can_id(RV_C_PRIORITY, SET_DATE_TIME_DGN, SOURCE_ADDRESS)


def is_address_claim(message: Any) -> bool:
    """Return whether a CAN message is an RV-C address-claim announcement."""
    return bool(message.is_extended_id and dgn_from_can_id(message.arbitration_id) == ADDRESS_CLAIM_DGN)


def address_claim_message(message: Any) -> tuple[int, RvcName] | None:
    """Return the claimed source address and NAME from an address-claim frame."""
    if not is_address_claim(message):
        return None
    return message.arbitration_id & 0xFF, decode_rvc_name(bytes(message.data))


def address_claim_payload(name: RvcName) -> bytes:
    """Build the payload for an RV-C Address Claimed announcement."""
    return encode_rvc_name(name)


def build_address_claim_message(source: int, name: RvcName, priority: int = RV_C_PRIORITY) -> Any:
    """Create an extended CAN message announcing one device's claimed address."""
    return build_can_message(priority, ADDRESS_CLAIM_DGN, source, address_claim_payload(name))


def address_claim_request_payload() -> bytes:
    """Build a global Request payload asking devices to announce their claims."""
    return REQUEST_DGN.to_bytes(3, "little")


def build_address_claim_request_message(source: int, priority: int = RV_C_PRIORITY) -> Any:
    """Create a global RV-C request message for address-claim announcements."""
    return build_can_message(priority, REQUEST_DGN, source, address_claim_request_payload())
