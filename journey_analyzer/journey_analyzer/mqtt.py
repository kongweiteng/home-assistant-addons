"""MQTT Discovery publisher for aggregate statistics only."""

from __future__ import annotations

from dataclasses import dataclass
import json
import threading


DISCOVERY_PREFIX = "homeassistant"
BASE_TOPIC = "journey_analyzer"
MQTT_STATUS_TOPIC = f"{BASE_TOPIC}/mqtt_status"


SENSORS = {
    "today_trip_count": {"name": "Today trip count", "icon": "mdi:routes"},
    "today_distance": {"name": "Today distance", "device_class": "distance", "unit_of_measurement": "km", "state_class": "measurement"},
    "today_duration": {"name": "Today duration", "device_class": "duration", "unit_of_measurement": "min", "state_class": "measurement"},
    "7d_distance": {"name": "7 day distance", "device_class": "distance", "unit_of_measurement": "km", "state_class": "measurement"},
    "30d_distance": {"name": "30 day distance", "device_class": "distance", "unit_of_measurement": "km", "state_class": "measurement"},
    "last_trip_distance": {"name": "Last trip distance", "device_class": "distance", "unit_of_measurement": "km", "state_class": "measurement"},
    "last_trip_duration": {"name": "Last trip duration", "device_class": "duration", "unit_of_measurement": "min", "state_class": "measurement"},
    "location_quality": {"name": "Location quality", "icon": "mdi:crosshairs-gps"},
}


SNAPSHOT_KEYS = {
    "today_trip_count": "today_trip_count",
    "today_distance": "today_distance_km",
    "today_duration": "today_duration_min",
    "7d_distance": "distance_7d_km",
    "30d_distance": "distance_30d_km",
    "last_trip_distance": "last_trip_distance_km",
    "last_trip_duration": "last_trip_duration_min",
    "location_quality": "status",
}


def discovery_messages() -> tuple[tuple[str, str], ...]:
    device = {
        "identifiers": ["journey_analyzer"],
        "name": "Journey Analyzer",
        "manufacturer": "Kongweiteng",
        "model": "Local journey analytics",
        "sw_version": "0.1.0",
    }
    messages: list[tuple[str, str]] = []
    for object_name, values in SENSORS.items():
        payload = {
            "name": values["name"],
            "unique_id": f"journey_analyzer_{object_name}",
            "object_id": f"journey_analyzer_{object_name}",
            "state_topic": f"{BASE_TOPIC}/state/{object_name}",
            "availability_topic": MQTT_STATUS_TOPIC,
            "payload_available": "online",
            "payload_not_available": "offline",
            "device": device,
            **{key: value for key, value in values.items() if key != "name"},
        }
        messages.append(
            (
                f"{DISCOVERY_PREFIX}/sensor/journey_analyzer_{object_name}/config",
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            )
        )
    availability = {
        "name": "Available",
        "unique_id": "journey_analyzer_available",
        "object_id": "journey_analyzer_available",
        "state_topic": f"{BASE_TOPIC}/state/available",
        "payload_on": "ON",
        "payload_off": "OFF",
        "device_class": "connectivity",
        "availability_topic": MQTT_STATUS_TOPIC,
        "payload_available": "online",
        "payload_not_available": "offline",
        "device": device,
    }
    messages.append(
        (
            f"{DISCOVERY_PREFIX}/binary_sensor/journey_analyzer_available/config",
            json.dumps(availability, ensure_ascii=False, separators=(",", ":")),
        )
    )
    return tuple(messages)


@dataclass(frozen=True)
class MqttSettings:
    host: str
    port: int
    username: str
    password: str
    use_tls: bool = False


class MqttPublisher:
    def __init__(self, settings: MqttSettings) -> None:
        import paho.mqtt.client as mqtt

        self._mqtt = mqtt
        self._settings = settings
        self._connected = threading.Event()
        self._snapshot: dict | None = None
        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id="journey-analyzer",
            clean_session=True,
        )
        if settings.username:
            self._client.username_pw_set(settings.username, settings.password)
        if settings.use_tls:
            self._client.tls_set()
        self._client.will_set(MQTT_STATUS_TOPIC, "offline", qos=1, retain=True)
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message

    def connect(self, timeout_s: float = 15.0) -> None:
        self._client.connect(self._settings.host, self._settings.port, keepalive=60)
        self._client.loop_start()
        if not self._connected.wait(timeout_s):
            self._client.loop_stop()
            raise RuntimeError("MQTT connection timed out")

    def publish_snapshot(self, snapshot: dict) -> None:
        self._snapshot = dict(snapshot)
        if not self._connected.is_set():
            return
        self._client.publish(MQTT_STATUS_TOPIC, "online", qos=1, retain=True)
        for object_name, snapshot_key in SNAPSHOT_KEYS.items():
            value = snapshot.get(snapshot_key)
            payload = "unknown" if value is None else str(value)
            self._client.publish(
                f"{BASE_TOPIC}/state/{object_name}", payload, qos=1, retain=True
            )
        self._client.publish(
            f"{BASE_TOPIC}/state/available",
            "ON" if snapshot.get("available") else "OFF",
            qos=1,
            retain=True,
        )

    def stop(self) -> None:
        if self._connected.is_set():
            self._client.publish(f"{BASE_TOPIC}/state/available", "OFF", qos=1, retain=True)
            self._client.publish(MQTT_STATUS_TOPIC, "offline", qos=1, retain=True)
            self._client.disconnect()
        self._client.loop_stop()

    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        if reason_code != 0:
            return
        self._connected.set()
        client.subscribe("homeassistant/status", qos=1)
        self._publish_discovery()
        if self._snapshot is not None:
            self.publish_snapshot(self._snapshot)

    def _on_message(self, client, userdata, message) -> None:
        if message.topic == "homeassistant/status" and message.payload == b"online":
            self._publish_discovery()
            if self._snapshot is not None:
                self.publish_snapshot(self._snapshot)

    def _publish_discovery(self) -> None:
        for topic, payload in discovery_messages():
            self._client.publish(topic, payload, qos=1, retain=True)
