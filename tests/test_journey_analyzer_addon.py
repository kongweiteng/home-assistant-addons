from datetime import datetime, timedelta, timezone
import json
import pathlib
import sys
import tempfile
import threading
import unittest
from urllib.request import urlopen


ROOT = pathlib.Path(__file__).resolve().parents[1]
ADDON = ROOT / "journey_analyzer"
sys.path.insert(0, str(ADDON))

from journey_analyzer.api import create_server, journey_detail, journey_summary, render_dashboard
from journey_analyzer.config import AppConfig
from journey_analyzer.ha_client import state_to_track_point
from journey_analyzer.models import TrackPoint
from journey_analyzer.mqtt import (
    HomeAssistantMqttPublisher,
    discovery_messages,
    snapshot_messages,
)
from journey_analyzer.runtime import CollectorService, RuntimeState
from journey_analyzer.segmentation import JourneyAnalyzer
from journey_analyzer.statistics import build_statistics
from journey_analyzer.storage import JourneyStore


UTC = timezone.utc


def point(minute: int, longitude: float) -> TrackPoint:
    observed = datetime(2026, 7, 31, 1, 0, tzinfo=UTC) + timedelta(minutes=minute)
    return TrackPoint(
        entity_id="person.example",
        observed_at=observed,
        received_at=observed + timedelta(seconds=2),
        latitude=30.0,
        longitude=longitude,
        accuracy_m=10.0,
        source="synthetic",
    )


def write_options(path: pathlib.Path, **overrides) -> AppConfig:
    payload = {
        "entity_ids": ["person.example"],
        "poll_interval_s": 60,
        "initial_history_hours": 24,
        "analysis_window_hours": 72,
        "stale_after_s": 900,
        "retention_days": 90,
        "timezone": "Asia/Shanghai",
        "max_accuracy_m": 100,
        "max_gap_s": 900,
        "stop_radius_m": 80,
        "stop_min_duration_s": 600,
        "min_journey_distance_m": 200,
        "min_journey_duration_s": 120,
        "max_speed_mps": 100,
        "amap_web_key": "",
        "amap_security_code": "",
    }
    payload.update(overrides)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return AppConfig.load(path)


class FakePublisher:
    def __init__(self) -> None:
        self.snapshots = []

    def publish_snapshot(self, snapshot: dict) -> None:
        self.snapshots.append(dict(snapshot))


class FakeHomeAssistant:
    def __init__(self, points=()) -> None:
        self.points = tuple(points)

    def collect_points(self, entity_ids, *, start, end, received_at):
        return self.points


class FakeServiceClient:
    def __init__(self) -> None:
        self.calls = []

    def call_service(self, domain, service, data) -> None:
        self.calls.append((domain, service, dict(data)))


