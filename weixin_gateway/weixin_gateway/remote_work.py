"""Owner-only Remote Work MQTT v1 adapter for the Mac Codex agent."""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import logging
import os
import queue
import re
import threading
from typing import Any, Mapping, Protocol

from .store import GatewayStore, StoreError


LOGGER = logging.getLogger("weixin_gateway_remote_work")

REQUEST_TOPIC = "home/codex-work/v1/request"
CONTROL_TOPIC = "home/codex-work/v1/control"
STATUS_TOPIC = "home/codex-work/v1/status"
RESULT_TOPIC = "home/codex-work/v1/result"
AGENT_TOPIC = "home/codex-work/v1/agent"

PROTOCOL_VERSION = 1
MQTT_CLIENT_ID = "weixin-gateway-remote-work-v1"
AUTHORITY = "owner_local_development_v1"
SUPPORTED_PROJECTS = frozenset({"renovation-hub"})
TASK_STATES = frozenset(
    {
        "waiting_mac",
        "queued",
        "running",
        "awaiting_confirmation",
        "completed",
        "failed",
        "cancelled",
        "expired",
        "recovery_required",
    }
)
TERMINAL_STATES = frozenset({"completed", "failed", "cancelled", "expired", "recovery_required"})

_TASK_ID_RE = re.compile(r"^RW-[A-Za-z0-9_-]{8,64}$")
_MESSAGE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
_AGENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,127}$")
_FORBIDDEN_RESULT_KEYS = frozenset(
    {"source", "sources", "diff", "patch", "raw_jsonl", "reasoning", "prompt", "logs", "stdout", "stderr"}
)


class WorkCommandError(ValueError):
    """Deterministic public parsing failure for an exact /work command."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class RemoteWorkValidationError(ValueError):
    """Stable validation error for a Remote Work MQTT document."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclasses.dataclass(frozen=True)
class WorkCommand:
    operation: str
    project_alias: str | None = None
    task_id: str | None = None
    instruction: str | None = None


@dataclasses.dataclass(frozen=True)
class RemoteWorkConfig:
    mqtt_host: str
    mqtt_port: int
    mqtt_username: str
    mqtt_password: str
    mqtt_tls: bool
    ttl_seconds: int
    addon_version: str

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "RemoteWorkConfig":
        host = env.get("WEIXIN_REMOTE_WORK_MQTT_HOST", "").strip()
        username = env.get("WEIXIN_REMOTE_WORK_MQTT_USERNAME", "").strip()
        password = env.get("WEIXIN_REMOTE_WORK_MQTT_PASSWORD", "")
        if not host:
            raise ValueError("remote work MQTT host is required")
        if not username or not password:
            raise ValueError("remote work MQTT username and password are required")
        try:
            port = int(env.get("WEIXIN_REMOTE_WORK_MQTT_PORT", "1883"))
            ttl_seconds = int(env.get("WEIXIN_REMOTE_WORK_TTL_SECONDS", "1800"))
        except ValueError as exc:
            raise ValueError("remote work MQTT port and TTL must be integers") from exc
        if not 1 <= port <= 65535:
            raise ValueError("remote work MQTT port must be from 1 to 65535")
        if not 60 <= ttl_seconds <= 3600:
            raise ValueError("remote work TTL must be from 60 to 3600 seconds")
        tls = env.get("WEIXIN_REMOTE_WORK_MQTT_TLS", "false").strip().lower()
        if tls not in {"true", "false"}:
            raise ValueError("remote work MQTT TLS must be true or false")
        return cls(
            mqtt_host=host,
            mqtt_port=port,
            mqtt_username=username,
            mqtt_password=password,
            mqtt_tls=tls == "true",
            ttl_seconds=ttl_seconds,
            addon_version=env.get("WEIXIN_ADDON_VERSION", "unknown").strip() or "unknown",
        )


class RemoteWorkPublisher(Protocol):
    def publish_pending(self) -> int:
        """Publish durable Gateway outbox messages and return the sent count."""


