"""Tests for the deterministic Hermes Home Assistant health snapshot."""

from __future__ import annotations

import base64
import importlib.util
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HEALTH_PATH = ROOT / "hermes_agent" / "homeassistant_health.py"
SPEC = importlib.util.spec_from_file_location("hermes_addon_health", HEALTH_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("Unable to load Home Assistant health helper")
HEALTH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HEALTH)

NOW = datetime(2026, 7, 31, 4, 0, tzinfo=timezone.utc)


def state(entity_id, value, unit=None, *, age=10, name=None):
    attributes = {}
    if unit is not None:
        attributes["unit_of_measurement"] = unit
    if name is not None:
        attributes["friendly_name"] = name
    sampled_at = datetime.fromtimestamp(NOW.timestamp() - age, timezone.utc).isoformat()
    return {
        "entity_id": entity_id,
        "state": str(value),
        "attributes": attributes,
        "last_updated": sampled_at,
        "last_changed": sampled_at,
    }


class HealthConfigTests(unittest.TestCase):
    def test_empty_config_is_safe_and_unconfigured(self):
        config, errors = HEALTH.decode_health_config("")
        self.assertEqual(errors, [])
        self.assertEqual(config["stale_after_seconds"], 300)

    def test_invalid_config_fails_closed(self):
        config, errors = HEALTH.decode_health_config("not-base64")
        self.assertEqual(config["metrics"], [])
        self.assertEqual(errors[0]["code"], "invalid_health_config_encoding")

    def test_valid_config_round_trips_without_plaintext_shell_json(self):
        payload = {
            "metrics": [{"metric": "disk_total_bytes", "entity_id": "sensor.disk_total"}],
            "statuses": [],
            "stale_after_seconds": 600,
        }
        encoded = base64.b64encode(json.dumps(payload).encode()).decode()
        config, errors = HEALTH.decode_health_config(encoded)
        self.assertEqual(errors, [])
        self.assertEqual(config, payload)


