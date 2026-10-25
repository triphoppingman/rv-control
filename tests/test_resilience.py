from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any

import pytest

from rv_control.config import load_config
from rv_control.sources.hughes import HughesSource
from rv_control.sources.renogy import RenogySource
from rv_control.sources.base import Source


def test_source_supervisor_restarts_dead_source() -> None:
    """Verify the source supervisor replaces a stopped source instance."""
    stop_event = threading.Event()

    class RestartableSource:
        source_name = "fake"
        config_section = "fake"
        instances = 0

        def __init__(self, _config: Any, _publisher: Any, event: threading.Event, section_name: str = "fake") -> None:
            """Track fake instances and retain the supervisor stop event."""
            self.event = event
            self.section_name = section_name
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


def test_source_base_binds_type_and_configured_section_to_publish() -> None:
    """Bind source provenance once in the base class instead of each source."""
    publications = []

    class Publisher:
        """Capture provenance provided by the source-bound publisher view."""

        def publish_source(
            self, topic: str, payload: dict[str, Any], source_type: str, source_section: str,
        ) -> None:
            """Record the original event and its source identity."""
            publications.append((topic, payload, source_type, source_section))

    class SourceStub(Source):
        """Provide the minimal abstract interface for source publisher binding."""

        source_name = "source-publisher-binding"

        def run(self) -> None:
            """Perform no collection in this publisher-only test."""

        def comms_check(self) -> dict[str, Any]:
            """Report the stub as available."""
            return {"ok": True}

    source = SourceStub(None, Publisher(), threading.Event(), "renogy_battery")
    source.publisher.publish("renogy", {"voltage": 12.4})

    assert publications == [
        ("renogy", {"voltage": 12.4}, "source-publisher-binding", "renogy_battery"),
    ]


def test_start_enabled_stops_started_sources_when_later_construction_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify partial source startup is rolled back when a later constructor fails."""
    stop_event = threading.Event()
    started: list[WaitingSource] = []

    class WaitingSource(threading.Thread):
        """Represent a source thread that exits when its shared stop event is set."""

        source_name = "waiting"
        section_name = "waiting"

        def __init__(self, _config: Any, _publisher: Any, event: threading.Event, _name: str) -> None:
            """Retain the shutdown event for the fake source worker."""
            super().__init__(daemon=True)
            self.stop_event = event
            started.append(self)

        def run(self) -> None:
            """Wait until startup rollback signals shutdown."""
            self.stop_event.wait()

    class FailingSource:
        """Fail construction to exercise rollback of previously started threads."""

        source_name = "failing"

        def __init__(self, *_args: Any) -> None:
            """Raise the configured construction failure."""
            raise ValueError("source construction failed")

    monkeypatch.setattr(
        Source,
        "enabled_sources",
        classmethod(lambda _cls, _config: (("waiting", WaitingSource), ("failing", FailingSource))),
    )

    with pytest.raises(ValueError, match="source construction failed"):
        Source.start_enabled(None, None, stop_event)

    assert stop_event.is_set()
    assert len(started) == 1
    assert not started[0].is_alive()


def test_source_shutdown_keeps_waiting_for_live_threads_after_grace_period() -> None:
    """Do not return from source shutdown while a source can still publish."""
    stop_event = threading.Event()
    release = threading.Event()

    class SlowSource(threading.Thread):
        """Represent a source that takes longer than the grace period to stop."""

        section_name = "slow"

        def run(self) -> None:
            """Remain active until the test releases this source."""
            release.wait()

    source = SlowSource()
    source.start()
    shutdown = threading.Thread(
        target=Source.stop_and_join,
        args=([source], stop_event, 0.01),
    )
    shutdown.start()
    shutdown.join(timeout=0.05)

    assert shutdown.is_alive()
    assert source.is_alive()
    assert stop_event.is_set()
    release.set()
    shutdown.join(timeout=1)

    assert not shutdown.is_alive()
    assert not source.is_alive()


def test_renogy_config_propagates_adapter_and_one_shot_mode(config_file: Path) -> None:
    """Verify Renogy configuration preserves adapter and one-shot settings."""
    config = load_config(str(config_file))
    config["renogy"]["adapter"] = "hci1"
    source = RenogySource(config, None, threading.Event())
    client_config = source._client_config(persistent_connection=False)
    assert client_config["device"]["adapter"] == "hci1"
    assert client_config["device"]["persistent_connection"] == "false"


def test_renogy_config_propagates_ble_retry_settings(config_file: Path) -> None:
    """Verify Renogy retry and timeout settings reach the BLE client configuration."""
    config = load_config(str(config_file))
    config["renogy"].update({
        "max_retry": "7",
        "reconnect_delay": "3",
        "max_reconnect_delay": "45",
        "discovery_timeout": "9",
        "read_timeout": "20",
    })
    source = RenogySource(config, None, threading.Event())
    client_config = source._client_config(persistent_connection=True)

    assert client_config["device"].getint("max_retry") == 7
    assert client_config["device"].getfloat("reconnect_delay") == 3
    assert client_config["device"].getfloat("max_reconnect_delay") == 45
    assert client_config["device"].getfloat("discovery_timeout") == 9
    assert client_config["data"].getfloat("read_timeout") == 20


def test_multiple_source_sections_resolve_by_type(config_file: Path) -> None:
    """Verify multiple configured sections instantiate the selected source type."""
    config = load_config(str(config_file))
    config["source"]["enabled-sources"] = "renogy_controller, renogy_battery"
    config["renogy_controller"] = dict(config["renogy"])
    config["renogy_battery"] = dict(config["renogy"])
    configured = Source.enabled_sources(config)
    source_map = {name: source_type(config, None, threading.Event(), name) for name, source_type in configured}
    assert set(source_map) == {"renogy_controller", "renogy_battery"}
    assert all(isinstance(source, RenogySource) for source in source_map.values())


def test_hughes_reconnects_after_session_failure(config_file: Path) -> None:
    """Verify Hughes reconnects after a transient BLE session failure."""
    config = load_config(str(config_file))
    config["hughes"]["persistent_connection"] = "true"
    config["service"]["reconnect_delay"] = "0"
    source = HughesSource(config, None, threading.Event())
    calls = []

    class FakeClient:
        def __init__(self, _address: str, **_kwargs: Any) -> None:
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


def test_hughes_reconnect_wait_is_interrupted_by_shutdown(config_file: Path) -> None:
    """Verify shutdown wakes a Hughes reconnect delay immediately."""
    config = load_config(str(config_file))
    config["hughes"]["persistent_connection"] = "true"
    config["service"]["reconnect_delay"] = "300"
    config["service"]["max_reconnect_delay"] = "300"
    source = HughesSource(config, None, threading.Event())
    calls = 0

    async def fail_session(*_args: Any) -> None:
        """Fail the first BLE session to enter the reconnect delay."""
        nonlocal calls
        calls += 1
        raise OSError("temporary failure")

    source._run_ble_session = fail_session

    async def run_until_shutdown() -> None:
        """Set the stop event during the long retry delay and await prompt exit."""
        asyncio.get_running_loop().call_later(0.02, source.stop_event.set)
        await asyncio.wait_for(
            source._run_ble(lambda *_args: None, "AA:BB:CC:DD:EE:FF"),
            timeout=0.5,
        )

    asyncio.run(run_until_shutdown())
    assert calls == 1