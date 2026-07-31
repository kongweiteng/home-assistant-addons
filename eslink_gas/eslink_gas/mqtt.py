"""MQTT Discovery publisher for low-sensitivity aggregate gas states only."""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import threading


DISCOVERY_PREFIX = "homeassistant"
BASE_TOPIC = "eslink_gas"
MQTT_STATUS_TOPIC = f"{BASE_TOPIC}/mqtt_status"
VERSION = "0.1.0"
LOGGER = logging.getLogger(__name__)


SENSORS = {
    "balance": {
        "name": "Balance",
        "device_class": "monetary",
        "unit_of_measurement": "CNY",
        "icon": "mdi:cash",
    },
    "meter_count": {"name": "Meter count", "icon": "mdi:counter"},
    "meter_status": {"name": "Meter status", "icon": "mdi:gauge"},
    "status": {"name": "Data status", "icon": "mdi:cloud-check-outline"},
    "last_success": {
        "name": "Last successful update",
        "device_class": "timestamp",
        "icon": "mdi:clock-check-outline",
    },
}


def discovery_messages(account_ids: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
    messages: list[tuple[str, str]] = []
    for account_id in account_ids:
        device = {
            "identifiers": [f"eslink_gas_{account_id}"],
            "name": f"ESLink Gas {account_id}",
            "manufacturer": "ESLink",
            "model": "Unofficial read-only gas account",
            "sw_version": VERSION,
        }
        for object_name, values in SENSORS.items():
            object_id = f"eslink_gas_{account_id}_{object_name}"
            payload = {
                "name": values["name"],
                "unique_id": object_id,
                "object_id": object_id,
                "state_topic": f"{BASE_TOPIC}/{account_id}/state/{object_name}",
                "availability_topic": MQTT_STATUS_TOPIC,
                "payload_available": "online",
                "payload_not_available": "offline",
                "device": device,
                **{key: value for key, value in values.items() if key != "name"},
            }
            messages.append(
                (
                    f"{DISCOVERY_PREFIX}/sensor/{object_id}/config",
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                )
            )
        availability_id = f"eslink_gas_{account_id}_available"
        availability = {
            "name": "Available",
            "unique_id": availability_id,
            "object_id": availability_id,
            "state_topic": f"{BASE_TOPIC}/{account_id}/state/available",
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
                f"{DISCOVERY_PREFIX}/binary_sensor/{availability_id}/config",
                json.dumps(availability, ensure_ascii=False, separators=(",", ":")),
            )
        )
    return tuple(messages)


def snapshot_messages(account_id: str, snapshot: dict) -> tuple[tuple[str, str], ...]:
    values = {
        "balance": snapshot.get("balance"),
        "meter_count": snapshot.get("meter_count"),
        "meter_status": snapshot.get("meter_status"),
        "status": snapshot.get("status"),
        "last_success": snapshot.get("last_success_at"),
    }
    messages: list[tuple[str, str]] = [(MQTT_STATUS_TOPIC, "online")]
    for key, value in values.items():
        messages.append(
            (
                f"{BASE_TOPIC}/{account_id}/state/{key}",
                "unknown" if value is None else str(value),
            )
        )
    messages.append(
        (
            f"{BASE_TOPIC}/{account_id}/state/available",
            "ON" if snapshot.get("available") else "OFF",
        )
    )
    return tuple(messages)


class HomeAssistantMqttPublisher:
    def __init__(self, ha_client, account_ids: tuple[str, ...]) -> None:
        self._ha_client = ha_client
        self._account_ids = account_ids

    def connect(self) -> None:
        self._publish(discovery_messages(self._account_ids))
        self._publish(((MQTT_STATUS_TOPIC, "online"),))

    def publish_snapshot(self, account_id: str, snapshot: dict) -> None:
        self._publish(snapshot_messages(account_id, snapshot))

    def stop(self) -> None:
        try:
            messages = [(MQTT_STATUS_TOPIC, "offline")]
            messages.extend(
                (f"{BASE_TOPIC}/{account_id}/state/available", "OFF")
                for account_id in self._account_ids
            )
            self._publish(tuple(messages))
        except Exception as error:
            LOGGER.warning("Shutdown availability publish failed (%s)", type(error).__name__)

    def _publish(self, messages: tuple[tuple[str, str], ...]) -> None:
        for topic, payload in messages:
            self._ha_client.call_service(
                "mqtt",
                "publish",
                {"topic": topic, "payload": payload, "qos": 1, "retain": True},
            )


@dataclass(frozen=True)
class MqttSettings:
    host: str
    port: int
    username: str
    password: str
    use_tls: bool = False


class MqttPublisher:
    def __init__(self, settings: MqttSettings, account_ids: tuple[str, ...]) -> None:
        import paho.mqtt.client as mqtt

        self._settings = settings
        self._account_ids = account_ids
        self._connected = threading.Event()
        self._snapshots: dict[str, dict] = {}
        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id="eslink-gas",
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

    def publish_snapshot(self, account_id: str, snapshot: dict) -> None:
        self._snapshots[account_id] = dict(snapshot)
        if self._connected.is_set():
            self._publish(snapshot_messages(account_id, snapshot))

    def stop(self) -> None:
        if self._connected.is_set():
            for account_id in self._account_ids:
                self._client.publish(
                    f"{BASE_TOPIC}/{account_id}/state/available",
                    "OFF",
                    qos=1,
                    retain=True,
                )
            self._client.publish(MQTT_STATUS_TOPIC, "offline", qos=1, retain=True)
            self._client.disconnect()
        self._client.loop_stop()

    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        if reason_code != 0:
            return
        self._connected.set()
        client.subscribe("homeassistant/status", qos=1)
        self._publish(discovery_messages(self._account_ids))
        for account_id, snapshot in self._snapshots.items():
            self.publish_snapshot(account_id, snapshot)

    def _on_message(self, client, userdata, message) -> None:
        if message.topic == "homeassistant/status" and message.payload == b"online":
            self._publish(discovery_messages(self._account_ids))
            for account_id, snapshot in self._snapshots.items():
                self.publish_snapshot(account_id, snapshot)

    def _publish(self, messages: tuple[tuple[str, str], ...]) -> None:
        for topic, payload in messages:
            self._client.publish(topic, payload, qos=1, retain=True)
