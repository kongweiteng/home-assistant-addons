"""Bounded SQLite read model for ref-only Codex Desktop takeover state."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import sqlite3
import threading
from typing import Any, Mapping

from .desktop_protocol import canonical_json
from .store import StoreError


MAX_HOSTS = 16
MAX_PROJECTS = 512
MAX_THREADS = 5000
SNAPSHOTS_PER_THREAD = 3
EVENTS_PER_HOST = 5000
MAX_COMMANDS = 5000
MAX_RECEIPTS = 5000
SAME_REVISION_CONTROL_FIELDS = frozenset(
    {
        "status",
        "active_turn_ref",
        "control_revision",
        "control_state",
        "history_incomplete",
        "turns",
    }
)
SAFETY_DEGRADED_CONTROL_STATES = frozenset(
    {"load_required", "recovery_required", "protocol_degraded", "control_offline"}
)
NON_WRITABLE_CONTROL_STATES = frozenset(
    {
        "load_required",
        "read_only",
        "recovery_required",
        "protocol_degraded",
        "control_offline",
    }
)
LEGACY_SEQUENCE_ENRICHMENT_FIELDS = frozenset(
    {"model", "reasoning_effort", "queued_submissions"}
)


class DesktopStore:
    def __init__(self, database_path: str | Path) -> None:
        self.database_path = str(database_path)
        self._lock = threading.RLock()
        self._migrate()
        self._recover_pending_commands()

    def ingest_snapshot(self, document: Mapping[str, Any], *, observed_at: str) -> dict[str, Any]:
        body = dict(document["snapshot"])
        host = dict(document.get("host") or {})
        host_ref = str(document["host_ref"])
        project_ref = str(document["project_ref"])
        thread_ref = str(document["thread_ref"])
        runner_id = str(document["runner_id"])
        revision = int(document["thread_revision"])
        snapshot_sequence = document.get("snapshot_sequence")
        digest = str(document["body_digest"])
        encoded_document = canonical_json(document)
        encoded_snapshot = canonical_json(body)
        encoded_host = canonical_json(host) if host else "{}"
        with self._lock, self._connect() as connection:
            existing_snapshot = connection.execute(
                "SELECT body_digest FROM desktop_snapshots WHERE body_digest=?",
                (digest,),
            ).fetchone()
            if existing_snapshot is not None:
                return {"status": "duplicate", "thread": self._thread_row(connection, thread_ref)}
            self._bind_host(connection, host_ref, runner_id, host, encoded_host, observed_at)
            self._upsert_project(
                connection,
                host_ref,
                project_ref,
                str(body["project_alias"]),
                observed_at,
            )
            existing = connection.execute(
                "SELECT id,host_ref,project_ref,runner_id,thread_revision,control_revision,snapshot_sequence,"
                "snapshot_digest,snapshot_json "
                "FROM desktop_threads WHERE thread_ref=?",
                (thread_ref,),
            ).fetchone()
            semantic_refresh = False
            degraded_latch = False
            legacy_sequence_enrichment = False
            effective_snapshot_sequence = snapshot_sequence
            if existing is not None:
                if (
                    existing["host_ref"] != host_ref
                    or existing["project_ref"] != project_ref
                    or existing["runner_id"] != runner_id
                ):
                    raise StoreError(
                        "desktop_thread_binding_conflict",
                        "Desktop Thread ref 与现有 host/project/Runner 绑定冲突",
                        status=409,
                    )
                previous_revision = int(existing["thread_revision"])
                if revision < previous_revision:
                    return {"status": "stale_ignored", "thread": self._thread_row(connection, thread_ref)}
                if revision == previous_revision and existing["snapshot_digest"] is None:
                    raise StoreError(
                        "desktop_revision_conflict",
                        "现有 Desktop snapshot 摘要缺失，拒绝覆盖",
                        status=409,
                    )
                if revision == previous_revision and existing["snapshot_digest"] != digest:
                    existing_sequence = existing["snapshot_sequence"]
                    if snapshot_sequence is None:
                        effective_snapshot_sequence = existing_sequence
                    elif existing_sequence is not None and snapshot_sequence < existing_sequence:
                        return {
                            "status": "stale_ignored",
                            "thread": self._thread_row(connection, thread_ref),
                        }
                    if (
                        snapshot_sequence is not None
                        and existing_sequence is not None
                        and snapshot_sequence == existing_sequence
                    ):
                        if existing["snapshot_json"] == encoded_snapshot:
                            semantic_refresh = True
                        else:
                            raise StoreError(
                                "desktop_revision_conflict",
                                "同一 Desktop snapshot sequence 出现不同快照",
                                status=409,
                            )
                        refresh = None
                    elif snapshot_sequence is not None and existing_sequence is None:
                        legacy_snapshot = connection.execute(
                            "SELECT body_digest,source_sequence FROM desktop_snapshots "
                            "WHERE thread_ref=? AND thread_revision=?",
                            (thread_ref, revision),
                        ).fetchone()
                        if (
                            legacy_snapshot is None
                            or legacy_snapshot["body_digest"] != existing["snapshot_digest"]
                            or legacy_snapshot["source_sequence"] is not None
                        ):
                            refresh = None
                        else:
                            legacy_sequence_enrichment = True
                            refresh = _legacy_sequence_refresh(
                                str(existing["snapshot_json"]), body, host
                            )
                    elif snapshot_sequence is not None and snapshot_sequence > existing_sequence:
                        changed_fields = _changed_snapshot_fields(str(existing["snapshot_json"]), body)
                        if changed_fields == {"queued_submissions"}:
                            refresh = "refreshed"
                        else:
                            refresh = _same_revision_refresh(str(existing["snapshot_json"]), body)
                    else:
                        refresh = _same_revision_refresh(str(existing["snapshot_json"]), body)
                    if refresh == "stale_ignored":
                        return {
                            "status": "stale_ignored",
                            "thread": self._thread_row(connection, thread_ref),
                        }
                    if refresh == "degraded_latched":
                        trusted = json.loads(str(existing["snapshot_json"]))
                        if legacy_sequence_enrichment:
                            for field in LEGACY_SEQUENCE_ENRICHMENT_FIELDS:
                                trusted[field] = body[field]
                        trusted["control_state"] = body["control_state"]
                        body = trusted
                        encoded_snapshot = canonical_json(body)
                        degraded_latch = True
                    if existing["snapshot_json"] == encoded_snapshot or refresh == "refreshed":
                        semantic_refresh = True
                    elif degraded_latch:
                        semantic_refresh = True
                    else:
                        raise StoreError(
                            "desktop_revision_conflict",
                            "同一 Desktop Thread revision 出现不同快照",
                            status=409,
                        )
            elif self._count(connection, "desktop_threads") >= MAX_THREADS:
                raise StoreError("desktop_thread_capacity", "Desktop Thread 容量已满", status=507)

            connection.execute(
                "INSERT INTO desktop_threads("
                "thread_ref,host_ref,project_ref,runner_id,title,status,active_turn_ref,thread_revision,"
                "control_revision,snapshot_sequence,control_state,snapshot_digest,snapshot_json,"
                "source_created_at,source_updated_at,observed_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(thread_ref) DO UPDATE SET "
                "title=excluded.title,status=excluded.status,active_turn_ref=excluded.active_turn_ref,"
                "thread_revision=excluded.thread_revision,control_revision=excluded.control_revision,"
                "snapshot_sequence=excluded.snapshot_sequence,control_state=excluded.control_state,"
                "snapshot_digest=excluded.snapshot_digest,snapshot_json=excluded.snapshot_json,"
                "source_created_at=excluded.source_created_at,source_updated_at=excluded.source_updated_at,"
                "observed_at=excluded.observed_at",
                (
                    thread_ref,
                    host_ref,
                    project_ref,
                    runner_id,
                    str(body.get("title") or "未命名任务")[:500],
                    str(body["status"]),
                    body.get("active_turn_ref"),
                    revision,
                    body.get("control_revision"),
                    effective_snapshot_sequence,
                    str(body.get("control_state") or "recovery_required")[:64],
                    digest,
                    encoded_snapshot,
                    body.get("created_at"),
                    body.get("updated_at"),
                    observed_at,
                ),
            )
            if semantic_refresh:
                refreshed = connection.execute(
                    "UPDATE desktop_snapshots SET body_digest=?,document_json=?,source_sequence=?,observed_at=? "
                    "WHERE thread_ref=? AND thread_revision=?",
                    (
                        digest,
                        encoded_document,
                        effective_snapshot_sequence,
                        observed_at,
                        thread_ref,
                        revision,
                    ),
                )
                if refreshed.rowcount == 0:
                    connection.execute(
                        "INSERT INTO desktop_snapshots("
                        "thread_ref,thread_revision,body_digest,document_json,source_sequence,observed_at"
                        ") VALUES(?,?,?,?,?,?)",
                        (
                            thread_ref,
                            revision,
                            digest,
                            encoded_document,
                            effective_snapshot_sequence,
                            observed_at,
                        ),
                    )
            else:
                connection.execute(
                    "INSERT INTO desktop_snapshots("
                    "thread_ref,thread_revision,body_digest,document_json,source_sequence,observed_at"
                    ") VALUES(?,?,?,?,?,?)",
                    (
                        thread_ref,
                        revision,
                        digest,
                        encoded_document,
                        effective_snapshot_sequence,
                        observed_at,
                    ),
                )
            self._prune_snapshots(connection, thread_ref)
            return {
                "status": (
                    "degraded_latched"
                    if degraded_latch
                    else "refreshed"
                    if semantic_refresh
                    else "stored"
                ),
                "thread": self._thread_row(connection, thread_ref),
            }

    def ingest_event(self, document: Mapping[str, Any]) -> dict[str, Any]:
        host_ref = str(document["host_ref"])
        project_ref = str(document["project_ref"])
        thread_ref = str(document["thread_ref"])
        runner_id = str(document["runner_id"])
        sequence = int(document["event_sequence"])
        digest = str(document["body_digest"])
        created_at = str(document["created_at"])
        payload = dict(document["payload"])
        with self._lock, self._connect() as connection:
            duplicate = connection.execute(
                "SELECT cursor FROM desktop_events WHERE body_digest=?",
                (digest,),
            ).fetchone()
            if duplicate is not None:
                return {"status": "duplicate", "cursor": int(duplicate["cursor"])}
            sequence_row = connection.execute(
                "SELECT body_digest FROM desktop_events WHERE host_ref=? AND event_sequence=?",
                (host_ref, sequence),
            ).fetchone()
            if sequence_row is not None:
                raise StoreError(
                    "desktop_event_sequence_conflict",
                    "同一 Desktop event sequence 出现不同正文",
                    status=409,
                )
            maximum = connection.execute(
                "SELECT MAX(event_sequence) AS value FROM desktop_events WHERE host_ref=?",
                (host_ref,),
            ).fetchone()["value"]
            if maximum is not None and sequence <= int(maximum):
                raise StoreError(
                    "desktop_event_sequence_stale",
                    "Desktop event sequence 非单调递增",
                    status=409,
                )
            self._bind_host(connection, host_ref, runner_id, {}, "{}", created_at)
            binding = connection.execute(
                "SELECT host_ref,project_ref,runner_id,thread_revision FROM desktop_threads WHERE thread_ref=?",
                (thread_ref,),
            ).fetchone()
            if binding is None:
                raise StoreError(
                    "desktop_snapshot_required",
                    "Desktop event 必须在目标 Thread 快照之后接收",
                    status=409,
                )
            if (
                binding["host_ref"] != host_ref
                or binding["project_ref"] != project_ref
                or binding["runner_id"] != runner_id
            ):
                raise StoreError(
                    "desktop_thread_binding_conflict",
                    "Desktop event 越过现有 Thread 绑定",
                    status=409,
                )
            target_snapshot = connection.execute(
                "SELECT 1 FROM desktop_snapshots WHERE thread_ref=? AND thread_revision=?",
                (thread_ref, int(document["thread_revision"])),
            ).fetchone()
            if target_snapshot is None:
                if int(binding["thread_revision"]) > int(document["thread_revision"]):
                    raise StoreError(
                        "desktop_event_sequence_stale",
                        "Desktop event 对应 revision 已被更新快照取代",
                        status=409,
                    )
                raise StoreError(
                    "desktop_snapshot_required",
                    "Desktop event 对应 revision 的快照必须先接收",
                    status=409,
                )
            cursor = connection.execute(
                "INSERT INTO desktop_events("
                "host_ref,project_ref,thread_ref,turn_ref,event_sequence,event_kind,source,thread_revision,"
                "body_digest,document_json,created_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?) RETURNING cursor",
                (
                    host_ref,
                    project_ref,
                    thread_ref,
                    document.get("turn_ref"),
                    sequence,
                    str(document["event_kind"]),
                    str(document["source"]),
                    int(document["thread_revision"]),
                    digest,
                    canonical_json(document),
                    created_at,
                ),
            ).fetchone()["cursor"]
            self._prune_events(connection, host_ref)
            return {"status": "stored", "cursor": int(cursor)}

    def prepare_command(
        self,
        *,
        command: Mapping[str, Any],
        intent_digest: str,
    ) -> tuple[dict[str, Any], bool]:
        request_id = str(command["request_id"])
        with self._lock, self._connect() as connection:
            existing = connection.execute(
                "SELECT * FROM desktop_commands WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if existing is not None:
                if existing["intent_digest"] != intent_digest:
                    raise StoreError(
                        "desktop_request_conflict",
                        "同一 Desktop request_id 使用了不同正文",
                        status=409,
                    )
                return self._command_row(existing), False
            if self._count(connection, "desktop_commands") >= MAX_COMMANDS:
                self._prune_terminal_commands(connection)
            if self._count(connection, "desktop_commands") >= MAX_COMMANDS:
                raise StoreError("desktop_command_capacity", "Desktop 命令容量已满", status=507)
            connection.execute(
                "INSERT INTO desktop_commands("
                "request_id,intent_digest,body_digest,runner_id,host_ref,thread_ref,action,state,error_code,"
                "command_json,receipt_json,created_at,updated_at,relay_delivered_at,runner_received_at,mac_confirmed_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,NULL)",
                (
                    request_id,
                    intent_digest,
                    command["body_digest"],
                    command["runner_id"],
                    command["host_ref"],
                    command["thread_ref"],
                    command["action"],
                    "pending",
                    None,
                    canonical_json(command),
                    None,
                    command["created_at"],
                    command["created_at"],
                ),
            )
            return self._command_row(
                connection.execute(
                    "SELECT * FROM desktop_commands WHERE request_id=?",
                    (request_id,),
                ).fetchone()
            ), True

    def replay_command(self, request_id: str, *, intent_digest: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM desktop_commands WHERE request_id=?",
                (request_id,),
            ).fetchone()
        if row is None:
            return None
        if row["intent_digest"] != intent_digest:
            raise StoreError(
                "desktop_request_conflict",
                "同一 Desktop request_id 使用了不同正文",
                status=409,
            )
        return self._command_row(row)

    def active_command(self, thread_ref: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM desktop_commands WHERE thread_ref=? "
                "AND state IN ('pending','submitted','accepted','unknown') "
                "ORDER BY created_at DESC LIMIT 1",
                (thread_ref,),
            ).fetchone()
        return None if row is None else self._command_row(row)

    def mark_command(
        self,
        request_id: str,
        *,
        state: str,
        error_code: str | None,
        updated_at: str,
    ) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            current = connection.execute(
                "SELECT * FROM desktop_commands WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if current is None:
                raise StoreError("desktop_command_unknown", "Desktop 命令不存在", status=404)
            if current["state"] == "pending":
                connection.execute(
                    "UPDATE desktop_commands SET state=?,error_code=?,updated_at=?,"
                    "relay_delivered_at=CASE WHEN ?='submitted' THEN COALESCE(relay_delivered_at,?) ELSE relay_delivered_at END "
                    "WHERE request_id=? AND state='pending'",
                    (state, error_code, updated_at, state, updated_at, request_id),
                )
            elif state == "submitted":
                connection.execute(
                    "UPDATE desktop_commands SET relay_delivered_at=COALESCE(relay_delivered_at,?) "
                    "WHERE request_id=?",
                    (updated_at, request_id),
                )
            return self._command_row(
                connection.execute(
                    "SELECT * FROM desktop_commands WHERE request_id=?",
                    (request_id,),
                ).fetchone()
            )

    def ingest_receipt(self, document: Mapping[str, Any]) -> dict[str, Any]:
        request_id = str(document["request_id"])
        digest = str(document["body_digest"])
        with self._lock, self._connect() as connection:
            duplicate = connection.execute(
                "SELECT request_id FROM desktop_receipts WHERE body_digest=?",
                (digest,),
            ).fetchone()
            if duplicate is not None:
                return {"status": "duplicate", "command": self._optional_command(connection, request_id)}
            command = connection.execute(
                "SELECT * FROM desktop_commands WHERE request_id=?",
                (request_id,),
            ).fetchone()
            previous_receipt = connection.execute(
                "SELECT state,document_json FROM desktop_receipts WHERE request_id=? ORDER BY id DESC LIMIT 1",
                (request_id,),
            ).fetchone()
            orphan = command is None
            state = str(document["state"])
            error_code = document.get("error_code")
            if previous_receipt is not None:
                previous_state = str(previous_receipt["state"])
                if previous_state != "accepted" or state == "accepted":
                    raise StoreError(
                        "desktop_receipt_conflict",
                        "同一 Desktop request_id 收据阶段冲突",
                        status=409,
                    )
            if command is not None:
                if (
                    command["runner_id"] != document["runner_id"]
                    or command["host_ref"] != document["host_ref"]
                    or command["thread_ref"] != document["thread_ref"]
                    or command["action"] != document["action"]
                ):
                    raise StoreError(
                        "desktop_receipt_binding_conflict",
                        "Desktop receipt 与命令绑定不一致",
                        status=409,
                    )
                command_document = json.loads(command["command_json"])
                expected_queue_ref = command_document.get("queue_ref")
                receipt_queue_ref = document.get("queue_ref")
                if (
                    command["action"] in {"queue_update", "queue_delete", "queue_start"}
                    and receipt_queue_ref != expected_queue_ref
                ):
                    raise StoreError(
                        "desktop_receipt_binding_conflict",
                        "Desktop 排队收据与命令绑定不一致",
                        status=409,
                    )
                if int(document["thread_revision"]) < int(
                    command_document["expected_thread_revision"]
                ):
                    raise StoreError(
                        "desktop_receipt_revision_stale",
                        "Desktop receipt revision 早于命令前提",
                        status=409,
                    )
            else:
                state = "recovery_required"
                error_code = "desktop_command_unknown"
            connection.execute(
                "INSERT INTO desktop_receipts("
                "request_id,runner_id,host_ref,thread_ref,action,state,error_code,thread_revision,turn_ref,"
                "body_digest,document_json,orphan,created_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    request_id,
                    document["runner_id"],
                    document["host_ref"],
                    document["thread_ref"],
                    document["action"],
                    state,
                    error_code,
                    document["thread_revision"],
                    document.get("turn_ref"),
                    digest,
                    canonical_json(document),
                    1 if orphan else 0,
                    document["created_at"],
                ),
            )
            if command is not None:
                connection.execute(
                    "UPDATE desktop_commands SET state=?,error_code=?,receipt_json=?,updated_at=?,"
                    "runner_received_at=CASE WHEN ?='accepted' THEN COALESCE(runner_received_at,?) ELSE runner_received_at END,"
                    "mac_confirmed_at=CASE WHEN ?='confirmed' THEN COALESCE(mac_confirmed_at,?) ELSE mac_confirmed_at END "
                    "WHERE request_id=?",
                    (
                        state,
                        error_code,
                        canonical_json(document),
                        document["created_at"],
                        state,
                        document["created_at"],
                        state,
                        document["created_at"],
                        request_id,
                    ),
                )
                if state in {"unknown", "recovery_required"}:
                    connection.execute(
                        "UPDATE desktop_threads SET status='recovery_required',control_state='recovery_required',"
                        "observed_at=? WHERE thread_ref=? AND host_ref=? AND runner_id=?",
                        (
                            document["created_at"],
                            document["thread_ref"],
                            document["host_ref"],
                            document["runner_id"],
                        ),
                    )
                elif state in {"confirmed", "conflict"}:
                    connection.execute(
                        "UPDATE desktop_threads SET control_state='refresh_required',observed_at=? "
                        "WHERE thread_ref=? AND host_ref=? AND runner_id=?",
                        (
                            document["created_at"],
                            document["thread_ref"],
                            document["host_ref"],
                            document["runner_id"],
                        ),
                    )
            else:
                connection.execute(
                    "UPDATE desktop_threads SET status='recovery_required',control_state='recovery_required',"
                    "observed_at=? WHERE thread_ref=? AND host_ref=? AND runner_id=?",
                    (
                        document["created_at"],
                        document["thread_ref"],
                        document["host_ref"],
                        document["runner_id"],
                    ),
                )
            self._prune_receipts(connection)
            return {
                "status": "stored",
                "orphan": orphan,
                "command": self._optional_command(connection, request_id),
            }

    def list_hosts(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT host_ref,state,control_enabled,document_json,synced_at,updated_at "
                "FROM desktop_hosts ORDER BY host_ref"
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            document = json.loads(row["document_json"])
            result.append(
                {
                    "host_ref": row["host_ref"],
                    "state": row["state"],
                    "control_enabled": bool(row["control_enabled"]),
                    "app_version": document.get("app_version"),
                    "app_build": document.get("app_build"),
                    "cli_version": document.get("cli_version"),
                    "schema_digest": document.get("schema_digest"),
                    "capabilities": list(document.get("capabilities") or []),
                    "models": list(document.get("models") or []),
                    "synced_at": row["synced_at"],
                    "updated_at": row["updated_at"],
                }
            )
        return result

    def host_runner_id(self, host_ref: str) -> str:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT runner_id FROM desktop_hosts WHERE host_ref=?",
                (host_ref,),
            ).fetchone()
        if row is None:
            raise StoreError("desktop_host_not_found", "Desktop host 不存在", status=404)
        return str(row["runner_id"])

    def list_projects(self, *, host_ref: str | None = None) -> list[dict[str, Any]]:
        parameters: list[Any] = []
        condition = ""
        if host_ref is not None:
            condition = "WHERE p.host_ref=?"
            parameters.append(host_ref)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT p.host_ref,p.project_ref,p.project_alias,p.updated_at,"
                "COUNT(t.thread_ref) AS total,"
                "SUM(CASE WHEN t.status='active' THEN 1 ELSE 0 END) AS active,"
                "SUM(CASE WHEN t.status='idle' THEN 1 ELSE 0 END) AS idle,"
                "SUM(CASE WHEN t.status='notLoaded' THEN 1 ELSE 0 END) AS not_loaded,"
                "SUM(CASE WHEN t.status='archived' THEN 1 ELSE 0 END) AS archived,"
                "SUM(CASE WHEN t.status='failed' THEN 1 ELSE 0 END) AS failed,"
                "SUM(CASE WHEN t.status IN ('recovery_required','protocol_degraded') THEN 1 ELSE 0 END) AS recovery "
                "FROM desktop_projects p LEFT JOIN desktop_threads t "
                "ON t.host_ref=p.host_ref AND t.project_ref=p.project_ref "
                f"{condition} GROUP BY p.host_ref,p.project_ref,p.project_alias,p.updated_at "
                "ORDER BY p.project_alias,p.project_ref",
                parameters,
            ).fetchall()
        return [
            {
                "host_ref": row["host_ref"],
                "project_ref": row["project_ref"],
                "project_alias": row["project_alias"],
                "updated_at": row["updated_at"],
                "counts": {
                    "total": int(row["total"] or 0),
                    "active": int(row["active"] or 0),
                    "idle": int(row["idle"] or 0),
                    "notLoaded": int(row["not_loaded"] or 0),
                    "archived": int(row["archived"] or 0),
                    "failed": int(row["failed"] or 0),
                    "recovery_required": int(row["recovery"] or 0),
                },
            }
            for row in rows
        ]

    def list_threads(
        self,
        *,
        host_ref: str | None = None,
        project_ref: str | None = None,
        status: str | None = None,
        after_cursor: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        conditions = ["id>?"]
        parameters: list[Any] = [after_cursor]
        for column, value in (("host_ref", host_ref), ("project_ref", project_ref), ("status", status)):
            if value is not None:
                conditions.append(f"{column}=?")
                parameters.append(value)
        parameters.append(limit + 1)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM desktop_threads WHERE " + " AND ".join(conditions) + " ORDER BY id LIMIT ?",
                parameters,
            ).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        return {
            "threads": [self._thread_public(row) for row in rows],
            "next_cursor": int(rows[-1]["id"]) if rows else after_cursor,
            "has_more": has_more,
        }

    def thread(self, thread_ref: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM desktop_threads WHERE thread_ref=?",
                (thread_ref,),
            ).fetchone()
            if row is None:
                raise StoreError("desktop_thread_not_found", "Desktop Thread 不存在", status=404)
            command = connection.execute(
                "SELECT * FROM desktop_commands WHERE thread_ref=? ORDER BY created_at DESC LIMIT 1",
                (thread_ref,),
            ).fetchone()
        result = self._thread_public(row, include_snapshot=True)
        result["latest_command"] = None if command is None else self._command_row(command)
        return result

    def events(self, thread_ref: str, *, after_cursor: int, limit: int) -> dict[str, Any]:
        with self._connect() as connection:
            if connection.execute(
                "SELECT 1 FROM desktop_threads WHERE thread_ref=?",
                (thread_ref,),
            ).fetchone() is None:
                raise StoreError("desktop_thread_not_found", "Desktop Thread 不存在", status=404)
            rows = connection.execute(
                "SELECT cursor,document_json FROM desktop_events "
                "WHERE thread_ref=? AND cursor>? ORDER BY cursor LIMIT ?",
                (thread_ref, after_cursor, limit + 1),
            ).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        events = []
        for row in rows:
            document = json.loads(row["document_json"])
            document.pop("runner_id", None)
            events.append({"cursor": int(row["cursor"]), **document})
        return {
            "events": events,
            "next_cursor": int(rows[-1]["cursor"]) if rows else after_cursor,
            "has_more": has_more,
        }

    def host_events(self, host_ref: str, *, after_cursor: int, limit: int) -> dict[str, Any]:
        with self._connect() as connection:
            if connection.execute(
                "SELECT 1 FROM desktop_hosts WHERE host_ref=?",
                (host_ref,),
            ).fetchone() is None:
                raise StoreError("desktop_host_not_found", "Desktop host 不存在", status=404)
            rows = connection.execute(
                "SELECT cursor,document_json FROM desktop_events "
                "WHERE host_ref=? AND cursor>? ORDER BY cursor LIMIT ?",
                (host_ref, after_cursor, limit + 1),
            ).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        events = []
        for row in rows:
            document = json.loads(row["document_json"])
            document.pop("runner_id", None)
            events.append({"cursor": int(row["cursor"]), **document})
        return {
            "events": events,
            "next_cursor": int(rows[-1]["cursor"]) if rows else after_cursor,
            "has_more": has_more,
        }

    def event_pruned_through(self, *, scope_kind: str, scope_ref: str) -> int:
        """Return the durable scope cursor high-water that can no longer be replayed."""
        if scope_kind not in {"host", "thread"}:
            raise ValueError("Desktop event watermark scope invalid")
        with self._connect() as connection:
            if scope_kind == "host":
                row = connection.execute(
                    "SELECT pruned_through FROM desktop_event_watermarks WHERE host_ref=?",
                    (scope_ref,),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT pruned_through FROM desktop_thread_event_watermarks "
                    "WHERE thread_ref=?",
                    (scope_ref,),
                ).fetchone()
        return int(row["pruned_through"]) if row is not None else 0

    def command(self, request_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM desktop_commands WHERE request_id=?",
                (request_id,),
            ).fetchone()
        if row is None:
            raise StoreError("desktop_command_unknown", "Desktop 命令不存在", status=404)
        return self._command_row(row)

    def runner_id(self, thread_ref: str) -> str:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT runner_id FROM desktop_threads WHERE thread_ref=?",
                (thread_ref,),
            ).fetchone()
        if row is None:
            raise StoreError("desktop_thread_not_found", "Desktop Thread 不存在", status=404)
        return str(row["runner_id"])

    def sweep_commands(self, *, now: str) -> int:
        current = dt.datetime.fromisoformat(now.replace("Z", "+00:00"))
        if current.tzinfo is None or current.utcoffset() is None:
            raise StoreError("desktop_clock_invalid", "Desktop sweep 时间必须包含时区", status=500)
        expired: list[str] = []
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT request_id,command_json FROM desktop_commands WHERE state IN ('submitted','accepted')"
            ).fetchall()
            for row in rows:
                try:
                    expires_at = dt.datetime.fromisoformat(
                        str(json.loads(row["command_json"])["expires_at"]).replace("Z", "+00:00")
                    )
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    expires_at = current
                if expires_at.tzinfo is None or expires_at.utcoffset() is None or expires_at <= current:
                    expired.append(str(row["request_id"]))
            if expired:
                connection.executemany(
                    "UPDATE desktop_commands SET state='unknown',error_code='desktop_receipt_timeout',updated_at=? "
                    "WHERE request_id=? AND state IN ('submitted','accepted')",
                    ((now, request_id) for request_id in expired),
                )
        return len(expired)

    def _migrate(self) -> None:
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS desktop_hosts(
                    host_ref TEXT PRIMARY KEY,
                    runner_id TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    control_enabled INTEGER NOT NULL,
                    document_json TEXT NOT NULL,
                    synced_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS desktop_projects(
                    host_ref TEXT NOT NULL,
                    project_ref TEXT NOT NULL,
                    project_alias TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(host_ref,project_ref)
                );
                CREATE TABLE IF NOT EXISTS desktop_threads(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    thread_ref TEXT NOT NULL UNIQUE,
                    host_ref TEXT NOT NULL,
                    project_ref TEXT NOT NULL,
                    runner_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL,
                    active_turn_ref TEXT,
                    thread_revision INTEGER NOT NULL,
                    control_revision INTEGER,
                    snapshot_sequence INTEGER,
                    control_state TEXT NOT NULL,
                    snapshot_digest TEXT,
                    snapshot_json TEXT NOT NULL,
                    source_created_at TEXT,
                    source_updated_at TEXT,
                    observed_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS desktop_threads_filters
                    ON desktop_threads(host_ref,project_ref,status,id);
                CREATE TABLE IF NOT EXISTS desktop_snapshots(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    thread_ref TEXT NOT NULL,
                    thread_revision INTEGER NOT NULL,
                    body_digest TEXT NOT NULL UNIQUE,
                    document_json TEXT NOT NULL,
                    source_sequence INTEGER,
                    observed_at TEXT NOT NULL,
                    UNIQUE(thread_ref,thread_revision)
                );
                CREATE TABLE IF NOT EXISTS desktop_events(
                    cursor INTEGER PRIMARY KEY AUTOINCREMENT,
                    host_ref TEXT NOT NULL,
                    project_ref TEXT NOT NULL,
                    thread_ref TEXT NOT NULL,
                    turn_ref TEXT,
                    event_sequence INTEGER NOT NULL,
                    event_kind TEXT NOT NULL,
                    source TEXT NOT NULL,
                    thread_revision INTEGER NOT NULL,
                    body_digest TEXT NOT NULL UNIQUE,
                    document_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(host_ref,event_sequence)
                );
                CREATE INDEX IF NOT EXISTS desktop_events_thread_cursor
                    ON desktop_events(thread_ref,cursor);
                CREATE TABLE IF NOT EXISTS desktop_event_watermarks(
                    host_ref TEXT PRIMARY KEY,
                    pruned_through INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS desktop_thread_event_watermarks(
                    thread_ref TEXT PRIMARY KEY,
                    pruned_through INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS desktop_commands(
                    request_id TEXT PRIMARY KEY,
                    intent_digest TEXT NOT NULL,
                    body_digest TEXT NOT NULL UNIQUE,
                    runner_id TEXT NOT NULL,
                    host_ref TEXT NOT NULL,
                    thread_ref TEXT NOT NULL,
                    action TEXT NOT NULL,
                    state TEXT NOT NULL,
                    error_code TEXT,
                    command_json TEXT NOT NULL,
                    receipt_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    relay_delivered_at TEXT,
                    runner_received_at TEXT,
                    mac_confirmed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS desktop_commands_thread_created
                    ON desktop_commands(thread_ref,created_at);
                CREATE TABLE IF NOT EXISTS desktop_receipts(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT NOT NULL,
                    runner_id TEXT NOT NULL,
                    host_ref TEXT NOT NULL,
                    thread_ref TEXT NOT NULL,
                    action TEXT NOT NULL,
                    state TEXT NOT NULL,
                    error_code TEXT,
                    thread_revision INTEGER NOT NULL,
                    turn_ref TEXT,
                    body_digest TEXT NOT NULL UNIQUE,
                    document_json TEXT NOT NULL,
                    orphan INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            # Older databases only recorded a host-wide prune watermark. Backfill
            # each existing Thread conservatively once so a reconnect never treats
            # missing, already-pruned history as replayable after an upgrade.
            connection.execute(
                "INSERT OR IGNORE INTO desktop_thread_event_watermarks(thread_ref,pruned_through) "
                "SELECT thread_ref,desktop_event_watermarks.pruned_through "
                "FROM desktop_threads JOIN desktop_event_watermarks USING(host_ref)"
            )
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(desktop_threads)").fetchall()
            }
            if "control_revision" not in columns:
                connection.execute("ALTER TABLE desktop_threads ADD COLUMN control_revision INTEGER")
            if "snapshot_sequence" not in columns:
                connection.execute("ALTER TABLE desktop_threads ADD COLUMN snapshot_sequence INTEGER")
            snapshot_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(desktop_snapshots)").fetchall()
            }
            if "source_sequence" not in snapshot_columns:
                connection.execute("ALTER TABLE desktop_snapshots ADD COLUMN source_sequence INTEGER")
            command_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(desktop_commands)").fetchall()
            }
            for name in ("relay_delivered_at", "runner_received_at", "mac_confirmed_at"):
                if name not in command_columns:
                    connection.execute(f"ALTER TABLE desktop_commands ADD COLUMN {name} TEXT")
            connection.execute("DROP INDEX IF EXISTS desktop_receipts_request")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS desktop_receipts_request_created "
                "ON desktop_receipts(request_id,id)"
            )

    def _recover_pending_commands(self) -> None:
        current = dt.datetime.now(dt.timezone.utc).astimezone(dt.timezone(dt.timedelta(hours=8))).isoformat()
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE desktop_commands SET state='unknown',error_code='controller_restarted_before_delivery',"
                "updated_at=? WHERE state='pending'",
                (current,),
            )

    def _bind_host(
        self,
        connection: sqlite3.Connection,
        host_ref: str,
        runner_id: str,
        host: Mapping[str, Any],
        encoded_host: str,
        observed_at: str,
    ) -> None:
        existing = connection.execute(
            "SELECT runner_id,document_json,synced_at FROM desktop_hosts WHERE host_ref=?",
            (host_ref,),
        ).fetchone()
        runner_binding = connection.execute(
            "SELECT host_ref FROM desktop_hosts WHERE runner_id=?",
            (runner_id,),
        ).fetchone()
        if existing is not None and existing["runner_id"] != runner_id:
            raise StoreError("desktop_host_binding_conflict", "Desktop host ref 绑定了其他 Runner", status=409)
        if runner_binding is not None and runner_binding["host_ref"] != host_ref:
            raise StoreError("desktop_host_binding_conflict", "Desktop Runner 绑定了其他 host ref", status=409)
        if existing is None and self._count(connection, "desktop_hosts") >= MAX_HOSTS:
            raise StoreError("desktop_host_capacity", "Desktop host 容量已满", status=507)
        if host:
            state = str(host.get("state") or "unavailable")[:64]
            control_enabled = 1 if host.get("control_enabled") is True else 0
            synced_at = str(host.get("synced_at") or observed_at)
            document_json = encoded_host
        elif existing is not None:
            state = str(json.loads(existing["document_json"]).get("state") or "unavailable")[:64]
            control_enabled = 0
            synced_at = str(existing["synced_at"])
            document_json = str(existing["document_json"])
        else:
            state = "unavailable"
            control_enabled = 0
            synced_at = observed_at
            document_json = canonical_json(
                {
                    "host_ref": host_ref,
                    "state": state,
                    "capabilities": [],
                    "control_enabled": False,
                    "synced_at": synced_at,
                }
            )
        if existing is None:
            connection.execute(
                "INSERT INTO desktop_hosts(host_ref,runner_id,state,control_enabled,document_json,synced_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (host_ref, runner_id, state, control_enabled, document_json, synced_at, observed_at),
            )
        elif host:
            connection.execute(
                "UPDATE desktop_hosts SET state=?,control_enabled=?,document_json=?,synced_at=?,updated_at=? "
                "WHERE host_ref=?",
                (state, control_enabled, document_json, synced_at, observed_at, host_ref),
            )
        else:
            connection.execute(
                "UPDATE desktop_hosts SET updated_at=? WHERE host_ref=?",
                (observed_at, host_ref),
            )

    def _upsert_project(
        self,
        connection: sqlite3.Connection,
        host_ref: str,
        project_ref: str,
        alias: str,
        updated_at: str,
    ) -> None:
        existing = connection.execute(
            "SELECT project_alias FROM desktop_projects WHERE host_ref=? AND project_ref=?",
            (host_ref, project_ref),
        ).fetchone()
        if existing is None and self._count(connection, "desktop_projects") >= MAX_PROJECTS:
            raise StoreError("desktop_project_capacity", "Desktop project 容量已满", status=507)
        effective_alias = alias
        if existing is not None and alias == "未同步项目":
            effective_alias = str(existing["project_alias"])
        connection.execute(
            "INSERT INTO desktop_projects(host_ref,project_ref,project_alias,updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(host_ref,project_ref) DO UPDATE SET project_alias=excluded.project_alias,updated_at=excluded.updated_at",
            (host_ref, project_ref, effective_alias, updated_at),
        )

    @staticmethod
    def _count(connection: sqlite3.Connection, table: str) -> int:
        return int(connection.execute(f"SELECT COUNT(*) AS value FROM {table}").fetchone()["value"])

    @staticmethod
    def _prune_snapshots(connection: sqlite3.Connection, thread_ref: str) -> None:
        connection.execute(
            "DELETE FROM desktop_snapshots WHERE thread_ref=? AND id NOT IN ("
            "SELECT id FROM desktop_snapshots WHERE thread_ref=? ORDER BY id DESC LIMIT ?)",
            (thread_ref, thread_ref, SNAPSHOTS_PER_THREAD),
        )

    @staticmethod
    def _prune_events(connection: sqlite3.Connection, host_ref: str) -> None:
        pruned_threads = connection.execute(
            "SELECT thread_ref,MAX(cursor) AS value FROM desktop_events "
            "WHERE host_ref=? AND cursor NOT IN ("
            "SELECT cursor FROM desktop_events WHERE host_ref=? ORDER BY cursor DESC LIMIT ?) "
            "GROUP BY thread_ref",
            (host_ref, host_ref, EVENTS_PER_HOST),
        ).fetchall()
        for row in pruned_threads:
            connection.execute(
                "INSERT INTO desktop_thread_event_watermarks(thread_ref,pruned_through) "
                "VALUES(?,?) ON CONFLICT(thread_ref) DO UPDATE SET pruned_through="
                "MAX(desktop_thread_event_watermarks.pruned_through,excluded.pruned_through)",
                (str(row["thread_ref"]), int(row["value"])),
            )
        pruned = connection.execute(
            "SELECT MAX(cursor) AS value FROM desktop_events WHERE host_ref=? AND cursor NOT IN ("
            "SELECT cursor FROM desktop_events WHERE host_ref=? ORDER BY cursor DESC LIMIT ?)",
            (host_ref, host_ref, EVENTS_PER_HOST),
        ).fetchone()["value"]
        if pruned is not None:
            connection.execute(
                "INSERT INTO desktop_event_watermarks(host_ref,pruned_through) VALUES(?,?) "
                "ON CONFLICT(host_ref) DO UPDATE SET pruned_through="
                "MAX(desktop_event_watermarks.pruned_through,excluded.pruned_through)",
                (host_ref, int(pruned)),
            )
        connection.execute(
            "DELETE FROM desktop_events WHERE host_ref=? AND cursor NOT IN ("
            "SELECT cursor FROM desktop_events WHERE host_ref=? ORDER BY cursor DESC LIMIT ?)",
            (host_ref, host_ref, EVENTS_PER_HOST),
        )

    @staticmethod
    def _prune_receipts(connection: sqlite3.Connection) -> None:
        connection.execute(
            "DELETE FROM desktop_receipts WHERE id NOT IN (SELECT id FROM desktop_receipts ORDER BY id DESC LIMIT ?)",
            (MAX_RECEIPTS,),
        )

    @staticmethod
    def _prune_terminal_commands(connection: sqlite3.Connection) -> None:
        over = int(connection.execute("SELECT COUNT(*) AS value FROM desktop_commands").fetchone()["value"]) - MAX_COMMANDS + 1
        if over <= 0:
            return
        connection.execute(
            "DELETE FROM desktop_commands WHERE request_id IN ("
            "SELECT request_id FROM desktop_commands WHERE state NOT IN ('pending','submitted','accepted','unknown') "
            "ORDER BY updated_at LIMIT ?)",
            (over,),
        )

    def _thread_row(self, connection: sqlite3.Connection, thread_ref: str) -> dict[str, Any]:
        row = connection.execute(
            "SELECT * FROM desktop_threads WHERE thread_ref=?",
            (thread_ref,),
        ).fetchone()
        if row is None:
            raise StoreError("desktop_thread_not_found", "Desktop Thread 不存在", status=404)
        return self._thread_public(row)

    @staticmethod
    def _thread_public(row: sqlite3.Row, *, include_snapshot: bool = False) -> dict[str, Any]:
        result: dict[str, Any] = {
            "cursor": int(row["id"]),
            "host_ref": row["host_ref"],
            "project_ref": row["project_ref"],
            "thread_ref": row["thread_ref"],
            "title": row["title"],
            "status": row["status"],
            "active_turn_ref": row["active_turn_ref"],
            "thread_revision": int(row["thread_revision"]),
            "snapshot_sequence": row["snapshot_sequence"],
            "control_revision": row["control_revision"],
            "control_state": row["control_state"],
            "created_at": row["source_created_at"],
            "updated_at": row["source_updated_at"],
            "observed_at": row["observed_at"],
        }
        if include_snapshot:
            result["snapshot"] = json.loads(row["snapshot_json"])
        return result

    @staticmethod
    def _command_row(row: sqlite3.Row) -> dict[str, Any]:
        command = json.loads(row["command_json"])
        receipt = None if row["receipt_json"] is None else json.loads(row["receipt_json"])
        if isinstance(receipt, dict):
            receipt.pop("runner_id", None)
        return {
            "request_id": row["request_id"],
            "host_ref": row["host_ref"],
            "thread_ref": row["thread_ref"],
            "action": row["action"],
            "mode": command.get("mode"),
            "model": command.get("model"),
            "effort": command.get("effort"),
            "queue_ref": command.get("queue_ref"),
            "queue_refs": command.get("queue_refs"),
            "expected_turn_ref": command.get("expected_turn_ref"),
            "expected_thread_revision": command.get("expected_thread_revision"),
            "expected_control_revision": command.get("expected_control_revision"),
            "state": row["state"],
            "error_code": row["error_code"],
            "created_at": row["created_at"],
            "expires_at": command.get("expires_at"),
            "updated_at": row["updated_at"],
            "receipt": receipt,
            "delivery_stage": _delivery_stage(row),
            "stage_timestamps": {
                "controller_received": row["created_at"],
                "relay_delivered": row["relay_delivered_at"],
                "runner_received": row["runner_received_at"],
                "mac_confirmed": row["mac_confirmed_at"],
            },
            "recovery_required": row["state"] in {"unknown", "recovery_required"},
        }

    def _optional_command(self, connection: sqlite3.Connection, request_id: str) -> dict[str, Any] | None:
        row = connection.execute(
            "SELECT * FROM desktop_commands WHERE request_id=?",
            (request_id,),
        ).fetchone()
        return None if row is None else self._command_row(row)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection


def _same_revision_refresh(existing_json: str, incoming: Mapping[str, Any]) -> str | None:
    """Classify exact availability or monotonic IPC-control refreshes."""

    try:
        existing = json.loads(existing_json)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(existing, dict):
        return None
    changed = {
        field
        for field in set(existing) | set(incoming)
        if existing.get(field) != incoming.get(field)
    }
    if not changed <= SAME_REVISION_CONTROL_FIELDS:
        return None
    if not changed:
        return "refreshed"
    incoming_control_revision = incoming.get("control_revision")
    existing_control_revision = existing.get("control_revision")
    if incoming_control_revision is None:
        if (
            "control_revision" in existing
            and "control_revision" in incoming
            and existing_control_revision is None
            and changed == {"status", "control_state"}
            and {existing.get("status"), incoming.get("status")}
            == {"archived", "notLoaded"}
            and existing.get("control_state") in NON_WRITABLE_CONTROL_STATES
            and incoming.get("control_state") in NON_WRITABLE_CONTROL_STATES
        ):
            return "refreshed"
        if (
            "control_revision" in existing
            and "control_revision" in incoming
            and existing_control_revision is None
            and changed == {"control_state"}
            and existing.get("control_state") in NON_WRITABLE_CONTROL_STATES
            and incoming.get("control_state") in NON_WRITABLE_CONTROL_STATES
        ):
            return "refreshed"
        if (
            "control_revision" in incoming
            and isinstance(existing_control_revision, int)
            and not isinstance(existing_control_revision, bool)
            and existing_control_revision >= 0
            and incoming.get("control_state") in SAFETY_DEGRADED_CONTROL_STATES
        ):
            return "degraded_latched"
        return None
    if (
        not isinstance(incoming_control_revision, int)
        or isinstance(incoming_control_revision, bool)
        or incoming_control_revision < 0
    ):
        return None
    if existing_control_revision is None:
        return "refreshed"
    if (
        not isinstance(existing_control_revision, int)
        or isinstance(existing_control_revision, bool)
        or existing_control_revision < 0
    ):
        return None
    if incoming_control_revision < existing_control_revision:
        return "stale_ignored"
    if incoming_control_revision == existing_control_revision:
        return None
    return "refreshed"


def _changed_snapshot_fields(existing_json: str, incoming: Mapping[str, Any]) -> set[str]:
    try:
        existing = json.loads(existing_json)
    except (TypeError, json.JSONDecodeError):
        return set()
    if not isinstance(existing, dict):
        return set()
    return _mapping_changes(existing, incoming)


def _mapping_changes(existing: Mapping[str, Any], incoming: Mapping[str, Any]) -> set[str]:
    return {
        field
        for field in set(existing) | set(incoming)
        if field not in existing or field not in incoming or existing[field] != incoming[field]
    }


def _legacy_sequence_refresh(
    existing_json: str,
    incoming: Mapping[str, Any],
    host: Mapping[str, Any],
) -> str | None:
    try:
        existing = json.loads(existing_json)
    except (TypeError, json.JSONDecodeError):
        return None
    capabilities = host.get("capabilities")
    if (
        not isinstance(existing, dict)
        or not isinstance(capabilities, list)
        or "thread_queue_v1" not in capabilities
        or "reasoning_effort_v1" not in capabilities
        or any(field in existing for field in LEGACY_SEQUENCE_ENRICHMENT_FIELDS)
        or set(incoming) != set(existing) | LEGACY_SEQUENCE_ENRICHMENT_FIELDS
    ):
        return None
    enriched = dict(existing)
    for field in LEGACY_SEQUENCE_ENRICHMENT_FIELDS:
        enriched[field] = incoming[field]
    return _same_revision_refresh(canonical_json(enriched), incoming)


def _delivery_stage(row: sqlite3.Row) -> str:
    if row["mac_confirmed_at"] is not None:
        return "mac_confirmed"
    if row["runner_received_at"] is not None:
        return "runner_received"
    if row["relay_delivered_at"] is not None:
        return "relay_delivered"
    return "controller_received"


__all__ = [
    "DesktopStore",
    "EVENTS_PER_HOST",
    "MAX_COMMANDS",
    "MAX_HOSTS",
    "MAX_PROJECTS",
    "MAX_RECEIPTS",
    "MAX_THREADS",
    "SNAPSHOTS_PER_THREAD",
]
