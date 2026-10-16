from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from rv_control.cli import cli


@pytest.mark.parametrize("name", ["root", "rv_control.sources.hughes"])
@pytest.mark.parametrize("level", [
    logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR, logging.CRITICAL,
])
def test_log_format_identifies_every_logger_call_site(
    monkeypatch: Any, config_file: Path, name: str, level: int,
) -> None:
    """Include file, line, and function for every logger and standard severity."""
    import rv_control.cli as cli_module

    settings: dict[str, Any] = {}
    monkeypatch.setattr(cli_module.logging, "basicConfig", lambda **kwargs: settings.update(kwargs))

    result = CliRunner().invoke(cli, ["--config", str(config_file), "check"])
    assert result.exit_code == 0
    record = logging.LogRecord(
        name, level, "/example/BaseClient.py", 123,
        "Connection failed: TimeoutError()", (), None, func="__on_connect_fail",
    )
    output = logging.Formatter(settings["format"]).format(record)
    assert (
        f"{logging.getLevelName(level)} {name} "
        "[BaseClient.py:123 __on_connect_fail]: Connection failed: TimeoutError()"
    ) in output


def test_configured_log_level(monkeypatch: Any, config_file: Path) -> None:
    """Use the configured logging threshold unless verbose mode is requested."""
    import rv_control.cli as cli_module

    levels: list[int] = []
    monkeypatch.setattr(
        cli_module.logging,
        "basicConfig",
        lambda **kwargs: levels.append(kwargs["level"]),
    )
    config_file.write_text(config_file.read_text().replace(
        "daemon_mode = true",
        "daemon_mode = true\nlog_level = WARNING",
    ))

    result = CliRunner().invoke(cli, ["--config", str(config_file), "check"])
    assert result.exit_code == 0
    assert levels[-1] == cli_module.logging.WARNING

    result = CliRunner().invoke(cli, ["--verbose", "--config", str(config_file), "check"])
    assert result.exit_code == 0
    assert levels[-1] == cli_module.logging.DEBUG


def test_invalid_configured_log_level(config_file: Path) -> None:
    """Reject unknown logging thresholds with a clear CLI error."""
    config_file.write_text(config_file.read_text().replace(
        "daemon_mode = true",
        "daemon_mode = true\nlog_level = VERBOSE",
    ))

    result = CliRunner().invoke(cli, ["--config", str(config_file), "check"])
    assert result.exit_code != 0
    assert "Invalid [service] log_level 'VERBOSE'" in result.output


def test_run_closes_mqtt_when_source_startup_fails(monkeypatch: Any, config_file: Path) -> None:
    """Verify CLI shutdown closes MQTT when source startup raises an error."""
    import rv_control.cli as cli_module

    class FakePublisher:
        closed = False

        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            """Provide a no-op publisher constructor for the CLI test."""
            pass

        def connect(self) -> None:
            """Simulate a successful broker connection."""
            pass

        def close(self) -> None:
            """Record the cleanup call made by the CLI."""
            self.closed = True

    publisher = FakePublisher()
    monkeypatch.setattr(cli_module.Target, "build", lambda *_args, **_kwargs: publisher)
    monkeypatch.setattr(cli_module.Source, "start_enabled", lambda *_args: (_ for _ in ()).throw(OSError("startup")))
    result = CliRunner().invoke(cli, ["--config", str(config_file), "run"])
    assert result.exit_code != 0
    assert publisher.closed