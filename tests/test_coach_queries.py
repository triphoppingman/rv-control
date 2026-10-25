"""Tests for read-only coach state query operations."""

from __future__ import annotations

import configparser
from typing import Any, Callable

import pytest

from rv_control.coach import Coach
from rv_control.coach_queries import CoachQueries
from rv_control.coach_state import EntityStateTracker


class FakeSource:
    """Source double recording status requests and optionally emitting telemetry."""

    supports_status_requests = True

    def __init__(self, callback: Callable[[list[int]], None] | None = None) -> None:
        """Create a live fake source with an optional request callback."""
        self.callback = callback
        self.requests: list[list[int]] = []
        self.alive = True
        self.error: Exception | None = None

    def request_status(self, status_ids: list[int]) -> None:
        """Record deduplicated source status identifiers and invoke the callback."""
        if self.error is not None:
            raise self.error
        self.requests.append(list(status_ids))
        if self.callback is not None:
            self.callback(status_ids)

    def is_alive(self) -> bool:
        """Return whether the source is available to service monitoring requests."""
        return self.alive


class UnsupportedSource:
    """Source double that deliberately has no approved status request interface."""

    supports_status_requests = False


@pytest.fixture
def coach() -> Coach:
    """Load the default coach entity definitions with an RV-C section mapping."""
    config = configparser.ConfigParser()
    config.read_dict({"coach": {"specfile": "thor-magnitude-bh35-2020", "rvc": "rv_c_bus"}})
    return Coach.load(config)


def dimmer_status(coach: Coach, entity: str, *, omit: str | None = None) -> dict[str, Any]:
    """Build a decoded status message for a selected dimmer entity."""
    payload: dict[str, Any] = {
        "dgn": "1FEDA",
        "instance": coach.entities[entity]["instance"],
        "loadstatus": 1,
        "operatingstatusbrightness": 80,
        "delay_duration": 255,
    }
    if omit is not None:
        payload.pop(omit)
    return payload


def build_queries(
    coach: Coach,
    sources: dict[str, Any] | None = None,
) -> tuple[CoachQueries, EntityStateTracker, list[tuple[str, dict[str, Any]]]]:
    """Construct a tracker, captured publisher, and query service."""
    published: list[tuple[str, dict[str, Any]]] = []
    tracker = EntityStateTracker(coach, lambda topic, payload: published.append((topic, payload)))
    return CoachQueries(coach, tracker, sources or {}), tracker, published


def test_entities_and_source_sections_expose_only_monitored_state(coach: Coach) -> None:
    """The query interface lists mapped state attributes and their source section."""
    queries, _, _ = build_queries(coach)
    fields = queries.entities()
    assert "mid_bath_fan" in fields
    assert fields["mid_bath_fan"] == ["level", "on", "remaining"]
    assert "patio_light" not in fields
    assert queries.source_sections(["mid_bath_fan"]) == {"rv_c_bus"}


def test_snapshot_is_cached_and_reports_missing_fields(coach: Coach) -> None:
    """Snapshot reports cached values and missing fields without polling sources."""
    source = FakeSource()
    queries, _, _ = build_queries(coach, {"rv_c_bus": source})

    result = queries.run("snapshot", {}, ["mid_bath_fan"], remaining_s=5)

    assert result["status"] == "completed"
    assert result["code"] == "queried"
    entity = result["data"]["entities"][0]
    assert entity["state"] == {}
    assert entity["missing_fields"] == ["level", "on", "remaining"]
    assert entity["observed_at"] == {"level": None, "on": None, "remaining": None}
    assert source.requests == []


