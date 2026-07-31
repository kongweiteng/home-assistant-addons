"""Collection, persistence, analysis and health state loop."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
import threading

from .config import AppConfig
from .ha_client import HomeAssistantClient
from .segmentation import JourneyAnalyzer
from .statistics import build_statistics
from .storage import JourneyStore


LOGGER = logging.getLogger(__name__)


class RuntimeState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snapshot: dict = {
            "status": "starting",
            "available": False,
            "generated_at": None,
            "last_observed_at": None,
        }
        self._last_error: str | None = None

    def update(self, snapshot: dict, error: str | None = None) -> None:
        with self._lock:
            self._snapshot = dict(snapshot)
            self._last_error = error

    def snapshot(self) -> dict:
        with self._lock:
            return {**self._snapshot, "last_error": self._last_error}


class CollectorService:
    def __init__(
        self,
        config: AppConfig,
        db_path: str,
        ha_client: HomeAssistantClient,
        publisher,
        runtime_state: RuntimeState,
    ) -> None:
        self.config = config
        self.db_path = db_path
        self.ha_client = ha_client
        self.publisher = publisher
        self.runtime_state = runtime_state
        self.analyzer = JourneyAnalyzer(config.analyzer_settings)

    def run_once(self, now: datetime | None = None) -> dict:
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        try:
            with JourneyStore(self.db_path) as store:
                for entity_id in self.config.entity_ids:
                    self._collect_entity(store, entity_id, current)
                store.prune_raw_points(current - timedelta(days=self.config.retention_days))
                snapshot = build_statistics(
                    store,
                    self.config.entity_ids,
                    timezone_name=self.config.timezone,
                    stale_after_s=self.config.stale_after_s,
                    now=current,
                )
            self.publisher.publish_snapshot(snapshot)
            self.runtime_state.update(snapshot)
            return snapshot
        except Exception as error:
            LOGGER.error("Collection cycle failed (%s)", type(error).__name__)
            previous = self.runtime_state.snapshot()
            failed = {
                key: value
                for key, value in previous.items()
                if key != "last_error"
            }
            failed.update(
                {
                    "status": "error",
                    "available": False,
                    "generated_at": current.isoformat().replace("+00:00", "Z"),
                }
            )
            self.publisher.publish_snapshot(failed)
            self.runtime_state.update(failed, type(error).__name__)
            return failed

    def run_forever(self, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            self.run_once()
            stop_event.wait(self.config.poll_interval_s)

    def _collect_entity(
        self, store: JourneyStore, entity_id: str, current: datetime
    ) -> None:
        watermark_raw = store.get_metadata(f"watermark:{entity_id}")
        if watermark_raw is None:
            collection_start = current - timedelta(hours=self.config.initial_history_hours)
        else:
            watermark = datetime.fromisoformat(watermark_raw.replace("Z", "+00:00"))
            collection_start = watermark - timedelta(seconds=self.config.analyzer_settings.max_gap_s)
        analysis_start = current - timedelta(hours=self.config.analysis_window_hours)
        collection_start = max(collection_start, analysis_start)
        points = self.ha_client.collect_points(
            (entity_id,),
            start=collection_start,
            end=current,
            received_at=current,
        )
        store.save_points(points)
        stored_points = store.list_points(
            entity_id, start=analysis_start, end=current
        )
        if stored_points:
            result = self.analyzer.analyze(stored_points)
            store.save_analysis(
                result, replace_start=analysis_start, replace_end=current
            )
            latest = max(point.observed_at for point in stored_points)
            store.set_metadata(
                f"watermark:{entity_id}",
                latest.isoformat().replace("+00:00", "Z"),
            )
