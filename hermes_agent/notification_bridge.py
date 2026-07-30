#!/usr/bin/env python3
"""MQTT to Hermes Weixin notification bridge.

The bridge implements the versioned ``home/notification/v1`` contract used by
Home Assistant.  It deliberately bypasses the model and invokes ``hermes send``
with the primary profile's Weixin Home Channel.

Only routing metadata is stored in SQLite.  Notification bodies, MQTT
credentials, and Weixin identities are never persisted by this process.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import logging
import os
import queue
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional


LOGGER = logging.getLogger("hermes_notification_bridge")

REQUEST_TOPIC = "home/notification/v1/request"
RESULT_TOPIC = "home/notification/v1/result"
STATUS_TOPIC = "home/notification/v1/status"
HA_BIRTH_TOPIC = "homeassistant/status"
DISCOVERY_PREFIX = "homeassistant"

PROTOCOL_VERSION = 1
DEFAULT_DEDUPE_WINDOW_SECONDS = 1800
DEFAULT_SOURCE_RATE_LIMIT = 3
DEFAULT_GLOBAL_RATE_LIMIT = 10
RATE_LIMIT_WINDOW_SECONDS = 60
RETRY_DELAYS_SECONDS = (5, 30, 120)
LEDGER_RETENTION_SECONDS = 30 * 24 * 60 * 60

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_STABLE_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.:-]{0,254}$")
_LEVEL_LABELS = {
    "info": "通知",
    "warning": "警告",
    "critical": "紧急",
}
_FINAL_STATUSES = frozenset({"duplicate", "expired", "sent", "failed"})


class RequestValidationError(ValueError):
    """A public validation failure represented by a stable error code."""

    def __init__(self, code: str, message: str, *, message_id: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message_id = message_id


@dataclasses.dataclass(frozen=True)
class BridgeConfig:
    mqtt_host: str
    mqtt_port: int
    mqtt_username: str
    mqtt_password: str
    mqtt_tls: bool
    mqtt_client_id: str
    allowed_audiences: frozenset[str]
    hermes_bin: str
    hermes_home: str
    ledger_path: Path
    addon_version: str

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "BridgeConfig":
        host = env.get("NOTIFICATION_MQTT_HOST", "").strip()
        username = env.get("NOTIFICATION_MQTT_USERNAME", "").strip()
        password = env.get("NOTIFICATION_MQTT_PASSWORD", "")
        hermes_bin = env.get("NOTIFICATION_HERMES_BIN", "").strip()
        hermes_home = env.get("NOTIFICATION_HERMES_HOME", "").strip()
        data_dir_raw = env.get("NOTIFICATION_DATA_DIR", "").strip()
        data_dir = Path(data_dir_raw)
        if not host:
            raise ValueError("NOTIFICATION_MQTT_HOST is required")
        if not username or not password:
            raise ValueError("MQTT username and password are required")
        if not hermes_bin or not hermes_home:
            raise ValueError("Hermes binary and home directory are required")
        if not data_dir_raw:
            raise ValueError("NOTIFICATION_DATA_DIR is required")

        try:
            port = int(env.get("NOTIFICATION_MQTT_PORT", "1883"))
        except ValueError as exc:
            raise ValueError("NOTIFICATION_MQTT_PORT must be an integer") from exc
        if not 1 <= port <= 65535:
            raise ValueError("NOTIFICATION_MQTT_PORT must be from 1 to 65535")

        audiences = frozenset(
            value.strip()
            for value in env.get("NOTIFICATION_ALLOWED_AUDIENCES", "owner").split(",")
            if value.strip()
        )
        if not audiences or any(not _STABLE_NAME_RE.match(value) for value in audiences):
            raise ValueError("At least one valid notification audience is required")

        tls_value = env.get("NOTIFICATION_MQTT_TLS", "false").strip().lower()
        if tls_value not in {"true", "false"}:
            raise ValueError("NOTIFICATION_MQTT_TLS must be true or false")

        client_id = env.get(
            "NOTIFICATION_MQTT_CLIENT_ID", "hermes-notification-bridge-v1"
        ).strip()
        if not _STABLE_NAME_RE.match(client_id):
            raise ValueError("Invalid MQTT client ID")

        return cls(
            mqtt_host=host,
            mqtt_port=port,
            mqtt_username=username,
            mqtt_password=password,
            mqtt_tls=tls_value == "true",
            mqtt_client_id=client_id,
            allowed_audiences=audiences,
            hermes_bin=hermes_bin,
            hermes_home=hermes_home,
            ledger_path=data_dir / "notification-ledger.sqlite3",
            addon_version=env.get("NOTIFICATION_ADDON_VERSION", "unknown").strip()
            or "unknown",
        )


@dataclasses.dataclass(frozen=True)
class NotificationRequest:
    message_id: str
    created_at: dt.datetime
    expires_at: dt.datetime
    level: str
    title: str
    message: str
    source: str
    dedupe_key: str
    ttl: int
    audience: str


def _require_string(
    payload: Mapping[str, Any],
    name: str,
    *,
    max_length: int,
    stable: bool = False,
    message_id: str = "",
) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise RequestValidationError(
            "invalid_payload",
            f"{name} must be a non-empty string",
            message_id=message_id,
        )
    value = value.strip()
    if len(value) > max_length:
        raise RequestValidationError(
            "invalid_payload", f"{name} is too long", message_id=message_id
        )
    if stable and not _STABLE_NAME_RE.match(value):
        raise RequestValidationError(
            "invalid_payload", f"{name} has an invalid format", message_id=message_id
        )
    return value


def parse_request(
    payload: Mapping[str, Any],
    *,
    allowed_audiences: Iterable[str],
    now: Optional[dt.datetime] = None,
) -> NotificationRequest:
    """Validate and normalize one v1 notification request."""
    if not isinstance(payload, Mapping):
        raise RequestValidationError("invalid_payload", "payload must be a JSON object")

    message_id_value = payload.get("message_id")
    message_id = message_id_value if isinstance(message_id_value, str) else ""
    if payload.get("version") != PROTOCOL_VERSION:
        raise RequestValidationError(
            "unsupported_version",
            "version must be 1",
            message_id=message_id,
        )
    if not _ID_RE.match(message_id):
        raise RequestValidationError("invalid_message_id", "invalid message_id")

    created_raw = payload.get("created_at")
    if not isinstance(created_raw, str):
        raise RequestValidationError(
            "invalid_created_at", "created_at must be an ISO 8601 string", message_id=message_id
        )
    try:
        created_at = dt.datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RequestValidationError(
            "invalid_created_at", "created_at is not valid ISO 8601", message_id=message_id
        ) from exc
    if created_at.tzinfo is None:
        raise RequestValidationError(
            "invalid_created_at", "created_at must include a timezone", message_id=message_id
        )

    current = now or dt.datetime.now(dt.timezone.utc)
    current = current.astimezone(dt.timezone.utc)
    created_utc = created_at.astimezone(dt.timezone.utc)
    if created_utc > current + dt.timedelta(minutes=5):
        raise RequestValidationError(
            "created_at_in_future", "created_at is too far in the future", message_id=message_id
        )

    ttl = payload.get("ttl")
    if isinstance(ttl, bool) or not isinstance(ttl, int) or not 30 <= ttl <= 86400:
        raise RequestValidationError(
            "invalid_ttl", "ttl must be an integer from 30 to 86400", message_id=message_id
        )

    level = payload.get("level")
    if level not in _LEVEL_LABELS:
        raise RequestValidationError(
            "invalid_level", "level must be info, warning, or critical", message_id=message_id
        )

    title = _require_string(payload, "title", max_length=100, message_id=message_id)
    message = _require_string(
        payload, "message", max_length=4000, message_id=message_id
    )
    source = _require_string(
        payload, "source", max_length=255, stable=True, message_id=message_id
    )
    dedupe_key = _require_string(
        payload, "dedupe_key", max_length=255, stable=True, message_id=message_id
    )
    audience = _require_string(
        payload, "audience", max_length=255, stable=True, message_id=message_id
    )
    if audience not in frozenset(allowed_audiences):
        raise RequestValidationError(
            "audience_not_allowed", "audience is not enabled", message_id=message_id
        )

    return NotificationRequest(
        message_id=message_id,
        created_at=created_utc,
        expires_at=created_utc + dt.timedelta(seconds=ttl),
        level=level,
        title=title,
        message=message,
        source=source,
        dedupe_key=dedupe_key,
        ttl=ttl,
        audience=audience,
    )


def format_weixin_text(request: NotificationRequest) -> str:
    """Return the deterministic, model-free Weixin message body."""
    return f"【{_LEVEL_LABELS[request.level]}】{request.title}\n{request.message}"


class Ledger:
    """Minimal persistent idempotency and rate-limit ledger."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.parent.chmod(0o700)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._db:
            self._db.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    message_id TEXT PRIMARY KEY,
                    dedupe_key TEXT NOT NULL,
                    source TEXT NOT NULL,
                    received_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    error_code TEXT,
                    finished_at REAL
                )
                """
            )
            self._db.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_dedupe ON messages(dedupe_key, received_at)"
            )
            self._db.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_source ON messages(source, received_at)"
            )
        path.chmod(0o600)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def get(self, message_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM messages WHERE message_id = ?", (message_id,)
            ).fetchone()
        return dict(row) if row else None

    def insert(self, request: NotificationRequest, *, status: str, now_ts: float) -> bool:
        try:
            with self._lock, self._db:
                self._db.execute(
                    """
                    INSERT INTO messages(
                        message_id, dedupe_key, source, received_at, expires_at,
                        status, attempts, error_code, finished_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 0, NULL, NULL)
                    """,
                    (
                        request.message_id,
                        request.dedupe_key,
                        request.source,
                        now_ts,
                        request.expires_at.timestamp(),
                        status,
                    ),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def update(
        self,
        message_id: str,
        *,
        status: str,
        attempts: int,
        error_code: Optional[str],
        finished_at: Optional[float] = None,
    ) -> None:
        with self._lock, self._db:
            self._db.execute(
                """
                UPDATE messages
                   SET status = ?, attempts = ?, error_code = ?, finished_at = ?
                 WHERE message_id = ?
                """,
                (status, attempts, error_code, finished_at, message_id),
            )

    def has_recent_dedupe(self, dedupe_key: str, *, since_ts: float) -> bool:
        with self._lock:
            row = self._db.execute(
                """
                SELECT 1 FROM messages
                 WHERE dedupe_key = ? AND received_at >= ?
                   AND status IN ('accepted', 'sending', 'retrying', 'sent')
                 LIMIT 1
                """,
                (dedupe_key, since_ts),
            ).fetchone()
        return row is not None

    def count_recent(self, *, since_ts: float, source: Optional[str] = None) -> int:
        params: list[Any] = [since_ts]
        source_clause = ""
        if source is not None:
            source_clause = " AND source = ?"
            params.append(source)
        with self._lock:
            row = self._db.execute(
                f"""
                SELECT COUNT(*) AS count FROM messages
                 WHERE received_at >= ?{source_clause}
                   AND status IN ('accepted', 'sending', 'retrying', 'sent')
                """,
                params,
            ).fetchone()
        return int(row["count"])

    def cleanup(self, *, before_ts: float) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM messages WHERE received_at < ?", (before_ts,))


def _result_payload(
    *,
    message_id: str,
    status: str,
    attempt: int,
    error_code: Optional[str],
    now: Optional[dt.datetime] = None,
) -> dict[str, Any]:
    current = now or dt.datetime.now(dt.timezone.utc)
    return {
        "version": PROTOCOL_VERSION,
        "message_id": message_id,
        "status": status,
        "channel": "weixin",
        "finished_at": current.astimezone().isoformat(),
        "attempt": attempt,
        "error_code": error_code,
    }


def discovery_messages(addon_version: str) -> dict[str, dict[str, Any]]:
    """Return retained Home Assistant MQTT Discovery payloads."""
    device = {
        "identifiers": ["hermes_notification_bridge_v1"],
        "name": "Hermes Notification Bridge",
        "manufacturer": "Hermes Agent HA Add-on",
        "model": "MQTT Weixin notification bridge",
        "sw_version": addon_version,
    }
    origin = {
        "name": "Hermes Agent HA Add-on",
        "sw_version": addon_version,
        "support_url": "https://github.com/kongweiteng/home-assistant-addons",
    }
    availability = {
        "availability_topic": STATUS_TOPIC,
        "availability_template": "{{ 'online' if value_json.online else 'offline' }}",
        "payload_available": "online",
        "payload_not_available": "offline",
    }
    return {
        f"{DISCOVERY_PREFIX}/binary_sensor/hermes_notification_bridge_online/config": {
            "name": "Online",
            "unique_id": "hermes_notification_bridge_online",
            "default_entity_id": "binary_sensor.hermes_notification_bridge_online",
            "device_class": "connectivity",
            "entity_category": "diagnostic",
            "state_topic": STATUS_TOPIC,
            "value_template": "{{ 'ON' if value_json.online else 'OFF' }}",
            "payload_on": "ON",
            "payload_off": "OFF",
            "qos": 1,
            "device": device,
            "origin": origin,
            **availability,
        },
        f"{DISCOVERY_PREFIX}/sensor/hermes_notification_last_result/config": {
            "name": "Last result",
            "unique_id": "hermes_notification_last_result",
            "default_entity_id": "sensor.hermes_notification_last_result",
            "entity_category": "diagnostic",
            "state_topic": RESULT_TOPIC,
            "value_template": "{{ value_json.status }}",
            "json_attributes_topic": RESULT_TOPIC,
            "qos": 1,
            "device": device,
            "origin": origin,
            **availability,
        },
    }


class NotificationProcessor:
    """Validate, deduplicate, send, and report one request at a time."""

    def __init__(
        self,
        *,
        config: BridgeConfig,
        ledger: Ledger,
        publish_result: Callable[[dict[str, Any]], bool],
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.timezone.utc),
        retry_delays: tuple[int, ...] = RETRY_DELAYS_SECONDS,
    ) -> None:
        self.config = config
        self.ledger = ledger
        self.publish_result = publish_result
        self.runner = runner
        self.sleeper = sleeper
        self.clock = clock
        self.retry_delays = retry_delays

    def _publish(
        self,
        message_id: str,
        status: str,
        *,
        attempt: int = 0,
        error_code: Optional[str] = None,
    ) -> bool:
        return self.publish_result(
            _result_payload(
                message_id=message_id,
                status=status,
                attempt=attempt,
                error_code=error_code,
                now=self.clock(),
            )
        )

    def _send_once(self, request: NotificationRequest) -> tuple[bool, str, bool]:
        env = os.environ.copy()
        # The Hermes command needs the primary profile, not the bridge's MQTT
        # credentials or routing configuration.  Keep those secrets scoped to
        # this process instead of passing them into an agent subprocess.
        for name in tuple(env):
            if name.startswith("NOTIFICATION_"):
                env.pop(name)
        env["HERMES_HOME"] = self.config.hermes_home
        try:
            result = self.runner(
                [
                    self.config.hermes_bin,
                    "send",
                    "-q",
                    "--to",
                    "weixin",
                    "--file",
                    "-",
                ],
                input=format_weixin_text(request),
                text=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=60,
                env=env,
                check=False,
            )
        except FileNotFoundError:
            return False, "hermes_command_missing", False
        except subprocess.TimeoutExpired:
            return False, "hermes_send_timeout", True
        except OSError:
            return False, "hermes_send_os_error", True
        if result.returncode == 0:
            return True, "", False
        return False, "hermes_send_failed", True

    def _finish(
        self,
        request: NotificationRequest,
        *,
        status: str,
        attempt: int,
        error_code: Optional[str],
    ) -> bool:
        self.ledger.update(
            request.message_id,
            status=status,
            attempts=attempt,
            error_code=error_code,
            finished_at=self.clock().timestamp(),
        )
        return self._publish(
            request.message_id, status, attempt=attempt, error_code=error_code
        )

    def process(self, raw_payload: bytes) -> bool:
        """Process one MQTT delivery; return whether it is safe to PUBACK."""
        try:
            decoded = raw_payload.decode("utf-8")
            payload = json.loads(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return self._publish(
                f"invalid-{uuid.uuid4().hex}", "failed", error_code="invalid_json"
            )

        try:
            request = parse_request(
                payload,
                allowed_audiences=self.config.allowed_audiences,
                now=self.clock(),
            )
        except RequestValidationError as exc:
            message_id = exc.message_id if _ID_RE.match(exc.message_id) else f"invalid-{uuid.uuid4().hex}"
            return self._publish(message_id, "failed", error_code=exc.code)

        now = self.clock()
        now_ts = now.timestamp()
        self.ledger.cleanup(before_ts=now_ts - LEDGER_RETENTION_SECONDS)

        existing = self.ledger.get(request.message_id)
        if existing:
            existing_status = str(existing["status"])
            attempt = int(existing["attempts"])
            error_code = existing["error_code"]
            if existing_status in _FINAL_STATUSES:
                return self._publish(
                    request.message_id,
                    existing_status,
                    attempt=attempt,
                    error_code=error_code,
                )
            if now >= request.expires_at:
                return self._finish(
                    request,
                    status="expired",
                    attempt=attempt,
                    error_code="ttl_expired",
                )
            if existing_status in {"sending", "retrying"}:
                return self._finish(
                    request,
                    status="failed",
                    attempt=attempt,
                    error_code="delivery_state_unknown",
                )
            # ``accepted`` is safe to resume because the send command had not
            # yet been entered when that state was written.
        else:
            if now >= request.expires_at:
                self.ledger.insert(request, status="expired", now_ts=now_ts)
                return self._finish(
                    request, status="expired", attempt=0, error_code="ttl_expired"
                )

            if self.ledger.has_recent_dedupe(
                request.dedupe_key,
                since_ts=now_ts - DEFAULT_DEDUPE_WINDOW_SECONDS,
            ):
                self.ledger.insert(request, status="duplicate", now_ts=now_ts)
                return self._finish(
                    request, status="duplicate", attempt=0, error_code="dedupe_window"
                )

            source_count = self.ledger.count_recent(
                since_ts=now_ts - RATE_LIMIT_WINDOW_SECONDS,
                source=request.source,
            )
            global_count = self.ledger.count_recent(
                since_ts=now_ts - RATE_LIMIT_WINDOW_SECONDS
            )
            if source_count >= DEFAULT_SOURCE_RATE_LIMIT or global_count >= DEFAULT_GLOBAL_RATE_LIMIT:
                self.ledger.insert(request, status="failed", now_ts=now_ts)
                return self._finish(
                    request, status="failed", attempt=0, error_code="rate_limited"
                )

            self.ledger.insert(request, status="accepted", now_ts=now_ts)
            self._publish(request.message_id, "accepted")

        attempts = 0
        delays = (0, *self.retry_delays)
        for delay in delays:
            if delay:
                retry_at = self.clock() + dt.timedelta(seconds=delay)
                if retry_at >= request.expires_at:
                    return self._finish(
                        request,
                        status="expired",
                        attempt=attempts,
                        error_code="ttl_expired",
                    )
                self.ledger.update(
                    request.message_id,
                    status="retrying",
                    attempts=attempts,
                    error_code="hermes_retry_scheduled",
                )
                self._publish(
                    request.message_id,
                    "retrying",
                    attempt=attempts,
                    error_code="hermes_retry_scheduled",
                )
                self.sleeper(delay)

            attempts += 1
            self.ledger.update(
                request.message_id,
                status="sending",
                attempts=attempts,
                error_code=None,
            )
            self._publish(request.message_id, "sending", attempt=attempts)
            ok, error_code, recoverable = self._send_once(request)
            if ok:
                return self._finish(
                    request, status="sent", attempt=attempts, error_code=None
                )
            if not recoverable:
                return self._finish(
                    request,
                    status="failed",
                    attempt=attempts,
                    error_code=error_code,
                )

        return self._finish(
            request,
            status="failed",
            attempt=attempts,
            error_code="retry_exhausted",
        )


class BridgeRuntime:
    """Paho MQTT lifecycle and single-worker delivery queue."""

    def __init__(self, config: BridgeConfig, mqtt_module: Any) -> None:
        self.config = config
        self.mqtt = mqtt_module
        self.stop_event = threading.Event()
        self.work_queue: queue.Queue[Any] = queue.Queue(maxsize=100)
        self.ledger = Ledger(config.ledger_path)

        connect_properties = mqtt_module.Properties(mqtt_module.PacketTypes.CONNECT)
        connect_properties.SessionExpiryInterval = 24 * 60 * 60
        self.connect_properties = connect_properties

        self.client = mqtt_module.Client(
            callback_api_version=mqtt_module.CallbackAPIVersion.VERSION2,
            client_id=config.mqtt_client_id,
            protocol=mqtt_module.MQTTv5,
            manual_ack=True,
        )
        self.client.username_pw_set(config.mqtt_username, config.mqtt_password)
        if config.mqtt_tls:
            self.client.tls_set()
        self.client.reconnect_delay_set(min_delay=2, max_delay=60)
        self.client.will_set(
            STATUS_TOPIC,
            payload=json.dumps(self._status_payload(False), separators=(",", ":")),
            qos=1,
            retain=True,
        )
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message

        self.processor = NotificationProcessor(
            config=config,
            ledger=self.ledger,
            publish_result=self._publish_result,
        )
        self.worker = threading.Thread(
            target=self._worker_loop,
            name="notification-worker",
            daemon=True,
        )

    def _status_payload(self, online: bool) -> dict[str, Any]:
        return {
            "online": online,
            "channel": "weixin",
            "version": PROTOCOL_VERSION,
            "updated_at": dt.datetime.now().astimezone().isoformat(),
        }

    def _publish_json(
        self, topic: str, payload: Mapping[str, Any], *, retain: bool
    ) -> bool:
        info = self.client.publish(
            topic,
            payload=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            qos=1,
            retain=retain,
        )
        try:
            info.wait_for_publish(timeout=5)
        except RuntimeError:
            return False
        return bool(info.is_published())

    def _publish_result(self, payload: dict[str, Any]) -> bool:
        return self._publish_json(RESULT_TOPIC, payload, retain=False)

    def _publish_discovery(self) -> None:
        for topic, payload in discovery_messages(self.config.addon_version).items():
            self._publish_json(topic, payload, retain=True)

    def _on_connect(
        self,
        client: Any,
        userdata: Any,
        flags: Any,
        reason_code: Any,
        properties: Any,
    ) -> None:
        if getattr(reason_code, "is_failure", False):
            LOGGER.error("MQTT connection rejected: %s", reason_code)
            return
        LOGGER.info("MQTT connected; subscribing to notification request topic")
        client.subscribe([(REQUEST_TOPIC, 1), (HA_BIRTH_TOPIC, 0)])
        self._publish_discovery()
        self._publish_json(STATUS_TOPIC, self._status_payload(True), retain=True)

    def _on_disconnect(
        self,
        client: Any,
        userdata: Any,
        disconnect_flags: Any,
        reason_code: Any,
        properties: Any,
    ) -> None:
        if not self.stop_event.is_set():
            LOGGER.warning("MQTT disconnected: %s", reason_code)

    def _on_message(self, client: Any, userdata: Any, message: Any) -> None:
        if message.topic == HA_BIRTH_TOPIC:
            if message.payload.decode("utf-8", errors="ignore").strip() == "online":
                self._publish_discovery()
            return
        if message.topic != REQUEST_TOPIC:
            return
        self.work_queue.put(message)

    def _connect(self) -> None:
        self.client.connect(
            self.config.mqtt_host,
            self.config.mqtt_port,
            keepalive=60,
            # A stable client ID and non-zero Session Expiry Interval only
            # preserve queued QoS messages across process restarts when the
            # first connection does not request a clean session.
            clean_start=False,
            properties=self.connect_properties,
        )

    def _worker_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                message = self.work_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                safe_to_ack = self.processor.process(message.payload)
                if safe_to_ack:
                    if message.qos > 0:
                        self.client.ack(message.mid, message.qos)
                else:
                    LOGGER.error("Final result was not published; request left unacknowledged")
            except Exception:
                LOGGER.exception("Unexpected notification processing failure")
            finally:
                self.work_queue.task_done()

    def stop(self, *_args: Any) -> None:
        if self.stop_event.is_set():
            return
        self.stop_event.set()
        try:
            self._publish_json(STATUS_TOPIC, self._status_payload(False), retain=True)
        finally:
            self.client.disconnect()

    def run(self) -> None:
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        self.worker.start()
        LOGGER.info(
            "Starting MQTT notification bridge (host=%s port=%s tls=%s audiences=%s)",
            self.config.mqtt_host,
            self.config.mqtt_port,
            self.config.mqtt_tls,
            ",".join(sorted(self.config.allowed_audiences)),
        )
        self._connect()
        try:
            self.client.loop_forever(retry_first_connection=True)
        finally:
            self.stop_event.set()
            self.worker.join(timeout=5)
            self.ledger.close()


def main() -> int:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        config = BridgeConfig.from_env()
        from paho.mqtt import client as mqtt

        BridgeRuntime(config, mqtt).run()
        return 0
    except Exception as exc:
        LOGGER.error("Notification bridge stopped: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
