"""Data contracts for local journey analysis."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
import math
import re


ENTITY_ID_PATTERN = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")


def as_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class TrackPoint:
    entity_id: str
    observed_at: datetime
    received_at: datetime
    latitude: float
    longitude: float
    accuracy_m: float | None = None
    source: str | None = None

    def __post_init__(self) -> None:
        if not ENTITY_ID_PATTERN.fullmatch(self.entity_id):
            raise ValueError("entity_id must be a stable Home Assistant entity id")
        if not math.isfinite(self.latitude) or not -90.0 <= self.latitude <= 90.0:
            raise ValueError("latitude must be finite and within [-90, 90]")
        if not math.isfinite(self.longitude) or not -180.0 <= self.longitude <= 180.0:
            raise ValueError("longitude must be finite and within [-180, 180]")
        if self.accuracy_m is not None and (
            not math.isfinite(self.accuracy_m) or self.accuracy_m < 0.0
        ):
            raise ValueError("accuracy_m must be finite and non-negative")
        object.__setattr__(self, "observed_at", as_utc(self.observed_at, "observed_at"))
        object.__setattr__(self, "received_at", as_utc(self.received_at, "received_at"))


@dataclass(frozen=True)
class PointRejection:
    entity_id: str
    observed_at: datetime
    reason: str


@dataclass(frozen=True)
class Stay:
    stay_id: str
    entity_id: str
    started_at: datetime
    ended_at: datetime
    centroid_latitude: float
    centroid_longitude: float
    point_count: int
    duration_s: int
    algorithm_version: str


@dataclass(frozen=True)
class Journey:
    journey_id: str
    entity_id: str
    started_at: datetime
    ended_at: datetime
    distance_m: float
    duration_s: int
    point_count: int
    average_speed_mps: float
    max_segment_speed_mps: float
    quality: str
    algorithm_version: str
    points: tuple[TrackPoint, ...] = field(repr=False, compare=False)


@dataclass(frozen=True)
class DailySummary:
    local_date: date
    trip_count: int
    distance_m: float
    duration_s: int
    longest_trip_m: float


@dataclass(frozen=True)
class AnalysisResult:
    entity_id: str | None
    accepted_points: tuple[TrackPoint, ...]
    rejections: tuple[PointRejection, ...]
    stays: tuple[Stay, ...]
    journeys: tuple[Journey, ...]
    reordered_point_count: int
    discarded_candidate_count: int
    algorithm_version: str

    @property
    def status(self) -> str:
        if not self.accepted_points:
            return "no_data"
        if not self.journeys:
            return "insufficient"
        if (
            self.rejections
            or self.reordered_point_count
            or any(journey.quality != "good" for journey in self.journeys)
        ):
            return "degraded"
        return "good"
