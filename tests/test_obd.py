from __future__ import annotations

import threading
from configparser import ConfigParser
from typing import Any

import pytest

from rv_control.sources.obd import (
    Elm327,
    ObdNoData,
    ObdPid,
    ObdSource,
    compile_decode,
    extract_data,
    validate_request,
)


class FakeElmSocket:
    """Emulate an ELM327 RFCOMM socket that answers commands from a response table."""

    def __init__(self, responses: dict[str, str]) -> None:
        """Store command responses and initialize captured traffic."""
        self.responses = responses
        self.sent: list[str] = []
        self.pending = b""
        self.closed = False

    def settimeout(self, _timeout: float) -> None:
        """Accept socket timeout configuration."""

    def sendall(self, data: bytes) -> None:
        """Record one command and queue its scripted response followed by the prompt."""
        if self.closed:
            raise OSError("socket closed")
        command = data.decode("ascii").strip()
        self.sent.append(command)
        reply = self.responses.get(command, "?")
        self.pending += f"{command}\r{reply}\r\r>".encode("ascii")

    def recv(self, size: int) -> bytes:
        """Return queued bytes in small chunks to exercise partial reads."""
        if not self.pending:
            raise TimeoutError
        chunk, self.pending = self.pending[:min(size, 3)], self.pending[min(size, 3):]
        return chunk

    def close(self) -> None:
        """Mark the fake socket closed."""
        self.closed = True


class FakePublisher:
    """Capture published MQTT payloads."""

    def __init__(self) -> None:
        """Initialize captured messages."""
        self.messages: list[tuple[str, dict[str, Any]]] = []

    def publish(self, topic: str, payload: dict[str, Any]) -> None:
        """Record one publication."""
        self.messages.append((topic, payload))


INIT = {"ATZ": "ELM327 v1.5", "ATE0": "OK", "ATL0": "OK", "ATS0": "OK", "ATH0": "OK", "ATSP0": "OK", "ATSH7E0": "OK",
        "ATRV": "12.6V", "ATDP": "AUTO, ISO 15765-4 (CAN 11/500)", "0100": "SEARCHING...\r4100BE3EB811"}


def make_config(**overrides: str) -> ConfigParser:
    """Build a configuration with one OBD source section."""
    config = ConfigParser()
    config.read_dict({
        "mqtt": {"base_topic": "rv"},
        "source": {"enabled-sources": "obd_engine"},
        "service": {"reconnect_delay": "10", "max_retry": "0"},
        "obd_engine": {
            "type": "obd", "address": "00:1D:A5:00:00:01", "header": "7E0", "poll_hz": "50", "topic": "obd",
            "reconnect_delay": "0.01", "max_reconnect_delay": "0.01", "max_failed_cycles": "2",
            "pid.egt11": "22, F478, ((b0*256+b1)*0.18)-40, °F",
            "pid.rpm": "01, 0C, (b0*256+b1)/4, RPM",
            "pid.coolant_temp": "01, 05, (b0-40)*1.8+32, °F",
            **overrides,
        },
    })
    return config


def make_source(responses: dict[str, str], **overrides: str) -> tuple[ObdSource, list[FakeElmSocket]]:
    """Create an OBD source whose connections use fresh fake sockets."""
    source = ObdSource(make_config(**overrides), FakePublisher(), threading.Event(), "obd_engine")
    sockets: list[FakeElmSocket] = []

    def factory() -> FakeElmSocket:
        """Return a new scripted socket for each connection attempt."""
        sockets.append(FakeElmSocket(responses))
        return sockets[-1]

    source.connection_factory = factory
    return source, sockets


def test_pid_parsing_keeps_commas_in_decode_and_validates_mode() -> None:
    """Verify PID entries split around decode expressions and reject write services."""
    pid = ObdPid.parse("clamped", "0x01, 0c, max(b0-40,0), °C")
    assert (pid.mode, pid.pid, pid.expression, pid.unit, pid.request) == (1, "0C", "max(b0-40,0)", "°C", "010C")
    transmission_temp = ObdPid.parse("tft", "22, 1E1C, ((b0-256 if b0>=128 else b0)*256+b1)*(9/80)+32, °F, 7E1")
    assert transmission_temp.header == "7E1"
    assert transmission_temp.decode(bytes([0xFF, 0x00])) == 3.2
    with pytest.raises(ValueError, match="not a read-only mode"):
        ObdPid.parse("clear", "04, , b0, ")
    with pytest.raises(ValueError, match="not a read-only mode"):
        validate_request("2EF47800")
    assert validate_request("22 F478") == (0x22, "F478")


