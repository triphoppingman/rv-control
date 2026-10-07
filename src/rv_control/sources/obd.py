"""Read-only OBD-II PID monitoring through an ELM327 Bluetooth RFCOMM adapter."""

from __future__ import annotations

import ast
import logging
import re
import socket
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from types import CodeType
from typing import Any, Callable, Iterator

from ..bluetooth import BluetoothAdapterRegistry
from .base import Source


LOGGER = logging.getLogger(__name__)

READ_ONLY_MODES = frozenset({0x01, 0x02, 0x03, 0x05, 0x06, 0x07, 0x09, 0x0A, 0x21, 0x22})
ERROR_RESPONSES = (
    "NO DATA", "?", "UNABLE TO CONNECT", "CAN ERROR", "BUS ERROR", "BUS BUSY", "BUS INIT: ...ERROR",
    "STOPPED", "ERROR", "DATA ERROR", "BUFFER FULL", "FB ERROR", "LV RESET", "ACT ALERT", "<RX ERROR",
)
IGNORED_PREFIXES = ("SEARCHING", "BUS INIT")
DECODE_FUNCTIONS: dict[str, Callable[..., Any]] = {"abs": abs, "min": min, "max": max, "round": round, "int": int, "float": float}
_BYTE_NAME = re.compile(r"^b\d{1,3}$")
_HEX = re.compile(r"^[0-9A-F]*$")
_MAC = re.compile(r"^[0-9A-F]{2}(:[0-9A-F]{2}){5}$")
_FRAME_LINE = re.compile(r"^([0-9A-F]):([0-9A-F]+)$")
_ALLOWED_NODES = (
    ast.Expression, ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare, ast.IfExp, ast.Call, ast.Name, ast.Load, ast.Constant,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow, ast.LShift, ast.RShift, ast.BitAnd, ast.BitOr,
    ast.BitXor, ast.Invert, ast.UAdd, ast.USub, ast.Not, ast.And, ast.Or, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
)


class ObdError(Exception):
    """Base error for OBD adapter and ECU failures."""


class ObdNoData(ObdError):
    """Raised when one PID request produces no usable data."""


class EcuUnavailable(ObdError):
    """Raised when the adapter is reachable but the vehicle ECU is not responding."""


def resolve_adapter_address(adapter: str) -> str:
    """Return the local Bluetooth address for an adapter name such as hci0, or a literal MAC."""
    value = adapter.strip().upper()
    if _MAC.match(value):
        return value
    match = re.fullmatch(r"hci(\d{1,3})", adapter.strip())
    if not match:
        raise ValueError(f"Bluetooth adapter must be hciN or a MAC address, got {adapter!r}")
    import fcntl
    import struct

    hci_get_dev_info = 0x800448D3
    info = bytearray(128)
    struct.pack_into("<H", info, 0, int(match.group(1)))
    hci = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_RAW, socket.BTPROTO_HCI)
    try:
        fcntl.ioctl(hci, hci_get_dev_info, info, True)
    except OSError as error:
        raise OSError(f"Bluetooth adapter {adapter} not found: {error}") from error
    finally:
        hci.close()
    return ":".join(f"{byte:02X}" for byte in reversed(info[10:16]))


def compile_decode(expression: str) -> CodeType:
    """Validate a PID decode expression against a safe arithmetic subset and compile it."""
    try:
        tree = ast.parse(expression.strip(), mode="eval")
    except SyntaxError as error:
        raise ValueError(f"invalid decode expression {expression!r}: {error.msg}") from error
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise ValueError(f"decode expression {expression!r} uses unsupported syntax: {type(node).__name__}")
        if isinstance(node, ast.Constant) and (isinstance(node.value, bool) or not isinstance(node.value, (int, float))):
            raise ValueError(f"decode expression {expression!r} may only contain numeric constants")
        if isinstance(node, ast.Name) and not (_BYTE_NAME.match(node.id) or node.id in DECODE_FUNCTIONS or node.id == "BARO"):
            raise ValueError(f"decode expression {expression!r} references unknown name {node.id!r}")
        if isinstance(node, ast.Call) and (not isinstance(node.func, ast.Name) or node.func.id not in DECODE_FUNCTIONS or node.keywords):
            raise ValueError(f"decode expression {expression!r} may only call {', '.join(sorted(DECODE_FUNCTIONS))}")
    return compile(tree, "<obd-decode>", "eval")