class HealthSnapshotTests(unittest.TestCase):
    def test_complete_snapshot_converts_units_and_reports_ok(self):
        config = {
            "metrics": [
                {"metric": "disk_total_bytes", "entity_id": "sensor.disk_total"},
                {"metric": "disk_used_bytes", "entity_id": "sensor.disk_used"},
                {"metric": "disk_free_bytes", "entity_id": "sensor.disk_free"},
                {"metric": "disk_used_percent", "entity_id": "sensor.disk_percent"},
                {"metric": "recorder_bytes", "entity_id": "sensor.recorder"},
                {"metric": "backup_count", "entity_id": "sensor.backup_count"},
                {"metric": "backup_bytes", "entity_id": "sensor.backup_size"},
                {"metric": "top_consumer", "entity_id": "sensor.addon_data"},
            ],
            "statuses": [
                {"entity_id": "binary_sensor.bridge_online", "expected_state": "on"}
            ],
            "stale_after_seconds": 300,
        }
        states = [
            state("sensor.disk_total", 100, "GiB"),
            state("sensor.disk_used", 40, "GiB"),
            state("sensor.disk_free", 60, "GiB"),
            state("sensor.disk_percent", 40, "%"),
            state("sensor.recorder", 512, "MiB"),
            state("sensor.backup_count", 3),
            state("sensor.backup_size", 2, "GiB"),
            state("sensor.addon_data", 1.5, "GiB", name="Add-on data"),
            state("binary_sensor.bridge_online", "on", name="Bridge online"),
        ]
        snapshot = HEALTH.build_health_snapshot(states, config, now=NOW)
        self.assertEqual(snapshot["status"], "ok")
        self.assertEqual(snapshot["disk"]["total_bytes"], 107_374_182_400)
        self.assertEqual(snapshot["recorder_bytes"], 536_870_912)
        self.assertEqual(snapshot["backup_count"], 3)
        self.assertEqual(snapshot["top_consumers"][0]["bytes"], 1_610_612_736)
        self.assertTrue(snapshot["components"][0]["healthy"])
        self.assertEqual(snapshot["missing_metrics"], [])

    def test_missing_values_are_null_never_zero(self):
        snapshot = HEALTH.build_health_snapshot(
            [], {"metrics": [], "statuses": [], "stale_after_seconds": 300}, now=NOW
        )
        self.assertEqual(snapshot["status"], "unavailable")
        self.assertIsNone(snapshot["disk"]["total_bytes"])
        self.assertIsNone(snapshot["recorder_bytes"])
        self.assertIsNone(snapshot["backup_count"])
        self.assertIn("disk_total_bytes", snapshot["missing_metrics"])

    def test_disk_values_are_derived_deterministically(self):
        config = {
            "metrics": [
                {"metric": "disk_total_bytes", "entity_id": "sensor.disk_total"},
                {"metric": "disk_free_bytes", "entity_id": "sensor.disk_free"},
            ],
            "statuses": [],
            "stale_after_seconds": 300,
        }
        snapshot = HEALTH.build_health_snapshot(
            [state("sensor.disk_total", 100, "GB"), state("sensor.disk_free", 25, "GB")],
            config,
            now=NOW,
        )
        self.assertEqual(snapshot["disk"]["used_bytes"], 75_000_000_000)
        self.assertEqual(snapshot["disk"]["used_percent"], 75.0)
        self.assertEqual(
            snapshot["derived_metrics"], ["disk_used_bytes", "disk_used_percent"]
        )
        self.assertEqual(snapshot["status"], "warning")

    def test_stale_source_has_precedence_over_partial_warning(self):
        config = {
            "metrics": [
                {"metric": "disk_total_bytes", "entity_id": "sensor.disk_total"}
            ],
            "statuses": [],
            "stale_after_seconds": 300,
        }
        snapshot = HEALTH.build_health_snapshot(
            [state("sensor.disk_total", 100, "GB", age=301)], config, now=NOW
        )
        self.assertEqual(snapshot["status"], "stale")
        self.assertEqual(snapshot["freshness_seconds"], 301)
        self.assertEqual(snapshot["sources"][0]["status"], "stale")

    def test_unavailable_and_bad_units_do_not_become_numbers(self):
        config = {
            "metrics": [
                {"metric": "recorder_bytes", "entity_id": "sensor.recorder"},
                {"metric": "backup_count", "entity_id": "sensor.backup_count"},
            ],
            "statuses": [],
            "stale_after_seconds": 300,
        }
        snapshot = HEALTH.build_health_snapshot(
            [state("sensor.recorder", "unavailable", "MiB"), state("sensor.backup_count", 1.5)],
            config,
            now=NOW,
        )
        self.assertEqual(snapshot["status"], "unavailable")
        self.assertIsNone(snapshot["recorder_bytes"])
        self.assertIsNone(snapshot["backup_count"])
        self.assertEqual(
            {item["code"] for item in snapshot["issues"]},
            {"source_unavailable", "invalid_count_value"},
        )

    def test_component_mismatch_is_warning_not_critical(self):
        config = {
            "metrics": [
                {"metric": "disk_total_bytes", "entity_id": "sensor.disk_total"},
                {"metric": "disk_used_bytes", "entity_id": "sensor.disk_used"},
                {"metric": "disk_free_bytes", "entity_id": "sensor.disk_free"},
                {"metric": "disk_used_percent", "entity_id": "sensor.disk_percent"},
                {"metric": "recorder_bytes", "entity_id": "sensor.recorder"},
                {"metric": "backup_count", "entity_id": "sensor.backup_count"},
                {"metric": "backup_bytes", "entity_id": "sensor.backup_size"},
            ],
            "statuses": [
                {"entity_id": "binary_sensor.bridge_online", "expected_state": "on"}
            ],
            "stale_after_seconds": 300,
        }
        states = [
            state("sensor.disk_total", 100, "GB"),
            state("sensor.disk_used", 40, "GB"),
            state("sensor.disk_free", 60, "GB"),
            state("sensor.disk_percent", 40, "%"),
            state("sensor.recorder", 1, "GB"),
            state("sensor.backup_count", 1),
            state("sensor.backup_size", 1, "GB"),
            state("binary_sensor.bridge_online", "off"),
        ]
        snapshot = HEALTH.build_health_snapshot(states, config, now=NOW)
        self.assertEqual(snapshot["status"], "warning")
        self.assertFalse(snapshot["components"][0]["healthy"])
        self.assertIn("component_unhealthy", {item["code"] for item in snapshot["issues"]})

    def test_duplicate_metric_does_not_override_first_source(self):
        config = {
            "metrics": [
                {"metric": "disk_total_bytes", "entity_id": "sensor.first"},
                {"metric": "disk_total_bytes", "entity_id": "sensor.second"},
            ],
            "statuses": [],
            "stale_after_seconds": 300,
        }
        snapshot = HEALTH.build_health_snapshot(
            [state("sensor.first", 10, "GB"), state("sensor.second", 99, "GB")],
            config,
            now=NOW,
        )
        self.assertEqual(snapshot["disk"]["total_bytes"], 10_000_000_000)
        self.assertIn("duplicate_metric", {item["code"] for item in snapshot["issues"]})

    def test_future_timestamp_is_rejected(self):
        config = {
            "metrics": [
                {"metric": "disk_total_bytes", "entity_id": "sensor.disk_total"}
            ],
            "statuses": [],
            "stale_after_seconds": 300,
        }
        snapshot = HEALTH.build_health_snapshot(
            [state("sensor.disk_total", 10, "GB", age=-60)], config, now=NOW
        )
        self.assertEqual(snapshot["status"], "unavailable")
        self.assertIn(
            "source_timestamp_in_future", {item["code"] for item in snapshot["issues"]}
        )

    def test_sources_do_not_echo_raw_attributes_or_values(self):
        config = {
            "metrics": [
                {"metric": "disk_total_bytes", "entity_id": "sensor.disk_total"}
            ],
            "statuses": [],
            "stale_after_seconds": 300,
        }
        item = state("sensor.disk_total", 10, "GB")
        item["attributes"]["access_token"] = "must-not-leak"
        snapshot = HEALTH.build_health_snapshot([item], config, now=NOW)
        serialized = json.dumps(snapshot)
        self.assertNotIn("must-not-leak", serialized)
        self.assertNotIn('"raw_state"', serialized)


if __name__ == "__main__":
    unittest.main()