@pytest.mark.parametrize("expression", ["__import__('os')", "().__class__", "b0.real", "'x'", "open", "[b0]", "lambda: 1"])
def test_decode_expressions_reject_unsafe_syntax(expression: str) -> None:
    """Verify decode expressions cannot reach attributes, builtins, or non-numeric values."""
    with pytest.raises(ValueError):
        compile_decode(expression)


def test_decode_reports_short_responses_and_rounds_float_noise() -> None:
    """Verify decoding binds bytes, rounds float noise, and treats missing bytes as N/A."""
    pid = ObdPid.parse("egt", "22, F478, ((b0*256+b1)*0.18)-40, °F")
    assert pid.decode(bytes([0x03, 0xE8])) == 140.0
    with pytest.raises(ObdNoData, match="short response"):
        pid.decode(bytes([0x03]))


def test_read_cycle_uses_baro_dependency_and_per_pid_can_header() -> None:
    """Verify Boost decodes against Baro and transmission requests switch the CAN header."""
    responses = {
        **INIT,
        "22F478": "62F47803E8",
        "010C": "410C1AF8",
        "0105": "41055A",
        "0133": "413364",
        "221440": "62144003E8",
        "ATSH7E1": "OK",
        "221E1C": "621E1CFF00",
    }
    source, _sockets = make_source(
        responses,
        **{
            "pid.baro": "01, 33, b0/100, bar",
            "pid.boost": "22, 1440, (b0*256+b1)*0.03625-BARO*14.5038, PSI",
            "pid.tft": "22, 1E1C, ((b0-256 if b0>=128 else b0)*256+b1)*(9/80)+32, °F, 7E1",
        },
    )
    elm = Elm327(FakeElmSocket(responses))

    values, errors = source.read_cycle(elm)

    assert values["baro"] == 1.0
    assert values["boost"] == 21.7462
    assert values["tft"] == 3.2
    assert not errors
    assert elm.connection.sent.index("ATSH7E1") < elm.connection.sent.index("221E1C")


def test_extract_data_handles_echo_errors_negative_and_multiframe() -> None:
    """Verify response parsing checks the echoed mode and PID and handles ELM error forms."""
    assert extract_data(["7F0100", "410C1AF8"], 0x01, "0C") == bytes([0x1A, 0xF8])
    assert extract_data(["62F47803E8"], 0x22, "F478") == bytes([0x03, 0xE8])
    assert extract_data(["014", "0:490201314431", "1:47503030523535", "2:42313233343536"], 0x09, "02")[:4] == bytes.fromhex("01314431")
    for lines, message in ((["NO DATA"], "NO DATA"), (["7F2231"], "0x31"), (["410D20"], "unexpected"), ([], "empty")):
        with pytest.raises(ObdNoData, match=message):
            extract_data(lines, 0x22 if lines == ["7F2231"] else 0x01, "F478" if lines == ["7F2231"] else "0C")


def test_elm_command_accumulates_partial_reads_until_prompt() -> None:
    """Verify commands read until the prompt, strip echo and SEARCHING lines, and keep data lines."""
    elm = Elm327(FakeElmSocket({"010C": "SEARCHING...\r41 0C 1A F8"}))
    assert elm.command("010C") == ["410C1AF8"]


def test_interrogate_reads_every_pid_and_reports_na() -> None:
    """Verify interrogation initializes the adapter and reports values, units, and N/A errors."""
    source, sockets = make_source({**INIT, "22F478": "62F47803E8", "010C": "410C1AF8", "0105": "NO DATA"})
    result = source.interrogate()
    assert result["ecu_online"] is True and result["adapter"] == "ELM327 V1.5" and result["voltage"] == "12.6V"
    assert result["values"]["egt11"] == {"value": 140.0, "unit": "°F"}
    assert result["values"]["rpm"] == {"value": 1726.0, "unit": "RPM"}
    assert result["values"]["coolant_temp"] == {"value": None, "unit": "°F", "error": "NO DATA"}
    assert sockets[0].sent[:7] == ["ATZ", "ATE0", "ATL0", "ATS0", "ATH0", "ATSP0", "ATSH7E0"]
    assert sockets[0].closed


