"""Durable renovation progress-capture sessions and exact media reconciliation."""

from __future__ import annotations

import base64
from datetime import datetime
import hashlib
import re
import sqlite3
from typing import Any
from zoneinfo import ZoneInfo

from .ledger import LedgerError, _idempotency_key, _text, canonical_json, utc_now


SESSION_ID_RE = re.compile(r"^PCS-[A-Z2-7]{26}$")
ITEM_ID_RE = re.compile(r"^PCI-[A-Z2-7]{26}$")
SHA256_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
SESSION_STATES = {"active", "paused", "finalizing", "completed", "cancelled"}
ITEM_STATES = {"pending", "stored", "failed"}
MEDIA_TYPES = {"image", "video"}
SHANGHAI = ZoneInfo("Asia/Shanghai")
CAPTION_RE = re.compile(r"^第\s*\d+\s*(?:张|个|段)?\s*[：:]\s*(.+)$", re.S)


def initialize_progress_capture_schema(store: Any) -> None:
    with store._connect() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS progress_capture_sessions (
                id TEXT PRIMARY KEY,
                scope_hash TEXT NOT NULL,
                project_id TEXT NOT NULL REFERENCES projects(id),
                stage_id TEXT REFERENCES stages(id),
                area_id TEXT REFERENCES areas(id),
                event_id TEXT NOT NULL REFERENCES events(id),
                state TEXT NOT NULL CHECK(state IN ('active','paused','finalizing','completed','cancelled')),
                title TEXT NOT NULL,
                intent_text TEXT NOT NULL DEFAULT '',
                business_date TEXT NOT NULL,
                timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
                received_count INTEGER NOT NULL DEFAULT 0 CHECK(received_count >= 0),
                source_message_hash TEXT NOT NULL,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                cancelled_at TEXT,
                version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS progress_capture_scope_open_idx
                ON progress_capture_sessions(scope_hash)
                WHERE state IN ('active','paused','finalizing');
            CREATE INDEX IF NOT EXISTS progress_capture_project_time_idx
                ON progress_capture_sessions(project_id, started_at DESC);
            CREATE TABLE IF NOT EXISTS progress_capture_items (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES progress_capture_sessions(id),
                position INTEGER NOT NULL CHECK(position >= 1 AND position <= 256),
                source_message_hash TEXT NOT NULL,
                source_ref_hash TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                media_type TEXT NOT NULL CHECK(media_type IN ('image','video')),
                size_bytes INTEGER NOT NULL CHECK(size_bytes > 0),
                display_name TEXT NOT NULL,
                caption TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL CHECK(state IN ('pending','stored','failed')),
                media_id TEXT REFERENCES media_assets(id),
                error_code TEXT,
                attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(session_id, position),
                UNIQUE(session_id, source_ref_hash)
            );
            CREATE INDEX IF NOT EXISTS progress_capture_item_state_idx
                ON progress_capture_items(session_id, state, position);
            CREATE TABLE IF NOT EXISTS progress_capture_notes (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES progress_capture_sessions(id),
                item_id TEXT REFERENCES progress_capture_items(id),
                note_type TEXT NOT NULL CHECK(note_type IN ('description','caption','correction','instruction')),
                text TEXT NOT NULL,
                source_message_hash TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS progress_capture_note_session_idx
                ON progress_capture_notes(session_id, created_at, id);
            """
        )
        connection.execute(
            "INSERT OR IGNORE INTO metadata(key,value) VALUES ('progress_capture_schema_version','1')"
        )


def _stable_id(prefix: str, material: str) -> str:
    encoded = base64.b32encode(hashlib.sha256(material.encode("utf-8")).digest()[:16]).decode("ascii").rstrip("=")
    return f"{prefix}-{encoded}"


def _sha256(value: Any, field: str) -> str:
    text = _text(value, field, 80, required=True)
    if not SHA256_RE.fullmatch(text):
        raise LedgerError("capture_invalid", f"{field} 必须是 sha256 摘要")
    return text


def _session_id(value: Any) -> str:
    text = _text(value, "session_id", 40, required=True)
    if not SESSION_ID_RE.fullmatch(text):
        raise LedgerError("capture_invalid", "session_id 无效")
    return text


def _item_id(value: Any) -> str:
    text = _text(value, "item_id", 40, required=True)
    if not ITEM_ID_RE.fullmatch(text):
        raise LedgerError("capture_invalid", "item_id 无效")
    return text


def _occurred(value: Any) -> tuple[str, str]:
    if not isinstance(value, str):
        raise LedgerError("invalid_datetime", "occurred_at 必须是带时区 ISO 8601")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LedgerError("invalid_datetime", "occurred_at 必须是带时区 ISO 8601") from exc
    if parsed.tzinfo is None:
        raise LedgerError("invalid_datetime", "occurred_at 必须包含时区")
    shanghai = parsed.astimezone(SHANGHAI)
    return parsed.astimezone(ZoneInfo("UTC")).isoformat().replace("+00:00", "Z"), shanghai.date().isoformat()


def complete_capture_item(
    connection: sqlite3.Connection,
    *,
    session_id: str,
    item_id: str,
    project_id: str,
    event_id: str,
    source_ref_hash: str,
    media_id: str,
    processing_status: str,
    error_code: str | None,
) -> None:
    """Link one persisted media asset back to its pre-registered capture item."""

    session = connection.execute(
        "SELECT project_id,event_id,state FROM progress_capture_sessions WHERE id=?",
        (session_id,),
    ).fetchone()
    item = connection.execute(
        "SELECT session_id,source_ref_hash FROM progress_capture_items WHERE id=?",
        (item_id,),
    ).fetchone()
    if session is None or item is None:
        raise LedgerError("capture_item_not_found", "采集条目不存在", status=404)
    if session["state"] in {"completed", "cancelled"}:
        raise LedgerError("capture_state_conflict", "采集会话已经结束", status=409)
    if (
        item["session_id"] != session_id
        or item["source_ref_hash"] != source_ref_hash
        or session["project_id"] != project_id
        or session["event_id"] != event_id
    ):
        raise LedgerError("capture_item_conflict", "媒体与采集条目不匹配", status=409)
    state = "stored" if processing_status == "ready" else "failed"
    connection.execute(
        "UPDATE progress_capture_items SET state=?,media_id=?,error_code=?,attempts=attempts+1,updated_at=? WHERE id=?",
        (state, media_id, None if state == "stored" else (error_code or "media_processing_failed"), utc_now(), item_id),
    )


class ProgressCaptureService:
    MAX_SESSION_ITEMS = 256
    MAX_REGISTER_BATCH = 16

    def __init__(self, store: Any, media: Any) -> None:
        self.store = store
        self.media = media
        initialize_progress_capture_schema(store)

    def action(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise LedgerError("capture_invalid", "采集请求必须是对象")
        action = payload.get("action")
        if action == "start":
            return self._start(payload)
        if action == "register_items":
            return self._register_items(payload)
        if action == "note":
            return self._note(payload)
        if action == "pause":
            return self._set_state(payload, "paused")
        if action == "resume":
            return self._set_state(payload, "active")
        if action == "cancel":
            return self._cancel(payload)
        if action == "status":
            return self.status(_session_id(payload.get("session_id")))
        if action == "mark_failed":
            return self._mark_failed(payload)
        if action == "retry_failed":
            return self._retry_failed(payload)
        if action == "finalize":
            return self._finalize(payload)
        raise LedgerError("capture_invalid", "不支持的采集动作")

    def _start(self, payload: dict[str, Any]) -> dict[str, Any]:
        session_id = _session_id(payload.get("session_id"))
        scope_hash = _sha256(payload.get("scope_hash"), "scope_hash")
        source_message_hash = _sha256(payload.get("source_message_hash"), "source_message_hash")
        key = _idempotency_key(payload.get("idempotency_key"))
        intent_text = _text(payload.get("text"), "text", 1000)
        occurred_at, business_date = _occurred(payload.get("occurred_at"))
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM progress_capture_sessions WHERE id=?",
                (session_id,),
            ).fetchone()
            if existing is not None:
                if existing["scope_hash"] != scope_hash:
                    raise LedgerError("idempotency_conflict", "同一采集会话对应不同作用域", status=409)
                return self._status_document(connection, session_id, idempotent_replay=True)
            open_session = connection.execute(
                "SELECT id FROM progress_capture_sessions WHERE scope_hash=? AND state IN ('active','paused','finalizing')",
                (scope_hash,),
            ).fetchone()
            if open_session is not None:
                return self._status_document(connection, str(open_session["id"]), idempotent_replay=True)
            self.store._require_writer(connection)
            project, stage, area = self._resolve_context(connection, payload, intent_text)
            title = self._capture_title(payload, project, stage, area, business_date)
            event_id = _stable_id("EV", f"progress-capture-event\n{session_id}")
            now = utc_now()
            connection.execute(
                "INSERT INTO events(id,project_id,stage_id,area_id,event_type,title,description,occurred_at,status,source_ref,created_at,updated_at) "
                "VALUES (?,?,?,?,? ,?,?,?,'active',?,?,?)",
                (
                    event_id,
                    project["id"],
                    None if stage is None else stage["id"],
                    None if area is None else area["id"],
                    "progress",
                    title,
                    intent_text,
                    occurred_at,
                    f"progress_capture:{session_id}",
                    now,
                    now,
                ),
            )
            connection.execute(
                "INSERT INTO progress_capture_sessions(id,scope_hash,project_id,stage_id,area_id,event_id,state,title,intent_text,business_date,timezone,source_message_hash,started_at,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,'active',?,?,?,?,?,?,?,?)",
                (
                    session_id,
                    scope_hash,
                    project["id"],
                    None if stage is None else stage["id"],
                    None if area is None else area["id"],
                    event_id,
                    title,
                    intent_text,
                    business_date,
                    "Asia/Shanghai",
                    source_message_hash,
                    occurred_at,
                    now,
                    now,
                ),
            )
            session = self._session(connection, session_id)
            self.store._domain_audit(
                connection,
                action="start_progress_capture",
                target_type="event",
                target_id=event_id,
                actor_hash="sha256:codex-controller",
                idempotency_key=key,
                before=None,
                after=session,
            )
            return self._status_document(connection, session_id, idempotent_replay=False)

    def _resolve_context(
        self,
        connection: sqlite3.Connection,
        payload: dict[str, Any],
        text: str,
    ) -> tuple[sqlite3.Row, sqlite3.Row | None, sqlite3.Row | None]:
        project = self._resolve_named(
            connection,
            table="projects",
            explicit=payload.get("project_id"),
            text=text,
            project_id=None,
            statuses=("active", "completed"),
            required=True,
            ambiguous_code="capture_project_ambiguous",
        )
        assert project is not None
        stage = self._resolve_named(
            connection,
            table="stages",
            explicit=payload.get("stage_id"),
            text=text,
            project_id=project["id"],
            statuses=("planned", "active", "completed"),
            required=False,
            ambiguous_code="capture_stage_ambiguous",
        )
        area = self._resolve_named(
            connection,
            table="areas",
            explicit=payload.get("area_id"),
            text=text,
            project_id=project["id"],
            statuses=("active",),
            required=False,
            ambiguous_code="capture_area_ambiguous",
        )
        return project, stage, area

    @staticmethod
    def _resolve_named(
        connection: sqlite3.Connection,
        *,
        table: str,
        explicit: Any,
        text: str,
        project_id: str | None,
        statuses: tuple[str, ...],
        required: bool,
        ambiguous_code: str,
    ) -> sqlite3.Row | None:
        clauses = [f"status IN ({','.join('?' for _ in statuses)})"]
        values: list[Any] = list(statuses)
        if project_id is not None:
            clauses.append("project_id=?")
            values.append(project_id)
        if explicit not in {None, ""}:
            identifier = _text(explicit, f"{table}_id", 64, required=True)
            row = connection.execute(
                f"SELECT * FROM {table} WHERE id=? AND {' AND '.join(clauses)}",
                [identifier, *values],
            ).fetchone()
            if row is None:
                raise LedgerError("capture_context_not_found", "采集上下文对象不存在", status=404)
            return row
        rows = connection.execute(
            f"SELECT * FROM {table} WHERE {' AND '.join(clauses)} ORDER BY name,id",
            values,
        ).fetchall()
        normalized = re.sub(r"\s+", "", text)
        matches = [row for row in rows if re.sub(r"\s+", "", str(row["name"])) in normalized]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise LedgerError(ambiguous_code, "文字匹配到多个装修上下文，请补充更具体名称", status=409)
        if required and len(rows) == 1:
            return rows[0]
        if required:
            raise LedgerError(ambiguous_code, "存在多个装修项目，请先说明项目名称", status=409)
        return None

    @staticmethod
    def _capture_title(
        payload: dict[str, Any],
        project: sqlite3.Row,
        stage: sqlite3.Row | None,
        area: sqlite3.Row | None,
        business_date: str,
    ) -> str:
        explicit = _text(payload.get("title"), "title", 160)
        if explicit:
            return explicit
        context_name = str((area or stage or project)["name"])
        return f"{business_date} {context_name}装修进度"

    def _register_items(self, payload: dict[str, Any]) -> dict[str, Any]:
        session_id = _session_id(payload.get("session_id"))
        key = _idempotency_key(payload.get("idempotency_key"))
        items = payload.get("items")
        if not isinstance(items, list) or not 1 <= len(items) <= self.MAX_REGISTER_BATCH:
            raise LedgerError("capture_batch_invalid", "单批采集条目必须为 1 到 16 项")
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self.store._require_writer(connection)
            session = self._session(connection, session_id)
            if session["state"] in {"completed", "cancelled"}:
                raise LedgerError("capture_state_conflict", "采集会话已经结束", status=409)
            now = utc_now()
            normalized: list[dict[str, Any]] = []
            for value in items:
                if not isinstance(value, dict):
                    raise LedgerError("capture_item_invalid", "采集条目必须是对象")
                position = value.get("position")
                size_bytes = value.get("size_bytes")
                if isinstance(position, bool) or not isinstance(position, int) or not 1 <= position <= self.MAX_SESSION_ITEMS:
                    raise LedgerError("capture_item_invalid", "采集条目序号无效")
                if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes < 1:
                    raise LedgerError("capture_item_invalid", "采集条目大小无效")
                media_type = value.get("media_type")
                if media_type not in MEDIA_TYPES:
                    raise LedgerError("capture_item_invalid", "采集媒体类型无效")
                item = {
                    "position": position,
                    "source_message_hash": _sha256(value.get("source_message_hash"), "source_message_hash"),
                    "source_ref_hash": _sha256(value.get("source_ref_hash"), "source_ref_hash"),
                    "sha256": _sha256(value.get("sha256"), "sha256"),
                    "media_type": media_type,
                    "size_bytes": size_bytes,
                    "display_name": _text(value.get("display_name"), "display_name", 255, required=True),
                }
                normalized.append(item)
            for item in normalized:
                existing = connection.execute(
                    "SELECT * FROM progress_capture_items WHERE session_id=? AND source_ref_hash=?",
                    (session_id, item["source_ref_hash"]),
                ).fetchone()
                if existing is not None:
                    if (
                        existing["position"] != item["position"]
                        or existing["sha256"] != item["sha256"]
                        or existing["media_type"] != item["media_type"]
                    ):
                        raise LedgerError("idempotency_conflict", "同一采集引用对应不同媒体", status=409)
                    continue
                item_id = _stable_id("PCI", f"{session_id}\n{item['source_ref_hash']}")
                connection.execute(
                    "INSERT INTO progress_capture_items(id,session_id,position,source_message_hash,source_ref_hash,sha256,media_type,size_bytes,display_name,state,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,'pending',?,?)",
                    (
                        item_id,
                        session_id,
                        item["position"],
                        item["source_message_hash"],
                        item["source_ref_hash"],
                        item["sha256"],
                        item["media_type"],
                        item["size_bytes"],
                        item["display_name"],
                        now,
                        now,
                    ),
                )
            count = connection.execute(
                "SELECT COUNT(*) FROM progress_capture_items WHERE session_id=?",
                (session_id,),
            ).fetchone()[0]
            if count > self.MAX_SESSION_ITEMS:
                raise LedgerError("capture_item_limit", "一次采集会话最多 256 项", status=409)
            connection.execute(
                "UPDATE progress_capture_sessions SET received_count=?,version=version+1,updated_at=? WHERE id=?",
                (count, now, session_id),
            )
            documents = [self._item(connection, session_id, item["source_ref_hash"]) for item in normalized]
            self.store._domain_audit(
                connection,
                action="register_progress_capture_items",
                target_type="event",
                target_id=session["event_id"],
                actor_hash="sha256:codex-controller",
                idempotency_key=key,
                before=None,
                after={"session_id": session_id, "positions": [item["position"] for item in documents]},
            )
            return {"items": documents, **self._status_document(connection, session_id)}

    def _note(self, payload: dict[str, Any]) -> dict[str, Any]:
        session_id = _session_id(payload.get("session_id"))
        key = _idempotency_key(payload.get("idempotency_key"))
        text = _text(payload.get("text"), "text", 2000, required=True)
        target_position = payload.get("target_position")
        if target_position is not None and (
            isinstance(target_position, bool) or not isinstance(target_position, int) or not 1 <= target_position <= 256
        ):
            raise LedgerError("capture_item_invalid", "说明目标序号无效")
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self.store._require_writer(connection)
            session = self._session(connection, session_id)
            if session["state"] in {"completed", "cancelled"}:
                raise LedgerError("capture_state_conflict", "采集会话已经结束", status=409)
            item = None
            note_type = "description"
            note_text = text
            if target_position is not None:
                item = connection.execute(
                    "SELECT * FROM progress_capture_items WHERE session_id=? AND position=?",
                    (session_id, target_position),
                ).fetchone()
                if item is None:
                    raise LedgerError("capture_item_not_found", "指定序号的媒体尚未收到", status=404)
                match = CAPTION_RE.match(text.strip())
                note_text = (match.group(1) if match else text).strip()
                connection.execute(
                    "UPDATE progress_capture_items SET caption=?,updated_at=? WHERE id=?",
                    (note_text, utc_now(), item["id"]),
                )
                note_type = "caption"
            note_id = _stable_id("PCN", f"{session_id}\n{key}")
            connection.execute(
                "INSERT OR IGNORE INTO progress_capture_notes(id,session_id,item_id,note_type,text,source_message_hash,created_at) VALUES (?,?,?,?,?,?,?)",
                (
                    note_id,
                    session_id,
                    None if item is None else item["id"],
                    note_type,
                    note_text,
                    _text(payload.get("source_message_hash"), "source_message_hash", 80),
                    utc_now(),
                ),
            )
            self._refresh_event_description(connection, session_id)
            result = self._status_document(connection, session_id)
            if item is not None:
                result["item"] = self._item_by_id(connection, item["id"])
            return result

    def _set_state(self, payload: dict[str, Any], state: str) -> dict[str, Any]:
        session_id = _session_id(payload.get("session_id"))
        _idempotency_key(payload.get("idempotency_key"))
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self.store._require_writer(connection)
            session = self._session(connection, session_id)
            if session["state"] in {"completed", "cancelled"}:
                raise LedgerError("capture_state_conflict", "采集会话已经结束", status=409)
            connection.execute(
                "UPDATE progress_capture_sessions SET state=?,version=version+1,updated_at=? WHERE id=?",
                (state, utc_now(), session_id),
            )
            return self._status_document(connection, session_id)

    def _cancel(self, payload: dict[str, Any]) -> dict[str, Any]:
        session_id = _session_id(payload.get("session_id"))
        _idempotency_key(payload.get("idempotency_key"))
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self.store._require_writer(connection)
            session = self._session(connection, session_id)
            if session["state"] != "cancelled":
                now = utc_now()
                connection.execute(
                    "UPDATE progress_capture_sessions SET state='cancelled',cancelled_at=?,version=version+1,updated_at=? WHERE id=? AND state!='completed'",
                    (now, now, session_id),
                )
                connection.execute(
                    "UPDATE events SET status='voided',version=version+1,updated_at=? WHERE id=?",
                    (now, session["event_id"]),
                )
            return self._status_document(connection, session_id)

    def _mark_failed(self, payload: dict[str, Any]) -> dict[str, Any]:
        session_id = _session_id(payload.get("session_id"))
        item_id = _item_id(payload.get("item_id"))
        error_code = _text(payload.get("error_code"), "error_code", 80, required=True)
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self.store._require_writer(connection)
            self._session(connection, session_id)
            updated = connection.execute(
                "UPDATE progress_capture_items SET state='failed',error_code=?,attempts=attempts+1,updated_at=? WHERE id=? AND session_id=?",
                (error_code, utc_now(), item_id, session_id),
            ).rowcount
            if updated != 1:
                raise LedgerError("capture_item_not_found", "采集条目不存在", status=404)
            return self._status_document(connection, session_id)

    def _retry_failed(self, payload: dict[str, Any]) -> dict[str, Any]:
        session_id = _session_id(payload.get("session_id"))
        _idempotency_key(payload.get("idempotency_key"))
        with self.store._connect() as connection:
            rows = connection.execute(
                "SELECT media_id FROM progress_capture_items WHERE session_id=? AND state='failed' AND media_id IS NOT NULL ORDER BY position",
                (session_id,),
            ).fetchall()
        for row in rows:
            self.media.reprocess(str(row["media_id"]))
        return self.status(session_id)

    def _finalize(self, payload: dict[str, Any]) -> dict[str, Any]:
        session_id = _session_id(payload.get("session_id"))
        key = _idempotency_key(payload.get("idempotency_key"))
        received = payload.get("expected_received_count")
        if isinstance(received, bool) or not isinstance(received, int) or not 0 <= received <= self.MAX_SESSION_ITEMS:
            raise LedgerError("capture_invalid", "expected_received_count 无效")
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self.store._require_writer(connection)
            session = self._session(connection, session_id)
            if session["state"] == "completed":
                result = self._status_document(connection, session_id, idempotent_replay=True)
                if result["reconciliation"]["received"] != received:
                    raise LedgerError("idempotency_conflict", "完成重放数量不一致", status=409)
                return result
            if session["state"] == "cancelled":
                raise LedgerError("capture_state_conflict", "采集会话已经取消", status=409)
            reconciliation = self._reconciliation(connection, session_id, received=received)
            if not (
                reconciliation["received"]
                == reconciliation["registered"]
                == reconciliation["stored"]
                == reconciliation["linked"]
                and reconciliation["failed"] == 0
                and reconciliation["pending"] == 0
            ):
                connection.execute(
                    "UPDATE progress_capture_sessions SET state='finalizing',updated_at=? WHERE id=?",
                    (utc_now(), session_id),
                )
                raise LedgerError(
                    "capture_reconciliation_pending",
                    f"采集尚未对齐：收到 {received}，登记 {reconciliation['registered']}，已存 {reconciliation['stored']}，已关联 {reconciliation['linked']}",
                    status=409,
                )
            now = utc_now()
            connection.execute(
                "UPDATE progress_capture_sessions SET state='completed',received_count=?,completed_at=?,version=version+1,updated_at=? WHERE id=?",
                (received, now, now, session_id),
            )
            self._refresh_event_description(connection, session_id, final=True)
            completed = self._session(connection, session_id)
            self.store._domain_audit(
                connection,
                action="finalize_progress_capture",
                target_type="event",
                target_id=completed["event_id"],
                actor_hash="sha256:codex-controller",
                idempotency_key=key,
                before=session,
                after={"session": completed, "reconciliation": reconciliation},
            )
            return self._status_document(connection, session_id, received=received)

    def status(self, session_id: str) -> dict[str, Any]:
        with self.store._connect() as connection:
            return self._status_document(connection, session_id)

    def _status_document(
        self,
        connection: sqlite3.Connection,
        session_id: str,
        *,
        received: int | None = None,
        idempotent_replay: bool = False,
    ) -> dict[str, Any]:
        session = self._session(connection, session_id)
        items = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM progress_capture_items WHERE session_id=? ORDER BY position",
                (session_id,),
            )
        ]
        reconciliation = self._reconciliation(
            connection,
            session_id,
            received=session["received_count"] if received is None else received,
        )
        return {
            "session": session,
            "items": items,
            "reconciliation": reconciliation,
            "idempotent_replay": idempotent_replay,
        }

    @staticmethod
    def _session(connection: sqlite3.Connection, session_id: str) -> dict[str, Any]:
        row = connection.execute(
            "SELECT * FROM progress_capture_sessions WHERE id=?",
            (session_id,),
        ).fetchone()
        if row is None:
            raise LedgerError("capture_session_not_found", "采集会话不存在", status=404)
        return dict(row)

    @staticmethod
    def _item(connection: sqlite3.Connection, session_id: str, source_ref_hash: str) -> dict[str, Any]:
        row = connection.execute(
            "SELECT * FROM progress_capture_items WHERE session_id=? AND source_ref_hash=?",
            (session_id, source_ref_hash),
        ).fetchone()
        if row is None:
            raise LedgerError("capture_item_not_found", "采集条目不存在", status=404)
        return dict(row)

    @staticmethod
    def _item_by_id(connection: sqlite3.Connection, item_id: str) -> dict[str, Any]:
        row = connection.execute("SELECT * FROM progress_capture_items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise LedgerError("capture_item_not_found", "采集条目不存在", status=404)
        return dict(row)

    @staticmethod
    def _reconciliation(
        connection: sqlite3.Connection,
        session_id: str,
        *,
        received: int,
    ) -> dict[str, int]:
        session = connection.execute(
            "SELECT event_id FROM progress_capture_sessions WHERE id=?",
            (session_id,),
        ).fetchone()
        if session is None:
            raise LedgerError("capture_session_not_found", "采集会话不存在", status=404)
        counts = {
            row["state"]: row["count"]
            for row in connection.execute(
                "SELECT state,COUNT(*) AS count FROM progress_capture_items WHERE session_id=? GROUP BY state",
                (session_id,),
            )
        }
        registered = sum(counts.values())
        linked = connection.execute(
            "SELECT COUNT(DISTINCT items.id) FROM progress_capture_items items "
            "JOIN media_links links ON links.media_id=items.media_id AND links.target_type='event' AND links.target_id=? "
            "WHERE items.session_id=? AND items.state='stored'",
            (session["event_id"], session_id),
        ).fetchone()[0]
        return {
            "received": received,
            "registered": registered,
            "stored": int(counts.get("stored", 0)),
            "linked": int(linked),
            "failed": int(counts.get("failed", 0)),
            "pending": int(counts.get("pending", 0)),
        }

    def _refresh_event_description(
        self,
        connection: sqlite3.Connection,
        session_id: str,
        *,
        final: bool = False,
    ) -> None:
        session = self._session(connection, session_id)
        notes = [
            str(row["text"])
            for row in connection.execute(
                "SELECT text FROM progress_capture_notes "
                "WHERE session_id=? AND note_type IN ('description','correction','instruction') "
                "ORDER BY created_at,id",
                (session_id,),
            )
        ]
        captions = [
            f"第{row['position']}项：{row['caption']}"
            for row in connection.execute(
                "SELECT position,caption FROM progress_capture_items WHERE session_id=? AND caption!='' ORDER BY position",
                (session_id,),
            )
        ]
        lines: list[str] = []
        for line in [session["intent_text"], *notes, *captions]:
            if line and line not in lines:
                lines.append(line)
        if final:
            counts = self._reconciliation(connection, session_id, received=session["received_count"])
            lines.append(f"已完成归档：共 {counts['stored']} 项图片/视频。")
        description = "\n".join(line for line in lines if line).strip()[:4000]
        connection.execute(
            "UPDATE events SET description=?,version=version+1,updated_at=? WHERE id=?",
            (description, utc_now(), session["event_id"]),
        )
