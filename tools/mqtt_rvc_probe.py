#!/usr/bin/env python3
"""Interactively map physical RV devices to RV-C message fields seen on MQTT.

Workflow: subscribe to the RV-C topics that rvcontrol publishes, record
traffic while you toggle one physical device a fixed number of times, rank
the message fields that changed in step with your toggles, and append the
confirmed mapping to a CSV file for review and later coach YAML authoring.
"""

from __future__ import annotations

import csv
import json
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Allow running directly from the tools/ directory without PYTHONPATH=src.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import click
import paho.mqtt.client as mqtt

from rv_control.config import load_config

# Field-name fragments that usually carry the interesting switch state.
HINT_WORDS = ("command", "status", "state", "instance", "level", "brightness", "switch", "output", "mode")

# Fields that identify which physical load a multiplexed message addresses.
# These are constant while one device is toggled, so they never appear as
# changing candidates; they are extracted as identity instead.
IDENTITY_FIELDS = ("instance", "group")

# Field names that indicate bus infrastructure rather than a device signal.
# On a multiplexed bus, "source" alternates between the command initiator
# and the acknowledging controller for every device, so it is not a useful
# discriminator and is penalized in ranking.
INFRASTRUCTURE_FIELDS = ("source", "dgn", "name", "data")

# Columns of the mapping CSV; values_seen is a ";" joined list of observed values.
CSV_FIELDS = [
    "captured_at",
    "label",
    "topic",
    "dgn",
    "source",
    "message_name",
    "field",
    "values_seen",
    "transitions",
    "expected_transitions",
    "toggles",
    "window_seconds",
    "match",
]


@dataclass
class Candidate:
    """One message field whose values changed during a capture window."""

    topic: str  # MQTT topic the values were observed on
    field: str  # Flattened payload field path (e.g. "command" or "status.level")
    values: list[Any]  # Ordered distinct values observed during the window
    transitions: int  # Number of value changes observed
    score: float  # Ranking score; higher means a better match to the toggles
    dgn: str = ""  # RV-C DGN reported by the payload, when available
    source: str = ""  # RV-C source address reported by the payload
    message_name: str = ""  # RV-C message name reported by the payload
    identity: dict[str, Any] | None = None  # Constant discriminator fields (instance, group)


