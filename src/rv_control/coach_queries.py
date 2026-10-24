"""Read-only monitoring operations for cached coach entity state."""

from __future__ import annotations

import logging
import math
import time
from typing import Any

from .coach import Coach
from .coach_state import EntityStateTracker
from .sources.base import Source

LOGGER = logging.getLogger(__name__)
DEFAULT_REFRESH_TIMEOUT_S = 5
MIN_REFRESH_TIMEOUT_S = 1
MAX_REFRESH_TIMEOUT_S = 10
MAX_SELECTED_ENTITIES = 200


class CoachQueries:
    """Run bounded snapshot, republish, and refresh operations for coach entities."""

    def __init__(
        self,
        coach: Coach,
        tracker: EntityStateTracker,
        sources: dict[str, Source],
    ) -> None:
        """Store the loaded coach, state tracker, and already-running sources."""
        self.coach = coach
        self.tracker = tracker
        self.sources = sources

    def entities(self) -> dict[str, list[str]]:
        """Return every monitored entity and its mapped state fields."""
        return self.tracker.monitored_fields()

    def source_sections(self, entities: list[str]) -> set[str]:
        """Return source configuration sections serving the selected entities."""
        return set(self.tracker.status_requests(entities))

    def run(
        self,
        action: str,
        params: dict[str, Any],
        entities: list[str],
        remaining_s: float,
    ) -> dict[str, Any]:
        """Execute one monitoring operation and return its status/code/data fragment."""
        self._validate_selection(entities)
        if not isinstance(params, dict):
            raise ValueError("params must be an object")
        if action == "snapshot":
            self._validate_parameter_names(params, {"entities"})
            self._validate_embedded_selection(params, entities)
            return {
                "status": "completed",
                "code": "queried",
                "data": {"entities": list(self.tracker.snapshots(entities).values())},
            }
        if action == "republish_states":
            self._validate_parameter_names(params, {"entities"})
            self._validate_embedded_selection(params, entities)
            published = self.tracker.republish_states(entities)
            return {
                "status": "completed",
                "code": "republished",
                "data": {
                    "published_entities": published["published"],
                    "missing_entities": published["missing"],
                },
            }
        if action == "refresh_states":
            self._validate_parameter_names(params, {"entities", "timeout_s"})
            self._validate_embedded_selection(params, entities)
            timeout_s = self._refresh_timeout(params)
            if not isinstance(remaining_s, (int, float)) or not math.isfinite(remaining_s) or remaining_s < timeout_s:
                return {"status": "failed", "code": "expired", "data": {"entities": []}}
            return self._refresh(entities, timeout_s)
        raise ValueError(f"Unsupported monitoring action: {action}")

    @staticmethod
    def _validate_parameter_names(params: dict[str, Any], allowed: set[str]) -> None:
        """Reject unrecognized query parameters before side effects."""
        unknown = set(params).difference(allowed)
        if unknown:
            raise ValueError(f"Unknown parameter(s): {', '.join(sorted(unknown))}")

    @staticmethod
    def _validate_embedded_selection(params: dict[str, Any], entities: list[str]) -> None:
        """Ensure an optional request selection matches the prevalidated selection."""
        if "entities" in params and params["entities"] != entities:
            raise ValueError("params.entities must match the selected entities")

    def _validate_selection(self, entities: list[str]) -> None:
        """Require a unique, non-empty selection of known monitored entities."""
        if not isinstance(entities, list) or not 1 <= len(entities) <= MAX_SELECTED_ENTITIES:
            raise ValueError(f"entities must contain 1–{MAX_SELECTED_ENTITIES} entity IDs")
        if any(not isinstance(entity, str) or not entity for entity in entities):
            raise ValueError("entity IDs must be non-empty strings")
        if len(set(entities)) != len(entities):
            raise ValueError("entities must not contain duplicates")
        unknown = set(entities).difference(self.entities())
        if unknown:
            raise ValueError(f"Unknown monitored entities: {', '.join(sorted(unknown))}")

    @staticmethod
    def _refresh_timeout(params: dict[str, Any]) -> int:
        """Validate and return the requested integer refresh wait in seconds."""
        timeout_s = params.get("timeout_s", DEFAULT_REFRESH_TIMEOUT_S)
        if isinstance(timeout_s, bool) or not isinstance(timeout_s, int):
            raise ValueError("timeout_s must be an integer from 1 through 10")
        if not MIN_REFRESH_TIMEOUT_S <= timeout_s <= MAX_REFRESH_TIMEOUT_S:
            raise ValueError("timeout_s must be an integer from 1 through 10")
        return timeout_s

    def _refresh(self, entities: list[str], timeout_s: int) -> dict[str, Any]:
        """Dispatch deduplicated safe status requests and summarize fresh observations."""
        deadline = time.monotonic() + timeout_s
        baseline = self.tracker.observation_baseline(entities)
        entity_requests = self.tracker.entity_status_requests(entities)
        request_map: dict[str, set[int]] = {}
        for sections in entity_requests.values():
            for section, status_ids in sections.items():
                request_map.setdefault(section, set()).update(status_ids)

        section_status: dict[str, str] = {}
        for section, status_ids in request_map.items():
            source = self.sources.get(section)
            if source is None:
                section_status[section] = "source_unavailable"
                continue
            request = getattr(source, "request_status", None)
            if getattr(source, "supports_status_requests", False) is not True or not callable(request):
                section_status[section] = "unsupported"
                continue
            is_alive = getattr(source, "is_alive", None)
            if callable(is_alive) and not is_alive():
                section_status[section] = "source_unavailable"
                continue
            try:
                request(sorted(status_ids))
            except Exception:
                LOGGER.exception("Status request failed for source [%s]", section)
                section_status[section] = "source_unavailable"
            else:
                section_status[section] = "requested"

        required_fields: dict[str, list[str]] = {}
        for entity in entities:
            sections = entity_requests[entity]
            if any(section_status.get(section) == "requested" for section in sections):
                required_fields[entity] = self.entities()[entity]
        if required_fields:
            self.tracker.wait_for_observations(
                baseline,
                required_fields,
                max(0.0, deadline - time.monotonic()),
            )

        snapshots = self.tracker.snapshots(entities)
        observed_by_entity = self.tracker.observed_fields_since(baseline, entities, observed_before=deadline)
        entity_results: list[dict[str, Any]] = []
        for entity in entities:
            snapshot = snapshots[entity]
            fields = self.entities()[entity]
            observed_fields = [field for field in fields if field in observed_by_entity[entity]]
            missing_fields = [field for field in fields if field not in observed_fields]
            if not missing_fields:
                status = "observed"
            elif observed_fields:
                status = "partial"
            else:
                statuses = [section_status.get(section, "unsupported") for section in entity_requests[entity]]
                if not statuses or all(value == "unsupported" for value in statuses):
                    status = "unsupported"
                elif all(value == "source_unavailable" for value in statuses):
                    status = "source_unavailable"
                else:
                    status = "timed_out"
            timestamps = snapshot["observed_at"]
            latest_observed_at = max(
                (timestamps[field] for field in observed_fields if timestamps[field] is not None),
                default=None,
            )
            entity_results.append(
                {
                    "entity": entity,
                    "status": status,
                    "observed_at": latest_observed_at,
                    "observed_fields": observed_fields,
                    "missing_fields": missing_fields,
                }
            )

        statuses = [result["status"] for result in entity_results]
        observed_count = sum(status == "observed" for status in statuses)
        any_observation = any(observed_by_entity.values())
        if statuses and all(status == "observed" for status in statuses):
            status, code = "completed", "observed"
        elif any_observation:
            status, code = "completed", "partial"
        elif statuses and all(status == "unsupported" for status in statuses):
            status, code = "failed", "unsupported"
        elif statuses and all(status == "source_unavailable" for status in statuses):
            status, code = "failed", "source_unavailable"
        else:
            status, code = "failed", "refresh_timeout"
        return {
            "status": status,
            "code": code,
            "data": {
                "entities": entity_results,
                "observed_count": observed_count,
            },
        }
