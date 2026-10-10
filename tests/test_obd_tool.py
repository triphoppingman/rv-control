"""Regression tests for configured-PID checks in the standalone OBD tool."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from test_obd import INIT, make_source


def load_tool() -> Any:
    """Load the standalone OBD tool without invoking its command group."""
    path = Path(__file__).parents[1] / "tools" / "obd_tool.py"
    spec = importlib.util.spec_from_file_location("obd_tool", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(("save_report", "existing_report"), [(False, False), (True, False), (True, True)])
def test_check_pids_reports_values_errors_headers_and_elapsed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, save_report: bool, existing_report: bool,
) -> None:
    """Check real source decoding, header changes, dependencies, and cleanup without hardware."""
    source, sockets = make_source(
        {
            **INIT, "22F478": "NO DATA", "010C": "410C0000", "0105": "4105",
            "0133": "413364", "221440": "62144003E8",
            "ATSH7E1": "OK", "221E1C": "621E1CFF00",
        },
        **{
            "pid.baro": "01, 33, b0/100, bar",
            "pid.boost": "22, 1440, (b0*256+b1)*0.03625-BARO*14.5038, PSI",
            "pid.tft": "22, 1E1C, ((b0-256 if b0>=128 else b0)*256+b1)*(9/80)+32, F, 7E1",
        },
    )
    tool = load_tool()
    monkeypatch.setattr(tool, "ObdSource", lambda *_args: source)
    config_path = tmp_path / "config.ini"
    with config_path.open("w", encoding="utf-8") as handle:
        source.config.write(handle)

    output_path = tmp_path / "report.json"
    arguments = ["--config", str(config_path), "check-pids"]
    if existing_report:
        output_path.write_text("previous report", encoding="utf-8")
    if save_report:
        arguments.extend(["--output", str(output_path)])
    result = CliRunner().invoke(tool.cli, arguments)

    assert result.exit_code == 0, result.output
    output = json.loads(result.output)
    if save_report:
        assert output_path.read_text(encoding="utf-8") == result.output
    else:
        assert not output_path.exists()
    assert (output["checked"], output["usable"], output["failed"]) == (6, 4, 2)
    assert output["elapsed_seconds"] >= 0
    pids = {pid["name"]: pid for pid in output["pids"]}
    assert {key: value for key, value in pids["rpm"].items() if key != "diagnostics"} == {
        "name": "rpm", "request": "010C", "header": "7E0", "unit": "RPM",
        "usable": True, "value": 0.0, "error": None,
    }
    assert not pids["egt11"]["usable"]
    assert pids["egt11"]["error"] == "NO DATA"
    assert pids["egt11"]["value"] is None
    assert "short response" in pids["coolant_temp"]["error"]
    assert pids["boost"]["value"] == 21.7462
    assert pids["tft"]["header"] == "7E1"
    assert pids["tft"]["value"] == 3.2
    assert pids["egt11"]["diagnostics"]["response"] == ["NO DATA"]
    assert pids["egt11"]["diagnostics"]["failure_stage"] == "response"
    assert pids["coolant_temp"]["diagnostics"]["failure_stage"] == "decode"
    assert pids["tft"]["diagnostics"]["actual_header"] == "7E1"
    assert pids["tft"]["diagnostics"]["data"] == "FF 00"
    assert output["adapter"]["ecu_online"]
    assert output["control"]["response"] == ["410C0000"]
    assert output["control"]["rpm"] == 0
    assert sockets[0].sent.count("22F478") == 1
    assert sockets[0].closed
    assert not source.publisher.messages


@pytest.mark.parametrize(
    ("reply", "meaning"),
    [("7F2231", "request out of range"), ("7F2211", "service not supported"),
     ("7F2222", "conditions not correct"), ("?", None)],
)
def test_check_pids_retains_rejection_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, reply: str, meaning: str | None,
) -> None:
    """Distinguish ECU negative replies from adapter rejection while retaining control evidence."""
    source, _sockets = make_source({**INIT, "22F478": reply, "010C": "410C1AF8", "0105": "41055A"})
    tool = load_tool()
    monkeypatch.setattr(tool, "ObdSource", lambda *_args: source)
    config_path = tmp_path / "config.ini"
    with config_path.open("w", encoding="utf-8") as handle:
        source.config.write(handle)

    result = CliRunner().invoke(tool.cli, ["--config", str(config_path), "check-pids"])

    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    evidence = report["pids"][0]["diagnostics"]
    assert evidence["response"] == [reply]
    assert evidence["failure_stage"] == "response"
    assert evidence.get("negative_response_meaning") == meaning
    assert evidence["actual_header"] == "7E0"
    assert report["control"]["rpm"] == 1726.0
    assert not source.publisher.messages


@pytest.mark.parametrize("failure", ["offline", "disconnect", "interrupt"])
def test_check_pids_fails_explicitly_and_closes_socket(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str,
) -> None:
    """Do not return a success report when the ECU is offline or the transport fails."""
    source, sockets = make_source({**INIT, "0100": "NO DATA"} if failure == "offline" else INIT)
    tool = load_tool()
    monkeypatch.setattr(tool, "ObdSource", lambda *_args: source)
    if failure != "offline":
        def disconnected_cycle(_elm: Any, _diagnostics: Any = None) -> Any:
            """Simulate transport loss or user interruption during the check."""
            if failure == "interrupt":
                raise KeyboardInterrupt
            raise OSError("socket disconnected")
        monkeypatch.setattr(source, "read_cycle", disconnected_cycle)
    config_path = tmp_path / "config.ini"
    with config_path.open("w", encoding="utf-8") as handle:
        source.config.write(handle)

    output_path = tmp_path / "report.json"
    output_path.write_text("previous report", encoding="utf-8")
    result = CliRunner().invoke(
        tool.cli, ["--config", str(config_path), "check-pids", "--output", str(output_path)],
    )

    assert result.exit_code != 0
    expected = {
        "offline": "ECU not responding",
        "disconnect": "socket disconnected",
        "interrupt": "Aborted",
    }
    assert expected[failure] in result.output
    assert sockets[0].closed
    assert output_path.read_text(encoding="utf-8") == "previous report"


def test_check_pids_reports_file_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Report an unwritable destination explicitly after closing the adapter session."""
    source, sockets = make_source({**INIT, "010C": "410C1AF8", "0105": "41055A"})
    tool = load_tool()
    monkeypatch.setattr(tool, "ObdSource", lambda *_args: source)
    config_path = tmp_path / "config.ini"
    with config_path.open("w", encoding="utf-8") as handle:
        source.config.write(handle)
    output_path = tmp_path / "missing" / "report.json"

    result = CliRunner().invoke(
        tool.cli, ["--config", str(config_path), "check-pids", "--output", str(output_path)],
    )

    assert result.exit_code != 0
    assert "Error:" in result.output
    assert not output_path.exists()
    assert sockets[0].closed


