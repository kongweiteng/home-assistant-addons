"""Deterministic Home Assistant health snapshot normalization.

The Hermes add-on intentionally keeps only ``homeassistant_api`` access.  This
module therefore consumes explicitly configured, read-only Home Assistant
entities instead of calling Supervisor backup or add-on management endpoints.
It never reads arbitrary files and never executes commands.
"""

from __future__ import annotations

import base64
import binascii
import json
import math
import re
from datetime import datetime, timezone
from typing import Any


_ENTITY_ID_RE = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z0-9_]+$")
_NUMERIC_METRICS = frozenset(
    {
        "disk_total_bytes",
        "disk_used_bytes",
        "disk_free_bytes",
        "disk_used_percent",
        "recorder_bytes",
        "backup_count",
        "backup_bytes",
        "top_consumer",
    }
)
_SINGLETON_METRICS = _NUMERIC_METRICS - {"top_consumer"}
_REQUIRED_METRICS = (
    "disk_total_bytes",
    "disk_used_bytes",
    "disk_free_bytes",
    "disk_used_percent",
    "recorder_bytes",
    "backup_count",
    "backup_bytes",
)
_UNAVAILABLE_STATES = frozenset({"", "none", "null", "unavailable", "unknown"})
_BYTE_FACTORS = {
    "b": 1,
    "byte": 1,
    "bytes": 1,
    "kb": 1_000,
    "mb": 1_000_000,
    "gb": 1_000_000_000,
    "tb": 1_000_000_000_000,
    "kib": 1_024,
    "mib": 1_048_576,
    "gib": 1_073_741_824,
    "tib": 1_099_511_627_776,
}
_MAX_METRIC_SOURCES = 32
_MAX_STATUS_SOURCES = 32


