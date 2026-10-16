from __future__ import annotations

from pathlib import Path
from typing import Any

from click.testing import CliRunner

from rv_control.cli import cli


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