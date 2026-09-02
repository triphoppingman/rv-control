from __future__ import annotations

from pathlib import Path
from typing import Any

from click.testing import CliRunner

from rv_control.cli import cli


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
    monkeypatch.setattr(cli_module, "MqttPublisher", lambda *_args, **_kwargs: publisher)
    monkeypatch.setattr(cli_module.Source, "start_enabled", lambda *_args: (_ for _ in ()).throw(OSError("startup")))
    result = CliRunner().invoke(cli, ["--config", str(config_file), "run"])
    assert result.exit_code != 0
    assert publisher.closed