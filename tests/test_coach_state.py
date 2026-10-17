"""Tests for coach entity state tracking and MQTT entity publishing."""

from __future__ import annotations

import configparser
from typing import Any

import pytest

from rv_control.coach import Coach
from rv_control.coach_state import CoachStatePublisher, EntityStateTracker, wrap_publisher
from rv_control.rvc_util import decode_dgn, load_spec

SPEC = load_spec()


class FakeClock:
    """Manually advanced monotonic clock."""

    def __init__(self) -> None:
        """Start the clock at zero."""
        self.now = 0.0

    def __call__(self) -> float:
        """Return the current fake time."""
        return self.now


class RecordingTarget:
    """Publisher double recording connect/publish/close calls."""

    def __init__(self) -> None:
        """Create empty call records."""
        self.published: list[tuple[str, dict[str, Any]]] = []
        self.connected = False
        self.closed = False

    def connect(self) -> None:
        """Record a connect."""
        self.connected = True

    def publish(self, topic: str, payload: dict[str, Any]) -> None:
        """Record a publish."""
        self.published.append((topic, payload))

    def close(self) -> None:
        """Record a close."""
        self.closed = True


def coach_config(**options: str) -> configparser.ConfigParser:
    """Build a config selecting the Thor coach file."""
    config = configparser.ConfigParser()
    config.read_dict({"coach": {"specfile": "thor-magnitude-bh35-2020", **options}})
    return config


@pytest.fixture()
def coach() -> Coach:
    """Load the Thor Magnitude coach entity map."""
    return Coach.load(coach_config())


def tracker_for(coach: Coach, republish: float = 60.0) -> tuple[EntityStateTracker, list, FakeClock]:
    """Build a tracker recording its publishes, with a fake clock."""
    published: list[tuple[str, dict[str, Any]]] = []
    clock = FakeClock()
    tracker = EntityStateTracker(coach, lambda topic, payload: published.append((topic, payload)), republish, clock)
    return tracker, published, clock


def dimmer_status(instance: int, on: bool, brightness: int = 200, delay: int = 0xFF) -> dict[str, Any]:
    """Decode a DC_DIMMER_STATUS_3 frame for one instance."""
    data = f"{instance:02X}FF{brightness if on else 0:02X}FC{delay:02X}{5 if on else 3:02X}{0x04 if on else 0x00:02X}FF"
    return decode_dgn("1FEDA", data, SPEC)


def test_mid_bath_fan_publishes_on_and_off(coach: Coach) -> None:
    """A fan's status frames publish boolean state to coach/entity/<name>."""
    tracker, published, _ = tracker_for(coach)
    instance = coach.entities["mid_bath_fan"]["instance"]
    tracker.handle(dimmer_status(instance, True))
    tracker.handle(dimmer_status(instance, False))
    topics = [topic for topic, _ in published]
    assert topics == ["coach/entity/mid_bath_fan", "coach/entity/mid_bath_fan"]
    assert published[0][1]["state"]["on"] is True
    assert published[1][1]["state"]["on"] is False
    assert published[0][1]["entity"] == "mid_bath_fan"
    assert published[0][1]["kind"] == coach.entities["mid_bath_fan"]["kind"]
    assert tracker.state("mid_bath_fan")["on"] is False


def test_unchanged_state_republishes_only_after_interval(coach: Coach) -> None:
    """Repeated identical status is suppressed until the republish interval passes."""
    tracker, published, clock = tracker_for(coach, republish=60)
    instance = coach.entities["mid_bath_fan"]["instance"]
    tracker.handle(dimmer_status(instance, True))
    clock.now = 30
    tracker.handle(dimmer_status(instance, True))
    assert len(published) == 1
    clock.now = 61
    tracker.handle(dimmer_status(instance, True))
    assert len(published) == 2


def test_republish_disabled_with_zero(coach: Coach) -> None:
    """A zero republish interval publishes on change only."""
    tracker, published, clock = tracker_for(coach, republish=0)
    instance = coach.entities["mid_bath_fan"]["instance"]
    tracker.handle(dimmer_status(instance, True))
    clock.now = 1000
    tracker.handle(dimmer_status(instance, True))
    assert len(published) == 1


def test_dimmable_light_level_and_unmapped_instance_ignored(coach: Coach) -> None:
    """Brightness is reported in percent and unmapped instances publish nothing."""
    tracker, published, _ = tracker_for(coach)
    instance = coach.entities["living_ceiling_light"]["instance"]
    tracker.handle(dimmer_status(instance, True, brightness=64))
    tracker.handle(dimmer_status(0x7E, True))
    assert len(published) == 1
    assert published[0][1]["state"]["level"] == 32.0
    assert published[0][1]["state"]["remaining"] is None


def test_tank_level_percent(coach: Coach) -> None:
    """Tank level is relative level divided by resolution, as a percent."""
    tracker, published, _ = tracker_for(coach)
    instance = coach.entities["fresh_tank"]["instance"]
    tracker.handle(decode_dgn("1FFB7", f"{instance:02X}0103FFFFFFFF", SPEC))
    assert published[-1][0] == "coach/entity/fresh_tank"
    assert published[-1][1]["state"]["level"] == 33


