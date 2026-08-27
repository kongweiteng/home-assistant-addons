"""Persistent, fail-closed queue and conversation mapping for Codex jobs."""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from typing import Any
import uuid
from zoneinfo import ZoneInfo

from .tool_catalog import (
    AITO_PREPARE_CAR_DEFINITIONS,
    BOOTSTRAP_HUB_DEFINITIONS,
    MEMO_DEFINITIONS,
    OPERATION_DEFINITIONS,
    TOOL_DEFINITIONS,
    ToolDefinition,
)


CONVERSATION_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
CAPABILITY_PROFILES = frozenset({"owner_legacy", "owner", "member_read_only"})
ACTIVE_STATES = ("queued", "running", "recovery_required")
FINAL_STATES = ("completed", "failed", "cancelled")
RECOVERY_RESOLUTIONS = {
    "confirmed_completed": ("completed", None),
    "confirmed_failed": ("failed", "recovery_review_failed"),
    "cancelled": ("cancelled", "recovery_review_cancelled"),
}
ARTIFACT_ID_RE = re.compile(r"^AR-[A-Z2-7]{26}$")
DOWNLOAD_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
DEFAULT_ARTIFACT_TTL_SECONDS = 86400
DEFAULT_MAX_ARTIFACT_BYTES = 20 * 1024 * 1024
DEFAULT_ARTIFACT_QUOTA_BYTES = 100 * 1024 * 1024
DEFAULT_MAX_ARTIFACTS_PER_JOB = 4
SHANGHAI = ZoneInfo("Asia/Shanghai")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def shanghai_now() -> datetime:
    return datetime.now(SHANGHAI)


def shanghai_now_iso() -> str:
    return datetime.now(SHANGHAI).isoformat()