def parse_mode(value: str) -> int:
    """Parse a hexadecimal OBD mode and reject services that are not read-only."""
    text = value.strip().upper().removeprefix("0X")
    if not text or len(text) > 2 or not _HEX.match(text):
        raise ValueError(f"invalid OBD mode {value!r}")
    mode = int(text, 16)
    if mode not in READ_ONLY_MODES:
        allowed = ", ".join(f"{item:02X}" for item in sorted(READ_ONLY_MODES))
        raise ValueError(f"OBD mode {mode:02X} is not a read-only mode ({allowed})")
    return mode


def parse_pid_hex(value: str) -> str:
    """Parse and normalize the hexadecimal PID bytes that follow the mode byte."""
    text = value.strip().upper().removeprefix("0X")
    if len(text) % 2 or len(text) > 6 or not _HEX.match(text):
        raise ValueError(f"invalid OBD PID {value!r}; expected 0 to 3 hexadecimal bytes")
    return text


def validate_request(request: str) -> tuple[int, str]:
    """Validate a raw read-only OBD request such as 010C and return its mode and PID."""
    text = request.strip().upper().replace(" ", "")
    if len(text) < 2:
        raise ValueError("OBD request must start with a two-digit hexadecimal mode")
    return parse_mode(text[:2]), parse_pid_hex(text[2:])


@dataclass(frozen=True)
class ObdPid:
    """One configured PID with its precompiled decode expression."""

    name: str
    mode: int
    pid: str
    expression: str
    unit: str
    code: CodeType
    header: str | None = None

    @classmethod
    def parse(cls, name: str, value: str) -> ObdPid:
        """Parse a PID entry with an optional per-PID CAN header."""
        fields = value.split(",")
        if len(fields) < 4:
            raise ValueError(f"pid.{name} must be 'mode, pid, decode, unit'")
        optional_header = fields[-1].strip().upper()
        has_header = len(optional_header) in (3, 6, 8) and bool(_HEX.match(optional_header))
        expression = ",".join(fields[2:-2] if has_header else fields[2:-1]).strip()
        if not expression:
            raise ValueError(f"pid.{name} requires a decode expression")
        unit = fields[-2].strip() if has_header else fields[-1].strip()
        return cls(name, parse_mode(fields[0]), parse_pid_hex(fields[1]), expression, unit, compile_decode(expression), optional_header if has_header else None)

    @property
    def request(self) -> str:
        """Return the ELM327 request string for this PID."""
        return f"{self.mode:02X}{self.pid}"

    def decode(self, data: bytes, context: dict[str, int | float] | None = None) -> int | float:
        """Evaluate the decode expression with response bytes and optional named values."""
        variables = {f"b{index}": value for index, value in enumerate(data)}
        variables.update(context or {})
        try:
            value = eval(self.code, {"__builtins__": {}, **DECODE_FUNCTIONS}, variables)  # noqa: S307 - AST validated
        except NameError as error:
            raise ObdNoData(f"short response ({len(data)} data bytes)") from error
        except (ArithmeticError, TypeError, ValueError) as error:
            raise ObdNoData(f"decode failed: {error}") from error
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ObdNoData(f"decode produced non-numeric value {value!r}")
        return round(value, 6) if isinstance(value, float) else value


def extract_data(lines: list[str], mode: int, pid: str) -> bytes:
    """Return the data bytes that follow the echoed mode and PID in an ELM327 response."""
    if not lines:
        raise ObdNoData("empty response")
    for line in lines:
        if line in ERROR_RESPONSES or line.startswith(("UNABLE", "ERROR")) or line.endswith("ERROR"):
            raise ObdNoData(line)
    frames = [match for match in (_FRAME_LINE.match(line) for line in lines) if match]
    if frames:
        candidates = ["".join(match.group(2) for match in frames)]
    else:
        candidates = [line for line in lines if _HEX.match(line) and len(line) % 2 == 0]
    prefix = bytes([mode + 0x40]) + bytes.fromhex(pid)
    negative = bytes([0x7F, mode])
    negative_code: int | None = None
    for candidate in candidates:
        payload = bytes.fromhex(candidate)
        if payload.startswith(prefix):
            return payload[len(prefix):]
        if payload.startswith(negative) and len(payload) >= 3:
            negative_code = payload[2]
    if negative_code is not None:
        raise ObdNoData(f"negative response code 0x{negative_code:02X}")
    raise ObdNoData(f"unexpected response {' '.join(lines)!r}")