def test_query_service_reuses_compiled_monitored_fields(coach: Coach, monkeypatch: Any) -> None:
    """Query selection and snapshots do not rebuild the tracker field index."""
    queries, _, _ = build_queries(coach)

    def unexpected_rebuild() -> dict[str, list[str]]:
        """Fail if request handling asks the tracker to reconstruct its metadata."""
        pytest.fail("monitored fields should be cached by tracker and query service")

    monkeypatch.setattr(queries.tracker, "monitored_fields", unexpected_rebuild)

    assert "mid_bath_fan" in queries.entities()
    result = queries.run("snapshot", {}, ["mid_bath_fan"], remaining_s=5)
    assert result["data"]["entities"][0]["entity"] == "mid_bath_fan"


def test_prevalidated_entities_in_params_match_selected_entities(coach: Coach) -> None:
    """The service may pass its prevalidated entity selection in both API inputs."""
    queries, _, _ = build_queries(coach)

    result = queries.run(
        "snapshot",
        {"entities": ["mid_bath_fan"]},
        ["mid_bath_fan"],
        remaining_s=5,
    )

    assert result["status"] == "completed"
    with pytest.raises(ValueError, match="match"):
        queries.run(
            "snapshot",
            {"entities": ["rear_bath_fan"]},
            ["mid_bath_fan"],
            remaining_s=5,
        )


def test_republish_returns_existing_and_missing_entities_without_changing_updated(coach: Coach) -> None:
    """Republish preserves the original payload timestamp and reports uncached entities."""
    queries, tracker, published = build_queries(coach)
    tracker.handle(dimmer_status(coach, "mid_bath_fan"))
    original = dict(published[-1][1])
    before = tracker.snapshots(["mid_bath_fan"])

    result = queries.run(
        "republish_states",
        {},
        ["mid_bath_fan", "rear_bath_fan"],
        remaining_s=5,
    )

    assert result == {
        "status": "completed",
        "code": "republished",
        "data": {
            "published_entities": ["mid_bath_fan"],
            "missing_entities": ["rear_bath_fan"],
        },
    }
    assert published[-1] == ("coach/entity/mid_bath_fan", original)
    assert tracker.snapshots(["mid_bath_fan"]) == before


def test_refresh_counts_unchanged_post_request_telemetry(coach: Coach) -> None:
    """Unchanged state counts as a fresh observation after a status request."""
    queries, tracker, _ = build_queries(coach)
    tracker.handle(dimmer_status(coach, "mid_bath_fan"))
    source = FakeSource(lambda _dgns: tracker.handle(dimmer_status(coach, "mid_bath_fan")))
    queries.sources["rv_c_bus"] = source

    result = queries.run(
        "refresh_states",
        {"timeout_s": 1},
        ["mid_bath_fan"],
        remaining_s=1,
    )

    assert source.requests == [[0x1FEDA]]
    assert result["status"] == "completed"
    assert result["code"] == "observed"
    entity = result["data"]["entities"][0]
    assert entity["status"] == "observed"
    assert entity["observed_fields"] == ["level", "on", "remaining"]
    assert entity["missing_fields"] == []
    assert entity["observed_at"] is not None


def test_refresh_deduplicates_shared_requests(coach: Coach) -> None:
    """Refreshing multiple entities on a source issues one request per DGN."""
    queries, tracker, _ = build_queries(coach)

    def report_all(_dgns: list[int]) -> None:
        """Emit current state for both selected lights."""
        tracker.handle(dimmer_status(coach, "mid_bath_fan"))
        tracker.handle(dimmer_status(coach, "rear_bath_fan"))

    source = FakeSource(report_all)
    queries.sources["rv_c_bus"] = source

    queries.run(
        "refresh_states",
        {"timeout_s": 1},
        ["mid_bath_fan", "rear_bath_fan"],
        remaining_s=1,
    )

    assert source.requests == [[0x1FEDA]]


