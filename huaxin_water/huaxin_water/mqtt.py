"""Retained MQTT Discovery publisher for privacy-safe account aggregates."""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import threading


DISCOVERY_PREFIX = "homeassistant"
BASE_TOPIC = "huaxin_water"
MQTT_STATUS_TOPIC = f"{BASE_TOPIC}/status"
HOME_ASSISTANT_STATUS_TOPIC = "homeassistant/status"
VERSION = "0.3.1"
LOGGER = logging.getLogger(__name__)


SENSORS = {
    "balance": {
        "name": "账户余额",
        "device_class": "monetary",
        "unit_of_measurement": "CNY",
        "state_class": "measurement",
        "icon": "mdi:cash",
        "suggested_display_precision": 2,
    },
    "arrears": {
        "name": "欠费金额",
        "device_class": "monetary",
        "unit_of_measurement": "CNY",
        "state_class": "measurement",
        "icon": "mdi:cash-alert",
        "suggested_display_precision": 2,
    },
    "current_charge": {
        "name": "本期水费",
        "device_class": "monetary",
        "unit_of_measurement": "CNY",
        "state_class": "measurement",
        "icon": "mdi:receipt-text-outline",
        "suggested_display_precision": 2,
    },
    "current_usage": {
        "name": "本期用水量",
        "device_class": "water",
        "unit_of_measurement": "m³",
        "state_class": "measurement",
        "icon": "mdi:water",
        "suggested_display_precision": 3,
    },
    "annual_usage": {
        "name": "本年用水量",
        "device_class": "water",
        "unit_of_measurement": "m³",
        "state_class": "total",
        "icon": "mdi:water-sync",
        "suggested_display_precision": 3,
    },
    "annual_charge": {
        "name": "本年应收水费",
        "device_class": "monetary",
        "unit_of_measurement": "CNY",
        "state_class": "total",
        "icon": "mdi:calendar-cash",
        "suggested_display_precision": 2,
    },
    "meter_reading": {
        "name": "最近水表读数",
        "device_class": "water",
        "unit_of_measurement": "m³",
        "icon": "mdi:gauge",
        "suggested_display_precision": 3,
    },
    "meter_count": {"name": "水表数量", "icon": "mdi:counter"},
    "payment_status": {"name": "缴费状态", "icon": "mdi:cash-check"},
    "billing_period": {
        "name": "当前计费月份",
        "icon": "mdi:calendar-month-outline",
        "entity_category": "diagnostic",
    },
    "last_update": {
        "name": "数据更新时间",
        "device_class": "timestamp",
        "icon": "mdi:clock-check-outline",
        "entity_category": "diagnostic",
    },
    "data_status": {
        "name": "数据状态",
        "icon": "mdi:database-check-outline",
        "entity_category": "diagnostic",
    },
}