def parse_work_command(text: str) -> WorkCommand | None:
    """Parse the exact V1 grammar; near matches remain ordinary chat."""
    if not isinstance(text, str):
        return None
    stripped = text.strip()
    if not stripped:
        return None
    tokens = stripped.split()
    if not tokens or tokens[0] != "/work":
        return None
    if len(tokens) == 1:
        raise WorkCommandError("work_command_invalid", "请使用 /work renovation-hub <任务>。")

    action = tokens[1]
    if action == "deploy":
        raise WorkCommandError(
            "production_confirmation_required",
            "微信远程开发 V1 不执行部署；请另行建立生产发布包并确认目标。",
        )
    if action == "status":
        if len(tokens) != 3 or not _TASK_ID_RE.fullmatch(tokens[2]):
            raise WorkCommandError("work_task_id_invalid", "请使用 /work status <task_id>。")
        return WorkCommand(operation="status", task_id=tokens[2])
    if action == "cancel":
        if len(tokens) != 3 or not _TASK_ID_RE.fullmatch(tokens[2]):
            raise WorkCommandError("work_task_id_invalid", "请使用 /work cancel <task_id>。")
        return WorkCommand(operation="cancel", task_id=tokens[2])
    if action == "continue":
        if len(tokens) < 4 or not _TASK_ID_RE.fullmatch(tokens[2]):
            raise WorkCommandError("work_command_invalid", "请使用 /work continue <task_id> <补充要求>。")
        instruction = stripped.split(None, 3)[3].strip()
        _validate_instruction(instruction)
        return WorkCommand(operation="continue", task_id=tokens[2], instruction=instruction)
    if action not in SUPPORTED_PROJECTS:
        raise WorkCommandError("work_project_unknown", "当前只注册了 renovation-hub 项目别名。")
    if len(tokens) < 3:
        raise WorkCommandError("work_instruction_required", "开发任务不能为空。")
    instruction = stripped.split(None, 2)[2].strip()
    _validate_instruction(instruction)
    return WorkCommand(operation="start", project_alias=action, instruction=instruction)


def _validate_instruction(instruction: str) -> None:
    if not instruction:
        raise WorkCommandError("work_instruction_required", "开发任务不能为空。")
    if len(instruction) > 12000:
        raise WorkCommandError("work_instruction_too_long", "开发任务不能超过 12000 个字符。")


def build_command_document(
    command: WorkCommand,
    *,
    message_id: str,
    task_id: str,
    principal_hash: str,
    now: dt.datetime,
    ttl_seconds: int,
) -> tuple[str, dict[str, Any]]:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    created_at = now.isoformat()
    expires_at = (now + dt.timedelta(seconds=ttl_seconds)).isoformat()
    source = {"channel": "weixin", "principal_hash": f"sha256:{principal_hash}", "role": "owner"}
    if command.operation == "start":
        document = {
            "version": PROTOCOL_VERSION,
            "message_id": message_id,
            "task_id": task_id,
            "created_at": created_at,
            "expires_at": expires_at,
            "project_alias": command.project_alias,
            "operation": "start",
            "instruction": command.instruction,
            "source": source,
            "authority": AUTHORITY,
        }
        validate_outgoing_document(REQUEST_TOPIC, document)
        return REQUEST_TOPIC, document
    if command.operation not in {"continue", "cancel"}:
        raise ValueError("status commands do not create MQTT documents")
    document: dict[str, Any] = {
        "version": PROTOCOL_VERSION,
        "control_id": message_id,
        "task_id": task_id,
        "created_at": created_at,
        "expires_at": expires_at,
        "action": command.operation,
        "source": source,
        "authority": AUTHORITY,
    }
    if command.operation == "continue":
        document["instruction"] = command.instruction
    validate_outgoing_document(CONTROL_TOPIC, document)
    return CONTROL_TOPIC, document


