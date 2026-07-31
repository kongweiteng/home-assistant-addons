"""SQLite persistence and bounded read queries."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from typing import Iterable, Sequence

from .models import AnalysisResult, Journey, TrackPoint


SCHEMA_VERSION = "1"


def encode_time(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("SQLite timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def decode_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


class JourneyStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=10.0)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA busy_timeout = 10000")
        self._initialize()

    def __enter__(self) -> "JourneyStore":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def close(self) -> None:
        self.connection.close()

    def _initialize(self) -> None:
        with self.connection:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS track_points (
                    entity_id TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    latitude REAL NOT NULL,
                    longitude REAL NOT NULL,
                    accuracy_m REAL,
                    source TEXT,
                    PRIMARY KEY (entity_id, observed_at)
                );
                CREATE INDEX IF NOT EXISTS track_points_observed_idx
                    ON track_points (observed_at);
                CREATE TABLE IF NOT EXISTS journeys (
                    journey_id TEXT PRIMARY KEY,
                    entity_id TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    ended_at TEXT NOT NULL,
                    distance_m REAL NOT NULL,
                    duration_s INTEGER NOT NULL,
                    point_count INTEGER NOT NULL,
                    average_speed_mps REAL NOT NULL,
                    max_segment_speed_mps REAL NOT NULL,
                    quality TEXT NOT NULL,
                    algorithm_version TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS journeys_entity_started_idx
                    ON journeys (entity_id, started_at);
                CREATE TABLE IF NOT EXISTS journey_points (
                    journey_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    entity_id TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    PRIMARY KEY (journey_id, sequence),
                    FOREIGN KEY (journey_id) REFERENCES journeys(journey_id)
                        ON DELETE CASCADE,
                    FOREIGN KEY (entity_id, observed_at)
                        REFERENCES track_points(entity_id, observed_at)
                        ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS stays (
                    stay_id TEXT PRIMARY KEY,
                    entity_id TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    ended_at TEXT NOT NULL,
                    centroid_latitude REAL NOT NULL,
                    centroid_longitude REAL NOT NULL,
                    point_count INTEGER NOT NULL,
                    duration_s INTEGER NOT NULL,
                    algorithm_version TEXT NOT NULL
                );
                """
            )
            row = self.connection.execute(
                "SELECT value FROM metadata WHERE key = 'schema_version'"
            ).fetchone()
            if row is None:
                self.connection.execute(
                    "INSERT INTO metadata(key, value) VALUES('schema_version', ?)",
                    (SCHEMA_VERSION,),
                )
            elif row["value"] != SCHEMA_VERSION:
                raise RuntimeError(
                    f"unsupported schema version {row['value']}; expected {SCHEMA_VERSION}"
                )

    def schema_version(self) -> str:
        return self.get_metadata("schema_version") or "unknown"

    def get_metadata(self, key: str) -> str | None:
        row = self.connection.execute(
            "SELECT value FROM metadata WHERE key = ?", (key,)
        ).fetchone()
        return None if row is None else str(row["value"])

    def set_metadata(self, key: str, value: str) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO metadata(key, value) VALUES(?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

    def save_points(self, points: Iterable[TrackPoint]) -> int:
        before = self.connection.total_changes
        with self.connection:
            self.connection.executemany(
                """
                INSERT INTO track_points(
                    entity_id, observed_at, received_at, latitude, longitude,
                    accuracy_m, source
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(entity_id, observed_at) DO UPDATE SET
                    received_at = excluded.received_at,
                    latitude = excluded.latitude,
                    longitude = excluded.longitude,
                    accuracy_m = excluded.accuracy_m,
                    source = excluded.source
                WHERE
                    (track_points.accuracy_m IS NULL AND excluded.accuracy_m IS NOT NULL)
                    OR (excluded.accuracy_m IS NOT NULL
                        AND track_points.accuracy_m IS NOT NULL
                        AND excluded.accuracy_m < track_points.accuracy_m)
                """,
                (
                    (
                        point.entity_id,
                        encode_time(point.observed_at),
                        encode_time(point.received_at),
                        point.latitude,
                        point.longitude,
                        point.accuracy_m,
                        point.source,
                    )
                    for point in points
                ),
            )
        return self.connection.total_changes - before

    def save_analysis(
        self,
        result: AnalysisResult,
        *,
        replace_start: datetime,
        replace_end: datetime,
    ) -> None:
        if result.entity_id is None:
            return
        if replace_end < replace_start:
            raise ValueError("replace_end must not be before replace_start")
        self.save_points(result.accepted_points)
        encoded_start = encode_time(replace_start)
        encoded_end = encode_time(replace_end)
        with self.connection:
            self.connection.execute(
                """
                DELETE FROM journeys
                WHERE entity_id = ? AND algorithm_version = ?
                  AND NOT (ended_at < ? OR started_at > ?)
                """,
                (result.entity_id, result.algorithm_version, encoded_start, encoded_end),
            )
            self.connection.execute(
                """
                DELETE FROM stays
                WHERE entity_id = ? AND algorithm_version = ?
                  AND NOT (ended_at < ? OR started_at > ?)
                """,
                (result.entity_id, result.algorithm_version, encoded_start, encoded_end),
            )
            for journey in result.journeys:
                self.connection.execute(
                    """
                    INSERT INTO journeys(
                        journey_id, entity_id, started_at, ended_at, distance_m,
                        duration_s, point_count, average_speed_mps,
                        max_segment_speed_mps, quality, algorithm_version
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        journey.journey_id,
                        journey.entity_id,
                        encode_time(journey.started_at),
                        encode_time(journey.ended_at),
                        journey.distance_m,
                        journey.duration_s,
                        journey.point_count,
                        journey.average_speed_mps,
                        journey.max_segment_speed_mps,
                        journey.quality,
                        journey.algorithm_version,
                    ),
                )
                self.connection.executemany(
                    """
                    INSERT INTO journey_points(journey_id, sequence, entity_id, observed_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        (
                            journey.journey_id,
                            sequence,
                            point.entity_id,
                            encode_time(point.observed_at),
                        )
                        for sequence, point in enumerate(journey.points)
                    ),
                )
            self.connection.executemany(
                """
                INSERT INTO stays(
                    stay_id, entity_id, started_at, ended_at,
                    centroid_latitude, centroid_longitude, point_count,
                    duration_s, algorithm_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    (
                        stay.stay_id,
                        stay.entity_id,
                        encode_time(stay.started_at),
                        encode_time(stay.ended_at),
                        stay.centroid_latitude,
                        stay.centroid_longitude,
                        stay.point_count,
                        stay.duration_s,
                        stay.algorithm_version,
                    )
                    for stay in result.stays
                ),
            )

    def list_points(
        self,
        entity_id: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
    ) -> tuple[TrackPoint, ...]:
        clauses = ["entity_id = ?"]
        values: list[object] = [entity_id]
        if start is not None:
            clauses.append("observed_at >= ?")
            values.append(encode_time(start))
        if end is not None:
            clauses.append("observed_at <= ?")
            values.append(encode_time(end))
        query = """
            SELECT entity_id, observed_at, received_at, latitude, longitude,
                   accuracy_m, source
            FROM track_points
            WHERE %s
            ORDER BY observed_at
        """ % " AND ".join(clauses)
        if limit is not None:
            query += " LIMIT ?"
            values.append(limit)
        rows = self.connection.execute(query, values).fetchall()
        return tuple(self._row_to_point(row) for row in rows)

    def latest_point_time(self, entity_ids: Sequence[str]) -> datetime | None:
        if not entity_ids:
            return None
        placeholders = ",".join("?" for _ in entity_ids)
        row = self.connection.execute(
            f"SELECT MAX(observed_at) AS observed_at FROM track_points WHERE entity_id IN ({placeholders})",
            tuple(entity_ids),
        ).fetchone()
        return None if row is None or row["observed_at"] is None else decode_time(row["observed_at"])

    def list_journeys(
        self,
        *,
        entity_id: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
        descending: bool = True,
    ) -> tuple[Journey, ...]:
        clauses: list[str] = []
        values: list[object] = []
        if entity_id is not None:
            clauses.append("entity_id = ?")
            values.append(entity_id)
        if start is not None:
            clauses.append("ended_at >= ?")
            values.append(encode_time(start))
        if end is not None:
            clauses.append("started_at <= ?")
            values.append(encode_time(end))
        where = "" if not clauses else " WHERE " + " AND ".join(clauses)
        order = "DESC" if descending else "ASC"
        rows = self.connection.execute(
            f"SELECT * FROM journeys{where} ORDER BY started_at {order} LIMIT ? OFFSET ?",
            (*values, limit, offset),
        ).fetchall()
        return tuple(self._row_to_journey(row, include_points=False) for row in rows)

    def get_journey(self, journey_id: str, *, point_limit: int = 2000) -> Journey | None:
        row = self.connection.execute(
            "SELECT * FROM journeys WHERE journey_id = ?", (journey_id,)
        ).fetchone()
        if row is None:
            return None
        return self._row_to_journey(row, include_points=True, point_limit=point_limit)

    def prune_raw_points(self, before: datetime) -> int:
        before_changes = self.connection.total_changes
        with self.connection:
            self.connection.execute(
                "DELETE FROM track_points WHERE observed_at < ?", (encode_time(before),)
            )
        return self.connection.total_changes - before_changes

    def _row_to_journey(
        self, row: sqlite3.Row, *, include_points: bool, point_limit: int = 2000
    ) -> Journey:
        points: tuple[TrackPoint, ...] = ()
        if include_points:
            point_rows = self.connection.execute(
                """
                SELECT p.entity_id, p.observed_at, p.received_at, p.latitude,
                       p.longitude, p.accuracy_m, p.source
                FROM journey_points jp
                JOIN track_points p
                  ON p.entity_id = jp.entity_id AND p.observed_at = jp.observed_at
                WHERE jp.journey_id = ?
                ORDER BY jp.sequence
                LIMIT ?
                """,
                (row["journey_id"], point_limit),
            ).fetchall()
            points = tuple(self._row_to_point(item) for item in point_rows)
        return Journey(
            journey_id=row["journey_id"],
            entity_id=row["entity_id"],
            started_at=decode_time(row["started_at"]),
            ended_at=decode_time(row["ended_at"]),
            distance_m=row["distance_m"],
            duration_s=row["duration_s"],
            point_count=row["point_count"],
            average_speed_mps=row["average_speed_mps"],
            max_segment_speed_mps=row["max_segment_speed_mps"],
            quality=row["quality"],
            algorithm_version=row["algorithm_version"],
            points=points,
        )

    @staticmethod
    def _row_to_point(row: sqlite3.Row) -> TrackPoint:
        return TrackPoint(
            entity_id=row["entity_id"],
            observed_at=decode_time(row["observed_at"]),
            received_at=decode_time(row["received_at"]),
            latitude=row["latitude"],
            longitude=row["longitude"],
            accuracy_m=row["accuracy_m"],
            source=row["source"],
        )