def decode_health_config(encoded: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Decode the add-on-owned base64 JSON configuration without logging it."""
    default = {"metrics": [], "statuses": [], "stale_after_seconds": 300}
    if not encoded:
        return default, []

    try:
        raw = base64.b64decode(encoded, validate=True).decode("utf-8")
        parsed = json.loads(raw)
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError):
        return default, [{"code": "invalid_health_config_encoding"}]

    if not isinstance(parsed, dict):
        return default, [{"code": "invalid_health_config_type"}]

    errors: list[dict[str, Any]] = []
    metrics = parsed.get("metrics", [])
    statuses = parsed.get("statuses", [])
    stale_after = parsed.get("stale_after_seconds", 300)
    if not isinstance(metrics, list):
        errors.append({"code": "invalid_metric_sources_type"})
        metrics = []
    if not isinstance(statuses, list):
        errors.append({"code": "invalid_status_sources_type"})
        statuses = []
    if (
        isinstance(stale_after, bool)
        or not isinstance(stale_after, int)
        or not 30 <= stale_after <= 86_400
    ):
        errors.append({"code": "invalid_stale_after_seconds"})
        stale_after = 300

    return {
        "metrics": metrics,
        "statuses": statuses,
        "stale_after_seconds": stale_after,
    }, errors


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _source_age_seconds(
    state: dict[str, Any], now: datetime
) -> tuple[int | None, str | None, str | None]:
    sampled_at_raw = state.get("last_updated") or state.get("last_changed")
    sampled_at = _parse_timestamp(sampled_at_raw)
    if sampled_at is None:
        return None, None, "missing_source_timestamp"
    age = (now - sampled_at).total_seconds()
    if age < -30:
        return None, sampled_at.isoformat(), "source_timestamp_in_future"
    return max(0, int(age)), sampled_at.isoformat(), None


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _normalize_unit(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip().replace(" ", "").lower()


def _metric_value(metric: str, state: dict[str, Any]) -> tuple[int | float | None, str | None]:
    raw_state = str(state.get("state", "")).strip()
    if raw_state.lower() in _UNAVAILABLE_STATES:
        return None, "source_unavailable"
    number = _finite_number(raw_state)
    if number is None or number < 0:
        return None, "invalid_numeric_state"

    unit = _normalize_unit(state.get("attributes", {}).get("unit_of_measurement"))
    if metric == "disk_used_percent":
        if unit != "%" or number > 100:
            return None, "invalid_percent_unit_or_range"
        return round(number, 2), None
    if metric == "backup_count":
        if unit not in {"", "backup", "backups", "item", "items"}:
            return None, "invalid_count_unit"
        if not number.is_integer():
            return None, "invalid_count_value"
        return int(number), None

    factor = _BYTE_FACTORS.get(unit)
    if factor is None:
        return None, "invalid_byte_unit"
    return int(round(number * factor)), None


def _new_source(metric: str, entity_id: str, unit: Any) -> dict[str, Any]:
    return {
        "source_type": "ha_entity",
        "metric": metric,
        "entity_id": entity_id,
        "sampled_at": None,
        "age_seconds": None,
        "unit": unit if isinstance(unit, str) and unit else None,
        "status": "invalid",
    }


def _derive_disk(values: dict[str, int | float]) -> list[str]:
    derived: list[str] = []
    total = values.get("disk_total_bytes")
    used = values.get("disk_used_bytes")
    free = values.get("disk_free_bytes")
    percent = values.get("disk_used_percent")

    if total is None and used is not None and free is not None:
        total = int(used + free)
        values["disk_total_bytes"] = total
        derived.append("disk_total_bytes")
    if used is None and total is not None and free is not None and total >= free:
        used = int(total - free)
        values["disk_used_bytes"] = used
        derived.append("disk_used_bytes")
    if used is None and total is not None and percent is not None:
        used = int(round(total * percent / 100))
        values["disk_used_bytes"] = used
        derived.append("disk_used_bytes")
    if free is None and total is not None and used is not None and total >= used:
        free = int(total - used)
        values["disk_free_bytes"] = free
        derived.append("disk_free_bytes")
    if percent is None and total and used is not None:
        percent = round(used * 100 / total, 2)
        values["disk_used_percent"] = percent
        derived.append("disk_used_percent")

    return derived


def _disk_quality_issues(values: dict[str, int | float]) -> list[dict[str, Any]]:
    total = values.get("disk_total_bytes")
    used = values.get("disk_used_bytes")
    free = values.get("disk_free_bytes")
    percent = values.get("disk_used_percent")
    issues: list[dict[str, Any]] = []
    if total is not None and total <= 0:
        issues.append({"code": "disk_total_not_positive"})
        return issues
    if total is not None and used is not None and free is not None:
        tolerance = max(10_000_000, total * 0.02)
        if abs(total - used - free) > tolerance:
            issues.append({"code": "disk_values_inconsistent"})
    if total and used is not None and percent is not None:
        expected = used * 100 / total
        if abs(expected - percent) > 2:
            issues.append({"code": "disk_percent_inconsistent"})
    return issues


def build_health_snapshot(
    states: list[dict[str, Any]],
    config: dict[str, Any],
    *,
    now: datetime | None = None,
    configuration_errors: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a bounded, deterministic snapshot from HA entity states."""
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    stale_after = config.get("stale_after_seconds", 300)
    if isinstance(stale_after, bool) or not isinstance(stale_after, int):
        stale_after = 300

    state_index = {
        item.get("entity_id"): item
        for item in states
        if isinstance(item, dict) and isinstance(item.get("entity_id"), str)
    }
    sources: list[dict[str, Any]] = []
    issues = list(configuration_errors or [])
    values: dict[str, int | float] = {}
    top_consumers: list[dict[str, Any]] = []
    components: list[dict[str, Any]] = []
    ages: list[int] = []
    seen_singletons: set[str] = set()

    metric_specs = config.get("metrics", [])
    if not isinstance(metric_specs, list):
        metric_specs = []
        issues.append({"code": "invalid_metric_sources_type"})
    if len(metric_specs) > _MAX_METRIC_SOURCES:
        issues.append({"code": "too_many_metric_sources", "limit": _MAX_METRIC_SOURCES})
    for index, spec in enumerate(metric_specs[:_MAX_METRIC_SOURCES]):
        if not isinstance(spec, dict):
            issues.append({"code": "invalid_metric_source", "index": index})
            continue
        metric = spec.get("metric", "")
        entity_id = spec.get("entity_id", "")
        source = _new_source(metric, entity_id, None)
        if metric not in _NUMERIC_METRICS:
            issues.append({"code": "unknown_metric", "index": index})
            sources.append(source)
            continue
        if not isinstance(entity_id, str) or not _ENTITY_ID_RE.match(entity_id) or not entity_id.startswith("sensor."):
            issues.append({"code": "invalid_metric_entity_id", "index": index})
            sources.append(source)
            continue
        if metric in _SINGLETON_METRICS and metric in seen_singletons:
            issues.append({"code": "duplicate_metric", "metric": metric})
            sources.append(source)
            continue
        seen_singletons.add(metric)

        state = state_index.get(entity_id)
        if state is None:
            source["status"] = "unavailable"
            issues.append({"code": "entity_not_found", "metric": metric})
            sources.append(source)
            continue
        unit = state.get("attributes", {}).get("unit_of_measurement")
        source["unit"] = unit if isinstance(unit, str) and unit else None
        age, sampled_at, timestamp_error = _source_age_seconds(state, current)
        source["age_seconds"] = age
        source["sampled_at"] = sampled_at
        if timestamp_error:
            issues.append({"code": timestamp_error, "metric": metric})
            sources.append(source)
            continue

        value, value_error = _metric_value(metric, state)
        if value_error:
            source["status"] = "unavailable" if value_error == "source_unavailable" else "invalid"
            issues.append({"code": value_error, "metric": metric})
            sources.append(source)
            continue

        source["status"] = "stale" if age is not None and age > stale_after else "ok"
        if age is not None:
            ages.append(age)
        sources.append(source)
        if metric == "top_consumer":
            top_consumers.append(
                {
                    "entity_id": entity_id,
                    "name": state.get("attributes", {}).get("friendly_name") or entity_id,
                    "bytes": value,
                    "sampled_at": sampled_at,
                }
            )
        else:
            values[metric] = value

    status_specs = config.get("statuses", [])
    if not isinstance(status_specs, list):
        status_specs = []
        issues.append({"code": "invalid_status_sources_type"})
    if len(status_specs) > _MAX_STATUS_SOURCES:
        issues.append({"code": "too_many_status_sources", "limit": _MAX_STATUS_SOURCES})
    seen_status_entities: set[str] = set()
    for index, spec in enumerate(status_specs[:_MAX_STATUS_SOURCES]):
        if not isinstance(spec, dict):
            issues.append({"code": "invalid_status_source", "index": index})
            continue
        entity_id = spec.get("entity_id", "")
        expected_state = spec.get("expected_state", "")
        source = _new_source("component_status", entity_id, None)
        if (
            not isinstance(entity_id, str)
            or not _ENTITY_ID_RE.match(entity_id)
            or not entity_id.startswith("binary_sensor.")
            or not isinstance(expected_state, str)
            or expected_state not in {"on", "off"}
        ):
            issues.append({"code": "invalid_status_entity", "index": index})
            sources.append(source)
            continue
        if entity_id in seen_status_entities:
            issues.append({"code": "duplicate_status_entity", "index": index})
            sources.append(source)
            continue
        seen_status_entities.add(entity_id)
        state = state_index.get(entity_id)
        if state is None:
            source["status"] = "unavailable"
            issues.append({"code": "status_entity_not_found", "index": index})
            sources.append(source)
            continue
        age, sampled_at, timestamp_error = _source_age_seconds(state, current)
        source["age_seconds"] = age
        source["sampled_at"] = sampled_at
        if timestamp_error:
            issues.append({"code": timestamp_error, "index": index})
            sources.append(source)
            continue
        actual_state = str(state.get("state", "")).strip().lower()
        if actual_state in _UNAVAILABLE_STATES:
            source["status"] = "unavailable"
            issues.append({"code": "component_unavailable", "index": index})
            sources.append(source)
            continue
        if actual_state not in {"on", "off"}:
            source["status"] = "invalid"
            issues.append({"code": "invalid_component_state", "index": index})
            sources.append(source)
            continue
        source["status"] = "stale" if age is not None and age > stale_after else "ok"
        if age is not None:
            ages.append(age)
        healthy = actual_state == expected_state
        components.append(
            {
                "entity_id": entity_id,
                "name": state.get("attributes", {}).get("friendly_name") or entity_id,
                "state": actual_state,
                "expected_state": expected_state,
                "healthy": healthy,
                "sampled_at": sampled_at,
            }
        )
        if not healthy:
            issues.append({"code": "component_unhealthy", "entity_id": entity_id})
        sources.append(source)

    derived_metrics = _derive_disk(values)
    issues.extend(_disk_quality_issues(values))
    missing_metrics = [metric for metric in _REQUIRED_METRICS if metric not in values]
    if not metric_specs and not status_specs:
        issues.append({"code": "no_health_sources_configured"})

    usable_source_count = sum(source["status"] in {"ok", "stale"} for source in sources)
    if usable_source_count == 0:
        status = "unavailable"
    elif any(source["status"] == "stale" for source in sources):
        status = "stale"
    elif issues or missing_metrics:
        status = "warning"
    else:
        status = "ok"

    top_consumers.sort(key=lambda item: (-item["bytes"], item["entity_id"]))
    return {
        "version": 1,
        "sampled_at": current.isoformat(),
        "freshness_seconds": max(ages) if ages else None,
        "stale_after_seconds": stale_after,
        "status": status,
        "disk": {
            "total_bytes": values.get("disk_total_bytes"),
            "used_bytes": values.get("disk_used_bytes"),
            "free_bytes": values.get("disk_free_bytes"),
            "used_percent": values.get("disk_used_percent"),
        },
        "recorder_bytes": values.get("recorder_bytes"),
        "backup_count": values.get("backup_count"),
        "backup_bytes": values.get("backup_bytes"),
        "top_consumers": top_consumers,
        "components": components,
        "sources": sources,
        "derived_metrics": derived_metrics,
        "missing_metrics": missing_metrics,
        "issues": issues,
    }
