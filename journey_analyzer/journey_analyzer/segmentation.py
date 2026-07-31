"""Deterministic filtering, stay detection and journey segmentation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
from itertools import groupby

from .geo import centroid, haversine_m, segment_speed_mps
from .models import AnalysisResult, Journey, PointRejection, Stay, TrackPoint


ALGORITHM_VERSION = "v1"


@dataclass(frozen=True)
class AnalyzerSettings:
    max_accuracy_m: float = 100.0
    max_gap_s: int = 900
    stop_radius_m: float = 80.0
    stop_min_duration_s: int = 600
    min_journey_distance_m: float = 200.0
    min_journey_duration_s: int = 120
    max_speed_mps: float = 100.0
    reject_zero_coordinate: bool = True

    def __post_init__(self) -> None:
        for name, value in vars(self).items():
            if name != "reject_zero_coordinate" and value <= 0:
                raise ValueError(f"{name} must be positive")


class JourneyAnalyzer:
    def __init__(self, settings: AnalyzerSettings | None = None) -> None:
        self.settings = settings or AnalyzerSettings()

    def analyze(self, points: list[TrackPoint] | tuple[TrackPoint, ...]) -> AnalysisResult:
        input_points = list(points)
        reordered_count = self._count_out_of_order(input_points)
        ordered = sorted(
            input_points,
            key=lambda point: (
                point.entity_id,
                point.observed_at,
                float("inf") if point.accuracy_m is None else point.accuracy_m,
                point.received_at,
            ),
        )
        entity_ids = {point.entity_id for point in ordered}
        if len(entity_ids) > 1:
            raise ValueError("analyze accepts points for exactly one entity")
        entity_id = next(iter(entity_ids), None)

        deduplicated, rejections = self._deduplicate(ordered)
        filtered: list[TrackPoint] = []
        for point in deduplicated:
            if self.settings.reject_zero_coordinate and (
                abs(point.latitude) < 1e-12 and abs(point.longitude) < 1e-12
            ):
                rejections.append(self._reject(point, "zero_coordinate"))
                continue
            if point.accuracy_m is not None and point.accuracy_m > self.settings.max_accuracy_m:
                rejections.append(self._reject(point, "poor_accuracy"))
                continue
            if filtered:
                previous = filtered[-1]
                elapsed_s = (point.observed_at - previous.observed_at).total_seconds()
                if elapsed_s <= self.settings.max_gap_s:
                    if segment_speed_mps(previous, point) > self.settings.max_speed_mps:
                        rejections.append(self._reject(point, "implausible_speed"))
                        continue
            filtered.append(point)

        stays: list[Stay] = []
        journeys: list[Journey] = []
        discarded = 0
        for track in self._split_tracks(filtered):
            track_stays, stay_ranges = self._detect_stays(track)
            stays.extend(track_stays)
            for candidate in self._journey_candidates(track, stay_ranges):
                journey = self._build_journey(candidate)
                if journey is None:
                    discarded += 1
                else:
                    journeys.append(journey)
        return AnalysisResult(
            entity_id=entity_id,
            accepted_points=tuple(filtered),
            rejections=tuple(sorted(rejections, key=lambda item: item.observed_at)),
            stays=tuple(stays),
            journeys=tuple(journeys),
            reordered_point_count=reordered_count,
            discarded_candidate_count=discarded,
            algorithm_version=ALGORITHM_VERSION,
        )

    @staticmethod
    def _count_out_of_order(points: list[TrackPoint]) -> int:
        latest_by_entity: dict[str, datetime] = {}
        reordered = 0
        for point in points:
            latest = latest_by_entity.get(point.entity_id)
            if latest is not None and point.observed_at < latest:
                reordered += 1
            if latest is None or point.observed_at > latest:
                latest_by_entity[point.entity_id] = point.observed_at
        return reordered

    @staticmethod
    def _reject(point: TrackPoint, reason: str) -> PointRejection:
        return PointRejection(point.entity_id, point.observed_at, reason)

    def _deduplicate(
        self, points: list[TrackPoint]
    ) -> tuple[list[TrackPoint], list[PointRejection]]:
        selected: list[TrackPoint] = []
        rejected: list[PointRejection] = []
        for _, timestamp_group in groupby(
            points, key=lambda point: (point.entity_id, point.observed_at)
        ):
            candidates = list(timestamp_group)
            best = min(
                candidates,
                key=lambda point: (
                    float("inf") if point.accuracy_m is None else point.accuracy_m,
                    point.received_at,
                    point.latitude,
                    point.longitude,
                ),
            )
            selected.append(best)
            rejected.extend(
                self._reject(point, "duplicate_timestamp")
                for point in candidates
                if point is not best
            )
        return selected, rejected

    def _split_tracks(self, points: list[TrackPoint]) -> list[tuple[TrackPoint, ...]]:
        if not points:
            return []
        tracks: list[list[TrackPoint]] = [[points[0]]]
        for point in points[1:]:
            previous = tracks[-1][-1]
            gap_s = (point.observed_at - previous.observed_at).total_seconds()
            if gap_s > self.settings.max_gap_s:
                tracks.append([point])
            else:
                tracks[-1].append(point)
        return [tuple(track) for track in tracks]

    def _detect_stays(
        self, points: tuple[TrackPoint, ...]
    ) -> tuple[list[Stay], list[tuple[int, int]]]:
        stays: list[Stay] = []
        ranges: list[tuple[int, int]] = []
        index = 0
        while index < len(points) - 1:
            end = index + 1
            while end < len(points) and haversine_m(points[index], points[end]) <= self.settings.stop_radius_m:
                end += 1
            last_inside = end - 1
            duration_s = int(
                (points[last_inside].observed_at - points[index].observed_at).total_seconds()
            )
            if last_inside > index and duration_s >= self.settings.stop_min_duration_s:
                stay_points = points[index : last_inside + 1]
                latitude, longitude = centroid(stay_points)
                stays.append(
                    Stay(
                        self._stable_id("stay", points[index].entity_id, points[index].observed_at, points[last_inside].observed_at),
                        points[index].entity_id,
                        points[index].observed_at,
                        points[last_inside].observed_at,
                        latitude,
                        longitude,
                        len(stay_points),
                        duration_s,
                        ALGORITHM_VERSION,
                    )
                )
                ranges.append((index, last_inside))
                index = last_inside + 1
            else:
                index += 1
        return stays, ranges

    @staticmethod
    def _journey_candidates(
        points: tuple[TrackPoint, ...], stay_ranges: list[tuple[int, int]]
    ) -> list[tuple[TrackPoint, ...]]:
        if len(points) < 2:
            return []
        if not stay_ranges:
            return [points]
        candidates: list[tuple[TrackPoint, ...]] = []
        left = 0
        for start, end in stay_ranges:
            if start > left:
                candidates.append(points[left : start + 1])
            left = end
        if left < len(points) - 1:
            candidates.append(points[left:])
        return candidates

    def _build_journey(self, points: tuple[TrackPoint, ...]) -> Journey | None:
        if len(points) < 2:
            return None
        duration_s = int((points[-1].observed_at - points[0].observed_at).total_seconds())
        distances = [haversine_m(first, second) for first, second in zip(points, points[1:])]
        distance_m = sum(distances)
        if duration_s < self.settings.min_journey_duration_s or distance_m < self.settings.min_journey_distance_m:
            return None
        speeds = [segment_speed_mps(first, second) for first, second in zip(points, points[1:])]
        gaps = [
            (second.observed_at - first.observed_at).total_seconds()
            for first, second in zip(points, points[1:])
        ]
        accuracy_degraded = any(
            point.accuracy_m is None or point.accuracy_m > self.settings.max_accuracy_m * 0.75
            for point in points
        )
        gap_degraded = max(gaps, default=0.0) > self.settings.max_gap_s / 2.0
        quality = "degraded" if accuracy_degraded or gap_degraded else "good"
        return Journey(
            self._stable_id("journey", points[0].entity_id, points[0].observed_at, points[-1].observed_at),
            points[0].entity_id,
            points[0].observed_at,
            points[-1].observed_at,
            distance_m,
            duration_s,
            len(points),
            distance_m / duration_s,
            max(speeds, default=0.0),
            quality,
            ALGORITHM_VERSION,
            points,
        )

    @staticmethod
    def _stable_id(kind: str, entity_id: str, started_at: datetime, ended_at: datetime) -> str:
        raw = "|".join((kind, ALGORITHM_VERSION, entity_id, started_at.isoformat(), ended_at.isoformat()))
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]
        return f"{'jrny' if kind == 'journey' else 'stay'}_{ALGORITHM_VERSION}_{digest}"