def test_refresh_distinguishes_partial_unsupported_and_unavailable(coach: Coach) -> None:
    """Refresh results distinguish partial observations from unavailable sources."""
    queries, tracker, _ = build_queries(coach)

    def report_partial(_dgns: list[int]) -> None:
        """Emit only the mapped ``on`` and ``level`` fields."""
        tracker.handle(dimmer_status(coach, "mid_bath_fan", omit="delay_duration"))

    source = FakeSource(report_partial)
    queries.sources["rv_c_bus"] = source
    partial = queries.run(
        "refresh_states",
        {"timeout_s": 1},
        ["mid_bath_fan"],
        remaining_s=1,
    )
    assert partial["status"] == "completed"
    assert partial["code"] == "partial"
    assert partial["data"]["entities"][0]["status"] == "partial"
    assert partial["data"]["entities"][0]["missing_fields"] == ["remaining"]

    unsupported_queries, _, _ = build_queries(coach, {"rv_c_bus": UnsupportedSource()})
    unsupported = unsupported_queries.run(
        "refresh_states",
        {"timeout_s": 1},
        ["mid_bath_fan"],
        remaining_s=1,
    )
    assert unsupported["status"] == "failed"
    assert unsupported["code"] == "unsupported"
    assert unsupported["data"]["entities"][0]["status"] == "unsupported"

    unavailable_queries, _, _ = build_queries(coach)
    unavailable = unavailable_queries.run(
        "refresh_states",
        {"timeout_s": 1},
        ["mid_bath_fan"],
        remaining_s=1,
    )
    assert unavailable["status"] == "failed"
    assert unavailable["code"] == "source_unavailable"
    assert unavailable["data"]["entities"][0]["status"] == "source_unavailable"


def test_refresh_request_failure_is_logged_and_reported(coach: Coach, caplog: pytest.LogCaptureFixture) -> None:
    """A failed source request is logged and represented as source unavailability."""
    source = FakeSource()
    source.error = RuntimeError("injected request failure")
    queries, _, _ = build_queries(coach, {"rv_c_bus": source})

    result = queries.run("refresh_states", {"timeout_s": 1}, ["mid_bath_fan"], remaining_s=1)

    assert result["data"]["entities"][0]["status"] == "source_unavailable"
    assert "Status request failed for source [rv_c_bus]" in caplog.text


def test_refresh_validates_timeout_and_expiry_before_dispatch(coach: Coach) -> None:
    """Invalid timeout parameters and insufficient lifetime never query a source."""
    source = FakeSource()
    queries, _, _ = build_queries(coach, {"rv_c_bus": source})

    with pytest.raises(ValueError, match="timeout_s"):
        queries.run("refresh_states", {"timeout_s": True}, ["mid_bath_fan"], remaining_s=5)
    with pytest.raises(ValueError, match="Unknown parameter"):
        queries.run(
            "refresh_states",
            {"timeout_s": 1, "dgns": [0x1FEDA]},
            ["mid_bath_fan"],
            remaining_s=5,
        )

    result = queries.run("refresh_states", {"timeout_s": 2}, ["mid_bath_fan"], remaining_s=1.9)

    assert result == {"status": "failed", "code": "expired", "data": {"entities": []}}
    assert source.requests == []


@pytest.mark.parametrize(
    "selection",
    [
        [],
        ["mid_bath_fan", "mid_bath_fan"],
        ["not-a-coach-entity"],
    ],
)
def test_query_rejects_empty_duplicate_and_unknown_entities(coach: Coach, selection: list[str]) -> None:
    """Selections must be non-empty, unique, and known before any operation."""
    queries, _, _ = build_queries(coach)
    with pytest.raises(ValueError):
        queries.run("snapshot", {}, selection, remaining_s=5)


def test_refresh_timeout_without_observations(coach: Coach) -> None:
    """A safe request with no resulting telemetry returns a refresh timeout."""
    queries, _, _ = build_queries(coach, {"rv_c_bus": FakeSource()})

    result = queries.run("refresh_states", {"timeout_s": 1}, ["mid_bath_fan"], remaining_s=1)

    assert result["status"] == "failed"
    assert result["code"] == "refresh_timeout"
    assert result["data"]["entities"][0]["status"] == "timed_out"
