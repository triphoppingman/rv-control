"""Regression tests for the interactive RV-C MQTT device mapping tool."""

from __future__ import annotations

import csv
import importlib.util
import sys
from pathlib import Path
from typing import Any


def _load_probe_module() -> Any:
    """Load the standalone probe tool module for direct behavior testing.

    The module is registered in sys.modules so the dataclass decorator can
    resolve the postponed annotations created by `from __future__ import
    annotations` in the tool.
    """
    tool_path = Path(__file__).parents[1] / "tools" / "mqtt_rvc_probe.py"
    spec = importlib.util.spec_from_file_location("mqtt_rvc_probe", tool_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["mqtt_rvc_probe"] = module
    spec.loader.exec_module(module)
    return module


def test_flatten_payload_flattens_nested_mappings() -> None:
    """Verify nested payload dictionaries flatten to dot-separated paths."""
    probe = _load_probe_module()
    payload = {"dgn": "1FEDB", "status": {"level": 50, "mode": "on"}}
    assert probe.flatten_payload(payload) == {
        "dgn": "1FEDB",
        "status.level": 50,
        "status.mode": "on",
    }


def test_analyze_capture_prefers_binary_field_matching_toggle_count() -> None:
    """Verify a binary switch-like field outranks noisy and static fields."""
    probe = _load_probe_module()
    topic = "rv/rvc/DC_DIMMER_STATUS"
    messages = []
    timestamp = 1_700_000_000.0
    for index in range(7):
        # The matched device field flips each message (6 transitions for 3 toggles).
        command = "on" if index % 2 == 0 else "off"
        # A noisy counter also changes every message but is not binary.
        counter = index
        payload = {"dgn": "1FEDB", "source": "2A", "name": "DC_DIMMER_STATUS", "instance": 7, "group": 1, "command": command, "counter": counter, "constant": 1}
        messages.append((timestamp + index, topic, payload))
    candidates = probe.analyze_capture(messages, toggles=3)
    assert candidates, "expected at least one candidate"
    assert candidates[0].field == "command"
    assert candidates[0].transitions == 6
    assert candidates[0].identity == {"instance": 7, "group": 1}
    assert all(candidate.field != "constant" for candidate in candidates)


def test_analyze_capture_penalizes_infrastructure_source_field() -> None:
    """Verify the bus-handshake source field loses to the command signal."""
    probe = _load_probe_module()
    topic = "rv/rvc/DC_DIMMER_COMMAND_2"
    messages = []
    for index in range(8):
        source = "9E" if index % 2 == 0 else "FC"
        command = 5 if index % 2 == 0 else 3
        payload = {"dgn": "1FEDB", "source": source, "name": "DC_DIMMER_COMMAND_2", "instance": 24, "group": 255, "command": command}
        messages.append((float(index), topic, payload))
    candidates = probe.analyze_capture(messages, toggles=3)
    assert candidates[0].field == "command"
    assert candidates[0].identity == {"instance": 24, "group": 255}


def test_analyze_capture_attaches_identity_to_top_signal_only() -> None:
    """Verify constant identity fields decorate the winning signal candidate."""
    probe = _load_probe_module()
    topic = "rv/rvc/DC_DIMMER_COMMAND_2"
    messages = [
        (1.0, topic, {"instance": 3, "group": 1, "command": 5, "level": 100}),
        (2.0, topic, {"instance": 3, "group": 1, "command": 3, "level": 0}),
        (3.0, topic, {"instance": 3, "group": 1, "command": 5, "level": 100}),
    ]
    candidates = probe.analyze_capture(messages, toggles=1)
    top = candidates[0]
    assert top.identity == {"instance": 3, "group": 1}
    assert all(candidate.identity is None for candidate in candidates[1:])


def test_analyze_capture_ignores_unchanging_fields() -> None:
    """Verify fields that never change are not reported as candidates."""
    probe = _load_probe_module()
    messages = [
        (1.0, "rv/rvc/X", {"dgn": "00000", "steady": 5}),
        (2.0, "rv/rvc/X", {"dgn": "00000", "steady": 5}),
    ]
    assert probe.analyze_capture(messages, toggles=3) == []


def test_append_csv_writes_header_once_and_appends_rows(tmp_path: Path) -> None:
    """Verify the CSV gains a header on first write and only rows afterwards."""
    probe = _load_probe_module()
    path = tmp_path / "device-map.csv"
    candidate = probe.Candidate("rv/rvc/X", "command", ["off", "on"], 1, 10.0, "1FEDB", "2A", "DC_DIMMER_STATUS", {"instance": 7, "group": 1})
    row = probe.candidate_row("ceiling_lights", candidate, toggles=3, window=20.0)
    probe.append_csv(path, [row])
    probe.append_csv(path, [row])
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 2
    assert rows[0]["label"] == "ceiling_lights"
    assert rows[0]["dgn"] == "1FEDB"
    assert rows[0]["values_seen"] == "'off';'on'"
    assert rows[0]["expected_transitions"] == "6"
    assert rows[0]["match"] == "instance=7;group=1"


def _dimmer_payload(instance: int, level: float) -> dict[str, Any]:
    """Build one decoded DC_DIMMER_COMMAND_2 payload for diff tests."""
    return {
        "dgn": "1FEDB",
        "source": "9E",
        "name": "DC_DIMMER_COMMAND_2",
        "instance": instance,
        "group": 255,
        "desired level": level,
        "command": 3 if level == 0 else 33,
    }


def test_diff_marks_finds_persistent_change_not_sweep() -> None:
    """Verify a persistent device change outranks the transient bus sweep.

    Timeline: the sweep flips instance 43's level every second (transient),
    while instance 56 (the device) changes 0 -> 100 across the mark and stays.
    Only the persistent change should be reported for the device instance.
    """
    probe = _load_probe_module()
    topic = "rv/rvc/DC_DIMMER_COMMAND_2"
    messages = []
    mark = 10.0
    # Sweep: instance 43 flips 0/100/0 around the mark (transient).
    messages.append((8.0, topic, _dimmer_payload(43, 0.0)))
    messages.append((10.2, topic, _dimmer_payload(43, 100.0)))
    messages.append((12.0, topic, _dimmer_payload(43, 0.0)))
    # Device: instance 56 goes 0 before the mark and 100 after, and stays.
    messages.append((8.5, topic, _dimmer_payload(56, 0.0)))
    messages.append((11.8, topic, _dimmer_payload(56, 100.0)))
    candidates = probe.diff_marks(messages, [mark], settle=1.5)
    fields = {(candidate.identity or {}).get("instance"): candidate for candidate in candidates}
    assert 56 in fields, "expected the persistently-changed device instance"
    # The sweep instance 43 must not be reported (its before/after match).
    assert all(candidate.identity is None or candidate.identity.get("instance") != 43 for candidate in candidates)


def test_diff_marks_ranks_multi_mark_persistence_highest() -> None:
    """Verify a field changed across two marks scores above a one-mark change."""
    probe = _load_probe_module()
    topic = "rv/rvc/DC_DIMMER_COMMAND_2"
    marks = [10.0, 30.0]
    messages = [
        (8.0, topic, _dimmer_payload(56, 0.0)),
        (11.0, topic, _dimmer_payload(56, 100.0)),
        (29.0, topic, _dimmer_payload(56, 100.0)),
        (32.0, topic, _dimmer_payload(56, 0.0)),
        # A second field changed only across the first mark.
        (8.5, topic, _dimmer_payload(60, 5.0)),
        (11.5, topic, _dimmer_payload(60, 9.0)),
        (29.5, topic, _dimmer_payload(60, 9.0)),
        (32.5, topic, _dimmer_payload(60, 9.0)),
    ]
    candidates = probe.diff_marks(messages, marks, settle=1.5)
    by_instance = {(c.identity or {}).get("instance"): c for c in candidates}
    assert by_instance[56].transitions == 2
    assert by_instance[60].transitions == 1
    assert candidates[0].identity.get("instance") == 56