def test_generator_status_and_runtime(coach: Coach) -> None:
    """Generator status maps to a label and runtime minutes convert to hours."""
    tracker, published, _ = tracker_for(coach)
    runtime_minutes = 8892
    data = f"03{runtime_minutes & 0xFF:02X}{runtime_minutes >> 8 & 0xFF:02X}0000FFFFFF"
    tracker.handle(decode_dgn("1FFDC", data, SPEC))
    state = tracker.state("generator")
    assert state["status"] == "running"
    assert state["run_hours"] == 148.2


def test_timed_pair_reports_each_direction(coach: Coach) -> None:
    """A timed pair entity reports each of its two instances under separate attributes."""
    tracker, _, _ = tracker_for(coach)
    lid = coach.entities["kitchen_fan_lid"]
    tracker.handle(dimmer_status(lid["first"], True, delay=20))
    state = tracker.state("kitchen_fan_lid")
    assert state[f"{lid['first_action']}_active"] is True
    assert state[f"{lid['first_action']}_remaining"] == 20
    tracker.handle(dimmer_status(lid["second"], False))
    state = tracker.state("kitchen_fan_lid")
    assert state[f"{lid['second_action']}_active"] is False
    assert state[f"{lid['first_action']}_active"] is True


def test_wrapper_forwards_and_tracks() -> None:
    """The wrapper forwards raw publishes and adds entity state publishes."""
    target = RecordingTarget()
    wrapped = wrap_publisher(coach_config(), target)
    assert isinstance(wrapped, CoachStatePublisher)
    wrapped.connect()
    instance = wrapped.tracker.coach.entities["mid_bath_fan"]["instance"]
    decoded = dimmer_status(instance, True)
    wrapped.publish("rvc/DC_DIMMER_STATUS_3", decoded)
    wrapped.publish("hughes/line1", {"volts": 120})
    wrapped.close()
    assert target.connected and target.closed
    assert [topic for topic, _ in target.published] == [
        "rvc/DC_DIMMER_STATUS_3",
        "coach/entity/mid_bath_fan",
        "hughes/line1",
    ]


def test_wrapper_survives_tracker_errors(caplog: pytest.LogCaptureFixture) -> None:
    """A tracker failure is logged and the raw publish still happens."""
    target = RecordingTarget()
    wrapped = wrap_publisher(coach_config(), target)

    def broken(decoded: dict[str, Any]) -> None:
        """Simulate a tracker bug."""
        raise RuntimeError("boom")

    wrapped.tracker.handle = broken  # type: ignore[method-assign]
    wrapped.publish("rvc/X", {"dgn": "1FEDA"})
    assert target.published == [("rvc/X", {"dgn": "1FEDA"})]
    assert "Coach entity state update failed" in caplog.text


def test_wrap_publisher_disabled() -> None:
    """No [coach] section or publish_state = false leaves the publisher unwrapped."""
    target = RecordingTarget()
    assert wrap_publisher(configparser.ConfigParser(), target) is target
    assert wrap_publisher(coach_config(publish_state="false"), target) is target


def test_all_entity_state_blocks_compile(coach: Coach) -> None:
    """Every entity with a state block in the coach file is tracked."""
    tracker, _, _ = tracker_for(coach)
    expected = sorted(name for name, entity in coach.entities.items() if entity.get("state"))
    assert tracker.tracked_entities == expected
    assert "mid_bath_fan" in expected


class FakeRvcSource:
    """Source double recording status requests."""

    def __init__(self) -> None:
        """Create an empty request record."""
        self.requested: list[int] = []

    def request_status(self, dgns: list[int]) -> None:
        """Record requested DGNs."""
        self.requested.extend(dgns)


def test_status_requests_grouped_by_section() -> None:
    """Every tracked RV-C status DGN is requested from the mapped RV-C section."""
    tracked = Coach.load(coach_config(rvc="rv_c"))
    tracker, _, _ = tracker_for(tracked)
    requests = tracker.status_requests()
    assert set(requests) == {"rv_c"}
    assert {0x1FEDA, 0x1FFB7, 0x1FFDC, 0x1FFE2, 0x1FF9C} <= set(requests["rv_c"])


def test_wrapper_requests_initial_status_once() -> None:
    """The wrapper asks the mapped RV-C source for status, unless disabled."""
    source = FakeRvcSource()
    wrapped = wrap_publisher(coach_config(rvc="rv_c"), RecordingTarget())
    wrapped.request_initial_status({"rv_c": source, "hughes": object()})
    assert 0x1FEDA in source.requested

    disabled_source = FakeRvcSource()
    disabled = wrap_publisher(coach_config(rvc="rv_c", request_status_on_start="false"), RecordingTarget())
    disabled.request_initial_status({"rv_c": disabled_source})
    assert disabled_source.requested == []