def validate_outgoing_document(topic: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise RemoteWorkValidationError("invalid_payload", "payload must be an object")
    required = {
        REQUEST_TOPIC: {
            "version", "message_id", "task_id", "created_at", "expires_at", "project_alias",
            "operation", "instruction", "source", "authority",
        },
        CONTROL_TOPIC: None,
    }.get(topic)
    if topic == CONTROL_TOPIC:
        required = {
            "version", "control_id", "task_id", "created_at", "expires_at", "action", "source", "authority"
        }
        if payload.get("action") == "continue":
            required = required | {"instruction"}
    if required is None or set(payload) != required:
        raise RemoteWorkValidationError("invalid_payload", "Remote Work command fields are invalid")
    _validate_common(payload, id_field="message_id" if topic == REQUEST_TOPIC else "control_id")
    if payload.get("authority") != AUTHORITY:
        raise RemoteWorkValidationError("authority_invalid", "authority is invalid")
    source = payload.get("source")
    if not isinstance(source, Mapping) or set(source) != {"channel", "principal_hash", "role"}:
        raise RemoteWorkValidationError("source_invalid", "source is invalid")
    if source.get("channel") != "weixin" or source.get("role") != "owner":
        raise RemoteWorkValidationError("source_invalid", "source is invalid")
    principal_hash = source.get("principal_hash")
    if not isinstance(principal_hash, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", principal_hash):
        raise RemoteWorkValidationError("source_invalid", "principal hash is invalid")
    operation = payload.get("operation") if topic == REQUEST_TOPIC else payload.get("action")
    if topic == REQUEST_TOPIC:
        if operation != "start" or payload.get("project_alias") not in SUPPORTED_PROJECTS:
            raise RemoteWorkValidationError("operation_invalid", "request operation is invalid")
        _validate_instruction_value(payload.get("instruction"))
    else:
        if operation not in {"continue", "cancel"}:
            raise RemoteWorkValidationError("operation_invalid", "control operation is invalid")
        if operation == "continue":
            _validate_instruction_value(payload.get("instruction"))
        elif "instruction" in payload:
            raise RemoteWorkValidationError("invalid_payload", "cancel instruction is forbidden")
    return dict(payload)


def validate_incoming_document(topic: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise RemoteWorkValidationError("invalid_payload", "payload must be an object")
    if any(key in _FORBIDDEN_RESULT_KEYS for key in payload):
        raise RemoteWorkValidationError("privacy_payload_rejected", "payload contains forbidden detail")
    if topic == AGENT_TOPIC:
        required = {
            "version", "online", "protocol_version", "agent_version", "codex_version",
            "capabilities", "queue_depth", "active_task_id", "updated_at",
        }
        if set(payload) != required:
            raise RemoteWorkValidationError("invalid_payload", "agent fields are invalid")
        if payload.get("version") != PROTOCOL_VERSION or payload.get("protocol_version") != PROTOCOL_VERSION:
            raise RemoteWorkValidationError("version_unsupported", "version is unsupported")
        if not isinstance(payload.get("online"), bool):
            raise RemoteWorkValidationError("invalid_payload", "online must be boolean")
        _parse_time(payload.get("updated_at"), "updated_at")
        if not isinstance(payload.get("agent_version"), str) or not _AGENT_ID_RE.fullmatch(str(payload["agent_version"])):
            raise RemoteWorkValidationError("invalid_payload", "agent version is invalid")
        codex_version = payload.get("codex_version")
        if codex_version is not None and (not isinstance(codex_version, str) or len(codex_version) > 128):
            raise RemoteWorkValidationError("invalid_payload", "Codex version is invalid")
        capabilities = payload.get("capabilities")
        if not isinstance(capabilities, list) or len(capabilities) > 32 or any(
            not isinstance(value, str) or not value or len(value) > 64 for value in capabilities
        ):
            raise RemoteWorkValidationError("invalid_payload", "capabilities are invalid")
        queue_depth = payload.get("queue_depth")
        if not isinstance(queue_depth, int) or isinstance(queue_depth, bool) or not 0 <= queue_depth <= 10000:
            raise RemoteWorkValidationError("invalid_payload", "queue depth is invalid")
        active_task = payload.get("active_task_id")
        if active_task is not None and (not isinstance(active_task, str) or not _TASK_ID_RE.fullmatch(active_task)):
            raise RemoteWorkValidationError("task_id_invalid", "active task id is invalid")
        return dict(payload)

    if topic == STATUS_TOPIC:
        required = {"version", "task_id", "run_seq", "sequence", "state", "stage", "updated_at"}
        allowed = required | {"queue_position", "error_code", "action_required"}
        if not required.issubset(payload) or not set(payload).issubset(allowed):
            raise RemoteWorkValidationError("invalid_payload", "status fields are invalid")
        _validate_task_document_common(payload)
        stages = {"waiting_mac", "preflight", "queued", "workspace", "codex", "verify", "git", "handoff", "recovery"}
        if payload.get("stage") not in stages:
            raise RemoteWorkValidationError("invalid_payload", "status stage is invalid")
        _parse_time(payload.get("updated_at"), "updated_at")
        queue_position = payload.get("queue_position")
        if queue_position is not None and (
            not isinstance(queue_position, int) or isinstance(queue_position, bool) or not 0 <= queue_position <= 10000
        ):
            raise RemoteWorkValidationError("invalid_payload", "queue position is invalid")
        _validate_optional_string(payload.get("error_code"), "error_code", 128)
        _validate_optional_string(payload.get("action_required"), "action_required", 1000)
        return dict(payload)
    if topic != RESULT_TOPIC:
        raise RemoteWorkValidationError("topic_invalid", "incoming topic is invalid")
    required = {
        "version", "task_id", "run_seq", "sequence", "state", "finished_at", "summary",
        "commits", "changed_path_count", "next_actions", "result_hash",
    }
    allowed = required | {"branch", "test_summary", "error_code"}
    if not required.issubset(payload) or not set(payload).issubset(allowed):
        raise RemoteWorkValidationError("invalid_payload", "result fields are invalid")
    _validate_task_document_common(payload)
    if payload.get("state") not in {
        "awaiting_confirmation", "completed", "failed", "cancelled", "expired", "recovery_required"
    }:
        raise RemoteWorkValidationError("state_invalid", "result state is invalid")
    _parse_time(payload.get("finished_at"), "finished_at")
    _validate_optional_string(payload.get("summary"), "summary", 6000)
    _validate_optional_string(payload.get("branch"), "branch", 256)
    _validate_optional_string(payload.get("test_summary"), "test_summary", 3000)
    _validate_optional_string(payload.get("error_code"), "error_code", 128)
    commits = payload.get("commits")
    if not isinstance(commits, list) or len(commits) > 20 or any(
        not isinstance(value, str) or len(value) > 128 for value in commits
    ):
        raise RemoteWorkValidationError("invalid_payload", "commits are invalid")
    changed = payload.get("changed_path_count")
    if not isinstance(changed, int) or isinstance(changed, bool) or not 0 <= changed <= 10000:
        raise RemoteWorkValidationError("invalid_payload", "changed path count is invalid")
    next_actions = payload.get("next_actions")
    if not isinstance(next_actions, list) or len(next_actions) > 20 or any(
        not isinstance(value, str) or len(value) > 1000 for value in next_actions
    ):
        raise RemoteWorkValidationError("invalid_payload", "next actions are invalid")
    result_hash = payload.get("result_hash")
    if not isinstance(result_hash, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", result_hash):
        raise RemoteWorkValidationError("invalid_payload", "result hash is invalid")
    return dict(payload)


def _validate_common(payload: Mapping[str, Any], *, id_field: str) -> None:
    if payload.get("version") != PROTOCOL_VERSION:
        raise RemoteWorkValidationError("version_unsupported", "version is unsupported")
    message_id = payload.get(id_field)
    task_id = payload.get("task_id")
    if not isinstance(message_id, str) or not _MESSAGE_ID_RE.fullmatch(message_id):
        raise RemoteWorkValidationError("message_id_invalid", "message id is invalid")
    if not isinstance(task_id, str) or not _TASK_ID_RE.fullmatch(task_id):
        raise RemoteWorkValidationError("task_id_invalid", "task id is invalid")
    created = _parse_time(payload.get("created_at"), "created_at")
    expires = _parse_time(payload.get("expires_at"), "expires_at")
    if expires <= created or (expires - created).total_seconds() > 3600:
        raise RemoteWorkValidationError("expiry_invalid", "expiry window is invalid")
    if len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > 32768:
        raise RemoteWorkValidationError("payload_too_large", "payload is too large")


def _validate_task_document_common(payload: Mapping[str, Any]) -> None:
    if payload.get("version") != PROTOCOL_VERSION:
        raise RemoteWorkValidationError("version_unsupported", "version is unsupported")
    task_id = payload.get("task_id")
    if not isinstance(task_id, str) or not _TASK_ID_RE.fullmatch(task_id):
        raise RemoteWorkValidationError("task_id_invalid", "task id is invalid")
    _validate_sequence(payload.get("run_seq"), "run_seq", minimum=1)
    _validate_sequence(payload.get("sequence"), "sequence", minimum=1)
    if payload.get("state") not in TASK_STATES:
        raise RemoteWorkValidationError("state_invalid", "task state is invalid")


def _validate_instruction_value(value: Any) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > 12000:
        raise RemoteWorkValidationError("instruction_invalid", "instruction is invalid")


def _validate_optional_string(value: Any, name: str, max_length: int) -> None:
    if value is not None and (not isinstance(value, str) or len(value) > max_length):
        raise RemoteWorkValidationError("invalid_payload", f"{name} is invalid")


def _validate_sequence(value: Any, name: str, *, minimum: int = 0) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= 2**31 - 1:
        raise RemoteWorkValidationError("invalid_payload", f"{name} is invalid")


def _parse_time(value: Any, name: str) -> dt.datetime:
    if not isinstance(value, str):
        raise RemoteWorkValidationError("invalid_payload", f"{name} is invalid")
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError as exc:
        raise RemoteWorkValidationError("invalid_payload", f"{name} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RemoteWorkValidationError("invalid_payload", f"{name} needs a timezone")
    return parsed


class GatewayRemoteWorkRuntime:
    """MQTT v5 adapter with a durable Gateway outbox and manual inbound ACK."""

    def __init__(self, config: RemoteWorkConfig, mqtt_module: Any, *, store: GatewayStore) -> None:
        self.config = config
        self.mqtt = mqtt_module
        self.store = store
        self.stop_event = threading.Event()
        self.work_queue: queue.Queue[Any] = queue.Queue(maxsize=200)
        self._closed = False

        connect_properties = mqtt_module.Properties(mqtt_module.PacketTypes.CONNECT)
        connect_properties.SessionExpiryInterval = 24 * 60 * 60
        self.connect_properties = connect_properties
        self.client = mqtt_module.Client(
            callback_api_version=mqtt_module.CallbackAPIVersion.VERSION2,
            client_id=MQTT_CLIENT_ID,
            protocol=mqtt_module.MQTTv5,
            manual_ack=True,
        )
        self.client.username_pw_set(config.mqtt_username, config.mqtt_password)
        if config.mqtt_tls:
            self.client.tls_set()
        self.client.reconnect_delay_set(min_delay=2, max_delay=60)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self.worker = threading.Thread(target=self._worker_loop, name="weixin-remote-work-worker", daemon=True)
        self.network = threading.Thread(target=self._network_loop, name="weixin-remote-work-mqtt", daemon=True)

    def _on_connect(self, client: Any, userdata: Any, flags: Any, reason_code: Any, properties: Any) -> None:
        if getattr(reason_code, "is_failure", False):
            LOGGER.error("Remote Work MQTT connection rejected: %s", reason_code)
            return
        client.subscribe([(STATUS_TOPIC, 1), (RESULT_TOPIC, 1), (AGENT_TOPIC, 1)])
        self.publish_pending()

    def _on_disconnect(
        self,
        client: Any,
        userdata: Any,
        disconnect_flags: Any,
        reason_code: Any,
        properties: Any,
    ) -> None:
        if not self.stop_event.is_set():
            LOGGER.warning("Remote Work MQTT disconnected: %s", reason_code)

    def _on_message(self, client: Any, userdata: Any, message: Any) -> None:
        if message.topic not in {STATUS_TOPIC, RESULT_TOPIC, AGENT_TOPIC}:
            return
        try:
            self.work_queue.put_nowait(message)
        except queue.Full:
            LOGGER.error("Remote Work queue is full; message left unacknowledged")

    def publish_pending(self) -> int:
        sent = 0
        for item in self.store.remote_work_pending_outbox():
            info = self.client.publish(item["topic"], payload=item["payload_json"], qos=1, retain=False)
            try:
                info.wait_for_publish(timeout=5)
            except RuntimeError:
                self.store.mark_remote_work_outbox(item["message_id"], success=False, error_code="mqtt_publish_failed")
                continue
            if not info.is_published():
                self.store.mark_remote_work_outbox(item["message_id"], success=False, error_code="mqtt_publish_failed")
                continue
            self.store.mark_remote_work_outbox(item["message_id"], success=True)
            sent += 1
        return sent

    def _worker_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                message = self.work_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            persisted = False
            try:
                raw = message.payload
                if len(raw) > 64 * 1024:
                    raise RemoteWorkValidationError("payload_too_large", "payload is too large")
                document = json.loads(raw)
                validated = validate_incoming_document(message.topic, document)
                self.store.record_remote_work_event(message.topic, validated)
                persisted = True
            except (UnicodeDecodeError, json.JSONDecodeError, RemoteWorkValidationError, StoreError) as exc:
                LOGGER.warning("Remote Work message rejected: %s", getattr(exc, "code", "invalid_json"))
            except Exception:
                LOGGER.exception("Unexpected Remote Work processing failure")
            finally:
                if persisted and message.qos > 0:
                    self.client.ack(message.mid, message.qos)
                self.work_queue.task_done()

    def _network_loop(self) -> None:
        try:
            self.client.loop_forever(retry_first_connection=True)
        except Exception:
            if not self.stop_event.is_set():
                LOGGER.exception("Remote Work MQTT loop stopped unexpectedly")

    def start(self) -> None:
        self.client.connect(
            self.config.mqtt_host,
            self.config.mqtt_port,
            keepalive=60,
            clean_start=False,
            properties=self.connect_properties,
        )
        self.worker.start()
        self.network.start()

    def stop(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.stop_event.set()
        self.client.disconnect()
        self.worker.join(timeout=5)
        self.network.join(timeout=5)