def discovery_messages(account_ids: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
    messages: list[tuple[str, str]] = []
    for account_id in account_ids:
        device_id = f"huaxin_water_{account_id}"
        node_id = f"huaxin_water_{_entity_token(account_id)}"
        state_topic = f"{BASE_TOPIC}/{account_id}/state"
        device = {
            "identifiers": [device_id],
            "name": f"华新水务 {account_id}",
            "manufacturer": "天津华新水务",
            "model": "非官方只读水务账户",
            "sw_version": VERSION,
        }
        for key, values in SENSORS.items():
            unique_id = f"{device_id}_{key}"
            payload = {
                "name": values["name"],
                "unique_id": unique_id,
                "object_id": f"{node_id}_{key}",
                "state_topic": state_topic,
                "value_template": (
                    "{{ value_json."
                    + key
                    + " if value_json."
                    + key
                    + " is not none else none }}"
                ),
                "availability_topic": MQTT_STATUS_TOPIC,
                "payload_available": "online",
                "payload_not_available": "offline",
                "device": device,
                **{name: value for name, value in values.items() if name != "name"},
            }
            messages.append(
                (
                    f"{DISCOVERY_PREFIX}/sensor/{node_id}/{key}/config",
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                )
            )

        unique_id = f"{device_id}_available"
        availability = {
            "name": "数据可用状态",
            "unique_id": unique_id,
            "object_id": f"{node_id}_available",
            "state_topic": state_topic,
            "value_template": "{{ 'ON' if value_json.available else 'OFF' }}",
            "payload_on": "ON",
            "payload_off": "OFF",
            "device_class": "connectivity",
            "availability_topic": MQTT_STATUS_TOPIC,
            "payload_available": "online",
            "payload_not_available": "offline",
            "entity_category": "diagnostic",
            "device": device,
        }
        messages.append(
            (
                f"{DISCOVERY_PREFIX}/binary_sensor/{node_id}/available/config",
                json.dumps(availability, ensure_ascii=False, separators=(",", ":")),
            )
        )
    return tuple(messages)


def mqtt_state_from_account(account: dict) -> dict:
    """Project a full private account snapshot into a bounded low-sensitivity state."""
    summary = account.get("summary") if isinstance(account.get("summary"), dict) else {}
    statistics = (
        account.get("statistics")
        if isinstance(account.get("statistics"), dict)
        else {}
    )
    current = _latest_billing_month(statistics)
    latest_year = statistics.get("latest_year")
    yearly = statistics.get("yearly") if isinstance(statistics.get("yearly"), list) else []
    annual = next(
        (
            item
            for item in yearly
            if isinstance(item, dict) and item.get("year") == latest_year
        ),
        {},
    )
    arrears = summary.get("arrears")
    if isinstance(arrears, (int, float)) and not isinstance(arrears, bool):
        payment_status = "欠费" if arrears > 0 else "无欠费"
    else:
        payment_status = "未知"
    status = account.get("status")
    return {
        "balance": summary.get("remaining"),
        "arrears": arrears,
        "current_charge": current.get("charge"),
        "current_usage": current.get("usage"),
        "annual_usage": annual.get("usage"),
        "annual_charge": annual.get("charge"),
        "meter_reading": summary.get("latest_reading"),
        "meter_count": summary.get("meter_count"),
        "payment_status": payment_status,
        "billing_period": current.get("period"),
        "last_update": account.get("last_success_at"),
        "data_status": status,
        "available": status in {"good", "degraded"},
    }


def state_message(account_id: str, state: dict) -> tuple[str, str]:
    return (
        f"{BASE_TOPIC}/{account_id}/state",
        json.dumps(state, ensure_ascii=False, separators=(",", ":"), allow_nan=False),
    )


def managed_topics(account_ids: tuple[str, ...]) -> set[str]:
    topics = {topic for topic, _ in discovery_messages(account_ids)}
    topics.update(f"{BASE_TOPIC}/{account_id}/state" for account_id in account_ids)
    return topics


def _latest_billing_month(statistics: dict) -> dict:
    monthly = statistics.get("monthly_by_year")
    years = statistics.get("years")
    if not isinstance(monthly, dict) or not isinstance(years, list):
        return {}
    for year in years:
        months = monthly.get(str(year))
        if not isinstance(months, list):
            continue
        for month in reversed(months):
            if not isinstance(month, dict) or not month.get("water_record_count"):
                continue
            month_number = month.get("month")
            if not isinstance(month_number, int):
                continue
            return {
                "period": f"{year:04d}-{month_number:02d}",
                "usage": month.get("usage"),
                "charge": month.get("charge"),
            }
    return {}


def _entity_token(account_id: str) -> str:
    return account_id.replace("_", "_u").replace("-", "_d")


@dataclass(frozen=True)
class MqttSettings:
    host: str
    port: int
    username: str
    password: str
    use_tls: bool = False


class MqttPublisher:
    def __init__(
        self,
        settings: MqttSettings,
        account_ids: tuple[str, ...],
        topic_registry_path: str | Path,
        mqtt_module=None,
    ) -> None:
        if mqtt_module is None:
            import paho.mqtt.client as mqtt_module

        self._settings = settings
        self._account_ids = account_ids
        self._registry_path = Path(topic_registry_path)
        self._connected = threading.Event()
        self._lock = threading.RLock()
        self._states: dict[str, dict] = {}
        self._client = mqtt_module.Client(
            mqtt_module.CallbackAPIVersion.VERSION2,
            client_id="huaxin-water",
            clean_session=True,
        )
        if settings.username:
            self._client.username_pw_set(settings.username, settings.password)
        if settings.use_tls:
            self._client.tls_set()
        self._client.will_set(MQTT_STATUS_TOPIC, "offline", qos=1, retain=True)
        self._client.reconnect_delay_set(min_delay=1, max_delay=60)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def connect(self, timeout_s: float = 15.0) -> None:
        self._client.connect(self._settings.host, self._settings.port, keepalive=60)
        self._client.loop_start()
        if not self._connected.wait(timeout_s):
            self._client.loop_stop()
            raise RuntimeError("MQTT connection timed out")

    def publish_snapshot(self, account_id: str, state: dict) -> None:
        if account_id not in self._account_ids:
            raise ValueError("unknown account id")
        with self._lock:
            self._states[account_id] = dict(state)
            if self._connected.is_set():
                self._publish((state_message(account_id, state),))

    def stop(self) -> None:
        if self._connected.is_set():
            info = self._client.publish(
                MQTT_STATUS_TOPIC, "offline", qos=1, retain=True
            )
            wait = getattr(info, "wait_for_publish", None)
            if callable(wait):
                wait(timeout=5)
            self._client.disconnect()
        self._client.loop_stop()
        self._connected.clear()

    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        if reason_code != 0:
            return
        self._connected.set()
        client.subscribe(HOME_ASSISTANT_STATUS_TOPIC, qos=1)
        with self._lock:
            self._synchronize_discovery()
            self._publish(((MQTT_STATUS_TOPIC, "online"),))
            self._publish_states()

    def _on_disconnect(
        self, client, userdata, disconnect_flags, reason_code, properties
    ) -> None:
        self._connected.clear()

    def _on_message(self, client, userdata, message) -> None:
        if (
            message.topic == HOME_ASSISTANT_STATUS_TOPIC
            and message.payload == b"online"
        ):
            with self._lock:
                self._synchronize_discovery()
                self._publish_states()

    def _synchronize_discovery(self) -> None:
        current = managed_topics(self._account_ids)
        previous = self._load_topics()
        obsolete = tuple((topic, "") for topic in sorted(previous - current))
        if obsolete:
            self._publish(obsolete)
        self._publish(discovery_messages(self._account_ids))
        self._save_topics(current)

    def _publish_states(self) -> None:
        self._publish(
            tuple(
                state_message(account_id, state)
                for account_id, state in self._states.items()
            )
        )

    def _publish(self, messages: tuple[tuple[str, str], ...]) -> None:
        for topic, payload in messages:
            self._client.publish(topic, payload, qos=1, retain=True)

    def _load_topics(self) -> set[str]:
        try:
            payload = json.loads(self._registry_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return set()
        except Exception as error:
            LOGGER.warning("MQTT topic registry ignored (%s)", type(error).__name__)
            return set()
        if not isinstance(payload, list):
            return set()
        return {topic for topic in payload if isinstance(topic, str)}

    def _save_topics(self, topics: set[str]) -> None:
        self._registry_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self._registry_path.with_suffix(self._registry_path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(sorted(topics), ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        temporary.replace(self._registry_path)
