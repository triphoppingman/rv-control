from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from click.testing import CliRunner

from rv_control.cli import cli
from rv_control.config import ConfigReloader


CONFIG = """
[target]
enabled-targets = log
[log]
type = log
[source]
enabled-sources = weather
[service]
daemon_mode = true
[weather]
type = open_meteo
latitude = 12.5
longitude = -45.5
"""


def edit(path: Path, contents: str) -> None:
    """Write a deterministic timestamp change without sleeping for filesystem ticks."""
    previous = path.stat().st_mtime_ns
    path.write_text(contents)
    os.utime(path, ns=(previous + 1_000_000, previous + 1_000_000))


def test_timestamp_and_contents_must_both_change(tmp_path: Path) -> None:
    """Avoid parsing unchanged timestamps and timestamp-only touches."""
    path = tmp_path / "config.ini"
    path.write_text(CONFIG)
    validated: list[Any] = []
    watcher = ConfigReloader(str(path), validated.append)
    assert watcher.poll() is None
    timestamp = path.stat().st_mtime_ns
    path.write_text(CONFIG + "\n# changed")
    os.utime(path, ns=(timestamp, timestamp))
    assert watcher.poll() is None
    edit(path, CONFIG)
    assert watcher.poll() is None
    edit(path, CONFIG + "\n# changed")
    candidate = watcher.poll()
    assert candidate is not None
    assert validated == [candidate]
    assert candidate["_meta"]["path"] == str(path.resolve())
    assert watcher.poll() is None


def test_invalid_edits_keep_active_config_and_recover(tmp_path: Path, caplog: Any) -> None:
    """Reject bad syntax/values once and recover after a subsequent valid save."""
    from rv_control.cli import _validate_reload

    path = tmp_path / "config.ini"
    path.write_text(CONFIG)
    watcher = ConfigReloader(str(path), _validate_reload)
    with caplog.at_level(logging.ERROR):
        for text in ("not ini", CONFIG.replace("12.5", "999"), CONFIG.replace("type = log", "type = missing")):
            edit(path, text)
            assert watcher.poll() is None
            count = len(caplog.records)
            edit(path, text)
            assert watcher.poll() is None
            assert len(caplog.records) == count
    assert "keeping active configuration" in caplog.text
    edit(path, CONFIG)
    assert watcher.poll() is None
    edit(path, CONFIG.replace("12.5", "25"))
    assert watcher.poll()["weather"]["latitude"] == "25"


def test_missing_or_non_utf8_file_recovers(tmp_path: Path, caplog: Any) -> None:
    """A temporarily missing/unreadable config must not stop running collectors."""
    path = tmp_path / "config.ini"
    path.write_text(CONFIG)
    watcher = ConfigReloader(str(path), lambda _config: None)
    path.unlink()
    assert watcher.poll() is None
    count = len(caplog.records)
    assert watcher.poll() is None
    assert len(caplog.records) == count
    path.write_bytes(b"\xff")
    assert watcher.poll() is None
    edit(path, CONFIG + "\n# recovered")
    assert watcher.poll() is not None


def test_atomic_replacement_is_detected(tmp_path: Path) -> None:
    """Support editor save-by-rename when the replacement timestamp changes."""
    path = tmp_path / "config.ini"
    path.write_text(CONFIG)
    watcher = ConfigReloader(str(path), lambda _config: None)
    replacement = tmp_path / "replacement.ini"
    replacement.write_text(CONFIG.replace("12.5", "25"))
    timestamp = path.stat().st_mtime_ns + 1_000_000
    os.utime(replacement, ns=(timestamp, timestamp))
    replacement.replace(path)
    assert watcher.poll()["weather"]["latitude"] == "25"


