"""Aggregate privacy-safe Home Assistant sensor values."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .models import Journey
from .storage import JourneyStore


def build_statistics(
    store: JourneyStore,
    entity_ids: tuple[str, ...],
    *,
    timezone_name: str,
    stale_after_s: int,
    now: datetime | None = None,
) -> dict:
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    zone = ZoneInfo(timezone_name)
    local_now = current.astimezone(zone)
    today_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    seven_day_start = today_start - timedelta(days=6)
    thirty_day_start = today_start - timedelta(days=29)
    journeys = _journeys_for_entities(
        store,
        entity_ids,
        start=thirty_day_start.astimezone(timezone.utc),
        end=current,
    )
    latest_point = store.latest_point_time(entity_ids)
    if latest_point is None:
        quality = "no_data"
    elif (current - latest_point).total_seconds() > stale_after_s:
        quality = "stale"
    elif not journeys:
        quality = "insufficient"
    elif any(journey.quality != "good" for journey in journeys):
        quality = "degraded"
    else:
        quality = "good"

    today = [item for item in journeys if item.started_at >= today_start.astimezone(timezone.utc)]
    seven_days = [item for item in journeys if item.started_at >= seven_day_start.astimezone(timezone.utc)]
    last = max(journeys, key=lambda item: item.started_at, default=None)
    has_location_data = latest_point is not None
    return {
        "status": quality,
        "available": True,
        "generated_at": current.isoformat().replace("+00:00", "Z"),
        "last_observed_at": None if latest_point is None else latest_point.isoformat().replace("+00:00", "Z"),
        "entity_count": len(entity_ids),
        "today_trip_count": None if not has_location_data else len(today),
        "today_distance_km": None if not has_location_data else round(sum(item.distance_m for item in today) / 1000.0, 3),
        "today_duration_min": None if not has_location_data else round(sum(item.duration_s for item in today) / 60.0, 1),
        "distance_7d_km": None if not has_location_data else round(sum(item.distance_m for item in seven_days) / 1000.0, 3),
        "distance_30d_km": None if not has_location_data else round(sum(item.distance_m for item in journeys) / 1000.0, 3),
        "last_trip_distance_km": None if last is None else round(last.distance_m / 1000.0, 3),
        "last_trip_duration_min": None if last is None else round(last.duration_s / 60.0, 1),
    }


def _journeys_for_entities(
    store: JourneyStore,
    entity_ids: tuple[str, ...],
    *,
    start: datetime,
    end: datetime,
) -> list[Journey]:
    journeys: list[Journey] = []
    for entity_id in entity_ids:
        journeys.extend(
            store.list_journeys(
                entity_id=entity_id,
                start=start,
                end=end,
                limit=10_000,
                offset=0,
                descending=False,
            )
        )
    return journeys
