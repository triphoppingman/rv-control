from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any

from rv_control.config import load_config
from rv_control.hughes import HughesSource
from rv_control.renogy import RenogySource
from rv_control.source import Source


def test_source_supervisor_restarts_dead_source() -> None:
    """Verify the source supervisor replaces a stopped source instance."""
    stop_event = threading.Event()

    class RestartableSource:
        source_name = "fake"
        config_section = "fake"
        instances = 0

        def __init__(self, _config: Any, _publisher: Any, event: threading.Event) -> None:
            """Track fake instances and retain the supervisor stop event."""
            self.event = event
            RestartableSource.instances += 1

        def start(self) -> None:
            """Stop the supervisor after the replacement starts."""
            if RestartableSource.instances > 1:
                self.event.set()

        def is_alive(self) -> bool:
            """Report a stopped fake thread so restart logic is exercised."""
            return False

    sources = [RestartableSource(None, None, stop_event)]
    source_map = {"fake": sources[0]}
    Source.run_until_stopped(None, None, stop_event, sources, source_map)
    assert RestartableSource.instances == 2
    assert source_map["fake"] is sources[0]


def test_renogy_config_propagates_adapter_and_one_shot_mode(config_file: Path) -> None:
    """Verify Renogy configuration preserves adapter and one-shot settings."""
    config = load_config(str(config_file))
    config["renogy"]["adapter"] = "hci1"
    source = RenogySource(config, None, threading.Event())
    client_config = source._client_config(persistent_connection=False)
    assert client_config["device"]["adapter"] == "hci1"
    assert client_config["device"]["persistent_connection"] == "false"


def test_hughes_reconnects_after_session_failure(config_file: Path) -> None:
    """Verify Hughes reconnects after a transient BLE session failure."""
    config = load_config(str(config_file))
    config["hughes"]["persistent_connection"] = "true"
    config["service"]["reconnect_delay"] = "0"
    source = HughesSource(config, None, threading.Event())
    calls = []

    class FakeClient:
        def __init__(self, _address: str) -> None:
            """Record each attempted BLE client construction."""
            calls.append("construct")

        async def __aenter__(self) -> Any:
            """Fail the first session and allow the reconnect to proceed."""
            if len(calls) == 1:
                raise OSError("temporary failure")
            return self

        async def __aexit__(self, *_args: Any) -> bool:
            """Leave exceptions visible to the reconnect loop."""
            return False

        @property
        def services(self) -> list[Any]:
            """Expose no services, selecting the legacy protocol path."""
            return []

        async def start_notify(self, _tx: str, _callback: Any) -> None:
            """End the successful retry once notifications are registered."""
            source.stop_event.set()

        async def stop_notify(self, _tx: str) -> None:
            """Accept notification cleanup without external BLE resources."""
            pass

    asyncio.run(source._run_ble(FakeClient, "AA:BB:CC:DD:EE:FF"))
    assert calls == ["construct", "construct"]