def shanghai_after(seconds: float) -> str:
    return (datetime.now(SHANGHAI) + timedelta(seconds=max(0.0, float(seconds)))).isoformat()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class StoreError(RuntimeError):
    """A deterministic queue validation or state error."""

    def __init__(self, code: str, message: str, *, status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


class ControllerStore:
    """SQLite source of truth for jobs, idempotency and Thread mappings."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        max_queue: int = 200,
        max_result_chars: int = 12000,
        artifact_dir: str | Path | None = None,
        artifact_ttl_seconds: int = DEFAULT_ARTIFACT_TTL_SECONDS,
        max_artifact_bytes: int = DEFAULT_MAX_ARTIFACT_BYTES,
        artifact_quota_bytes: int = DEFAULT_ARTIFACT_QUOTA_BYTES,
        max_artifacts_per_job: int = DEFAULT_MAX_ARTIFACTS_PER_JOB,
    ):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.max_queue = max_queue
        self.max_result_chars = max_result_chars
        self.artifact_dir = Path(artifact_dir) if artifact_dir is not None else self.database_path.parent / "job-artifacts"
        self.artifact_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        if self.artifact_dir.is_symlink() or not self.artifact_dir.is_dir():
            raise StoreError("artifact_storage_invalid", "Controller artifact 目录无效", status=500)
        os.chmod(self.artifact_dir, 0o700)
        self.artifact_ttl_seconds = max(300, int(artifact_ttl_seconds))
        self.max_artifact_bytes = max(1024, int(max_artifact_bytes))
        self.artifact_quota_bytes = max(self.max_artifact_bytes, int(artifact_quota_bytes))
        self.max_artifacts_per_job = max(1, int(max_artifacts_per_job))
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            existing_tables = {
                row["name"]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    conversation_key TEXT PRIMARY KEY,
                    thread_id TEXT UNIQUE,
                    display_name TEXT,
                    state TEXT NOT NULL DEFAULT 'active'
                        CHECK(state IN ('active','archived','blocked')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    message_id TEXT NOT NULL UNIQUE,
                    conversation_key TEXT NOT NULL,
                    thread_id TEXT,
                    turn_id TEXT UNIQUE,
                    state TEXT NOT NULL
                        CHECK(state IN ('queued','running','completed','failed','cancelled','recovery_required')),
                    input_digest TEXT NOT NULL,
                    input_json TEXT NOT NULL,
                    result_text TEXT,
                    result_summary TEXT,
                    error_code TEXT,
                    error_type TEXT,
                    upstream_http_status INTEGER,
                    retryable INTEGER NOT NULL DEFAULT 0 CHECK(retryable IN (0,1)),
                    output_observed INTEGER NOT NULL DEFAULT 0 CHECK(output_observed IN (0,1)),
                    tool_activity_observed INTEGER NOT NULL DEFAULT 0 CHECK(tool_activity_observed IN (0,1)),
                    artifact_observed INTEGER NOT NULL DEFAULT 0 CHECK(artifact_observed IN (0,1)),
                    retry_not_before TEXT,
                    attempt INTEGER NOT NULL DEFAULT 0,
                    capability_profile TEXT NOT NULL DEFAULT 'owner_legacy'
                        CHECK(capability_profile IN ('owner_legacy','owner','member_read_only')),
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    FOREIGN KEY(conversation_key) REFERENCES conversations(conversation_key)
                );
                CREATE INDEX IF NOT EXISTS jobs_state_created_idx ON jobs(state, created_at);
                CREATE TABLE IF NOT EXISTS job_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    item_type TEXT,
                    content_length INTEGER,
                    error_code TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(job_id) REFERENCES jobs(job_id)
                );
                CREATE TABLE IF NOT EXISTS job_artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    artifact_type TEXT NOT NULL CHECK(artifact_type IN ('image')),
                    mime_type TEXT NOT NULL CHECK(mime_type IN ('image/png')),
                    storage_name TEXT NOT NULL UNIQUE,
                    size_bytes INTEGER NOT NULL CHECK(size_bytes > 0),
                    sha256 TEXT NOT NULL,
                    width INTEGER NOT NULL CHECK(width > 0),
                    height INTEGER NOT NULL CHECK(height > 0),
                    summary_json TEXT NOT NULL,
                    download_token_hash TEXT NOT NULL UNIQUE,
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(job_id,sha256),
                    FOREIGN KEY(job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS job_artifacts_job_idx ON job_artifacts(job_id,created_at);
                CREATE INDEX IF NOT EXISTS job_artifacts_expiry_idx ON job_artifacts(expires_at);
                CREATE TABLE IF NOT EXISTS controller_meta (
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    display_secret BLOB NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tool_policy_meta (
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    catalog_revision INTEGER NOT NULL CHECK(catalog_revision >= 1),
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tool_policies (
                    tool_name TEXT PRIMARY KEY,
                    enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
                    revision INTEGER NOT NULL CHECK(revision >= 1),
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS admin_mutations (
                    request_id TEXT PRIMARY KEY,
                    scope TEXT NOT NULL,
                    request_digest TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tool_invocations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT,
                    turn_id TEXT,
                    tool_name TEXT NOT NULL,
                    outcome TEXT NOT NULL CHECK(outcome IN ('succeeded','rejected','failed')),
                    error_code TEXT,
                    duration_ms INTEGER NOT NULL CHECK(duration_ms >= 0),
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS tool_invocations_name_created_idx
                    ON tool_invocations(tool_name, created_at DESC, id DESC);
                CREATE TABLE IF NOT EXISTS mcp_catalog_observation (
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    catalog_revision INTEGER,
                    published_tools_json TEXT NOT NULL,
                    error_code TEXT,
                    observed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS hub_manifest_state (
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    document_json TEXT,
                    catalog_digest TEXT,
                    hub_revision INTEGER,
                    synchronized_at TEXT,
                    error_code TEXT,
                    error_at TEXT
                );
                CREATE TABLE IF NOT EXISTS hub_tool_history (
                    tool_name TEXT PRIMARY KEY,
                    definition_json TEXT NOT NULL,
                    retired INTEGER NOT NULL DEFAULT 0 CHECK(retired IN (0,1)),
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS aito_prepare_car_confirmations (
                    confirmation_id TEXT PRIMARY KEY,
                    conversation_key TEXT NOT NULL,
                    request_message_id TEXT NOT NULL UNIQUE,
                    execute_message_id TEXT UNIQUE,
                    target_on INTEGER NOT NULL CHECK(target_on IN (0,1)),
                    state TEXT NOT NULL CHECK(state IN (
                        'pending','executing','submitted','failed','unknown','cancelled','expired'
                    )),
                    requested_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    consumed_at TEXT,
                    completed_at TEXT,
                    result_json TEXT,
                    error_code TEXT,
                    FOREIGN KEY(conversation_key) REFERENCES conversations(conversation_key)
                );
                CREATE INDEX IF NOT EXISTS aito_prepare_car_conversation_idx
                    ON aito_prepare_car_confirmations(conversation_key, requested_at DESC);
                CREATE UNIQUE INDEX IF NOT EXISTS aito_prepare_car_pending_idx
                    ON aito_prepare_car_confirmations(conversation_key)
                    WHERE state='pending';
                """
            )
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(jobs)")}
            if "capability_profile" not in columns:
                connection.execute(
                    "ALTER TABLE jobs ADD COLUMN capability_profile TEXT NOT NULL DEFAULT 'owner_legacy'"
                )
            if "result_summary" not in columns:
                connection.execute("ALTER TABLE jobs ADD COLUMN result_summary TEXT")
            job_additions = {
                "error_type": "TEXT",
                "upstream_http_status": "INTEGER",
                "retryable": "INTEGER NOT NULL DEFAULT 0",
                "output_observed": "INTEGER NOT NULL DEFAULT 0",
                "tool_activity_observed": "INTEGER NOT NULL DEFAULT 0",
                "artifact_observed": "INTEGER NOT NULL DEFAULT 0",
                "retry_not_before": "TEXT",
            }
            for name, declaration in job_additions.items():
                if name not in columns:
                    connection.execute(f"ALTER TABLE jobs ADD COLUMN {name} {declaration}")
            invocation_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(tool_invocations)")
            }
            if "job_id" not in invocation_columns:
                connection.execute("ALTER TABLE tool_invocations ADD COLUMN job_id TEXT")
            if "turn_id" not in invocation_columns:
                connection.execute("ALTER TABLE tool_invocations ADD COLUMN turn_id TEXT")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS tool_invocations_job_turn_idx "
                "ON tool_invocations(job_id, turn_id, created_at DESC, id DESC)"
            )
            now = utc_now()
            if "controller_meta" not in existing_tables:
                connection.execute(
                    "INSERT INTO controller_meta(id,display_secret,created_at) VALUES (1,?,?)",
                    (os.urandom(32), now),
                )
            if "tool_policy_meta" not in existing_tables:
                connection.execute(
                    "INSERT INTO tool_policy_meta(id,catalog_revision,updated_at) VALUES (1,1,?)",
                    (now,),
                )
            connection.execute(
                "INSERT OR IGNORE INTO hub_manifest_state(id) VALUES (1)"
            )
            for definition in BOOTSTRAP_HUB_DEFINITIONS:
                connection.execute(
                    "INSERT OR IGNORE INTO hub_tool_history(tool_name,definition_json,retired,first_seen_at,last_seen_at) "
                    "VALUES (?,?,0,?,?)",
                    (definition.name, canonical_json(definition.public_metadata()), now, now),
                )
        os.chmod(self.database_path, 0o600)

    @staticmethod
    def validate_job(payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict) or payload.get("version") != 1:
            raise StoreError("invalid_job", "作业版本无效")
        message_id = payload.get("message_id")
        conversation_key = payload.get("conversation_key")
        text = payload.get("text")
        received_at = payload.get("received_at")
        attachments = payload.get("attachments")
        reply_capabilities = payload.get("reply_capabilities")
        capability_profile = payload.get("capability_profile", "owner_legacy")
        if not isinstance(message_id, str) or not 1 <= len(message_id) <= 256:
            raise StoreError("invalid_job", "message_id 无效")
        if not isinstance(conversation_key, str) or not CONVERSATION_RE.fullmatch(conversation_key):
            raise StoreError("invalid_job", "conversation_key 无效")
        if not isinstance(received_at, str) or len(received_at) > 64:
            raise StoreError("invalid_job", "received_at 无效")
        if not isinstance(text, str) or len(text) > 32000:
            raise StoreError("invalid_job", "text 无效")
        if not isinstance(attachments, list) or len(attachments) > 16:
            raise StoreError("invalid_job", "attachments 无效")
        normalized_attachments: list[dict[str, Any]] = []
        for attachment in attachments:
            if not isinstance(attachment, dict):
                raise StoreError("invalid_job", "附件元数据无效")
            reference = attachment.get("attachment_ref")
            media_type = attachment.get("media_type")
            size_bytes = attachment.get("size_bytes")
            digest = attachment.get("sha256")
            if not isinstance(reference, str) or not 16 <= len(reference) <= 256:
                raise StoreError("invalid_job", "attachment_ref 无效")
            if media_type not in {"image", "file", "video", "audio"}:
                raise StoreError("invalid_job", "media_type 无效")
            if not isinstance(size_bytes, int) or not 1 <= size_bytes <= 50 * 1024 * 1024:
                raise StoreError("invalid_job", "附件大小无效")
            if not isinstance(digest, str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
                raise StoreError("invalid_job", "附件摘要无效")
            normalized_attachments.append(
                {
                    "attachment_ref": reference,
                    "media_type": media_type,
                    "size_bytes": size_bytes,
                    "sha256": digest,
                    **({"display_name": str(attachment["display_name"])[:255]} if attachment.get("display_name") else {}),
                }
            )
        if not isinstance(reply_capabilities, list) or not set(reply_capabilities).issubset({"text", "image", "file"}):
            raise StoreError("invalid_job", "reply_capabilities 无效")
        if "capability_profile" in payload and capability_profile not in {"owner", "member_read_only"}:
            raise StoreError("invalid_capability_profile", "作业能力画像无效")
        return {
            "version": 1,
            "message_id": message_id,
            "conversation_key": conversation_key,
            "received_at": received_at,
            "text": text,
            "attachments": normalized_attachments,
            "reply_capabilities": sorted(set(reply_capabilities)),
            "capability_profile": capability_profile,
        }

    def create_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        normalized = self.validate_job(payload)
        serialized = canonical_json(normalized)
        digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT * FROM jobs WHERE message_id = ?", (normalized["message_id"],)).fetchone()
            if existing is not None:
                if existing["input_digest"] != digest:
                    raise StoreError("idempotency_conflict", "同一 message_id 对应不同请求", status=409)
                return self._job_document(connection, existing)
            active_count = connection.execute(
                "SELECT COUNT(*) FROM jobs WHERE state IN ('queued','running','recovery_required')"
            ).fetchone()[0]
            if active_count >= self.max_queue:
                raise StoreError("queue_full", "任务队列已满", status=429)
            connection.execute(
                "INSERT OR IGNORE INTO conversations(conversation_key,state,created_at,updated_at) VALUES (?, 'active', ?, ?)",
                (normalized["conversation_key"], now, now),
            )
            job_id = str(uuid.uuid4())
            connection.execute(
                "INSERT INTO jobs(job_id,message_id,conversation_key,state,input_digest,input_json,capability_profile,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    job_id,
                    normalized["message_id"],
                    normalized["conversation_key"],
                    "queued",
                    digest,
                    serialized,
                    normalized["capability_profile"],
                    now,
                ),
            )
            row = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            self._event(connection, job_id, "queued")
            return self._job_document(connection, row)

    @staticmethod
    def _prepare_car_document(row: sqlite3.Row, *, idempotent_replay: bool = False) -> dict[str, Any]:
        result = None
        if row["result_json"]:
            try:
                result = json.loads(row["result_json"])
            except json.JSONDecodeError:
                result = None
        return {
            "confirmation_id": row["confirmation_id"],
            "status": "pending_confirmation" if row["state"] == "pending" else row["state"],
            "target": bool(row["target_on"]),
            "requested_at": row["requested_at"],
            "expires_at": row["expires_at"],
            "error_code": row["error_code"],
            "result": result,
            "idempotent_replay": idempotent_replay,
        }

    def prepare_car_request(
        self,
        conversation_key: str,
        message_id: str,
        target_on: bool,
        *,
        ttl_seconds: int,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if not CONVERSATION_RE.fullmatch(conversation_key) or not isinstance(message_id, str) or not message_id:
            raise StoreError("prepare_car_context_invalid", "备车确认上下文无效")
        if not isinstance(target_on, bool) or not 30 <= ttl_seconds <= 300:
            raise StoreError("prepare_car_request_invalid", "备车确认参数无效")
        current = (now or shanghai_now()).astimezone(SHANGHAI)
        requested_at = current.isoformat(timespec="seconds")
        expires_at = (current + timedelta(seconds=ttl_seconds)).isoformat(timespec="seconds")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM aito_prepare_car_confirmations WHERE request_message_id=? OR execute_message_id=?",
                (message_id, message_id),
            ).fetchone()
            if existing is not None:
                if existing["request_message_id"] != message_id or existing["conversation_key"] != conversation_key or bool(existing["target_on"]) is not target_on:
                    raise StoreError("prepare_car_idempotency_conflict", "备车消息幂等冲突", status=409)
                return self._prepare_car_document(existing, idempotent_replay=True)
            connection.execute(
                "UPDATE aito_prepare_car_confirmations SET state='cancelled',completed_at=?,error_code='SUPERSEDED' "
                "WHERE conversation_key=? AND state='pending'",
                (requested_at, conversation_key),
            )
            confirmation_id = f"PC-{uuid.uuid4().hex}"
            connection.execute(
                "INSERT INTO aito_prepare_car_confirmations(confirmation_id,conversation_key,request_message_id,target_on,state,requested_at,expires_at) "
                "VALUES (?,?,?,?, 'pending', ?, ?)",
                (confirmation_id, conversation_key, message_id, int(target_on), requested_at, expires_at),
            )
            row = connection.execute(
                "SELECT * FROM aito_prepare_car_confirmations WHERE confirmation_id=?",
                (confirmation_id,),
            ).fetchone()
            return self._prepare_car_document(row)

    def cancel_prepare_car_pending(
        self,
        conversation_key: str,
        next_message_id: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        current = (now or shanghai_now()).astimezone(SHANGHAI).isoformat(timespec="seconds")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT confirmation_id,request_message_id FROM aito_prepare_car_confirmations "
                "WHERE conversation_key=? AND state='pending'",
                (conversation_key,),
            ).fetchone()
            if row is None or row["request_message_id"] == next_message_id:
                return False
            connection.execute(
                "UPDATE aito_prepare_car_confirmations SET state='cancelled',completed_at=?,error_code='NEXT_MESSAGE_CANCELLED' "
                "WHERE confirmation_id=? AND state='pending'",
                (current, row["confirmation_id"]),
            )
            return connection.total_changes == 1

    def claim_prepare_car_execute(
        self,
        conversation_key: str,
        message_id: str,
        target_on: bool,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if not CONVERSATION_RE.fullmatch(conversation_key) or not isinstance(message_id, str) or not message_id:
            raise StoreError("prepare_car_context_invalid", "备车执行上下文无效")
        if not isinstance(target_on, bool):
            raise StoreError("prepare_car_request_invalid", "备车执行参数无效")
        current_dt = (now or shanghai_now()).astimezone(SHANGHAI)
        current = current_dt.isoformat(timespec="seconds")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            replay = connection.execute(
                "SELECT * FROM aito_prepare_car_confirmations WHERE execute_message_id=?",
                (message_id,),
            ).fetchone()
            if replay is not None:
                if replay["conversation_key"] != conversation_key or bool(replay["target_on"]) is not target_on:
                    raise StoreError("prepare_car_idempotency_conflict", "备车确认消息幂等冲突", status=409)
                document = self._prepare_car_document(replay, idempotent_replay=True)
                if document["result"] is not None:
                    result = dict(document["result"])
                    result["idempotent_replay"] = True
                    return result
                return document
            request_reuse = connection.execute(
                "SELECT 1 FROM aito_prepare_car_confirmations WHERE request_message_id=?",
                (message_id,),
            ).fetchone()
            if request_reuse is not None:
                raise StoreError("prepare_car_idempotency_conflict", "请求消息不能作为确认消息重用", status=409)
            row = connection.execute(
                "SELECT * FROM aito_prepare_car_confirmations WHERE conversation_key=? AND state='pending'",
                (conversation_key,),
            ).fetchone()
            if row is None:
                raise StoreError("CONFIRMATION_MISSING", "没有待确认的备车请求", status=409)
            try:
                expires = datetime.fromisoformat(row["expires_at"])
            except ValueError as exc:
                raise StoreError("prepare_car_store_invalid", "备车确认记录无效", status=500) from exc
            if expires.tzinfo is None or expires.astimezone(SHANGHAI) <= current_dt:
                connection.execute(
                    "UPDATE aito_prepare_car_confirmations SET state='expired',completed_at=?,error_code='CONFIRMATION_EXPIRED' "
                    "WHERE confirmation_id=? AND state='pending'",
                    (current, row["confirmation_id"]),
                )
                connection.commit()
                raise StoreError("CONFIRMATION_EXPIRED", "备车确认已过期", status=409)
            if bool(row["target_on"]) is not target_on:
                connection.execute(
                    "UPDATE aito_prepare_car_confirmations SET state='cancelled',completed_at=?,error_code='CONFIRMATION_ACTION_MISMATCH' "
                    "WHERE confirmation_id=? AND state='pending'",
                    (current, row["confirmation_id"]),
                )
                connection.commit()
                raise StoreError("CONFIRMATION_ACTION_MISMATCH", "备车确认动作不一致", status=409)
            connection.execute(
                "UPDATE aito_prepare_car_confirmations SET state='executing',execute_message_id=?,consumed_at=? "
                "WHERE confirmation_id=? AND state='pending'",
                (message_id, current, row["confirmation_id"]),
            )
            return {
                "confirmation_id": row["confirmation_id"],
                "status": "executing",
                "target": target_on,
                "idempotent_replay": False,
            }

    def finish_prepare_car_execute(
        self,
        confirmation_id: str,
        result: dict[str, Any],
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        status = result.get("status")
        state_by_status = {
            "submitted": "submitted",
            "already_confirmed": "submitted",
            "failed": "failed",
            "unknown": "unknown",
        }
        state = state_by_status.get(status)
        if state is None:
            raise StoreError("prepare_car_result_invalid", "备车执行结果无效", status=500)
        completed_at = (now or shanghai_now()).astimezone(SHANGHAI).isoformat(timespec="seconds")
        serialized = canonical_json(result)
        error_code = result.get("error_code")
        if error_code is not None and (not isinstance(error_code, str) or len(error_code) > 128):
            raise StoreError("prepare_car_result_invalid", "备车执行错误码无效", status=500)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state,result_json FROM aito_prepare_car_confirmations WHERE confirmation_id=?",
                (confirmation_id,),
            ).fetchone()
            if row is None:
                raise StoreError("prepare_car_confirmation_missing", "备车确认记录不存在", status=404)
            if row["state"] != "executing":
                if row["result_json"]:
                    try:
                        replay = json.loads(row["result_json"])
                    except json.JSONDecodeError as exc:
                        raise StoreError("prepare_car_store_invalid", "备车结果记录无效", status=500) from exc
                    replay["idempotent_replay"] = True
                    return replay
                raise StoreError("prepare_car_state_conflict", "备车确认状态冲突", status=409)
            connection.execute(
                "UPDATE aito_prepare_car_confirmations SET state=?,completed_at=?,result_json=?,error_code=? "
                "WHERE confirmation_id=? AND state='executing'",
                (state, completed_at, serialized, error_code, confirmation_id),
            )
            return dict(result)

    def get_job(self, job_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            if row is None:
                raise StoreError("job_not_found", "作业不存在", status=404)
            return self._job_document(connection, row)

    def claim_next(self) -> dict[str, Any] | None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "SELECT 1 FROM jobs WHERE state IN ('running','recovery_required') LIMIT 1"
            ).fetchone():
                return None
            row = connection.execute("SELECT * FROM jobs WHERE state = 'queued' ORDER BY created_at, job_id LIMIT 1").fetchone()
            if row is None:
                return None
            retry_not_before = row["retry_not_before"]
            if isinstance(retry_not_before, str) and retry_not_before > shanghai_now_iso():
                return None
            now = utc_now()
            connection.execute(
                "UPDATE jobs SET state='running',started_at=?,finished_at=NULL,attempt=attempt+1,"
                "error_code=NULL,error_type=NULL,upstream_http_status=NULL,retryable=0,"
                "output_observed=0,tool_activity_observed=0,artifact_observed=0,retry_not_before=NULL "
                "WHERE job_id=? AND state='queued'",
                (now, row["job_id"]),
            )
            self._event(connection, row["job_id"], "dispatching")
            claimed = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (row["job_id"],)).fetchone()
            return self._job_document(connection, claimed, include_input=True)

    def assign_thread(self, job_id: str, thread_id: str) -> None:
        if not thread_id:
            raise StoreError("thread_unavailable", "Thread ID 为空", status=502)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT conversation_key,state FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None or row["state"] != "running":
                raise StoreError("job_state_conflict", "作业不在运行状态", status=409)
            now = utc_now()
            connection.execute(
                "UPDATE conversations SET thread_id=?,updated_at=? WHERE conversation_key=?",
                (thread_id, now, row["conversation_key"]),
            )
            connection.execute("UPDATE jobs SET thread_id=? WHERE job_id=?", (thread_id, job_id))

    def complete_new_thread(self, job_id: str, thread_id: str, result_text: str) -> dict[str, Any]:
        """Atomically replace the conversation Thread and complete its control job."""
        if not thread_id:
            raise StoreError("thread_unavailable", "Thread ID 为空", status=502)
        bounded = result_text[: self.max_result_chars]
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT conversation_key,state FROM jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            if row is None or row["state"] != "running":
                raise StoreError("job_state_conflict", "作业不在运行状态", status=409)
            now = utc_now()
            connection.execute(
                "UPDATE conversations SET thread_id=?,updated_at=? WHERE conversation_key=?",
                (thread_id, now, row["conversation_key"]),
            )
            connection.execute(
                "UPDATE jobs SET thread_id=?,state='completed',result_text=?,error_code=NULL,finished_at=? "
                "WHERE job_id=? AND state='running'",
                (thread_id, bounded, now, job_id),
            )
            self._event(
                connection,
                job_id,
                "new_thread_started",
                item_type="controllerControl",
                content_length=len(result_text),
            )
            completed = connection.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            return self._job_document(connection, completed)

    def complete_direct_result(
        self,
        job_id: str,
        result_text: str,
        *,
        item_type: str,
    ) -> dict[str, Any]:
        """Complete one deterministic Controller action without creating an app-server Turn."""

        bounded = result_text[: self.max_result_chars]
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                "UPDATE jobs SET state='completed',result_text=?,error_code=NULL,finished_at=? "
                "WHERE job_id=? AND state='running'",
                (bounded, utc_now(), job_id),
            ).rowcount
            if updated != 1:
                raise StoreError("job_state_conflict", "作业不在运行状态", status=409)
            self._event(
                connection,
                job_id,
                "completed",
                item_type=item_type,
                content_length=len(result_text),
            )
            completed = connection.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            return self._job_document(connection, completed)

    def conversation_thread(self, conversation_key: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT thread_id FROM conversations WHERE conversation_key=? AND state='active'",
                (conversation_key,),
            ).fetchone()
            return None if row is None else row["thread_id"]

    def turn_attempt(self, turn_id: str) -> int | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT attempt FROM jobs WHERE turn_id=? AND state='running'",
                (turn_id,),
            ).fetchone()
            return None if row is None else int(row["attempt"])

    def assign_turn(self, job_id: str, turn_id: str) -> None:
        if not turn_id:
            raise StoreError("turn_state_unknown", "Turn ID 为空", status=502)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                "UPDATE jobs SET turn_id=? WHERE job_id=? AND state='running'",
                (turn_id, job_id),
            ).rowcount
            if updated != 1:
                raise StoreError("job_state_conflict", "无法绑定 Turn", status=409)
            self._event(connection, job_id, "turn_started")

    def set_result_text(self, turn_id: str, text: str, *, item_type: str = "agentMessage") -> bool:
        bounded = text[: self.max_result_chars]
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT job_id FROM jobs WHERE turn_id=? AND state='running'", (turn_id,)).fetchone()
            if row is None:
                return False
            connection.execute(
                "UPDATE jobs SET result_text=?,output_observed=1 WHERE job_id=?",
                (bounded, row["job_id"]),
            )
            self._event(connection, row["job_id"], "item_completed", item_type=item_type, content_length=len(text))
            return True

    def observe_turn_activity(
        self,
        turn_id: str,
        *,
        output_observed: bool = False,
        tool_activity_observed: bool = False,
        artifact_observed: bool = False,
        item_type: str | None = None,
    ) -> bool:
        safe_item_type = item_type if isinstance(item_type, str) and re.fullmatch(r"[A-Za-z0-9_]{1,64}", item_type) else None
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT job_id,output_observed,tool_activity_observed,artifact_observed "
                "FROM jobs WHERE turn_id=? AND state='running'",
                (turn_id,),
            ).fetchone()
            if row is None:
                return False
            next_output = bool(row["output_observed"]) or bool(output_observed)
            next_tool = bool(row["tool_activity_observed"]) or bool(tool_activity_observed)
            next_artifact = bool(row["artifact_observed"]) or bool(artifact_observed)
            changed = (
                next_output != bool(row["output_observed"])
                or next_tool != bool(row["tool_activity_observed"])
                or next_artifact != bool(row["artifact_observed"])
            )
            connection.execute(
                "UPDATE jobs SET output_observed=?,tool_activity_observed=?,artifact_observed=? WHERE job_id=?",
                (int(next_output), int(next_tool), int(next_artifact), row["job_id"]),
            )
            if changed:
                self._event(connection, row["job_id"], "turn_activity_observed", item_type=safe_item_type)
            return True

    def observe_turn_error(
        self,
        turn_id: str,
        *,
        error_type: str,
        error_code: str,
        upstream_http_status: int | None,
        retryable: bool,
        will_retry: bool,
    ) -> bool:
        safe_type = error_type if re.fullmatch(r"[a-z0-9_]{1,64}", error_type) else "unknown"
        safe_code = error_code if re.fullmatch(r"[a-z0-9_]{1,64}", error_code) else "turn_failed"
        safe_status = (
            upstream_http_status
            if isinstance(upstream_http_status, int)
            and not isinstance(upstream_http_status, bool)
            and 100 <= upstream_http_status <= 599
            else None
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT job_id FROM jobs WHERE turn_id=? AND state='running'",
                (turn_id,),
            ).fetchone()
            if row is None:
                return False
            connection.execute(
                "UPDATE jobs SET error_type=?,error_code=?,upstream_http_status=?,retryable=? WHERE job_id=?",
                (safe_type, safe_code, safe_status, int(bool(retryable)), row["job_id"]),
            )
            self._event(
                connection,
                row["job_id"],
                "turn_error_retrying" if will_retry else "turn_error_terminal",
                item_type=safe_type,
                error_code=safe_code,
            )
            return True

    def complete_turn(
        self,
        turn_id: str,
        turn_status: str,
        *,
        error_code: str | None = None,
        error_type: str | None = None,
        upstream_http_status: int | None = None,
        retryable: bool | None = None,
        retry_delay_seconds: float | None = None,
        max_attempts: int = 3,
        output_observed: bool = False,
        tool_activity_observed: bool = False,
        artifact_observed: bool = False,
    ) -> bool:
        state = "completed" if turn_status == "completed" else "cancelled" if turn_status == "interrupted" else "failed"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM jobs WHERE turn_id=? AND state='running'", (turn_id,)).fetchone()
            if row is None:
                return False
            safe_type = (
                error_type
                if isinstance(error_type, str) and re.fullmatch(r"[a-z0-9_]{1,64}", error_type)
                else row["error_type"] or "unknown"
            )
            safe_code = (
                error_code
                if isinstance(error_code, str) and re.fullmatch(r"[a-z0-9_]{1,64}", error_code)
                else row["error_code"] or ("turn_failed" if state == "failed" else None)
            )
            safe_status = (
                upstream_http_status
                if isinstance(upstream_http_status, int)
                and not isinstance(upstream_http_status, bool)
                and 100 <= upstream_http_status <= 599
                else row["upstream_http_status"]
            )
            effective_retryable = bool(row["retryable"]) if retryable is None else bool(retryable)
            effective_output = bool(row["output_observed"]) or bool(output_observed)
            effective_tool = bool(row["tool_activity_observed"]) or bool(tool_activity_observed)
            effective_artifact = bool(row["artifact_observed"]) or bool(artifact_observed)
            has_artifact = connection.execute(
                "SELECT 1 FROM job_artifacts WHERE job_id=? LIMIT 1",
                (row["job_id"],),
            ).fetchone() is not None
            can_retry = bool(
                state == "failed"
                and retry_delay_seconds is not None
                and effective_retryable
                and row["attempt"] < max(1, int(max_attempts))
                and not effective_output
                and not effective_tool
                and not effective_artifact
                and not has_artifact
            )
            if can_retry:
                connection.execute(
                    "UPDATE jobs SET state='queued',turn_id=NULL,started_at=NULL,finished_at=NULL,"
                    "result_text=NULL,result_summary=NULL,error_code=?,error_type=?,upstream_http_status=?,"
                    "retryable=1,output_observed=0,tool_activity_observed=0,artifact_observed=0,"
                    "retry_not_before=? WHERE job_id=?",
                    (
                        safe_code,
                        safe_type,
                        safe_status,
                        shanghai_after(float(retry_delay_seconds)),
                        row["job_id"],
                    ),
                )
                self._event(
                    connection,
                    row["job_id"],
                    "requeued",
                    item_type=safe_type,
                    error_code=safe_code,
                )
                return True
            connection.execute(
                "UPDATE jobs SET state=?,error_code=?,error_type=?,upstream_http_status=?,retryable=?,"
                "output_observed=?,tool_activity_observed=?,artifact_observed=?,retry_not_before=NULL,finished_at=? "
                "WHERE job_id=?",
                (
                    state,
                    safe_code if state == "failed" else error_code,
                    safe_type if state == "failed" else None,
                    safe_status if state == "failed" else None,
                    int(effective_retryable) if state == "failed" else 0,
                    int(effective_output),
                    int(effective_tool),
                    int(effective_artifact or has_artifact),
                    utc_now(),
                    row["job_id"],
                ),
            )
            self._event(connection, row["job_id"], state, item_type=safe_type if state == "failed" else None, error_code=safe_code if state == "failed" else error_code)
            return True

    def capture_chart_artifact(
        self,
        job_id: str,
        chart: dict[str, Any],
        content: bytes,
    ) -> dict[str, Any]:
        """Validate and atomically persist one trusted Hub PNG for a running job."""
        if not isinstance(job_id, str) or not job_id:
            raise StoreError("artifact_context_invalid", "artifact 作业上下文无效", status=409)
        if not isinstance(chart, dict):
            raise StoreError("artifact_metadata_invalid", "图表元数据无效", status=502)
        download_ref = chart.get("download_ref")
        size_bytes = chart.get("size_bytes")
        expected_digest = chart.get("sha256")
        width = chart.get("width")
        height = chart.get("height")
        summary = chart.get("summary")
        if not isinstance(download_ref, str) or not re.fullmatch(r"summary-[a-f0-9]{32}\.png", download_ref):
            raise StoreError("artifact_reference_invalid", "图表引用无效", status=502)
        if not isinstance(content, bytes) or not content.startswith(PNG_MAGIC):
            raise StoreError("artifact_content_invalid", "图表不是有效 PNG", status=502)
        if not isinstance(size_bytes, int) or size_bytes != len(content) or not 1 <= size_bytes <= self.max_artifact_bytes:
            raise StoreError("artifact_size_invalid", "图表大小不一致或越界", status=502)
        if not isinstance(expected_digest, str):
            raise StoreError("artifact_digest_invalid", "图表摘要缺失", status=502)
        expected_digest = expected_digest.removeprefix("sha256:")
        actual_digest = hashlib.sha256(content).hexdigest()
        if not re.fullmatch(r"[a-f0-9]{64}", expected_digest) or not hmac.compare_digest(expected_digest, actual_digest):
            raise StoreError("artifact_digest_invalid", "图表摘要不一致", status=502)
        if (
            isinstance(width, bool)
            or not isinstance(width, int)
            or isinstance(height, bool)
            or not isinstance(height, int)
            or not 1 <= width <= 8192
            or not 1 <= height <= 8192
        ):
            raise StoreError("artifact_dimensions_invalid", "图表尺寸无效", status=502)
        if not isinstance(summary, dict):
            raise StoreError("artifact_summary_invalid", "图表汇总无效", status=502)
        summary_json = canonical_json(summary)
        if len(summary_json.encode("utf-8")) > 1024 * 1024:
            raise StoreError("artifact_summary_invalid", "图表汇总过大", status=502)
        result_summary = self._chart_result_summary(summary)
        self.cleanup_artifacts()
        now = utc_now()
        expires_at = (datetime.now(timezone.utc) + timedelta(seconds=self.artifact_ttl_seconds)).isoformat()
        artifact_id = self._new_artifact_id()
        storage_name = f"{uuid.uuid4().hex}.png"
        token = self._download_token(artifact_id, expires_at)
        token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
        descriptor, temporary_name = tempfile.mkstemp(prefix=".artifact.", dir=self.artifact_dir)
        temporary = Path(temporary_name)
        target = self.artifact_dir / storage_name
        target_created = False
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                job = connection.execute("SELECT state FROM jobs WHERE job_id=?", (job_id,)).fetchone()
                if job is None or job["state"] != "running":
                    raise StoreError("artifact_context_invalid", "图表只能绑定到运行中的作业", status=409)
                existing = connection.execute(
                    "SELECT * FROM job_artifacts WHERE job_id=? AND sha256=?",
                    (job_id, actual_digest),
                ).fetchone()
                if existing is not None:
                    connection.execute(
                        "UPDATE jobs SET result_summary=?,artifact_observed=1 WHERE job_id=?",
                        (result_summary, job_id),
                    )
                    return self._artifact_public_document(connection, existing)
                artifact_count = connection.execute(
                    "SELECT COUNT(*) FROM job_artifacts WHERE job_id=?",
                    (job_id,),
                ).fetchone()[0]
                if artifact_count >= self.max_artifacts_per_job:
                    raise StoreError("artifact_job_limit", "单个作业的 artifact 数量超过上限", status=409)
                used_bytes = connection.execute(
                    "SELECT COALESCE(SUM(size_bytes),0) FROM job_artifacts WHERE expires_at>?",
                    (now,),
                ).fetchone()[0]
                if used_bytes + size_bytes > self.artifact_quota_bytes:
                    raise StoreError("artifact_quota_exceeded", "Controller artifact 配额不足", status=507)
                os.replace(temporary, target)
                target_created = True
                connection.execute(
                    "INSERT INTO job_artifacts(artifact_id,job_id,artifact_type,mime_type,storage_name,size_bytes,sha256,width,height,summary_json,download_token_hash,expires_at,created_at) "
                    "VALUES (?,?, 'image','image/png',?,?,?,?,?,?,?,?,?)",
                    (
                        artifact_id,
                        job_id,
                        storage_name,
                        size_bytes,
                        actual_digest,
                        width,
                        height,
                        summary_json,
                        token_hash,
                        expires_at,
                        now,
                    ),
                )
                connection.execute(
                    "UPDATE jobs SET result_summary=?,artifact_observed=1 WHERE job_id=?",
                    (result_summary, job_id),
                )
                row = connection.execute("SELECT * FROM job_artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()
                self._event(connection, job_id, "artifact_captured", item_type="image", content_length=size_bytes)
                return self._artifact_public_document(connection, row)
        except Exception:
            if target_created and target.is_file() and not target.is_symlink():
                target.unlink()
            raise
        finally:
            if temporary.exists():
                temporary.unlink()

    def read_job_artifact(self, job_id: str, artifact_id: str) -> tuple[dict[str, Any], bytes]:
        if not isinstance(job_id, str) or not re.fullmatch(r"[0-9a-f-]{36}", job_id):
            raise StoreError("artifact_not_found", "artifact 不存在", status=404)
        if not isinstance(artifact_id, str) or not ARTIFACT_ID_RE.fullmatch(artifact_id):
            raise StoreError("artifact_not_found", "artifact 不存在", status=404)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM job_artifacts WHERE job_id=? AND artifact_id=? AND expires_at>?",
                (job_id, artifact_id, utc_now()),
            ).fetchone()
            if row is None:
                raise StoreError("artifact_not_found", "artifact 不存在或已过期", status=404)
            return self._read_artifact_row(connection, row)

    def read_download_artifact(self, token: str) -> tuple[dict[str, Any], bytes]:
        if not isinstance(token, str) or not DOWNLOAD_TOKEN_RE.fullmatch(token):
            raise StoreError("artifact_not_found", "下载链接无效", status=404)
        token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM job_artifacts WHERE download_token_hash=? AND expires_at>?",
                (token_hash, utc_now()),
            ).fetchone()
            if row is None:
                raise StoreError("artifact_not_found", "下载链接不存在或已过期", status=404)
            return self._read_artifact_row(connection, row)

    def cleanup_artifacts(self) -> int:
        """Remove expired records/files and untracked regular files inside the private directory."""
        removed = 0
        tracked: set[str] = set()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            expired = connection.execute(
                "SELECT artifact_id,storage_name FROM job_artifacts WHERE expires_at<=?",
                (utc_now(),),
            ).fetchall()
            for row in expired:
                connection.execute("DELETE FROM job_artifacts WHERE artifact_id=?", (row["artifact_id"],))
                path = self.artifact_dir / row["storage_name"]
                if path.is_file() and not path.is_symlink():
                    path.unlink()
                    removed += 1
            tracked = {
                row["storage_name"]
                for row in connection.execute("SELECT storage_name FROM job_artifacts")
            }
        for path in self.artifact_dir.iterdir():
            if path.name not in tracked and path.is_file() and not path.is_symlink():
                path.unlink()
                removed += 1
        return removed

    def fail_claimed(self, job_id: str, error_code: str, *, uncertain: bool) -> None:
        state = "recovery_required" if uncertain else "failed"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE jobs SET state=?,error_code=?,finished_at=? WHERE job_id=? AND state='running'",
                (state, error_code, utc_now(), job_id),
            )
            self._event(connection, job_id, state, error_code=error_code)

    def retry_overloaded(
        self,
        job_id: str,
        *,
        max_attempts: int = 3,
        retry_delay_seconds: float = 0.0,
    ) -> bool:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT attempt FROM jobs WHERE job_id=? AND state='running'", (job_id,)).fetchone()
            if row is None or row["attempt"] >= max_attempts:
                return False
            connection.execute(
                "UPDATE jobs SET state='queued',turn_id=NULL,started_at=NULL,finished_at=NULL,"
                "error_code='app_server_overloaded',error_type='server_overloaded',retryable=1,"
                "retry_not_before=? WHERE job_id=?",
                (shanghai_after(retry_delay_seconds), job_id),
            )
            self._event(
                connection,
                job_id,
                "requeued",
                item_type="server_overloaded",
                error_code="app_server_overloaded",
            )
            return True

    def recover_running(self) -> int:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute("SELECT job_id FROM jobs WHERE state='running'").fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE jobs SET state='recovery_required',error_code='turn_state_unknown',finished_at=? WHERE job_id=?",
                    (utc_now(), row["job_id"]),
                )
                self._event(connection, row["job_id"], "recovery_required", error_code="turn_state_unknown")
            return len(rows)

    def resolve_recovery(self, job_id: str, resolution: str) -> dict[str, Any]:
        target = RECOVERY_RESOLUTIONS.get(resolution)
        if target is None:
            raise StoreError("invalid_recovery_resolution", "恢复核对结论无效")
        state, error_code = target
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT state FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                raise StoreError("job_not_found", "作业不存在", status=404)
            if row["state"] != "recovery_required":
                raise StoreError("job_state_conflict", "作业不在恢复核对状态", status=409)
            connection.execute(
                "UPDATE jobs SET state=?,error_code=?,finished_at=? WHERE job_id=? AND state='recovery_required'",
                (state, error_code, utc_now(), job_id),
            )
            self._event(
                connection,
                job_id,
                "recovery_resolved",
                item_type=resolution,
                error_code=error_code,
            )
            resolved = connection.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            return self._job_document(connection, resolved)

    def cancel_queued(self, job_id: str) -> bool:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                "UPDATE jobs SET state='cancelled',finished_at=? WHERE job_id=? AND state='queued'",
                (utc_now(), job_id),
            ).rowcount
            if updated:
                self._event(connection, job_id, "cancelled")
            return bool(updated)

    def status(self) -> dict[str, Any]:
        with self._connect() as connection:
            counts = {row["state"]: row["count"] for row in connection.execute("SELECT state,COUNT(*) AS count FROM jobs GROUP BY state")}
            active = connection.execute(
                "SELECT job_id,conversation_key,thread_id,turn_id,state,started_at,capability_profile FROM jobs WHERE state='running' LIMIT 1"
            ).fetchone()
            return {
                "jobs": {state: counts.get(state, 0) for state in (*ACTIVE_STATES, *FINAL_STATES)},
                "threads": connection.execute("SELECT COUNT(*) FROM conversations WHERE thread_id IS NOT NULL").fetchone()[0],
                "active_job": None
                if active is None
                else {
                    "job_short": self.short_id("JB", active["job_id"], connection=connection),
                    "conversation_short": self.short_id("CV", active["conversation_key"], connection=connection),
                    "thread_short": self.short_id("TH", active["thread_id"], connection=connection),
                    "turn_short": self.short_id("TN", active["turn_id"], connection=connection),
                    "state": active["state"],
                    "started_at": active["started_at"],
                },
            }

    def short_id(
        self,
        prefix: str,
        value: str | None,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> str | None:
        if not value:
            return None
        if not re.fullmatch(r"[A-Z]{2}", prefix):
            raise ValueError("短标识前缀无效")
        owns_connection = connection is None
        active = self._connect() if connection is None else connection
        try:
            row = active.execute("SELECT display_secret FROM controller_meta WHERE id=1").fetchone()
            if row is None or not isinstance(row["display_secret"], bytes) or len(row["display_secret"]) != 32:
                raise StoreError("display_secret_invalid", "Controller 短标识密钥不可用", status=503)
            digest = hmac.new(row["display_secret"], f"{prefix}\n{value}".encode("utf-8"), hashlib.sha256).digest()
            token = base64.b32encode(digest).decode("ascii").rstrip("=")[:10]
            return f"{prefix}-{token}"
        finally:
            if owns_connection:
                active.close()

    def public_job(self, document: dict[str, Any]) -> dict[str, Any]:
        completed = document.get("state") == "completed"
        return {
            "job_id": document["job_id"],
            "job_short": self.short_id("JB", document.get("job_id")),
            "conversation_short": self.short_id("CV", document.get("conversation_key")),
            "thread_short": self.short_id("TH", document.get("thread_id")),
            "turn_short": self.short_id("TN", document.get("turn_id")),
            "state": document["state"],
            "queue_position": document.get("queue_position"),
            "result": document.get("result"),
            "result_summary": document.get("result_summary") if completed else None,
            "artifacts": self._public_artifacts(str(document.get("job_id") or "")) if completed else [],
            "error_code": document.get("error_code"),
            "attempt": document.get("attempt"),
            "created_at": document.get("created_at"),
            "started_at": document.get("started_at"),
            "finished_at": document.get("finished_at"),
        }

    def get_public_job(self, job_id: str) -> dict[str, Any]:
        return self.public_job(self.get_job(job_id))

    @staticmethod
    def _manifest_names(document: Any) -> frozenset[str]:
        if not isinstance(document, dict) or not isinstance(document.get("tools"), list):
            raise StoreError("hub_manifest_invalid", "Hub last-good manifest 损坏", status=503)
        names: set[str] = set()
        for tool in document["tools"]:
            name = tool.get("name") if isinstance(tool, dict) else None
            if not isinstance(name, str) or not re.fullmatch(r"(?:ledger|renovation)_[a-z0-9_]{1,79}", name):
                raise StoreError("hub_manifest_invalid", "Hub last-good manifest 工具名损坏", status=503)
            if name in names:
                raise StoreError("hub_manifest_invalid", "Hub last-good manifest 工具重复", status=503)
            names.add(name)
        if not names:
            raise StoreError("hub_manifest_invalid", "Hub last-good manifest 工具为空", status=503)
        return frozenset(names)

    def load_hub_manifest_document(self) -> dict[str, Any] | None:
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT document_json FROM hub_manifest_state WHERE id=1"
                ).fetchone()
            if row is None or row["document_json"] is None:
                return None
            document = json.loads(row["document_json"])
            self._manifest_names(document)
            return document
        except json.JSONDecodeError as exc:
            raise StoreError("hub_manifest_invalid", "Hub last-good manifest JSON 损坏", status=503) from exc
        except sqlite3.DatabaseError as exc:
            raise StoreError("hub_manifest_invalid", "Hub last-good manifest 读取失败", status=503) from exc

    def active_tool_names(self) -> frozenset[str]:
        document = self.load_hub_manifest_document()
        hub_names = (
            frozenset(definition.name for definition in BOOTSTRAP_HUB_DEFINITIONS)
            if document is None
            else self._manifest_names(document)
        )
        return (
            hub_names
            | frozenset(definition.name for definition in MEMO_DEFINITIONS)
            | frozenset(definition.name for definition in AITO_PREPARE_CAR_DEFINITIONS)
            | frozenset(definition.name for definition in OPERATION_DEFINITIONS)
        )

    def historical_tool_names(self) -> frozenset[str]:
        try:
            with self._connect() as connection:
                rows = connection.execute("SELECT tool_name FROM hub_tool_history").fetchall()
        except sqlite3.DatabaseError as exc:
            raise StoreError("hub_manifest_invalid", "Hub 工具历史读取失败", status=503) from exc
        hub_names = {row["tool_name"] for row in rows if isinstance(row["tool_name"], str)}
        return (
            frozenset(hub_names)
            | frozenset(definition.name for definition in MEMO_DEFINITIONS)
            | frozenset(definition.name for definition in AITO_PREPARE_CAR_DEFINITIONS)
            | frozenset(definition.name for definition in OPERATION_DEFINITIONS)
        )

    def apply_hub_manifest(self, document: dict[str, Any]) -> dict[str, Any]:
        names = self._manifest_names(document)
        digest = document.get("catalog_digest")
        hub_revision = document.get("catalog_revision")
        if not isinstance(digest, str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
            raise StoreError("hub_manifest_invalid", "Hub manifest digest 无效", status=503)
        if not isinstance(hub_revision, int) or isinstance(hub_revision, bool) or hub_revision < 1:
            raise StoreError("hub_manifest_invalid", "Hub manifest revision 无效", status=503)
        serialized = canonical_json(document)
        now = utc_now()
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                current = connection.execute(
                    "SELECT catalog_digest FROM hub_manifest_state WHERE id=1"
                ).fetchone()
                current_digest = None if current is None else current["catalog_digest"]
                changed = current_digest != digest
                connection.execute(
                    "INSERT INTO hub_manifest_state(id,document_json,catalog_digest,hub_revision,synchronized_at,error_code,error_at) "
                    "VALUES (1,?,?,?,?,NULL,NULL) ON CONFLICT(id) DO UPDATE SET "
                    "document_json=excluded.document_json,catalog_digest=excluded.catalog_digest,"
                    "hub_revision=excluded.hub_revision,synchronized_at=excluded.synchronized_at,error_code=NULL,error_at=NULL",
                    (serialized, digest, hub_revision, now),
                )
                connection.execute("UPDATE hub_tool_history SET retired=1")
                by_name = {tool["name"]: tool for tool in document["tools"]}
                for name in sorted(names):
                    connection.execute(
                        "INSERT INTO hub_tool_history(tool_name,definition_json,retired,first_seen_at,last_seen_at) "
                        "VALUES (?,?,0,?,?) ON CONFLICT(tool_name) DO UPDATE SET "
                        "definition_json=excluded.definition_json,retired=0,last_seen_at=excluded.last_seen_at",
                        (name, canonical_json(by_name[name]), now, now),
                    )
                if changed:
                    connection.execute(
                        "UPDATE tool_policy_meta SET catalog_revision=catalog_revision+1,updated_at=? WHERE id=1",
                        (now,),
                    )
                revision_row = connection.execute(
                    "SELECT catalog_revision FROM tool_policy_meta WHERE id=1"
                ).fetchone()
                if revision_row is None or not isinstance(revision_row["catalog_revision"], int):
                    raise StoreError("tool_policy_invalid", "工具策略元数据损坏", status=503)
                return {
                    "changed": changed,
                    "revision": revision_row["catalog_revision"],
                    "hub_revision": hub_revision,
                    "catalog_digest": digest,
                }
        except sqlite3.DatabaseError as exc:
            raise StoreError("hub_manifest_invalid", "Hub manifest 保存失败", status=503) from exc

    def record_hub_manifest_error(self, error_code: str) -> None:
        bounded = error_code if isinstance(error_code, str) and re.fullmatch(r"[a-z0-9_]{1,64}", error_code) else "hub_manifest_sync_failed"
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "INSERT INTO hub_manifest_state(id,error_code,error_at) VALUES (1,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET error_code=excluded.error_code,error_at=excluded.error_at",
                    (bounded, utc_now()),
                )
        except sqlite3.DatabaseError as exc:
            raise StoreError("hub_manifest_invalid", "Hub manifest 错误状态保存失败", status=503) from exc

    def hub_manifest_status(self) -> dict[str, Any]:
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT document_json,catalog_digest,hub_revision,synchronized_at,error_code,error_at "
                    "FROM hub_manifest_state WHERE id=1"
                ).fetchone()
        except sqlite3.DatabaseError as exc:
            raise StoreError("hub_manifest_invalid", "Hub manifest 状态读取失败", status=503) from exc
        last_good = row is not None and row["document_json"] is not None
        return {
            "source": "last_good" if last_good else "bootstrap",
            "catalog_digest": row["catalog_digest"] if last_good else None,
            "hub_revision": row["hub_revision"] if last_good else None,
            "synchronized_at": row["synchronized_at"] if last_good else None,
            "error_code": None if row is None else row["error_code"],
            "error_at": None if row is None else row["error_at"],
        }

    def tool_policy_snapshot(self) -> dict[str, Any]:
        try:
            with self._connect() as connection:
                meta_rows = connection.execute(
                    "SELECT catalog_revision FROM tool_policy_meta WHERE id=1"
                ).fetchall()
                if len(meta_rows) != 1 or not isinstance(meta_rows[0]["catalog_revision"], int):
                    raise StoreError("tool_policy_invalid", "工具策略元数据损坏", status=503)
                revision = meta_rows[0]["catalog_revision"]
                if revision < 1:
                    raise StoreError("tool_policy_invalid", "工具策略版本损坏", status=503)
                active_names = set(self.active_tool_names())
                historical_names = set(self.historical_tool_names())
                enabled = set(active_names)
                rows = connection.execute(
                    "SELECT tool_name,enabled,revision FROM tool_policies ORDER BY tool_name"
                ).fetchall()
                for row in rows:
                    if row["tool_name"] not in historical_names:
                        raise StoreError("tool_policy_invalid", "工具策略包含未知工具", status=503)
                    if row["enabled"] not in (0, 1) or not isinstance(row["revision"], int):
                        raise StoreError("tool_policy_invalid", "工具策略值损坏", status=503)
                    if row["revision"] < 1 or row["revision"] > revision:
                        raise StoreError("tool_policy_invalid", "工具策略版本越界", status=503)
                    if row["enabled"] == 0:
                        enabled.discard(row["tool_name"])
                return {"revision": revision, "enabled": frozenset(enabled)}
        except sqlite3.DatabaseError as exc:
            raise StoreError("tool_policy_invalid", "工具策略读取失败", status=503) from exc

    def tool_catalog_revision(self) -> int:
        return int(self.tool_policy_snapshot()["revision"])

    def update_tool_policy(
        self,
        tool_name: str,
        *,
        enabled: bool,
        revision: int,
        request_id: str,
    ) -> dict[str, Any]:
        active_names = set(self.active_tool_names())
        historical_names = set(self.historical_tool_names())
        if tool_name not in active_names:
            raise StoreError("tool_not_found", "工具不存在", status=404)
        if not isinstance(enabled, bool):
            raise StoreError("invalid_tool_policy", "enabled 必须是布尔值")
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            raise StoreError("invalid_tool_policy", "revision 无效")
        if not isinstance(request_id, str) or not REQUEST_ID_RE.fullmatch(request_id):
            raise StoreError("invalid_request_id", "request_id 无效")
        request_document = {
            "scope": f"tool_policy:{tool_name}",
            "enabled": enabled,
            "revision": revision,
        }
        request_digest = hashlib.sha256(canonical_json(request_document).encode("utf-8")).hexdigest()
        now = utc_now()
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                replay = connection.execute(
                    "SELECT request_digest,response_json FROM admin_mutations WHERE request_id=?",
                    (request_id,),
                ).fetchone()
                if replay is not None:
                    if replay["request_digest"] != request_digest:
                        raise StoreError("idempotency_conflict", "request_id 已用于不同管理请求", status=409)
                    return json.loads(replay["response_json"])
                meta = connection.execute(
                    "SELECT catalog_revision FROM tool_policy_meta WHERE id=1"
                ).fetchone()
                if meta is None or not isinstance(meta["catalog_revision"], int) or meta["catalog_revision"] < 1:
                    raise StoreError("tool_policy_invalid", "工具策略元数据损坏", status=503)
                current_revision = meta["catalog_revision"]
                for row in connection.execute(
                    "SELECT tool_name,enabled,revision FROM tool_policies"
                ):
                    if row["tool_name"] not in historical_names:
                        raise StoreError("tool_policy_invalid", "工具策略包含未知工具", status=503)
                    if row["enabled"] not in (0, 1) or not isinstance(row["revision"], int):
                        raise StoreError("tool_policy_invalid", "工具策略值损坏", status=503)
                    if row["revision"] < 1 or row["revision"] > current_revision:
                        raise StoreError("tool_policy_invalid", "工具策略版本越界", status=503)
                if current_revision != revision:
                    raise StoreError("revision_conflict", "工具目录版本已变化，请刷新后重试", status=409)
                next_revision = current_revision + 1
                connection.execute(
                    "INSERT INTO tool_policies(tool_name,enabled,revision,updated_at) VALUES (?,?,?,?) "
                    "ON CONFLICT(tool_name) DO UPDATE SET enabled=excluded.enabled,revision=excluded.revision,updated_at=excluded.updated_at",
                    (tool_name, int(enabled), next_revision, now),
                )
                connection.execute(
                    "UPDATE tool_policy_meta SET catalog_revision=?,updated_at=? WHERE id=1",
                    (next_revision, now),
                )
                response = {
                    "tool_name": tool_name,
                    "enabled": enabled,
                    "revision": next_revision,
                    "request_id": request_id,
                }
                serialized = canonical_json(response)
                connection.execute(
                    "INSERT INTO admin_mutations(request_id,scope,request_digest,response_json,created_at) VALUES (?,?,?,?,?)",
                    (request_id, f"tool_policy:{tool_name}", request_digest, serialized, now),
                )
                return response
        except sqlite3.DatabaseError as exc:
            raise StoreError("tool_policy_invalid", "工具策略写入失败", status=503) from exc

    def record_mcp_catalog(self, revision: int | None, tools: list[str]) -> dict[str, Any]:
        if revision is not None and (not isinstance(revision, int) or isinstance(revision, bool) or revision < 1):
            raise StoreError("invalid_catalog_observation", "MCP 目录版本无效")
        active_names = set(self.active_tool_names())
        if not isinstance(tools, list) or any(name not in active_names for name in tools):
            raise StoreError("invalid_catalog_observation", "MCP 目录包含未知工具")
        normalized = sorted(set(tools)) if revision is not None else []
        error_code = None if revision is not None else "tool_policy_invalid"
        observed_at = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO mcp_catalog_observation(id,catalog_revision,published_tools_json,error_code,observed_at) "
                "VALUES (1,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "catalog_revision=excluded.catalog_revision,published_tools_json=excluded.published_tools_json,"
                "error_code=excluded.error_code,observed_at=excluded.observed_at",
                (revision, canonical_json(normalized), error_code, observed_at),
            )
        return {"revision": revision, "tools": normalized, "observed_at": observed_at, "error_code": error_code}

    def record_tool_invocation(
        self,
        tool_name: str,
        *,
        outcome: str,
        error_code: str | None,
        duration_ms: int,
        job_id: str | None = None,
        turn_id: str | None = None,
    ) -> None:
        try:
            known_names = set(self.historical_tool_names())
        except StoreError:
            return
        if tool_name not in known_names or outcome not in {"succeeded", "rejected", "failed"}:
            return
        bounded_error = None
        if isinstance(error_code, str) and re.fullmatch(r"[a-z0-9_]{1,64}", error_code):
            bounded_error = error_code
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO tool_invocations(job_id,turn_id,tool_name,outcome,error_code,duration_ms,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    job_id,
                    turn_id,
                    tool_name,
                    outcome,
                    bounded_error,
                    max(0, min(int(duration_ms), 86_400_000)),
                    utc_now(),
                ),
            )
            if isinstance(job_id, str) and job_id:
                connection.execute(
                    "UPDATE jobs SET tool_activity_observed=1 WHERE job_id=? AND state='running'",
                    (job_id,),
                )
            connection.execute(
                "DELETE FROM tool_invocations WHERE id NOT IN (SELECT id FROM tool_invocations ORDER BY id DESC LIMIT 1000)"
            )

    def tool_control_document(
        self,
        configured_names: set[str] | frozenset[str],
        callable_names: set[str] | frozenset[str] | None = None,
        definitions: tuple[ToolDefinition, ...] | list[ToolDefinition] | None = None,
    ) -> dict[str, Any]:
        active_definitions = tuple(TOOL_DEFINITIONS if definitions is None else definitions)
        definition_names = {definition.name for definition in active_definitions}
        configured = set(configured_names) & definition_names
        route_ready = configured if callable_names is None else set(callable_names) & configured
        policy_error: str | None = None
        try:
            snapshot = self.tool_policy_snapshot()
            revision = snapshot["revision"]
            enabled_names = set(snapshot["enabled"])
        except StoreError as exc:
            policy_error = exc.code
            revision = None
            enabled_names = set()
        with self._connect() as connection:
            observation = connection.execute(
                "SELECT catalog_revision,published_tools_json,error_code,observed_at FROM mcp_catalog_observation WHERE id=1"
            ).fetchone()
            latest_rows = connection.execute(
                "SELECT tool_name,outcome,error_code,duration_ms,created_at FROM tool_invocations ORDER BY id DESC"
            ).fetchall()
        latest: dict[str, dict[str, Any]] = {}
        for row in latest_rows:
            if row["tool_name"] not in latest:
                latest[row["tool_name"]] = {
                    "outcome": row["outcome"],
                    "error_code": row["error_code"],
                    "duration_ms": row["duration_ms"],
                    "created_at": row["created_at"],
                }
        published_names: set[str] = set()
        observed_revision = None
        observed_at = None
        observation_error = None
        if observation is not None:
            observed_revision = observation["catalog_revision"]
            observed_at = observation["observed_at"]
            observation_error = observation["error_code"]
            try:
                decoded = json.loads(observation["published_tools_json"])
                if isinstance(decoded, list) and all(name in definition_names for name in decoded):
                    published_names = set(decoded)
                else:
                    observation_error = "catalog_observation_invalid"
            except json.JSONDecodeError:
                observation_error = "catalog_observation_invalid"
        observation_current = revision is not None and observed_revision == revision and observation_error is None
        tools = []
        for definition in active_definitions:
            is_configured = definition.name in configured
            is_enabled = policy_error is None and definition.name in enabled_names
            is_published = observation_current and definition.name in published_names
            tools.append(
                {
                    **definition.public_metadata(),
                    "known": True,
                    "configured": is_configured,
                    "enabled": is_enabled,
                    "mcp_published": is_published,
                    "callable": definition.name in route_ready and is_enabled,
                    "waiting_for_mcp_refresh": is_configured and is_enabled and not is_published,
                    "last_invocation": latest.get(definition.name),
                }
            )
        return {
            "revision": revision,
            "policy_error": policy_error,
            "mcp": {
                "observed_revision": observed_revision,
                "observed_at": observed_at,
                "error_code": observation_error,
                "current": observation_current,
                "published_count": len(published_names) if observation_current else 0,
            },
            "summary": {
                "known": len(active_definitions),
                "configured": len(configured),
                "enabled": len(enabled_names & configured),
                "published": len(published_names & configured) if observation_current else 0,
                "callable": len(enabled_names & route_ready),
            },
            "hub_manifest": self.hub_manifest_status(),
            "tools": tools,
        }

    def _job_document(self, connection: sqlite3.Connection, row: sqlite3.Row, *, include_input: bool = False) -> dict[str, Any]:
        queue_position = None
        if row["state"] == "queued":
            queue_position = connection.execute(
                "SELECT COUNT(*) FROM jobs WHERE state='queued' AND (created_at < ? OR (created_at=? AND job_id<=?))",
                (row["created_at"], row["created_at"], row["job_id"]),
            ).fetchone()[0]
        document = {
            "job_id": row["job_id"],
            "message_id": row["message_id"],
            "conversation_key": row["conversation_key"],
            "thread_id": row["thread_id"],
            "turn_id": row["turn_id"],
            "state": row["state"],
            "queue_position": queue_position,
            "result": row["result_text"],
            "result_summary": row["result_summary"],
            "error_code": row["error_code"],
            "error_type": row["error_type"],
            "upstream_http_status": row["upstream_http_status"],
            "retryable": bool(row["retryable"]),
            "output_observed": bool(row["output_observed"]),
            "tool_activity_observed": bool(row["tool_activity_observed"]),
            "artifact_observed": bool(row["artifact_observed"]),
            "retry_not_before": row["retry_not_before"],
            "attempt": row["attempt"],
            "capability_profile": row["capability_profile"],
            "created_at": row["created_at"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
        }
        if include_input:
            document["input"] = json.loads(row["input_json"])
        return document

    def _public_artifacts(self, job_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM job_artifacts WHERE job_id=? AND expires_at>? ORDER BY created_at,artifact_id",
                (job_id, utc_now()),
            ).fetchall()
            return [self._artifact_public_document(connection, row) for row in rows]

    def _artifact_public_document(self, connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        token = self._download_token(row["artifact_id"], row["expires_at"], connection=connection)
        token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
        stored_token_hash = row["download_token_hash"]
        if (
            not isinstance(stored_token_hash, str)
            or not re.fullmatch(r"[a-f0-9]{64}", stored_token_hash)
            or not hmac.compare_digest(token_hash, stored_token_hash)
        ):
            raise StoreError("artifact_token_invalid", "artifact 下载令牌校验失败", status=409)
        return {
            "artifact_id": row["artifact_id"],
            "type": row["artifact_type"],
            "mime_type": row["mime_type"],
            "size_bytes": row["size_bytes"],
            "sha256": f"sha256:{row['sha256']}",
            "width": row["width"],
            "height": row["height"],
            "fallback_path": f"/downloads/artifacts/{token}",
        }

    def _read_artifact_row(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> tuple[dict[str, Any], bytes]:
        try:
            path = (self.artifact_dir / row["storage_name"]).resolve(strict=True)
        except FileNotFoundError as exc:
            raise StoreError("artifact_not_found", "artifact 文件缺失", status=404) from exc
        if path.parent != self.artifact_dir.resolve() or not path.is_file() or path.is_symlink():
            raise StoreError("artifact_storage_invalid", "artifact 存储越界", status=409)
        content = path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        if (
            row["mime_type"] != "image/png"
            or not content.startswith(PNG_MAGIC)
            or len(content) != row["size_bytes"]
            or not hmac.compare_digest(digest, row["sha256"])
        ):
            raise StoreError("artifact_content_invalid", "artifact 内容校验失败", status=409)
        return self._artifact_public_document(connection, row), content

    @staticmethod
    def _new_artifact_id() -> str:
        value = base64.b32encode(os.urandom(16)).decode("ascii").rstrip("=")
        return f"AR-{value}"

    def _download_token(
        self,
        artifact_id: str,
        expires_at: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> str:
        owns_connection = connection is None
        active = self._connect() if connection is None else connection
        try:
            row = active.execute("SELECT display_secret FROM controller_meta WHERE id=1").fetchone()
            if row is None or not isinstance(row["display_secret"], bytes) or len(row["display_secret"]) != 32:
                raise StoreError("display_secret_invalid", "Controller 下载密钥不可用", status=503)
            digest = hmac.new(
                row["display_secret"],
                f"artifact-download\n{artifact_id}\n{expires_at}".encode("utf-8"),
                hashlib.sha256,
            ).digest()
            return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
        finally:
            if owns_connection:
                active.close()

    @staticmethod
    def _chart_result_summary(summary: dict[str, Any]) -> str:
        count = summary.get("transaction_count")
        amount = summary.get("net_amount")
        if (
            isinstance(count, bool)
            or not isinstance(count, int)
            or not 0 <= count <= 1000
            or not isinstance(amount, str)
            or not re.fullmatch(r"-?\d{1,12}\.\d{2}", amount)
        ):
            raise StoreError("artifact_summary_invalid", "图表汇总字段无效", status=502)
        return f"已生成装修账单统计图：共 {count} 笔记录，净支出 ¥{amount}。"

    @staticmethod
    def _event(
        connection: sqlite3.Connection,
        job_id: str,
        event_type: str,
        *,
        item_type: str | None = None,
        content_length: int | None = None,
        error_code: str | None = None,
    ) -> None:
        connection.execute(
            "INSERT INTO job_events(job_id,event_type,item_type,content_length,error_code,created_at) VALUES (?,?,?,?,?,?)",
            (job_id, event_type, item_type, content_length, error_code, utc_now()),
        )