def flatten_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Flatten a decoded RV-C payload into dot-separated field paths."""
    flat: dict[str, Any] = {}

    def walk(prefix: str, value: Any) -> None:
        """Record leaf values under their dotted path, recursing into mappings."""
        if isinstance(value, dict):
            for key, sub_value in value.items():
                walk(f"{prefix}{key}.", sub_value)
        else:
            flat[prefix.rstrip(".")] = value

    walk("", payload)
    return flat


def analyze_capture(messages: list[tuple[float, str, dict[str, Any]]], toggles: int) -> list[Candidate]:
    """Rank message fields whose value changes best match the toggle count.

    A device toggled N times on/off should produce about 2N transitions in
    its controlling field. Fields score higher when their transition count is
    close to that expectation, when they look binary (two distinct values),
    and when their name hints at a command or status role. Bus-infrastructure
    fields such as "source" are penalized because they alternate for every
    device and are not useful discriminators.

    For multiplexed buses (e.g. DC_DIMMER_COMMAND_2) the per-device identity
    (instance, group) is constant during a capture, so it never appears as a
    changing field. After ranking the changing signal fields, the identity
    fields are read from the messages that carried the winning signal and
    attached to that candidate so the CSV records which load it addresses.
    """
    expected = max(1, toggles * 2)
    series: dict[tuple[str, str], list[Any]] = {}
    meta: dict[str, tuple[str, str, str]] = {}
    # Track which flattened payloads contained each (topic, field) so identity
    # can be read from the messages that actually carried the winning signal.
    carriers: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for _timestamp, topic, payload in messages:
        flat = flatten_payload(payload)
        meta[topic] = (
            str(payload.get("dgn", "")),
            str(payload.get("source", "")),
            str(payload.get("name", "")),
        )
        for name, value in flat.items():
            key = (topic, name)
            carriers.setdefault(key, []).append(flat)
            values = series.setdefault(key, [])
            if not values or values[-1] != value:
                values.append(value)
    candidates: list[Candidate] = []
    for (topic, name), values in series.items():
        transitions = len(values) - 1
        if transitions == 0:
            continue
        distinct = len({repr(value) for value in values})
        score = float(transitions - abs(transitions - expected) * 2)
        if distinct == 2:
            score += expected
        lowered = name.lower()
        if any(hint in lowered for hint in HINT_WORDS):
            score += 1
        if lowered in INFRASTRUCTURE_FIELDS:
            score -= expected * 2
        dgn, source, message_name = meta.get(topic, ("", "", ""))
        candidates.append(Candidate(topic, name, values, transitions, score, dgn, source, message_name))
    candidates.sort(key=lambda candidate: (-candidate.score, candidate.topic, candidate.field))
    if candidates:
        top = candidates[0]
        # Read identity from the messages that carried the winning signal so
        # the instance/group belong to the same device, not just the topic.
        identity: dict[str, Any] = {}
        for flat in carriers.get((top.topic, top.field), []):
            for field in IDENTITY_FIELDS:
                if field in flat and field not in identity:
                    identity[field] = flat[field]
            if len(identity) == len(IDENTITY_FIELDS):
                break
        top.identity = identity or None
    return candidates


def candidate_row(label: str, candidate: Candidate, toggles: int, window: float) -> dict[str, Any]:
    """Build one CSV row describing a confirmed device-to-field mapping."""
    return {
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "label": label,
        "topic": candidate.topic,
        "dgn": candidate.dgn,
        "source": candidate.source,
        "message_name": candidate.message_name,
        "field": candidate.field,
        "values_seen": ";".join(repr(value) for value in candidate.values),
        "transitions": candidate.transitions,
        "expected_transitions": max(1, toggles * 2),
        "toggles": toggles,
        "window_seconds": f"{window:g}",
        "match": ";".join(f"{key}={value}" for key, value in (candidate.identity or {}).items()),
    }


def append_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    """Append mapping rows to the CSV, writing a header for new files."""
    exists = path.exists() and path.stat().st_size > 0
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        if not exists:
            writer.writeheader()
        writer.writerows(rows)


class MqttRecorder:
    """Subscribe to an MQTT topic filter and record JSON messages with timestamps."""

    def __init__(self, host: str, port: int, username: str, password: str, topic_filter: str) -> None:
        """Initialize the recorder connection settings and capture state."""
        self.topic_filter = topic_filter
        self.connected = threading.Event()
        self.error: str | None = None
        self._lock = threading.Lock()
        self._messages: list[tuple[float, str, dict[str, Any]]] = []
        self._drained = 0
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="rv-control-rvc-probe")
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.on_disconnect = self._on_disconnect
        if username:
            self.client.username_pw_set(username, password)
        self.host = host
        self.port = port

    def _on_connect(self, client: Any, _userdata: Any, _flags: Any, reason_code: Any, _properties: Any = None) -> None:
        """Subscribe to the probe topic filter once the broker connects."""
        if reason_code != 0:
            self.error = f"MQTT connection refused: {reason_code}"
            self.connected.set()
            return
        result = client.subscribe(self.topic_filter, qos=0)
        if result[0] != mqtt.MQTT_ERR_SUCCESS:
            self.error = f"MQTT subscribe failed: rc={result[0]}"
        self.connected.set()

    def _on_disconnect(self, _client: Any, _userdata: Any, _flags: Any, reason_code: Any, _properties: Any = None) -> None:
        """Record unexpected disconnects so callers fail instead of hanging."""
        if reason_code != 0:
            self.error = f"MQTT disconnected: {reason_code}"
            self.connected.set()

    def _on_message(self, _client: Any, _userdata: Any, message: Any) -> None:
        """Record one JSON object payload with its arrival time and topic."""
        try:
            payload = json.loads(bytes(message.payload).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if not isinstance(payload, dict):
            return
        with self._lock:
            self._messages.append((time.time(), str(message.topic), payload))

    def start(self, timeout: float = 10.0) -> None:
        """Connect to the broker and begin recording in the background."""
        self.client.connect(self.host, self.port, keepalive=30)
        self.client.loop_start()
        if not self.connected.wait(timeout):
            raise RuntimeError(self.error or "timed out waiting for MQTT connection")
        if self.error:
            raise RuntimeError(self.error)

    def drain(self) -> list[tuple[float, str, dict[str, Any]]]:
        """Return messages recorded since the last drain call."""
        with self._lock:
            new_messages = self._messages[self._drained:]
            self._drained = len(self._messages)
            return list(new_messages)

    def clear(self) -> None:
        """Discard all recorded messages before starting a new window."""
        with self._lock:
            self._messages.clear()
            self._drained = 0

    @property
    def messages(self) -> list[tuple[float, str, dict[str, Any]]]:
        """Return a snapshot of every recorded message."""
        with self._lock:
            return list(self._messages)

    def stop(self) -> None:
        """Stop the MQTT loop and disconnect from the broker."""
        self.client.loop_stop()
        self.client.disconnect()


def build_recorder(settings: dict[str, Any]) -> MqttRecorder:
    """Construct a recorder from CLI settings and fail with a clean error."""
    recorder = MqttRecorder(
        settings["host"],
        settings["port"],
        settings["username"],
        settings["password"],
        settings["topic_filter"],
    )
    try:
        recorder.start()
    except (OSError, RuntimeError) as error:
        raise click.ClickException(str(error)) from error
    return recorder


def _wait_for_keypress(stop: threading.Event) -> None:
    """Set the stop event on the first keypress without requiring Enter.

    Puts the terminal into cbreak mode so a single keystroke is delivered
    immediately, then restores the previous terminal settings. Falls back to a
    blocking read when stdin is not a terminal (e.g. piped input).
    """
    try:
        import termios
        import tty

        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            sys.stdin.read(1)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)
    except (ImportError, OSError, termios.error):
        try:
            sys.stdin.read(1)
        except (OSError, ValueError):
            pass
    finally:
        stop.set()


@click.group()
@click.option("--config", "config_path", default="config.ini", show_default=True, type=click.Path(exists=True, dir_okay=False), help="INI configuration file.")
@click.option("--topic", "topic_override", default=None, help="MQTT topic filter to subscribe to instead of the configured RV-C prefix.")
@click.pass_context
def cli(context: click.Context, config_path: str, topic_override: str | None) -> None:
    """Probe RV-C MQTT traffic to map physical devices to message fields."""
    config = load_config(config_path)
    mqtt_section = config["mqtt"]
    base_topic = mqtt_section.get("base_topic", "rv").strip("/")
    rvc_topic = "rvc"
    if config.has_section("rv_c"):
        rvc_topic = config["rv_c"].get("topic", "rvc").strip("/") or "rvc"
    context.ensure_object(dict)
    context.obj["host"] = mqtt_section.get("host", "localhost")
    context.obj["port"] = mqtt_section.getint("port", fallback=1883)
    context.obj["username"] = mqtt_section.get("username", "")
    context.obj["password"] = mqtt_section.get("password", "")
    context.obj["topic_filter"] = topic_override.strip("/") if topic_override else f"{base_topic}/{rvc_topic}/#"


@cli.command()
@click.pass_context
def listen(context: click.Context) -> None:
    """Print live RV-C MQTT traffic, showing only fields that change."""
    recorder = build_recorder(context.obj)
    click.echo(f"Listening on {context.obj['topic_filter']}; press Ctrl-C to stop.")
    last_seen: dict[str, dict[str, Any]] = {}
    try:
        while True:
            time.sleep(0.2)
            for timestamp, topic, payload in recorder.drain():
                flat = flatten_payload(payload)
                previous = last_seen.get(topic, {})
                changes = {key: (previous.get(key), value) for key, value in flat.items() if key not in previous or previous[key] != value}
                last_seen[topic] = flat
                stamp = time.strftime("%H:%M:%S", time.localtime(timestamp))
                click.echo(f"{stamp} {topic}")
                for key, (old, new) in changes.items():
                    click.echo(f"    {key}: {old!r} -> {new!r}")
    except KeyboardInterrupt:
        click.echo("Stopping")
    finally:
        recorder.stop()


@cli.command("map")
@click.option("--csv", "csv_path", default="device-map.csv", show_default=True, type=click.Path(dir_okay=False), help="Mapping CSV to append confirmed devices to.")
@click.option("--toggles", default=3, show_default=True, type=click.IntRange(min=1), help="On/off toggle cycles performed per device.")
@click.option("--window", default=20.0, show_default=True, type=click.FloatRange(min=1), help="Capture window length in seconds per device.")
@click.option("--top", default=8, show_default=True, type=click.IntRange(min=1), help="Number of candidate fields to display per device.")
@click.pass_context
def map_devices(context: click.Context, csv_path: str, toggles: int, window: float, top: int) -> None:
    """Interactively capture toggles per device and append mappings to a CSV."""
    recorder = build_recorder(context.obj)
    output = Path(csv_path)
    click.echo(f"Subscribed to {context.obj['topic_filter']}.")
    click.echo("For each device: name it, toggle it on/off during the window, then pick the matching field.")
    expected = toggles * 2
    try:
        while True:
            label = click.prompt("Device label (blank to finish)", default="", show_default=False).strip()
            if not label:
                break
            click.echo(f"Ready to toggle '{label}' {toggles} time(s) on/off; expect about {expected} transitions in the matching field.")
            click.prompt("Press Enter to start the capture window", default="", show_default=False)
            recorder.clear()
            click.echo(f"Recording up to {window:g}s; press any key when done toggling.")
            stop = threading.Event()
            waiter = threading.Thread(target=_wait_for_keypress, args=(stop,), daemon=True)
            waiter.start()
            deadline = time.monotonic() + window
            while not stop.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                click.echo(f"\rRecording... {remaining:4.1f}s left", nl=False)
                time.sleep(min(0.25, remaining))
            click.echo("\rRecording... done          ")
            candidates = analyze_capture(recorder.messages, toggles)
            if not candidates:
                click.echo("No changing fields observed; check that rvcontrol is publishing and try again.")
                continue
            shown = candidates[:top]
            click.echo(f"{'#':>2} {'score':>6} {'chg':>4}  {'topic':<40} {'field':<24} values")
            for index, candidate in enumerate(shown, start=1):
                values = ";".join(repr(value) for value in candidate.values[:6])
                identity = " ".join(f"{key}={value}" for key, value in (candidate.identity or {}).items())
                suffix = f" [{identity}]" if identity else ""
                click.echo(f"{index:>2} {candidate.score:>6.0f} {candidate.transitions:>4}  {candidate.topic:<40} {candidate.field:<24} {values}{suffix}")
            choice = click.prompt(f"Candidate to keep [1-{len(shown)}] or 's' to skip", default="1")
            if choice.strip().lower() == "s":
                click.echo("Skipped.")
                continue
            try:
                selected = shown[int(choice) - 1]
            except (ValueError, IndexError):
                click.echo("Invalid selection; skipped.")
                continue
            append_csv(output, [candidate_row(label, selected, toggles, window)])
            click.echo(f"Recorded '{label}' -> {selected.topic} field '{selected.field}' in {output}.")
    except (KeyboardInterrupt, EOFError):
        click.echo("\nStopping")
    finally:
        recorder.stop()
    click.echo(f"Mappings written to {output}.")


def diff_marks(messages: list[tuple[float, str, dict[str, Any]]], marks: list[float], settle: float) -> list[Candidate]:
    """Rank fields whose state persistently changed across each button mark.

    The bus runs a continuous background sweep that flips dimmer fields
    transiently. A real button press instead leaves the device's field at a
    new value that persists. For each mark, the latest flattened value per
    (topic, field) is taken just before the mark and again after a settle
    delay; fields whose before/after values differ are scored. Fields that
    stay changed across multiple marks rank highest, because the sweep
    reverts within one sweep period while a real state change persists.
    """
    # Latest value per (topic, field, identity) across the whole capture. On a
    # multiplexed bus each physical load has a constant instance/group, so the
    # stream is split per identity before diffing; otherwise one instance's
    # value would be diffed against another's on the shared topic.
    timeline: list[tuple[float, str, dict[str, Any]]] = sorted(messages)
    changed_counts: dict[tuple[str, str, tuple], int] = {}
    changed_values: dict[tuple[str, str, tuple], list[Any]] = {}
    identity_by_key: dict[tuple[str, str, tuple], dict[str, Any]] = {}
    meta: dict[str, tuple[str, str, str]] = {}
    for _timestamp, topic, payload in timeline:
        meta[topic] = (
            str(payload.get("dgn", "")),
            str(payload.get("source", "")),
            str(payload.get("name", "")),
        )
    for mark in marks:
        # Latest value per (topic, field, identity) strictly before the mark.
        before: dict[tuple[str, str, tuple], Any] = {}
        for timestamp, topic, payload in timeline:
            if timestamp >= mark:
                break
            flat = flatten_payload(payload)
            identity = tuple((f, flat[f]) for f in IDENTITY_FIELDS if f in flat)
            for name, value in flat.items():
                before[(topic, name, identity)] = value
        # First value per (topic, field, identity) at or after the settle point.
        after: dict[tuple[str, str, tuple], Any] = {}
        for timestamp, topic, payload in timeline:
            if timestamp < mark + settle:
                continue
            flat = flatten_payload(payload)
            identity = tuple((f, flat[f]) for f in IDENTITY_FIELDS if f in flat)
            for name, value in flat.items():
                key = (topic, name, identity)
                if key not in after:
                    after[key] = value
        for key, after_value in after.items():
            if key in before and before[key] != after_value:
                changed_counts[key] = changed_counts.get(key, 0) + 1
                values = changed_values.setdefault(key, [])
                if before[key] not in values:
                    values.append(before[key])
                if after_value not in values:
                    values.append(after_value)
                identity_by_key[key] = dict(key[2])
    candidates: list[Candidate] = []
    for (topic, name, identity_tuple), count in changed_counts.items():
        # Skip pure identity/label fields; the device state fields are what matter.
        if name in IDENTITY_FIELDS or name in INFRASTRUCTURE_FIELDS:
            continue
        values = changed_values[(topic, name, identity_tuple)]
        dgn, source, message_name = meta.get(topic, ("", "", ""))
        identity = identity_by_key.get((topic, name, identity_tuple)) or None
        candidates.append(
            Candidate(topic, name, values, count, float(count), dgn, source, message_name, identity)
        )
    candidates.sort(key=lambda candidate: (-candidate.score, candidate.topic, candidate.field))
    return candidates


@cli.command("mark")
@click.option("--csv", "csv_path", default="device-map.csv", show_default=True, type=click.Path(dir_okay=False), help="Mapping CSV to append confirmed devices to.")
@click.option("--settle", default=1.5, show_default=True, type=click.FloatRange(min=0.1), help="Seconds after a mark to wait before sampling the new state.")
@click.option("--top", default=8, show_default=True, type=click.IntRange(min=1), help="Number of candidate fields to display per device.")
@click.pass_context
def mark_devices(context: click.Context, csv_path: str, settle: float, top: int) -> None:
    """Map a device by marking the moment of each button press.

    The bus sweep flips dimmer fields continuously, so counting transitions
    cannot isolate one device. Instead: press the physical button, then press
    Enter here at that instant. Repeat the press/mark a few times. Fields whose
    state persistently changed across every mark belong to the device.
    """
    recorder = build_recorder(context.obj)
    output = Path(csv_path)
    click.echo(f"Subscribed to {context.obj['topic_filter']}.")
    click.echo("Let the bus settle, then: toggle the device, and press Enter here at that moment. Repeat.")
    try:
        while True:
            label = click.prompt("Device label (blank to finish)", default="", show_default=False).strip()
            if not label:
                break
            recorder.clear()
            marks: list[float] = []
            click.echo("Recording. Toggle the device, then press Enter here to mark each press. Blank Enter when done.")
            while True:
                click.prompt("Press Enter to mark (blank to analyze)", default="", show_default=False)
                marks.append(time.time())
                click.echo(f"  marked press #{len(marks)}; toggle again and mark, or blank Enter to analyze.")
                # A blank line still appends a mark; treat consecutive quick
                # Enters as "done" once at least one real mark exists.
                if len(marks) >= 2 and (marks[-1] - marks[-2]) < 1.0:
                    marks.pop()
                    break
            if not marks:
                click.echo("No marks recorded; skipped.")
                continue
            candidates = diff_marks(recorder.messages, marks, settle)
            if not candidates:
                click.echo("No persistent changes found across those marks; try marking closer to each press.")
                continue
            shown = candidates[:top]
            click.echo(f"{'#':>2} {'hits':>4}  {'topic':<40} {'field':<24} values")
            for index, candidate in enumerate(shown, start=1):
                values = ";".join(repr(value) for value in candidate.values[:6])
                identity = " ".join(f"{key}={value}" for key, value in (candidate.identity or {}).items())
                suffix = f" [{identity}]" if identity else ""
                click.echo(f"{index:>2} {candidate.transitions:>4}  {candidate.topic:<40} {candidate.field:<24} {values}{suffix}")
            choice = click.prompt(f"Candidate to keep [1-{len(shown)}] or 's' to skip", default="1")
            if choice.strip().lower() == "s":
                click.echo("Skipped.")
                continue
            try:
                selected = shown[int(choice) - 1]
            except (ValueError, IndexError):
                click.echo("Invalid selection; skipped.")
                continue
            append_csv(output, [candidate_row(label, selected, len(marks), 0.0)])
            click.echo(f"Recorded '{label}' -> {selected.topic} field '{selected.field}' in {output}.")
    except (KeyboardInterrupt, EOFError):
        click.echo("\nStopping")
    finally:
        recorder.stop()
    click.echo(f"Mappings written to {output}.")


if __name__ == "__main__":
    cli()