def test_save_during_read_is_deferred(tmp_path: Path, monkeypatch: Any) -> None:
    """Never apply bytes read while the file is being replaced or edited."""
    path = tmp_path / "config.ini"
    path.write_text(CONFIG)
    watcher = ConfigReloader(str(path), lambda _config: None)
    edit(path, CONFIG.replace("12.5", "25"))
    read_bytes = Path.read_bytes

    def racing_read(file: Path) -> bytes:
        """Simulate a second editor save between the before/after stat calls."""
        contents = read_bytes(file)
        edit(path, CONFIG.replace("12.5", "30"))
        return contents

    monkeypatch.setattr(Path, "read_bytes", racing_read)
    assert watcher.poll() is None
    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    assert watcher.poll()["weather"]["latitude"] == "30"


def test_reload_flag_default_disabled(tmp_path: Path, monkeypatch: Any) -> None:
    """Default daemon operation never creates a watcher."""
    import rv_control.cli as module

    path = tmp_path / "config.ini"
    path.write_text(CONFIG)
    sessions: list[Any] = []
    monkeypatch.setattr(module, "_run_session", lambda config, watcher: sessions.append(watcher))
    result = CliRunner().invoke(cli, ["--config", str(path), "run"])
    assert result.exit_code == 0, result.output
    assert sessions == [None]


def test_reload_cleans_up_before_restarting(tmp_path: Path, monkeypatch: Any) -> None:
    """Exercise real session wiring with deterministic polling and fake hardware."""
    import rv_control.cli as module

    monkeypatch.setattr(logging.getLogger(), "level", logging.getLogger().level)
    path = tmp_path / "config.ini"
    path.write_text(CONFIG)
    events: list[str] = []
    contexts: list[Any] = []

    class Publisher:
        """Track target lifecycle without a broker."""

        def connect(self) -> None:
            """Record connection."""
            events.append("connect")

        def close(self) -> None:
            """Record shutdown."""
            events.append("close")

    class Source:
        """Record source cleanup and support the reload overlap check."""

        def join(self, timeout: float) -> None:
            """Record source shutdown."""
            events.append("join")

        def is_alive(self) -> bool:
            """Report a fully stopped source."""
            return False

    def build(*_args: Any) -> Publisher:
        """Create a target for each generation."""
        events.append("build")
        return Publisher()

    def supervise(config: Any, publisher: Any, _stop: Any, _sources: Any, _map: Any, **kwargs: Any) -> None:
        """Deliver invalid, touched and valid saves during the first generation."""
        contexts.append(publisher.runtime_context)
        events.append("run:" + config["weather"]["latitude"])
        if len(contexts) == 1:
            edit(path, "bad ini")
            kwargs["on_tick"]()
            assert not _stop.is_set()
            edit(path, CONFIG)
            kwargs["on_tick"]()
            assert not _stop.is_set()
            edit(path, CONFIG.replace("12.5", "25"))
            kwargs["on_tick"]()
            assert _stop.is_set()

    monkeypatch.setattr(module.Target, "build", build)
    monkeypatch.setattr(module, "_validate_reload", lambda config: module.RuntimeContext(config).validate_sources(config))
    monkeypatch.setattr(module.Source, "start_enabled", lambda *_args: ([Source()], {}))
    monkeypatch.setattr(module.Source, "run_until_stopped", supervise)
    result = CliRunner().invoke(cli, ["--config", str(path), "--reload-config", "run"])
    assert result.exit_code == 0, result.output
    assert events == ["build", "connect", "run:12.5", "join", "close",
                      "build", "connect", "run:25", "join", "close"]
    assert contexts[0] is not contexts[1]


def test_supervisor_polls_and_stops_before_restarting_sources() -> None:
    """A reload callback interrupts the normal supervisor loop without extra threads."""
    import threading

    from rv_control.sources.base import Source

    stop = threading.Event()
    calls: list[bool] = []

    def on_tick() -> None:
        """Request a reload stop at the supervision boundary."""
        calls.append(True)
        stop.set()

    Source.run_until_stopped(None, None, stop, [], {}, on_tick=on_tick)
    assert calls == [True]