class Elm327:
    """Line-oriented ELM327 command channel over a connected stream socket."""

    def __init__(self, connection: Any, read_timeout: float = 5.0, stop_event: threading.Event | None = None) -> None:
        """Wrap a connected socket and configure short reads for stop responsiveness."""
        self.connection = connection
        self.read_timeout = read_timeout
        self.stop_event = stop_event
        self._stale = False
        self.connection.settimeout(0.25)

    def _discard_stale(self) -> None:
        """Drop late bytes left behind by a previously timed-out command."""
        deadline = time.monotonic() + 0.5
        while self._stale and time.monotonic() < deadline:
            try:
                data = self.connection.recv(1024)
            except (socket.timeout, TimeoutError):
                break
            if not data:
                raise ConnectionError("ELM327 closed the connection")
            if b">" in data:
                break
        self._stale = False

    def command(self, command: str, timeout: float | None = None) -> list[str]:
        """Send one command and return the response lines received before the > prompt."""
        self._discard_stale()
        self.connection.sendall(f"{command}\r".encode("ascii"))
        buffer = bytearray()
        deadline = time.monotonic() + (timeout or self.read_timeout)
        while b">" not in buffer:
            if self.stop_event is not None and self.stop_event.is_set():
                raise ConnectionAbortedError("source is stopping")
            if time.monotonic() >= deadline:
                self._stale = True
                raise TimeoutError(f"ELM327 did not answer {command!r}")
            try:
                data = self.connection.recv(1024)
            except (socket.timeout, TimeoutError):
                continue
            if not data:
                raise ConnectionError("ELM327 closed the connection")
            buffer.extend(data)
        text = bytes(buffer[:buffer.index(b">")]).replace(b"\0", b"").decode("ascii", errors="ignore")
        lines = []
        for line in re.split(r"[\r\n]+", text):
            line = line.strip().upper()
            if not line or line == command.upper() or line.startswith(IGNORED_PREFIXES):
                continue
            lines.append(line.replace(" ", "") if _HEX.match(line.replace(" ", "")) else line)
        return lines

    def close(self) -> None:
        """Close the underlying socket, ignoring errors from an already-dead link."""
        try:
            self.connection.close()
        except OSError:
            pass


