"""Persistent, fail-closed queue and conversation mapping for Codex jobs."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any
import uuid


CONVERSATION_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
ACTIVE_STATES = ("queued", "running", "recovery_required")
FINAL_STATES = ("completed", "failed", "cancelled")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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

    def __init__(self, database_path: str | Path, *, max_queue: int = 200, max_result_chars: int = 12000):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.max_queue = max_queue
        self.max_result_chars = max_result_chars
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
                    error_code TEXT,
                    attempt INTEGER NOT NULL DEFAULT 0,
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
                """
            )

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
        return {
            "version": 1,
            "message_id": message_id,
            "conversation_key": conversation_key,
            "received_at": received_at,
            "text": text,
            "attachments": normalized_attachments,
            "reply_capabilities": sorted(set(reply_capabilities)),
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
                "INSERT INTO jobs(job_id,message_id,conversation_key,state,input_digest,input_json,created_at) VALUES (?,?,?,?,?,?,?)",
                (job_id, normalized["message_id"], normalized["conversation_key"], "queued", digest, serialized, now),
            )
            row = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            self._event(connection, job_id, "queued")
            return self._job_document(connection, row)

    def get_job(self, job_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            if row is None:
                raise StoreError("job_not_found", "作业不存在", status=404)
            return self._job_document(connection, row)

    def claim_next(self) -> dict[str, Any] | None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT 1 FROM jobs WHERE state = 'running' LIMIT 1").fetchone():
                return None
            row = connection.execute("SELECT * FROM jobs WHERE state = 'queued' ORDER BY created_at, job_id LIMIT 1").fetchone()
            if row is None:
                return None
            now = utc_now()
            connection.execute(
                "UPDATE jobs SET state='running', started_at=?, attempt=attempt+1, error_code=NULL WHERE job_id=? AND state='queued'",
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

    def conversation_thread(self, conversation_key: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT thread_id FROM conversations WHERE conversation_key=? AND state='active'",
                (conversation_key,),
            ).fetchone()
            return None if row is None else row["thread_id"]

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
            connection.execute("UPDATE jobs SET result_text=? WHERE job_id=?", (bounded, row["job_id"]))
            self._event(connection, row["job_id"], "item_completed", item_type=item_type, content_length=len(text))
            return True

    def complete_turn(self, turn_id: str, turn_status: str, *, error_code: str | None = None) -> bool:
        state = "completed" if turn_status == "completed" else "cancelled" if turn_status == "interrupted" else "failed"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT job_id FROM jobs WHERE turn_id=? AND state='running'", (turn_id,)).fetchone()
            if row is None:
                return False
            connection.execute(
                "UPDATE jobs SET state=?,error_code=?,finished_at=? WHERE job_id=?",
                (state, error_code, utc_now(), row["job_id"]),
            )
            self._event(connection, row["job_id"], state, error_code=error_code)
            return True

    def fail_claimed(self, job_id: str, error_code: str, *, uncertain: bool) -> None:
        state = "recovery_required" if uncertain else "failed"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE jobs SET state=?,error_code=?,finished_at=? WHERE job_id=? AND state='running'",
                (state, error_code, utc_now(), job_id),
            )
            self._event(connection, job_id, state, error_code=error_code)

    def retry_overloaded(self, job_id: str, *, max_attempts: int = 3) -> bool:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT attempt FROM jobs WHERE job_id=? AND state='running'", (job_id,)).fetchone()
            if row is None or row["attempt"] >= max_attempts:
                return False
            connection.execute("UPDATE jobs SET state='queued',started_at=NULL,error_code='app_server_overloaded' WHERE job_id=?", (job_id,))
            self._event(connection, job_id, "requeued", error_code="app_server_overloaded")
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
                "SELECT job_id,message_id,thread_id,turn_id,state,started_at FROM jobs WHERE state='running' LIMIT 1"
            ).fetchone()
            return {
                "jobs": {state: counts.get(state, 0) for state in (*ACTIVE_STATES, *FINAL_STATES)},
                "threads": connection.execute("SELECT COUNT(*) FROM conversations WHERE thread_id IS NOT NULL").fetchone()[0],
                "active_job": None if active is None else dict(active),
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
            "error_code": row["error_code"],
            "attempt": row["attempt"],
            "created_at": row["created_at"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
        }
        if include_input:
            document["input"] = json.loads(row["input_json"])
        return document

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
