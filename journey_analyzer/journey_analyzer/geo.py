"""Coordinate-independent geodesic helpers."""

from __future__ import annotations

import math
from typing import Iterable

from .models import TrackPoint


EARTH_RADIUS_M = 6_371_008.8


def haversine_m(first: TrackPoint, second: TrackPoint) -> float:
    latitude_1 = math.radians(first.latitude)
    latitude_2 = math.radians(second.latitude)
    delta_latitude = latitude_2 - latitude_1
    delta_longitude = math.radians(second.longitude - first.longitude)
    value = (
        math.sin(delta_latitude / 2.0) ** 2
        + math.cos(latitude_1)
        * math.cos(latitude_2)
        * math.sin(delta_longitude / 2.0) ** 2
    )
    return 2.0 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(value)))


def segment_speed_mps(first: TrackPoint, second: TrackPoint) -> float:
    elapsed_s = (second.observed_at - first.observed_at).total_seconds()
    if elapsed_s <= 0.0:
        return math.inf
    return haversine_m(first, second) / elapsed_s


def centroid(points: Iterable[TrackPoint]) -> tuple[float, float]:
    items = tuple(points)
    if not items:
        raise ValueError("centroid requires at least one point")
    return (
        sum(point.latitude for point in items) / len(items),
        sum(point.longitude for point in items) / len(items),
    )
