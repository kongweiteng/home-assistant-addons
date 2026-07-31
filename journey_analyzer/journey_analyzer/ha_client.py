"""Read-only Home Assistant Core REST client."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from typing import Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from .models import TrackPoint


class HomeAssistantApiError(RuntimeError):
    """A coordinate-free Home Assistant API failure."""


class HomeAssistantClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout_s: float = 15.0,
        opener: Callable = urlopen,
    ) -> None:
        if not token:
            raise ValueError("Home Assistant token is required")
        self.base_url = base_url.rstrip("/")
        self._token = token
        self.timeout_s = timeout_s
        self._opener = opener

    def collect_points(
        self,
        entity_ids: Iterable[str],
        *,
        start: datetime,
        end: datetime,
        received_at: datetime,
    ) -> tuple[TrackPoint, ...]:
        ids = tuple(entity_ids)
        if not ids:
            return ()
        start_text = start.astimezone(timezone.utc).isoformat()
        end_text = end.astimezone(timezone.utc).isoformat()
        query = urlencode(
            {
                "filter_entity_id": ",".join(ids),
                "end_time": end_text,
                "significant_changes_only": "0",
            }
        )
        history = self._get_json(
            f"/history/period/{quote(start_text, safe='')}?{query}"
        )
        states: list[dict] = []
        if isinstance(history, list):
            for group in history:
                if isinstance(group, list):
                    states.extend(item for item in group if isinstance(item, dict))
        for entity_id in ids:
            try:
                state = self._get_json(f"/states/{quote(entity_id, safe='')}")
            except HomeAssistantApiError as error:
                if "status_404" in str(error):
                    continue
                raise
            if isinstance(state, dict):
                states.append(state)
        points: list[TrackPoint] = []
        for state in states:
            point = state_to_track_point(state, received_at=received_at)
            if point is not None and point.entity_id in ids:
                points.append(point)
        return tuple(points)

    def _get_json(self, path: str):
        request = Request(
            f"{self.base_url}{path}",
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/json",
            },
            method="GET",
        )
        try:
            with self._opener(request, timeout=self.timeout_s) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            raise HomeAssistantApiError(f"status_{error.code}") from error
        except URLError as error:
            raise HomeAssistantApiError("connection_failed") from error
        except (TimeoutError, json.JSONDecodeError) as error:
            raise HomeAssistantApiError("invalid_or_timed_out_response") from error


def state_to_track_point(
    state: dict, *, received_at: datetime
) -> TrackPoint | None:
    entity_id = state.get("entity_id")
    attributes = state.get("attributes")
    if not isinstance(entity_id, str) or not isinstance(attributes, dict):
        return None
    latitude = _finite_float(attributes.get("latitude"))
    longitude = _finite_float(attributes.get("longitude"))
    if latitude is None or longitude is None:
        return None
    observed_raw = state.get("last_updated") or state.get("last_changed")
    if not isinstance(observed_raw, str):
        return None
    try:
        observed_at = datetime.fromisoformat(observed_raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        return None
    accuracy = _finite_float(
        attributes.get("gps_accuracy", attributes.get("accuracy"))
    )
    source_raw = attributes.get("source") or attributes.get("source_type")
    source = None if source_raw is None else str(source_raw)[:128]
    try:
        return TrackPoint(
            entity_id=entity_id,
            observed_at=observed_at,
            received_at=received_at,
            latitude=latitude,
            longitude=longitude,
            accuracy_m=accuracy,
            source=source,
        )
    except ValueError:
        return None


def _finite_float(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None