class ObdSource(Source, source_name="obd"):
    """Poll configured OBD-II PIDs from an ELM327 Bluetooth adapter and publish them read-only."""

    source_name = "obd"
    config_section = "obd"

    def __init__(self, config: Any, publisher: Any, stop_event: Any, section_name: str | None = None) -> None:
        """Initialize an OBD source without contacting the adapter."""
        super().__init__(config, publisher, stop_event, section_name)
        self.connection_factory: Callable[[], Any] = self._open_rfcomm
        self._pids: tuple[ObdPid, ...] | None = None
        self._online: bool | None = None
        self._failing_pids: set[str] = set()

    @property
    def pids(self) -> tuple[ObdPid, ...]:
        """Return configured PIDs, parsing and validating them once per source instance."""
        if self._pids is None:
            pids = tuple(ObdPid.parse(key[4:], value) for key, value in self.section.items() if key.startswith("pid."))
            if not pids:
                raise ValueError(f"[{self.section_name}] requires at least one pid.<name> entry")
            self._pids = pids
        return self._pids

    def attribute_inventory(self) -> list[dict[str, str]]:
        """Describe the configured PIDs as read-only attributes."""
        return [{"attribute": pid.name, "unit": pid.unit, "access": "read"} for pid in self.pids]

    def _address(self) -> tuple[str, int]:
        """Return the validated adapter Bluetooth address and RFCOMM channel."""
        address = self.section.get("address", "").strip().upper()
        if not _MAC.match(address):
            raise ValueError(f"[{self.section_name}] address must be a Bluetooth MAC address")
        channel = self.section.getint("channel", fallback=1)
        if not 1 <= channel <= 30:
            raise ValueError(f"[{self.section_name}] channel must be between 1 and 30")
        return address, channel

    def _protocol(self, name: str, default: str) -> str:
        """Return a validated ELM327 protocol number (0 through C) or blank."""
        value = self.section.get(name, default).strip().upper()
        if value and (len(value) != 1 or value not in "0123456789ABC"):
            raise ValueError(f"[{self.section_name}] {name} must be an ELM327 protocol 0 through C")
        return value

    def _header(self) -> str:
        """Return the validated CAN request header or blank for the adapter default."""
        header = self.section.get("header", "").strip().upper()
        if header and (len(header) not in (3, 6, 8) or not _HEX.match(header)):
            raise ValueError(f"[{self.section_name}] header must be 3, 6, or 8 hexadecimal digits")
        return header

    def _poll_interval(self) -> float:
        """Return the poll cycle interval in seconds from poll_hz."""
        poll_hz = self.section.getfloat("poll_hz", fallback=2.0)
        if poll_hz <= 0:
            raise ValueError(f"[{self.section_name}] poll_hz must be greater than zero")
        return 1.0 / poll_hz

    def _retry_setting(self, name: str, default: int | float, value_type: Callable[[str], int | float]) -> int | float:
        """Return a source retry override or the corresponding service default."""
        value = self.section.get(name, "").strip()
        if value:
            return value_type(value)
        return value_type(self.config["service"].get(name, str(default)))

    def _topic(self) -> str:
        """Return the MQTT topic below the base topic for PID snapshots."""
        return self.section.get("topic", self.section_name).strip("/")

    def _open_rfcomm(self) -> socket.socket:
        """Open a raw RFCOMM socket to the adapter while holding the shared adapter connect slot."""
        if not hasattr(socket, "AF_BLUETOOTH") or not hasattr(socket, "BTPROTO_RFCOMM"):
            raise OSError("this Python build does not support Bluetooth RFCOMM sockets")
        address, channel = self._address()
        adapter = self.section.get("adapter", "hci0").strip() or "hci0"
        local_address = resolve_adapter_address(adapter)
        connection = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
        try:
            connection.settimeout(self.section.getfloat("connect_timeout", fallback=10.0))
            connection.bind((local_address, 0))
            with BluetoothAdapterRegistry.for_adapter(adapter).connect_slot_sync():
                connection.connect((address, channel))
        except BaseException:
            connection.close()
            raise
        return connection

    def connect(self) -> Elm327:
        """Open a fresh ELM327 command channel to the configured adapter."""
        self._address()
        return Elm327(self.connection_factory(), self.section.getfloat("read_timeout", fallback=5.0), self.stop_event)

    def initialize(self, elm: Elm327) -> dict[str, Any]:
        """Reset and configure the adapter, then probe the ECU and return adapter details."""
        reset = elm.command("ATZ")
        adapter = next((line for line in reset if "ELM" in line), reset[-1] if reset else "unknown")
        commands = ["ATE0", "ATL0", "ATS0", "ATH0", f"ATSP{self._protocol('protocol', '0') or '0'}"]
        header = self._header()
        if header:
            commands.append(f"ATSH{header}")
        for command in commands:
            reply = elm.command(command)
            if "OK" not in reply:
                raise ConnectionError(f"ELM327 rejected {command}: {' '.join(reply) or 'no reply'}")
        voltage = " ".join(elm.command("ATRV"))
        info = {"adapter": adapter, "voltage": voltage, "protocol": None, "ecu_online": False, "message": ""}
        try:
            self._probe_ecu(elm)
        except ObdNoData as error:
            fallback = self._protocol("fallback_protocol", "")
            if not fallback:
                info["message"] = f"ECU not responding ({error})"
                return info
            LOGGER.info("OBD %s: automatic protocol failed (%s); trying protocol %s", self.section_name, error, fallback)
            if "OK" not in elm.command(f"ATSP{fallback}"):
                raise ConnectionError(f"ELM327 rejected ATSP{fallback}")
            try:
                self._probe_ecu(elm)
            except ObdNoData as fallback_error:
                info["message"] = f"ECU not responding ({fallback_error})"
                return info
        info["protocol"] = " ".join(elm.command("ATDP"))
        info["ecu_online"] = True
        info["message"] = "ECU responding"
        return info

    @staticmethod
    def _probe_ecu(elm: Elm327) -> None:
        """Request mode 01 PID 00 to establish the bus protocol and confirm the ECU answers."""
        extract_data(elm.command("0100", timeout=max(elm.read_timeout, 10.0)), 0x01, "00")

    @contextmanager
    def session(self) -> Iterator[tuple[Elm327, dict[str, Any]]]:
        """Yield an initialized adapter channel and its details, always closing the socket."""
        elm = self.connect()
        try:
            yield elm, self.initialize(elm)
        finally:
            elm.close()

    @staticmethod
    def query(elm: Elm327, pid: ObdPid, context: dict[str, int | float] | None = None) -> int | float:
        """Request and decode one PID, raising ObdNoData when no usable value is returned."""
        return pid.decode(extract_data(elm.command(pid.request), pid.mode, pid.pid), context)

    def read_cycle(self, elm: Elm327) -> tuple[dict[str, int | float | None], dict[str, str]]:
        """Query every configured PID sequentially, recording N/A results without stopping."""
        values: dict[str, int | float | None] = {}
        errors: dict[str, str] = {}
        current_header = self._header()
        for pid in self.pids:
            try:
                target_header = pid.header or self._header()
                if target_header and target_header != current_header:
                    if "OK" not in elm.command(f"ATSH{target_header}"):
                        raise ObdNoData(f"ELM327 rejected ATSH{target_header}")
                    current_header = target_header
                context: dict[str, int | float] = {}
                if pid.name == "boost" and isinstance(values.get("baro"), (int, float)):
                    context["BARO"] = values["baro"]
                values[pid.name] = self.query(elm, pid, context)
            except (ObdNoData, TimeoutError) as error:
                values[pid.name] = None
                errors[pid.name] = str(error)
        self._log_pid_transitions(errors)
        return values, errors

    def _log_pid_transitions(self, errors: dict[str, str]) -> None:
        """Log PIDs when they start or stop returning N/A instead of on every cycle."""
        for name, message in errors.items():
            if name not in self._failing_pids:
                LOGGER.warning("OBD %s PID %s: N/A (%s)", self.section_name, name, message)
            else:
                LOGGER.debug("OBD %s PID %s: N/A (%s)", self.section_name, name, message)
        for name in self._failing_pids - set(errors):
            LOGGER.info("OBD %s PID %s responding again", self.section_name, name)
        self._failing_pids = set(errors)

    def supported_pids(self, elm: Elm327) -> list[str]:
        """Return mode 01 PIDs the ECU reports as supported through the 0x00/0x20/... bitmaps."""
        supported = []
        for base in range(0x00, 0xE0, 0x20):
            try:
                data = extract_data(elm.command(f"01{base:02X}"), 0x01, f"{base:02X}")
            except ObdNoData:
                break
            bitmap = int.from_bytes(data[:4].ljust(4, b"\0"), "big")
            supported.extend(f"{base + bit + 1:02X}" for bit in range(32) if bitmap & (1 << (31 - bit)))
            if not bitmap & 1:
                break
        return supported

    def comms_check(self) -> dict[str, Any]:
        """Connect fresh to the adapter, initialize it, and report adapter and ECU status."""
        attributes: list[dict[str, str]] = []
        try:
            attributes = self.attribute_inventory()
            with self.session() as (_elm, info):
                message = f"{info['adapter']} reachable ({info['voltage'] or 'voltage unknown'}); {info['message']}"
                return {"ok": True, "message": message, "attributes": attributes}
        except (OSError, ValueError, ObdError) as error:
            return {"ok": False, "message": f"OBD check failed: {error}", "attributes": attributes}

    def interrogate(self) -> dict[str, Any]:
        """Connect once and return adapter details with one reading of every configured PID."""
        with self.session() as (elm, info):
            if not info["ecu_online"]:
                values: dict[str, Any] = {pid.name: None for pid in self.pids}
                errors = {pid.name: info["message"] for pid in self.pids}
            else:
                values, errors = self.read_cycle(elm)
        units = {pid.name: pid.unit for pid in self.pids}
        info["values"] = {name: {"value": value, "unit": units[name], **({"error": errors[name]} if name in errors else {})} for name, value in values.items()}
        return info

    def _publish(self, topic: str, payload: dict[str, Any]) -> None:
        """Publish a payload when a publisher is attached."""
        if self.publisher is not None:
            self.publisher.publish(topic, payload)

    def _set_online(self, online: bool, info: dict[str, Any] | None = None, message: str = "") -> None:
        """Log and publish adapter/ECU availability only when it changes."""
        if online == self._online:
            if not online:
                LOGGER.debug("OBD %s still offline: %s", self.section_name, message)
            return
        was_online = self._online is True
        self._online = online
        status = {"online": online, "timestamp": _timestamp(), **(info or {})}
        if message:
            status["message"] = message
        if online:
            LOGGER.info("OBD %s online: %s, %s, %s", self.section_name, status.get("adapter"), status.get("protocol"), status.get("voltage"))
        else:
            LOGGER.warning("OBD %s offline: %s", self.section_name, message)
        self._publish(f"{self._topic()}/status", status)
        if was_online:
            # Clear consumers that only watch the snapshot topic so stale engine values are not displayed.
            self._publish(self._topic(), {**{pid.name: None for pid in self.pids}, "timestamp": status["timestamp"]})

    def _poll(self, elm: Elm327, topic: str, interval: float, max_failed_cycles: int) -> None:
        """Poll every PID once per interval and publish snapshots until stopped or the ECU goes silent."""
        failed_cycles = 0
        overrun_logged = False
        next_cycle = time.monotonic()
        while not self.stop_event.is_set():
            values, errors = self.read_cycle(elm)
            if len(errors) == len(values):
                failed_cycles += 1
                if failed_cycles >= max_failed_cycles:
                    raise EcuUnavailable(f"no PID responses for {failed_cycles} consecutive cycles")
            else:
                failed_cycles = 0
                self._publish(topic, {**values, "timestamp": _timestamp()})
            next_cycle += interval
            now = time.monotonic()
            if next_cycle < now:
                if not overrun_logged:
                    LOGGER.warning("OBD %s poll cycle exceeds %.3fs; lower poll_hz or remove PIDs", self.section_name, interval)
                    overrun_logged = True
                next_cycle = now
            self.stop_event.wait(next_cycle - now)

    def run(self) -> None:
        """Run the OBD collector and log failures that stop its thread."""
        try:
            self._run_daemon()
        except Exception:
            LOGGER.exception("OBD source instance %s stopped", self.section_name)

    def _run_daemon(self) -> None:
        """Maintain an adapter session, reconnecting with bounded backoff as the engine cycles."""
        _ = self.pids  # validate PID configuration before touching Bluetooth
        self._address()
        topic = self._topic()
        interval = self._poll_interval()
        reconnect_delay = self._retry_setting("reconnect_delay", 2, float)
        max_reconnect_delay = self._retry_setting("max_reconnect_delay", 60, float)
        max_retry = self._retry_setting("max_retry", 0, int)
        max_failed_cycles = max(self.section.getint("max_failed_cycles", fallback=3), 1)
        failures = 0
        while not self.stop_event.is_set():
            elm = None
            try:
                elm = self.connect()
                info = self.initialize(elm)
                if not info["ecu_online"]:
                    raise EcuUnavailable(f"{info['adapter']} reachable ({info['voltage']}) but {info['message']}")
                failures = 0
                self._set_online(True, info)
                self._poll(elm, topic, interval, max_failed_cycles)
            except (OSError, ObdError) as error:
                if self.stop_event.is_set():
                    return
                failures += 1
                if max_retry and failures > max_retry:
                    raise RuntimeError(f"OBD reconnect limit reached ({max_retry})") from error
                self._set_online(False, message=str(error) or type(error).__name__)
            finally:
                if elm is not None:
                    elm.close()
            delay = min(reconnect_delay * (2 ** max(failures - 1, 0)), max_reconnect_delay)
            self.stop_event.wait(delay)


def _timestamp() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")