def test_initialize_uses_fallback_protocol_when_auto_fails() -> None:
    """Verify the configured fallback protocol is tried when automatic detection fails."""
    responses = {**INIT, "0100": "UNABLE TO CONNECT", "ATSP6": "OK"}
    source, sockets = make_source(responses, fallback_protocol="6")
    sockets_before = len(sockets)
    with source.session() as (_elm, info):
        assert info["ecu_online"] is False
    assert "ATSP6" in sockets[sockets_before].sent


def test_comms_check_succeeds_with_engine_off() -> None:
    """Verify an adapter reachable with a silent ECU passes comms-check and explains the state."""
    source, _sockets = make_source({**INIT, "0100": "UNABLE TO CONNECT"})
    result = source.comms_check()
    assert result["ok"] is True and "ECU not responding" in result["message"]
    assert {"attribute": "rpm", "unit": "RPM", "access": "read"} in result["attributes"]


def test_comms_check_reports_unreachable_adapter() -> None:
    """Verify connection failures are reported as failed checks rather than raised."""
    source, _sockets = make_source(INIT)

    def refuse() -> FakeElmSocket:
        """Simulate an adapter that is powered off."""
        raise ConnectionRefusedError("Host is down")

    source.connection_factory = refuse
    assert source.comms_check()["ok"] is False


def test_daemon_publishes_snapshots_and_reconnects_when_ecu_goes_silent() -> None:
    """Verify polling publishes values, marks offline after silent cycles, then recovers."""
    responses = {**INIT, "22F478": "62F47803E8", "010C": "410C1AF8", "0105": "41055A"}
    source, sockets = make_source(responses)
    publisher = source.publisher
    published_values = threading.Event()

    original_publish = publisher.publish
    original_factory = source.connection_factory

    def publish(topic: str, payload: dict[str, Any]) -> None:
        """Record messages and simulate the engine shutting off after the first snapshot."""
        original_publish(topic, payload)
        if topic == "obd" and not published_values.is_set():
            published_values.set()
            for key in ("22F478", "010C", "0105", "0100"):
                responses[key] = "NO DATA"

    def factory() -> FakeElmSocket:
        """Stop the source after two reconnect attempts with the engine off."""
        if len(sockets) >= 3:
            source.stop_event.set()
        return original_factory()

    publisher.publish = publish
    source.connection_factory = factory
    source.run()
    snapshots = [payload for topic, payload in publisher.messages if topic == "obd"]
    statuses = [payload["online"] for topic, payload in publisher.messages if topic == "obd/status"]
    assert snapshots[0]["rpm"] == 1726.0 and snapshots[0]["coolant_temp"] == 122.0 and "timestamp" in snapshots[0]
    assert statuses == [True, False]
    assert all(value is None for key, value in snapshots[-1].items() if key != "timestamp")
    assert all(sock.closed for sock in sockets)


def test_daemon_stops_after_configured_retry_limit(caplog: pytest.LogCaptureFixture) -> None:
    """Verify max_retry bounds consecutive connection failures."""
    source, _sockets = make_source(INIT, max_retry="2")
    attempts = []

    def refuse() -> FakeElmSocket:
        """Simulate a powered-off adapter."""
        attempts.append(1)
        raise ConnectionRefusedError("Host is down")

    source.connection_factory = refuse
    source.run()
    assert len(attempts) == 3
    assert "reconnect limit reached" in caplog.text


def test_invalid_configuration_fails_before_connecting() -> None:
    """Verify bad PID and address configuration is rejected without opening a socket."""
    source, sockets = make_source(INIT, address="not-a-mac")
    assert source.comms_check()["ok"] is False
    source, sockets = make_source(INIT, **{"pid.bad": "01, 0C, b0.real, RPM"})
    source.run()
    assert sockets == []


def test_resolve_adapter_address_accepts_mac_and_rejects_bad_names() -> None:
    """Verify adapters may be given as a literal MAC and invalid names are rejected before any socket I/O."""
    from rv_control.sources.obd import resolve_adapter_address

    assert resolve_adapter_address("ac:7b:a1:00:00:01") == "AC:7B:A1:00:00:01"
    with pytest.raises(ValueError):
        resolve_adapter_address("bluetooth0")