def test_check_pids_records_timeout_and_continues(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Retain an individual timeout as an error and still check subsequent PIDs."""
    source, sockets = make_source({**INIT, "010C": "410C1AF8", "0105": "41055A"})
    original_query = source.query

    def timed_query(
        elm: Any, pid: Any, context: Any = None, diagnostics: Any = None,
    ) -> int | float:
        """Time out the first PID and delegate the remaining queries to the source."""
        if pid.name == "egt11":
            raise TimeoutError("timed out waiting for 22F478")
        return original_query(elm, pid, context, diagnostics)

    monkeypatch.setattr(source, "query", timed_query)
    tool = load_tool()
    monkeypatch.setattr(tool, "ObdSource", lambda *_args: source)
    config_path = tmp_path / "config.ini"
    with config_path.open("w", encoding="utf-8") as handle:
        source.config.write(handle)

    result = CliRunner().invoke(tool.cli, ["--config", str(config_path), "check-pids"])

    assert result.exit_code == 0, result.output
    output = json.loads(result.output)
    assert (output["checked"], output["usable"], output["failed"]) == (3, 2, 1)
    assert output["pids"][0]["error"] == "timed out waiting for 22F478"
    assert output["pids"][0]["diagnostics"]["failure_stage"] == "transport"
    assert output["pids"][1]["value"] == 1726.0
    assert output["pids"][2]["value"] == 122.0
    assert sockets[0].closed


def test_check_pids_retains_header_rejection_and_failed_control(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Continue after a failed control, but never send a PID after its header was rejected."""
    source, sockets = make_source(
        {**INIT, "22F478": "62F47803E8", "010C": "NO DATA", "0105": "41055A", "ATSH7E1": "?"},
        **{"pid.tft": "22, 1E1C, b0, F, 7E1"},
    )
    tool = load_tool()
    monkeypatch.setattr(tool, "ObdSource", lambda *_args: source)
    config_path = tmp_path / "config.ini"
    with config_path.open("w", encoding="utf-8") as handle:
        source.config.write(handle)

    result = CliRunner().invoke(tool.cli, ["--config", str(config_path), "check-pids"])

    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["control"]["error"] == "NO DATA"
    assert report["pids"][0]["value"] == 140.0
    evidence = report["pids"][-1]["diagnostics"]
    assert evidence["failure_stage"] == "header"
    assert evidence["header_response"] == ["?"]
    assert evidence["actual_header"] == "7E0"
    assert not evidence["request_attempted"]
    assert "221E1C" not in sockets[0].sent
    assert sockets[0].closed