class JourneyAnalyzerAddonTests(unittest.TestCase):
    def test_required_files_and_minimum_permissions(self) -> None:
        for relative in (
            "config.yaml",
            "build.yaml",
            "Dockerfile",
            "run.sh",
            "README.md",
            "DOCS.md",
            "CHANGELOG.md",
        ):
            self.assertTrue((ADDON / relative).is_file(), relative)
        config = (ADDON / "config.yaml").read_text(encoding="utf-8")
        self.assertIn("slug: journey_analyzer", config)
        self.assertIn("homeassistant_api: true", config)
        self.assertIn("ingress: true", config)
        self.assertIn("  - mqtt:want", config)
        self.assertNotIn("  - mqtt:need", config)
        self.assertIn("backup: cold", config)
        self.assertNotIn("host_network", config)
        self.assertNotIn("privileged", config)
        self.assertNotIn("ports:", config)
        self.assertNotIn("hassio_api", config)

    def test_options_reject_empty_or_non_location_entities(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            options = pathlib.Path(tmp) / "options.json"
            options.write_text('{"entity_ids":[]}', encoding="utf-8")
            with self.assertRaises(ValueError):
                AppConfig.load(options)
            options.write_text('{"entity_ids":["light.example"]}', encoding="utf-8")
            with self.assertRaises(ValueError):
                AppConfig.load(options)

    def test_home_assistant_state_parser_preserves_source_time(self) -> None:
        received = datetime(2026, 7, 31, 2, 0, tzinfo=UTC)
        result = state_to_track_point(
            {
                "entity_id": "device_tracker.example",
                "state": "not_home",
                "last_updated": "2026-07-31T01:59:00Z",
                "attributes": {
                    "latitude": 30.0,
                    "longitude": 120.0,
                    "gps_accuracy": 12,
                    "source_type": "gps",
                },
            },
            received_at=received,
        )
        self.assertEqual(result.observed_at, datetime(2026, 7, 31, 1, 59, tzinfo=UTC))
        self.assertEqual(result.received_at, received)
        self.assertEqual(result.accuracy_m, 12.0)

    def test_unknown_state_without_coordinates_is_no_data(self) -> None:
        self.assertIsNone(
            state_to_track_point(
                {
                    "entity_id": "person.example",
                    "state": "unknown",
                    "last_updated": "2026-07-31T01:59:00Z",
                    "attributes": {},
                },
                received_at=datetime.now(UTC),
            )
        )

    def test_discovery_contains_aggregate_values_only(self) -> None:
        messages = discovery_messages()
        combined = "\n".join(topic + payload for topic, payload in messages)
        self.assertIn("journey_analyzer_today_distance", combined)
        self.assertIn("journey_analyzer_available", combined)
        self.assertNotIn("latitude", combined)
        self.assertNotIn("longitude", combined)
        self.assertEqual(len(messages), 9)

    def test_home_assistant_mqtt_publisher_uses_configured_core_broker(self) -> None:
        client = FakeServiceClient()
        publisher = HomeAssistantMqttPublisher(client)
        snapshot = {
            "today_trip_count": None,
            "today_distance_km": None,
            "today_duration_min": None,
            "distance_7d_km": None,
            "distance_30d_km": None,
            "last_trip_distance_km": None,
            "last_trip_duration_min": None,
            "status": "no_data",
            "available": False,
        }
        publisher.connect()
        publisher.publish_snapshot(snapshot)
        publisher.stop()
        self.assertEqual(len(client.calls), 22)
        self.assertTrue(
            all(domain == "mqtt" and service == "publish" for domain, service, _ in client.calls)
        )
        combined = json.dumps([data for _, _, data in client.calls])
        self.assertIn("journey_analyzer_today_distance", combined)
        self.assertIn('"payload": "unknown"', combined)
        self.assertNotIn("latitude", combined)
        self.assertNotIn("longitude", combined)
        self.assertEqual(len(snapshot_messages(snapshot)), 10)

    def test_no_location_data_is_unknown_not_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with JourneyStore(pathlib.Path(tmp) / "journeys.db") as store:
                stats = build_statistics(
                    store,
                    ("person.example",),
                    timezone_name="Asia/Shanghai",
                    stale_after_s=900,
                    now=datetime(2026, 7, 31, 2, 0, tzinfo=UTC),
                )
        self.assertEqual(stats["status"], "no_data")
        self.assertIsNone(stats["today_trip_count"])
        self.assertIsNone(stats["today_distance_km"])

    def test_runtime_persists_and_publishes_a_journey(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            config = write_options(tmp_path / "options.json")
            publisher = FakePublisher()
            service = CollectorService(
                config,
                str(tmp_path / "journeys.db"),
                FakeHomeAssistant((point(0, 120.0), point(5, 120.005), point(10, 120.01))),
                publisher,
                RuntimeState(),
            )
            snapshot = service.run_once(datetime(2026, 7, 31, 2, 0, tzinfo=UTC))
            with JourneyStore(tmp_path / "journeys.db") as store:
                journeys = store.list_journeys(entity_id="person.example")
        self.assertEqual(len(journeys), 1)
        self.assertEqual(snapshot["today_trip_count"], 1)
        self.assertTrue(snapshot["available"])
        self.assertEqual(publisher.snapshots[-1], snapshot)

    def test_summary_excludes_coordinates_and_detail_contains_them(self) -> None:
        journey = JourneyAnalyzer().analyze(
            [point(0, 120.0), point(5, 120.005), point(10, 120.01)]
        ).journeys[0]
        summary = json.dumps(journey_summary(journey))
        detail = journey_detail(journey)
        self.assertNotIn("latitude", summary)
        self.assertNotIn("longitude", summary)
        self.assertIn("latitude", detail["points"][0])
        self.assertLessEqual(len(detail["points"]), 2000)

    def test_ingress_api_is_bounded_and_health_is_coordinate_free(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            config = write_options(tmp_path / "options.json")
            db_path = tmp_path / "journeys.db"
            result = JourneyAnalyzer().analyze(
                [point(0, 120.0), point(5, 120.005), point(10, 120.01)]
            )
            with JourneyStore(db_path) as store:
                store.save_points(result.accepted_points)
                store.save_analysis(
                    result,
                    replace_start=datetime(2026, 7, 31, 0, 0, tzinfo=UTC),
                    replace_end=datetime(2026, 7, 31, 3, 0, tzinfo=UTC),
                )
            server = create_server("127.0.0.1", 0, str(db_path), config, RuntimeState())
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                with urlopen(base + "/health") as response:
                    health = response.read().decode()
                with urlopen(base + "/api/v1/journeys?limit=1") as response:
                    listing = json.loads(response.read())
                self.assertNotIn("latitude", health)
                self.assertEqual(listing["limit"], 1)
                self.assertEqual(len(listing["items"]), 1)
            finally:
                server.shutdown()
                server.server_close()

    def test_dashboard_has_amap_and_display_boundary_conversion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = write_options(
                pathlib.Path(tmp) / "options.json",
                amap_web_key="example-key",
                amap_security_code="example-code",
            )
        html = render_dashboard(config)
        self.assertIn("webapi.amap.com/maps?v=2.0", html)
        self.assertIn("wgs84ToGcj02", html)
        self.assertIn("AMap.TileLayer.Traffic", html)
        self.assertIn("moveAlong", html)

    def test_runtime_script_does_not_echo_secrets(self) -> None:
        script = (ADDON / "run.sh").read_text(encoding="utf-8")
        self.assertNotIn("set -x", script)
        self.assertNotIn("echo \"$SUPERVISOR_TOKEN", script)
        self.assertNotIn("echo \"$JOURNEY_MQTT_PASSWORD", script)
        self.assertNotIn("bashio::services.available mqtt", script)
        self.assertIn('url = "http://supervisor/services/mqtt"', script)
        self.assertIn('JOURNEY_PUBLISHER="ha_mqtt"', script)


if __name__ == "__main__":
    unittest.main()
