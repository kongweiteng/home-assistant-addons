"""Validated add-on options."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from .models import ENTITY_ID_PATTERN
from .segmentation import AnalyzerSettings


@dataclass(frozen=True)
class AppConfig:
    entity_ids: tuple[str, ...]
    poll_interval_s: int
    initial_history_hours: int
    analysis_window_hours: int
    stale_after_s: int
    retention_days: int
    timezone: str
    analyzer_settings: AnalyzerSettings
    amap_web_key: str = ""
    amap_security_code: str = ""

    @classmethod
    def load(cls, path: str | Path) -> "AppConfig":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        entity_ids = tuple(dict.fromkeys(raw.get("entity_ids", [])))
        if not entity_ids:
            raise ValueError("entity_ids must contain at least one entity")
        for entity_id in entity_ids:
            if not isinstance(entity_id, str) or not ENTITY_ID_PATTERN.fullmatch(entity_id):
                raise ValueError("entity_ids contains an invalid entity id")
            if not entity_id.startswith(("person.", "device_tracker.")):
                raise ValueError("entity_ids only accepts person or device_tracker entities")
        timezone_name = str(raw.get("timezone", "Asia/Shanghai"))
        ZoneInfo(timezone_name)
        return cls(
            entity_ids=entity_ids,
            poll_interval_s=_bounded_int(raw, "poll_interval_s", 60, 15, 3600),
            initial_history_hours=_bounded_int(raw, "initial_history_hours", 24, 1, 168),
            analysis_window_hours=_bounded_int(raw, "analysis_window_hours", 72, 1, 720),
            stale_after_s=_bounded_int(raw, "stale_after_s", 900, 60, 86400),
            retention_days=_bounded_int(raw, "retention_days", 90, 1, 3650),
            timezone=timezone_name,
            analyzer_settings=AnalyzerSettings(
                max_accuracy_m=_positive_float(raw, "max_accuracy_m", 100.0),
                max_gap_s=_bounded_int(raw, "max_gap_s", 900, 30, 86400),
                stop_radius_m=_positive_float(raw, "stop_radius_m", 80.0),
                stop_min_duration_s=_bounded_int(raw, "stop_min_duration_s", 600, 30, 86400),
                min_journey_distance_m=_positive_float(raw, "min_journey_distance_m", 200.0),
                min_journey_duration_s=_bounded_int(raw, "min_journey_duration_s", 120, 1, 86400),
                max_speed_mps=_positive_float(raw, "max_speed_mps", 100.0),
            ),
            amap_web_key=str(raw.get("amap_web_key", "") or ""),
            amap_security_code=str(raw.get("amap_security_code", "") or ""),
        )


def _bounded_int(raw: dict, name: str, default: int, minimum: int, maximum: int) -> int:
    value = int(raw.get(name, default))
    if value < minimum or value > maximum:
        raise ValueError(f"{name} is outside its allowed range")
    return value


def _positive_float(raw: dict, name: str, default: float) -> float:
    value = float(raw.get(name, default))
    if value <= 0.0:
        raise ValueError(f"{name} must be positive")
    return value
