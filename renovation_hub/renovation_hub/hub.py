"""Project, stage, area and timeline domain for Renovation Hub."""

from __future__ import annotations

from datetime import datetime, timezone
import sqlite3
from typing import Any
import uuid

from .ledger import (
    LedgerError,
    LedgerStore,
    _idempotency_key,
    _positive_cents,
    _classification_code,
    CLASSIFICATION_FIELDS,
    _text,
    _validate_date,
    canonical_json,
    digest_json,
    normalize_grouped_tags,
    normalize_tags,
    utc_now,
)


HUB_SCHEMA_VERSION = 2
PROJECT_STATUSES = {"active", "completed", "archived"}
STAGE_STATUSES = {"planned", "active", "completed", "archived"}
AREA_STATUSES = {"active", "archived"}
EVENT_TYPES = {"progress", "note", "decision", "inspection", "milestone"}
EVENT_STATUSES = {"active", "voided"}
MUTATION_TARGET_TYPES = {"project", "stage", "area", "event", "transaction"}
MUTATION_MAX_TARGETS = 1000


def _choice(value: Any, field: str, allowed: set[str], *, default: str | None = None) -> str:
    if value is None and default is not None:
        return default
    if not isinstance(value, str) or value not in allowed:
        raise LedgerError("invalid_input", f"{field} 不在允许范围")
    return value


def _non_negative_cents(value: Any, field: str = "budget_cents") -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 100_000_000_000:
        raise LedgerError("invalid_input", f"{field} 必须是非负整数分")
    return value


def _position(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 10000:
        raise LedgerError("invalid_input", "position 必须是 0 到 10000 的整数")
    return value


def _version(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise LedgerError("version_required", "version 必须是正整数", status=409)
    return value


def _optional_date(value: Any, field: str) -> str | None:
    if value in {None, ""}:
        return None
    return _validate_date(value, field)


def _datetime(value: Any, field: str = "occurred_at") -> str:
    if not isinstance(value, str):
        raise LedgerError("invalid_datetime", f"{field} 必须是带时区的 ISO 8601")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LedgerError("invalid_datetime", f"{field} 必须是带时区的 ISO 8601") from exc
    if parsed.tzinfo is None:
        raise LedgerError("invalid_datetime", f"{field} 必须包含时区")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class RenovationHubStore(LedgerStore):
    """Ledger-compatible store extended with renovation project records."""

    def _initialize(self) -> None:
        super()._initialize()
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    timezone TEXT NOT NULL,
                    budget_cents INTEGER NOT NULL DEFAULT 0 CHECK(budget_cents >= 0),
                    status TEXT NOT NULL CHECK(status IN ('active','completed','archived')),
                    version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS stages (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(id),
                    name TEXT NOT NULL,
                    position INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL CHECK(status IN ('planned','active','completed','archived')),
                    color TEXT NOT NULL DEFAULT '#8B5CF6',
                    planned_start TEXT,
                    planned_end TEXT,
                    actual_start TEXT,
                    actual_end TEXT,
                    version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(project_id, name)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS stages_one_active_per_project
                    ON stages(project_id) WHERE status='active';
                CREATE TABLE IF NOT EXISTS areas (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(id),
                    name TEXT NOT NULL,
                    position INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL CHECK(status IN ('active','archived')),
                    version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(project_id, name)
                );
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(id),
                    stage_id TEXT REFERENCES stages(id),
                    area_id TEXT REFERENCES areas(id),
                    event_type TEXT NOT NULL CHECK(event_type IN ('progress','note','decision','inspection','milestone')),
                    title TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    occurred_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','voided')),
                    source_ref TEXT NOT NULL DEFAULT '',
                    version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_project_time ON events(project_id, occurred_at DESC);
                CREATE INDEX IF NOT EXISTS events_stage_time ON events(stage_id, occurred_at DESC);
                CREATE INDEX IF NOT EXISTS events_area_time ON events(area_id, occurred_at DESC);
                CREATE TABLE IF NOT EXISTS transaction_context (
                    transaction_id TEXT PRIMARY KEY REFERENCES transactions(id),
                    project_id TEXT NOT NULL REFERENCES projects(id),
                    stage_id TEXT REFERENCES stages(id),
                    area_id TEXT REFERENCES areas(id),
                    version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS payment_plans (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(id),
                    name TEXT NOT NULL,
                    total_amount_cents INTEGER NOT NULL CHECK(total_amount_cents > 0),
                    version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(project_id,name)
                );
                CREATE TABLE IF NOT EXISTS payment_plan_nodes (
                    id TEXT PRIMARY KEY,
                    payment_plan_id TEXT NOT NULL REFERENCES payment_plans(id),
                    name TEXT NOT NULL,
                    amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
                    due_on TEXT,
                    position INTEGER NOT NULL DEFAULT 0 CHECK(position >= 0),
                    version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(payment_plan_id,name)
                );
                CREATE TABLE IF NOT EXISTS payment_plan_allocations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    payment_plan_node_id TEXT NOT NULL REFERENCES payment_plan_nodes(id),
                    transaction_id TEXT NOT NULL REFERENCES transactions(id),
                    amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
                    created_at TEXT NOT NULL,
                    UNIQUE(payment_plan_node_id,transaction_id)
                );
                CREATE INDEX IF NOT EXISTS payment_plan_allocations_transaction_idx
                    ON payment_plan_allocations(transaction_id);
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO metadata(key,value) VALUES ('hub_schema_version',?)",
                (str(HUB_SCHEMA_VERSION),),
            )
        from .media import initialize_media_schema
        from .progress_capture import initialize_progress_capture_schema

        initialize_media_schema(self)
        initialize_progress_capture_schema(self)

    def status(self) -> dict[str, Any]:
        result = super().status()
        with self._connect() as connection:
            result["hub_schema_version"] = HUB_SCHEMA_VERSION
            result["counts"].update(
                {
                    "projects": connection.execute("SELECT count(*) FROM projects WHERE status!='archived'").fetchone()[0],
                    "stages": connection.execute("SELECT count(*) FROM stages WHERE status!='archived'").fetchone()[0],
                    "areas": connection.execute("SELECT count(*) FROM areas WHERE status!='archived'").fetchone()[0],
                    "events": connection.execute("SELECT count(*) FROM events WHERE status='active'").fetchone()[0],
                    "media": connection.execute("SELECT count(*) FROM media_assets WHERE processing_status='ready'").fetchone()[0],
                    "progress_captures": connection.execute(
                        "SELECT count(*) FROM progress_capture_sessions WHERE state IN ('active','paused','finalizing')"
                    ).fetchone()[0],
                }
            )
        return result

    def _domain_audit(
        self,
        connection: sqlite3.Connection,
        *,
        action: str,
        target_type: str,
        target_id: str,
        actor_hash: str,
        idempotency_key: str,
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
        reason: str = "",
    ) -> None:
        connection.execute(
            "INSERT INTO audit_log(action,target_type,target_id,actor_hash,idempotency_key,reason,before_json,after_json,result,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                action,
                target_type,
                target_id,
                actor_hash[:128],
                idempotency_key,
                reason[:500],
                canonical_json(before) if before is not None else None,
                canonical_json(after) if after is not None else None,
                "success",
                utc_now(),
            ),
        )

    def _after_transaction_insert(
        self,
        connection: sqlite3.Connection,
        transaction_id: str,
        payload: dict[str, Any],
        *,
        original_payment_id: str | None = None,
    ) -> None:
        project_id = _text(payload.get("project_id"), "project_id", 64)
        stage_id = _text(payload.get("stage_id"), "stage_id", 64) or None
        area_id = _text(payload.get("area_id"), "area_id", 64) or None
        if original_payment_id:
            inherited = connection.execute(
                "SELECT project_id,stage_id,area_id FROM transaction_context WHERE transaction_id=?",
                (original_payment_id,),
            ).fetchone()
            if inherited:
                requested = (project_id or None, stage_id, area_id)
                expected = (inherited["project_id"], inherited["stage_id"], inherited["area_id"])
                if any(value is not None for value in requested) and requested != expected:
                    raise LedgerError("refund_context_mismatch", "退款必须继承原付款的项目上下文", status=409)
                project_id = inherited["project_id"]
                stage_id = inherited["stage_id"]
                area_id = inherited["area_id"]
        if not project_id:
            return
        self._validate_context_refs(connection, project_id, stage_id, area_id)
        connection.execute(
            "INSERT INTO transaction_context(transaction_id,project_id,stage_id,area_id,version,updated_at) VALUES (?,?,?,?,1,?)",
            (transaction_id, project_id, stage_id, area_id, utc_now()),
        )

    def _after_canonical_restore(
        self,
        connection: sqlite3.Connection,
        state: dict[str, Any],
        source_sha256: str,
    ) -> None:
        project_id = str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"renovation-hub:{source_sha256}")
        )
        timestamp = max(
            (str(item["updated_at"]) for item in state["transactions"]),
            default="1970-01-01T00:00:00Z",
        )
        connection.execute(
            """
            INSERT INTO projects(
                id,name,timezone,budget_cents,status,version,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO NOTHING
            """,
            (
                project_id,
                "装修历史账本",
                "Asia/Shanghai",
                0,
                "active",
                1,
                timestamp,
                timestamp,
            ),
        )
        for item in state["transactions"]:
            connection.execute(
                """
                INSERT INTO transaction_context(
                    transaction_id,project_id,stage_id,area_id,version,updated_at
                ) VALUES (?,?,NULL,NULL,1,?)
                ON CONFLICT(transaction_id) DO NOTHING
                """,
                (str(item["id"]), project_id, timestamp),
            )

    def _validate_canonical_extensions(
        self,
        connection: sqlite3.Connection,
        state: dict[str, Any],
        source_sha256: str,
    ) -> None:
        project_id = str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"renovation-hub:{source_sha256}")
        )
        timestamp = max(
            (str(item["updated_at"]) for item in state["transactions"]),
            default="1970-01-01T00:00:00Z",
        )
        projects = [
            dict(row)
            for row in connection.execute(
                "SELECT id,name,timezone,budget_cents,status,version,created_at,updated_at FROM projects ORDER BY id"
            )
        ]
        expected_projects = [
            {
                "id": project_id,
                "name": "装修历史账本",
                "timezone": "Asia/Shanghai",
                "budget_cents": 0,
                "status": "active",
                "version": 1,
                "created_at": timestamp,
                "updated_at": timestamp,
            }
        ]
        if projects != expected_projects:
            raise LedgerError("invariant_mismatch", "影子默认项目与来源派生状态不一致")
        contexts = [
            dict(row)
            for row in connection.execute(
                "SELECT transaction_id,project_id,stage_id,area_id,version,updated_at FROM transaction_context ORDER BY CAST(transaction_id AS INTEGER)"
            )
        ]
        expected_contexts = [
            {
                "transaction_id": str(item["id"]),
                "project_id": project_id,
                "stage_id": None,
                "area_id": None,
                "version": 1,
                "updated_at": timestamp,
            }
            for item in sorted(state["transactions"], key=lambda item: int(item["id"]))
        ]
        if contexts != expected_contexts:
            raise LedgerError("invariant_mismatch", "影子账目空间上下文与来源派生状态不一致")
        for table in (
            "stages",
            "areas",
            "events",
            "media_assets",
            "media_links",
            "uploads",
            "media_ingest_results",
        ):
            if connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]:
                raise LedgerError("invariant_mismatch", f"影子派生表 {table} 包含未授权数据")

    def _validate_transaction_version(
        self,
        connection: sqlite3.Connection,
        transaction_id: str,
        payload: dict[str, Any],
    ) -> None:
        changes = payload.get("changes") or {}
        context_fields = {"project_id", "stage_id", "area_id"}
        if context_fields & set(changes) and "version" not in payload:
            raise LedgerError("version_required", "修改项目上下文必须提供 version", status=409)
        if "version" not in payload:
            return
        expected = _version(payload.get("version"))
        row = connection.execute("SELECT version FROM transaction_context WHERE transaction_id=?", (transaction_id,)).fetchone()
        if row is None:
            if expected == 1:
                return
            raise LedgerError("version_conflict", "账目已被其他请求修改", status=409)
        if row["version"] != expected:
            raise LedgerError("version_conflict", "账目已被其他请求修改", status=409)

    def _after_transaction_update(
        self,
        connection: sqlite3.Connection,
        transaction_id: str,
        payload: dict[str, Any],
    ) -> None:
        changes = payload.get("changes") or {}
        context_fields = {"project_id", "stage_id", "area_id"}
        context_row = connection.execute(
            "SELECT project_id,stage_id,area_id,version,updated_at FROM transaction_context WHERE transaction_id=?",
            (transaction_id,),
        ).fetchone()
        if context_fields & set(changes):
            expected = _version(payload.get("version"))
            current = dict(context_row) if context_row else {}
            project_changed = "project_id" in changes and (changes.get("project_id") or None) != current.get("project_id")
            project_id = changes.get("project_id", current.get("project_id"))
            project_id = None if project_id in {None, ""} else _text(project_id, "project_id", 64, required=True)
            stage_id = changes.get("stage_id", None if project_changed else current.get("stage_id"))
            area_id = changes.get("area_id", None if project_changed else current.get("area_id"))
            stage_id = _text(stage_id, "stage_id", 64) or None
            area_id = _text(area_id, "area_id", 64) or None
            if project_id is None and (stage_id or area_id):
                raise LedgerError("invalid_input", "没有项目归属时不能保留阶段或空间")
            if project_id is not None:
                self._validate_context_refs(connection, project_id, stage_id, area_id)
            if context_row is None:
                if expected != 1:
                    raise LedgerError("version_conflict", "账目已被其他请求修改", status=409)
                if project_id is not None:
                    connection.execute(
                        "INSERT INTO transaction_context(transaction_id,project_id,stage_id,area_id,version,updated_at) VALUES (?,?,?,?,1,?)",
                        (transaction_id, project_id, stage_id, area_id, utc_now()),
                    )
            elif project_id is None:
                connection.execute("DELETE FROM transaction_context WHERE transaction_id=?", (transaction_id,))
            else:
                cursor = connection.execute(
                    "UPDATE transaction_context SET project_id=?,stage_id=?,area_id=?,version=version+1,updated_at=? WHERE transaction_id=? AND version=?",
                    (project_id, stage_id, area_id, utc_now(), transaction_id, expected),
                )
                if cursor.rowcount != 1:
                    raise LedgerError("version_conflict", "账目已被其他请求修改", status=409)
            self._cascade_transaction_context(
                connection,
                transaction_id,
                project_id,
                stage_id,
                area_id,
                actor_hash=str(payload.get("_actor_hash") or "system"),
                idempotency_key=str(payload.get("_idempotency_key") or "system"),
                reason=str(payload.get("reason") or ""),
            )
            return
        if "version" not in payload or context_row is None:
            return
        expected = _version(payload.get("version"))
        cursor = connection.execute(
            "UPDATE transaction_context SET version=version+1,updated_at=? WHERE transaction_id=? AND version=?",
            (utc_now(), transaction_id, expected),
        )
        if cursor.rowcount != 1:
            raise LedgerError("version_conflict", "账目已被其他请求修改", status=409)

    def _clean_correction_extensions(self, changes: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for field in ("project_id", "stage_id", "area_id"):
            if field in changes:
                value = changes[field]
                result[field] = None if value in {None, ""} else _text(value, field, 64, required=True)
        return result

    def _correction_extension_fields(self) -> set[str]:
        return {"project_id", "stage_id", "area_id"}

    def _cascade_transaction_context(
        self,
        connection: sqlite3.Connection,
        payment_id: str,
        project_id: str | None,
        stage_id: str | None,
        area_id: str | None,
        *,
        actor_hash: str,
        idempotency_key: str,
        reason: str,
    ) -> None:
        refund_rows = connection.execute(
            "SELECT id FROM transactions WHERE original_payment_id=? ORDER BY id",
            (payment_id,),
        ).fetchall()
        target = None if project_id is None else {
            "project_id": project_id,
            "stage_id": stage_id,
            "area_id": area_id,
        }
        for refund in refund_rows:
            refund_id = refund["id"]
            current = connection.execute(
                "SELECT project_id,stage_id,area_id,version,updated_at FROM transaction_context WHERE transaction_id=?",
                (refund_id,),
            ).fetchone()
            before = dict(current) if current else None
            if target is None:
                connection.execute("DELETE FROM transaction_context WHERE transaction_id=?", (refund_id,))
            elif current is None:
                connection.execute(
                    "INSERT INTO transaction_context(transaction_id,project_id,stage_id,area_id,version,updated_at) VALUES (?,?,?,?,1,?)",
                    (refund_id, project_id, stage_id, area_id, utc_now()),
                )
            else:
                connection.execute(
                    "UPDATE transaction_context SET project_id=?,stage_id=?,area_id=?,version=version+1,updated_at=? WHERE transaction_id=?",
                    (project_id, stage_id, area_id, utc_now(), refund_id),
                )
            self._domain_audit(
                connection,
                action="cascade_transaction_context",
                target_type="transaction",
                target_id=refund_id,
                actor_hash=actor_hash,
                idempotency_key=idempotency_key,
                before=before,
                after=target,
                reason=reason,
            )

    def _row_json(self, connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        result = super()._row_json(connection, row)
        context = connection.execute(
            "SELECT project_id,stage_id,area_id,version,updated_at FROM transaction_context WHERE transaction_id=?",
            (result["id"],),
        ).fetchone()
        result["context"] = dict(context) if context else None
        result["version"] = context["version"] if context else 1
        return result

    @staticmethod
    def _object(connection: sqlite3.Connection, table: str, object_id: str) -> dict[str, Any]:
        if table not in {"projects", "stages", "areas", "events"}:
            raise LedgerError("invalid_input", "对象类型无效")
        row = connection.execute(f"SELECT * FROM {table} WHERE id=?", (object_id,)).fetchone()
        if row is None:
            raise LedgerError("not_found", "对象不存在", status=404)
        return dict(row)

    @staticmethod
    def _validate_context_refs(
        connection: sqlite3.Connection,
        project_id: str,
        stage_id: str | None,
        area_id: str | None,
    ) -> None:
        project = connection.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone()
        if project is None:
            raise LedgerError("project_not_found", "项目不存在", status=404)
        if stage_id:
            stage = connection.execute("SELECT 1 FROM stages WHERE id=? AND project_id=?", (stage_id, project_id)).fetchone()
            if stage is None:
                raise LedgerError("stage_not_found", "阶段不存在或不属于项目", status=404)
        if area_id:
            area = connection.execute("SELECT 1 FROM areas WHERE id=? AND project_id=?", (area_id, project_id)).fetchone()
            if area is None:
                raise LedgerError("area_not_found", "空间不存在或不属于项目", status=404)

    def create_project(self, payload: dict[str, Any], *, actor_hash: str = "system") -> dict[str, Any]:
        key = _idempotency_key(payload.get("idempotency_key"))
        clean = {
            "name": _text(payload.get("name"), "name", 120, required=True),
            "timezone": _text(payload.get("timezone", "Asia/Shanghai"), "timezone", 64, required=True),
            "budget_cents": _non_negative_cents(payload.get("budget_cents", 0)),
            "status": _choice(payload.get("status"), "status", PROJECT_STATUSES, default="active"),
        }

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            object_id = str(uuid.uuid4())
            now = utc_now()
            connection.execute(
                "INSERT INTO projects(id,name,timezone,budget_cents,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                (object_id, clean["name"], clean["timezone"], clean["budget_cents"], clean["status"], now, now),
            )
            result = self._object(connection, "projects", object_id)
            self._domain_audit(connection, action="create_project", target_type="project", target_id=object_id, actor_hash=actor_hash, idempotency_key=key, before=None, after=result)
            return result

        result, replayed = self._run_idempotent(key=key, request={"tool": "renovation_project_create", **clean}, operation=operation)
        return {"project": result, "idempotent_replay": replayed}

    def update_project(self, payload: dict[str, Any], *, actor_hash: str = "system") -> dict[str, Any]:
        return self._update_simple(
            table="projects",
            target_type="project",
            payload=payload,
            actor_hash=actor_hash,
            allowed={
                "name": lambda value: _text(value, "name", 120, required=True),
                "timezone": lambda value: _text(value, "timezone", 64, required=True),
                "budget_cents": _non_negative_cents,
                "status": lambda value: _choice(value, "status", PROJECT_STATUSES),
            },
        )

    def create_stage(self, payload: dict[str, Any], *, actor_hash: str = "system") -> dict[str, Any]:
        key = _idempotency_key(payload.get("idempotency_key"))
        clean = {
            "project_id": _text(payload.get("project_id"), "project_id", 64, required=True),
            "name": _text(payload.get("name"), "name", 100, required=True),
            "position": _position(payload.get("position", 0)),
            "status": _choice(payload.get("status"), "status", STAGE_STATUSES, default="planned"),
            "color": _text(payload.get("color", "#8B5CF6"), "color", 32, required=True),
            "planned_start": _optional_date(payload.get("planned_start"), "planned_start"),
            "planned_end": _optional_date(payload.get("planned_end"), "planned_end"),
            "actual_start": _optional_date(payload.get("actual_start"), "actual_start"),
            "actual_end": _optional_date(payload.get("actual_end"), "actual_end"),
        }
        self._validate_date_order(clean)

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            self._validate_context_refs(connection, clean["project_id"], None, None)
            if clean["status"] == "active" and connection.execute("SELECT 1 FROM stages WHERE project_id=? AND status='active'", (clean["project_id"],)).fetchone():
                raise LedgerError("stage_active_conflict", "同一项目只能有一个进行中阶段", status=409)
            object_id = str(uuid.uuid4())
            now = utc_now()
            try:
                connection.execute(
                    "INSERT INTO stages(id,project_id,name,position,status,color,planned_start,planned_end,actual_start,actual_end,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (object_id, clean["project_id"], clean["name"], clean["position"], clean["status"], clean["color"], clean["planned_start"], clean["planned_end"], clean["actual_start"], clean["actual_end"], now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise LedgerError("stage_conflict", "阶段名称或进行中状态冲突", status=409) from exc
            result = self._object(connection, "stages", object_id)
            self._domain_audit(connection, action="create_stage", target_type="stage", target_id=object_id, actor_hash=actor_hash, idempotency_key=key, before=None, after=result)
            return result

        result, replayed = self._run_idempotent(key=key, request={"tool": "renovation_stage_create", **clean}, operation=operation)
        return {"stage": result, "idempotent_replay": replayed}

    def update_stage(self, payload: dict[str, Any], *, actor_hash: str = "system") -> dict[str, Any]:
        clean_payload = dict(payload)
        for field in ("planned_start", "planned_end", "actual_start", "actual_end"):
            if field in clean_payload:
                clean_payload[field] = _optional_date(clean_payload[field], field)
        result = self._update_simple(
            table="stages",
            target_type="stage",
            payload=clean_payload,
            actor_hash=actor_hash,
            allowed={
                "name": lambda value: _text(value, "name", 100, required=True),
                "position": _position,
                "status": lambda value: _choice(value, "status", STAGE_STATUSES),
                "color": lambda value: _text(value, "color", 32, required=True),
                "planned_start": lambda value: value,
                "planned_end": lambda value: value,
                "actual_start": lambda value: value,
                "actual_end": lambda value: value,
            },
            validate_dates=True,
        )
        return result

    def create_area(self, payload: dict[str, Any], *, actor_hash: str = "system") -> dict[str, Any]:
        key = _idempotency_key(payload.get("idempotency_key"))
        clean = {
            "project_id": _text(payload.get("project_id"), "project_id", 64, required=True),
            "name": _text(payload.get("name"), "name", 100, required=True),
            "position": _position(payload.get("position", 0)),
            "status": _choice(payload.get("status"), "status", AREA_STATUSES, default="active"),
        }

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            self._validate_context_refs(connection, clean["project_id"], None, None)
            object_id = str(uuid.uuid4())
            now = utc_now()
            try:
                connection.execute(
                    "INSERT INTO areas(id,project_id,name,position,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                    (object_id, clean["project_id"], clean["name"], clean["position"], clean["status"], now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise LedgerError("area_conflict", "空间名称冲突", status=409) from exc
            result = self._object(connection, "areas", object_id)
            self._domain_audit(connection, action="create_area", target_type="area", target_id=object_id, actor_hash=actor_hash, idempotency_key=key, before=None, after=result)
            return result

        result, replayed = self._run_idempotent(key=key, request={"tool": "renovation_area_create", **clean}, operation=operation)
        return {"area": result, "idempotent_replay": replayed}

    def update_area(self, payload: dict[str, Any], *, actor_hash: str = "system") -> dict[str, Any]:
        return self._update_simple(
            table="areas",
            target_type="area",
            payload=payload,
            actor_hash=actor_hash,
            allowed={
                "name": lambda value: _text(value, "name", 100, required=True),
                "position": _position,
                "status": lambda value: _choice(value, "status", AREA_STATUSES),
            },
        )

    def create_event(self, payload: dict[str, Any], *, actor_hash: str = "system") -> dict[str, Any]:
        key = _idempotency_key(payload.get("idempotency_key"))
        clean = {
            "project_id": _text(payload.get("project_id"), "project_id", 64, required=True),
            "stage_id": _text(payload.get("stage_id"), "stage_id", 64) or None,
            "area_id": _text(payload.get("area_id"), "area_id", 64) or None,
            "event_type": _choice(payload.get("event_type"), "event_type", EVENT_TYPES, default="progress"),
            "title": _text(payload.get("title"), "title", 160, required=True),
            "description": _text(payload.get("description"), "description", 4000),
            "occurred_at": _datetime(payload.get("occurred_at")),
            "source_ref": _text(payload.get("source_ref"), "source_ref", 256),
        }

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            self._validate_context_refs(connection, clean["project_id"], clean["stage_id"], clean["area_id"])
            object_id = str(uuid.uuid4())
            now = utc_now()
            connection.execute(
                "INSERT INTO events(id,project_id,stage_id,area_id,event_type,title,description,occurred_at,status,source_ref,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (object_id, clean["project_id"], clean["stage_id"], clean["area_id"], clean["event_type"], clean["title"], clean["description"], clean["occurred_at"], "active", clean["source_ref"], now, now),
            )
            result = self._object(connection, "events", object_id)
            self._domain_audit(connection, action="create_event", target_type="event", target_id=object_id, actor_hash=actor_hash, idempotency_key=key, before=None, after=result)
            return result

        result, replayed = self._run_idempotent(key=key, request={"tool": "renovation_event_create", **clean}, operation=operation)
        return {"event": result, "idempotent_replay": replayed}

    def update_event(self, payload: dict[str, Any], *, actor_hash: str = "system") -> dict[str, Any]:
        return self._update_simple(
            table="events",
            target_type="event",
            payload=payload,
            actor_hash=actor_hash,
            allowed={
                "stage_id": lambda value: _text(value, "stage_id", 64) or None,
                "area_id": lambda value: _text(value, "area_id", 64) or None,
                "event_type": lambda value: _choice(value, "event_type", EVENT_TYPES),
                "title": lambda value: _text(value, "title", 160, required=True),
                "description": lambda value: _text(value, "description", 4000),
                "occurred_at": _datetime,
                "status": lambda value: _choice(value, "status", EVENT_STATUSES),
            },
            validate_context=True,
        )

    def mutate(self, payload: dict[str, Any], *, actor_hash: str = "system") -> dict[str, Any]:
        """Preview or apply a bounded, typed mutation across Hub business objects.

        The model can select explicit object IDs and a declared patch.  Preview
        returns a digest over the exact current rows; apply must present that
        digest and an explicit confirmation, so stale or guessed batch writes
        fail closed without adding a second persistence model.
        """

        mode = _choice(payload.get("mode", "preview"), "mode", {"preview", "apply"})
        target_type = _choice(payload.get("target_type"), "target_type", MUTATION_TARGET_TYPES)
        selector = self._clean_mutation_selector(target_type, payload.get("selector")) if payload.get("selector") is not None else None
        if selector is not None and payload.get("target_ids") is not None:
            raise LedgerError("invalid_input", "target_ids 与 selector 不能同时使用")
        if selector is not None:
            with self._connect() as connection:
                target_ids = self._mutation_selector_ids(connection, selector)
            if not target_ids and mode == "apply":
                raise LedgerError("no_matching_transactions", "批量条件没有匹配的有效付款", status=404)
        else:
            target_ids = self._mutation_target_ids(payload.get("target_ids"))
        reason = _text(payload.get("reason"), "reason", 500, required=True)
        patch = self._clean_mutation_patch(target_type, payload.get("patch"))

        if mode == "preview":
            with self._connect() as connection:
                prepared = self._prepare_mutation(connection, target_type, target_ids, patch)
            result = self._mutation_preview_result(target_type, target_ids, patch, reason, prepared)
            if selector is not None:
                result["selector"] = selector
            return result

        if payload.get("confirmed") is not True:
            raise LedgerError("confirmation_required", "应用变更前必须明确确认预览内容", status=409)
        preview_digest = _text(payload.get("preview_digest"), "preview_digest", 71, required=True)
        key = _idempotency_key(payload.get("idempotency_key"))

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            effective_target_ids = (
                self._mutation_selector_ids(connection, selector)
                if selector is not None
                else target_ids
            )
            if not effective_target_ids:
                raise LedgerError("no_matching_transactions", "批量条件没有匹配的有效付款", status=404)
            prepared = self._prepare_mutation(connection, target_type, effective_target_ids, patch)
            if prepared["preview_digest"] != preview_digest:
                raise LedgerError("preview_stale", "预览对应的数据已变化，请重新生成预览", status=409)
            results = self._apply_mutation(
                connection,
                target_type,
                prepared["items"],
                patch,
                reason=reason,
                actor_hash=actor_hash,
                idempotency_key=key,
            )
            return {
                "mode": "apply",
                "target_type": target_type,
                "target_ids": effective_target_ids,
                "selector": selector,
                "count": len(results),
                "items": results,
                "preview_digest": preview_digest,
            }

        result, replayed = self._run_idempotent(
            key=key,
            request={
                "tool": "renovation_mutate",
                "target_type": target_type,
                "target_ids": target_ids,
                "selector": selector,
                "patch": patch,
                "reason": reason,
                "preview_digest": preview_digest,
            },
            operation=operation,
        )
        return {**result, "selector": selector, "idempotent_replay": replayed}

    @staticmethod
    def _mutation_target_ids(value: Any) -> list[str]:
        if not isinstance(value, list) or not value or len(value) > MUTATION_MAX_TARGETS:
            raise LedgerError("invalid_input", f"target_ids 必须是 1 到 {MUTATION_MAX_TARGETS} 个 ID")
        result = [_text(item, "target_id", 64, required=True) for item in value]
        if len(set(result)) != len(result):
            raise LedgerError("invalid_input", "target_ids 不能重复")
        return result

    @classmethod
    def _clean_mutation_selector(cls, target_type: str, value: Any) -> dict[str, Any]:
        if target_type != "transaction":
            raise LedgerError("invalid_input", "selector 仅支持 transaction 批量修改")
        if not isinstance(value, dict) or not value:
            raise LedgerError("invalid_input", "selector 必须是非空对象")
        allowed = {
            "transaction_ids",
            "project_id",
            "stage_id",
            "area_id",
            "start",
            "end",
            "main_category",
            "legacy_main_category",
            "category",
            "subcategory",
            "expense_type",
            "status",
        }
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise LedgerError("invalid_input", f"selector 不支持字段：{', '.join(unknown)}")
        result: dict[str, Any] = {}
        if "transaction_ids" in value:
            result["transaction_ids"] = cls._mutation_target_ids(value["transaction_ids"])
        for field in ("project_id", "stage_id", "area_id"):
            if field in value:
                result[field] = cls._optional_id(value[field], field)
        for field in ("start", "end"):
            if field in value:
                result[field] = _validate_date(value[field], field)
        category = value.get("main_category", value.get("legacy_main_category"))
        if category is not None:
            result["main_category"] = _text(category, "main_category", 80, required=True)
        for field in CLASSIFICATION_FIELDS:
            if field in value:
                result[field] = _classification_code(value[field], field)
        if "status" in value:
            result["status"] = _choice(value["status"], "status", {"active"})
        if not result:
            raise LedgerError("invalid_input", "selector 至少需要一个筛选条件")
        if result.get("start") and result.get("end") and result["start"] > result["end"]:
            raise LedgerError("invalid_date_range", "start 不能晚于 end")
        return result

    @staticmethod
    def _mutation_selector_ids(connection: sqlite3.Connection, selector: dict[str, Any]) -> list[str]:
        clauses = ["t.type='payment'", "t.status='active'"]
        values: list[Any] = []
        if selector.get("transaction_ids"):
            ids = selector["transaction_ids"]
            placeholders = ",".join("?" for _ in ids)
            clauses.append(f"t.id IN ({placeholders})")
            values.extend(ids)
        if selector.get("project_id"):
            clauses.append("c.project_id=?")
            values.append(selector["project_id"])
        if selector.get("stage_id"):
            clauses.append("c.stage_id=?")
            values.append(selector["stage_id"])
        if selector.get("area_id"):
            clauses.append("c.area_id=?")
            values.append(selector["area_id"])
        if selector.get("start"):
            clauses.append("t.occurred_on>=?")
            values.append(selector["start"])
        if selector.get("end"):
            clauses.append("t.occurred_on<=?")
            values.append(selector["end"])
        if selector.get("main_category"):
            clauses.append("t.main_category=?")
            values.append(selector["main_category"])
        for field in CLASSIFICATION_FIELDS:
            if selector.get(field):
                clauses.append(f"t.{field}=?")
                values.append(selector[field])
        sql = f"SELECT t.id FROM transactions t LEFT JOIN transaction_context c ON c.transaction_id=t.id WHERE {' AND '.join(clauses)} ORDER BY t.occurred_on,t.id LIMIT ?"
        values.append(MUTATION_MAX_TARGETS + 1)
        rows = connection.execute(sql, values).fetchall()
        if len(rows) > MUTATION_MAX_TARGETS:
            raise LedgerError("mutation_limit_exceeded", f"匹配流水超过 {MUTATION_MAX_TARGETS} 条，请缩小筛选范围", status=413)
        return [row["id"] for row in rows]

    @staticmethod
    def _optional_id(value: Any, field: str) -> str | None:
        if value is None or value == "":
            return None
        return _text(value, field, 64, required=True)

    @classmethod
    def _clean_mutation_patch(cls, target_type: str, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict) or not value:
            raise LedgerError("invalid_input", "patch 必须是非空对象")
        allowed = {
            "project": {"name", "timezone", "budget_cents", "status"},
            "stage": {"name", "position", "status", "color", "planned_start", "planned_end", "actual_start", "actual_end"},
            "area": {"name", "position", "status"},
            "event": {"project_id", "stage_id", "area_id", "event_type", "title", "description", "occurred_at", "status"},
            "transaction": {"amount_cents", "occurred_on", "main_category", "category", "subcategory", "expense_type", "merchant", "note", "is_deposit", "tags", "grouped_tags", "project_id", "stage_id", "area_id"},
        }[target_type]
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise LedgerError("invalid_input", f"{target_type} 不支持字段：{', '.join(unknown)}")
        if "tags" in value and "grouped_tags" in value:
            raise LedgerError("invalid_input", "tags 与 grouped_tags 不能同时修改")

        clean: dict[str, Any] = {}
        for field, raw in value.items():
            if field == "amount_cents":
                clean[field] = _positive_cents(raw)
            elif field == "budget_cents":
                clean[field] = _non_negative_cents(raw)
            elif field == "occurred_on":
                clean[field] = _validate_date(raw, field)
            elif field in {"planned_start", "planned_end", "actual_start", "actual_end"}:
                clean[field] = None if raw is None or raw == "" else _validate_date(raw, field)
            elif field == "occurred_at":
                clean[field] = _datetime(raw)
            elif field in {"project_id", "stage_id", "area_id"}:
                clean[field] = cls._optional_id(raw, field) if target_type == "transaction" or field != "project_id" else _text(raw, field, 64, required=True)
            elif field == "position":
                clean[field] = _position(raw)
            elif field == "status":
                choices = {
                    "project": PROJECT_STATUSES,
                    "stage": STAGE_STATUSES,
                    "area": AREA_STATUSES,
                    "event": EVENT_STATUSES,
                    "transaction": set(),
                }[target_type]
                if not choices:
                    raise LedgerError("invalid_input", "流水状态不能通过统一变更工具修改")
                clean[field] = _choice(raw, field, choices)
            elif field == "event_type":
                clean[field] = _choice(raw, field, EVENT_TYPES)
            elif field == "is_deposit":
                if not isinstance(raw, bool):
                    raise LedgerError("invalid_input", "is_deposit 必须是布尔值")
                clean[field] = raw
            elif field == "tags":
                clean[field] = normalize_tags(raw)
            elif field == "grouped_tags":
                clean[field] = normalize_grouped_tags(raw)[1]
            elif field in CLASSIFICATION_FIELDS:
                clean[field] = _classification_code(raw, field)
            elif field in {"name", "timezone", "color", "main_category", "merchant", "note", "title", "description"}:
                maximum = {
                    "name": 120 if target_type == "project" else 100,
                    "timezone": 64,
                    "color": 32,
                    "main_category": 80,
                    "merchant": 200,
                    "note": 2000,
                    "title": 160,
                    "description": 4000,
                }[field]
                clean[field] = _text(raw, field, maximum, required=field in {"name", "main_category", "title"})
            else:
                raise LedgerError("invalid_input", f"不支持字段：{field}")
        return clean

    def _mutation_record(self, connection: sqlite3.Connection, target_type: str, object_id: str) -> dict[str, Any]:
        if target_type == "transaction":
            row = connection.execute(
                "SELECT * FROM transactions WHERE id=? AND type='payment' AND status='active'",
                (object_id,),
            ).fetchone()
            if row is None:
                raise LedgerError("payment_not_found", "付款不存在或已撤销", status=404)
            return self._row_json(connection, row)
        table = {"project": "projects", "stage": "stages", "area": "areas", "event": "events"}[target_type]
        return self._object(connection, table, object_id)

    def _mutation_candidate(
        self,
        connection: sqlite3.Connection,
        target_type: str,
        before: dict[str, Any],
        patch: dict[str, Any],
    ) -> dict[str, Any]:
        after = dict(before)
        if target_type != "transaction":
            after.update(patch)
            self._validate_mutation_candidate(connection, target_type, before, after, patch)
            return after

        ledger_version = int(before.get("ledger_format_version") or 1)
        if ledger_version == 1 and "grouped_tags" in patch:
            raise LedgerError("invalid_input", "v1 付款不能使用 grouped_tags")
        if ledger_version == 2 and ({"main_category", "tags"} & set(patch)):
            raise LedgerError("invalid_input", "v2 付款不能使用 legacy 分类或标签")
        for field, value in patch.items():
            if field not in {"project_id", "stage_id", "area_id", "grouped_tags", "tags"}:
                after[field] = value
        if "grouped_tags" in patch:
            after["grouped_tags"] = patch["grouped_tags"]
        if "tags" in patch:
            after["tags"] = patch["tags"]

        context_fields = {"project_id", "stage_id", "area_id"}
        if context_fields & set(patch):
            current = before.get("context") or {}
            project_changed = "project_id" in patch and patch.get("project_id") != current.get("project_id")
            project_id = patch.get("project_id", current.get("project_id"))
            stage_id = patch.get("stage_id", None if project_changed else current.get("stage_id"))
            area_id = patch.get("area_id", None if project_changed else current.get("area_id"))
            if project_id is None and (stage_id or area_id):
                raise LedgerError("invalid_input", "没有项目归属时不能保留阶段或空间")
            after["context"] = (
                None
                if project_id is None
                else {
                    "project_id": project_id,
                    "stage_id": stage_id,
                    "area_id": area_id,
                    "version": (int(current.get("version") or 0) + 1) if current else 1,
                    "updated_at": current.get("updated_at"),
                }
            )
        elif before.get("context"):
            after["context"] = {
                **before["context"],
                "version": int(before["context"].get("version") or 0) + 1,
            }
        after["version"] = (after.get("context") or {}).get("version", 1)
        self._validate_mutation_candidate(connection, target_type, before, after, patch)
        return after

    def _validate_mutation_candidate(
        self,
        connection: sqlite3.Connection,
        target_type: str,
        before: dict[str, Any],
        after: dict[str, Any],
        patch: dict[str, Any],
    ) -> None:
        if target_type == "stage" and after.get("status") == "active":
            conflict = connection.execute(
                "SELECT 1 FROM stages WHERE project_id=? AND status='active' AND id<>?",
                (before["project_id"], before["id"]),
            ).fetchone()
            if conflict:
                raise LedgerError("stage_active_conflict", "同一项目只能有一个进行中阶段", status=409)
        if target_type == "event":
            self._validate_context_refs(
                connection,
                after["project_id"],
                after.get("stage_id"),
                after.get("area_id"),
            )
        if target_type == "transaction":
            context = after.get("context")
            if context:
                self._validate_context_refs(
                    connection,
                    context["project_id"],
                    context.get("stage_id"),
                    context.get("area_id"),
                )
            classification = {
                field: str(after.get(field) or before.get(field) or "")
                for field in CLASSIFICATION_FIELDS
            }
            self._validate_classification(connection, **classification)
            if "amount_cents" in patch:
                refunded = connection.execute(
                    "SELECT coalesce(sum(amount_cents),0) FROM transactions WHERE type='refund' AND status='active' AND original_payment_id=?",
                    (before["id"],),
                ).fetchone()[0]
                if patch["amount_cents"] < refunded:
                    raise LedgerError("refund_exceeds_payment", "付款金额不能低于累计退款", status=409)

    def _prepare_mutation(
        self,
        connection: sqlite3.Connection,
        target_type: str,
        target_ids: list[str],
        patch: dict[str, Any],
    ) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        for object_id in target_ids:
            before = self._mutation_record(connection, target_type, object_id)
            after = self._mutation_candidate(connection, target_type, before, patch)
            items.append({
                "id": object_id,
                "before": before,
                "after": after,
                "diff": self._mutation_diff(target_type, object_id, before, after, patch),
            })
        digest = "sha256:" + digest_json({
            "target_type": target_type,
            "target_ids": target_ids,
            "patch": patch,
            "before": [item["before"] for item in items],
        })
        return {"items": items, "preview_digest": digest}

    @staticmethod
    def _mutation_diff(
        target_type: str,
        object_id: str,
        before: dict[str, Any],
        after: dict[str, Any],
        patch: dict[str, Any],
    ) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for field in patch:
            if target_type == "transaction" and field in {"project_id", "stage_id", "area_id"}:
                before_value = (before.get("context") or {}).get(field)
                after_value = (after.get("context") or {}).get(field)
            else:
                before_value = before.get(field)
                after_value = after.get(field)
            values[field] = {"before": before_value, "after": after_value}
        return {"id": object_id, "version": before.get("version", 1), "changes": values}

    @staticmethod
    def _mutation_preview_result(
        target_type: str,
        target_ids: list[str],
        patch: dict[str, Any],
        reason: str,
        prepared: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "mode": "preview",
            "target_type": target_type,
            "target_ids": target_ids,
            "count": len(prepared["items"]),
            "changes": [item["diff"] for item in prepared["items"]],
            "patch": patch,
            "reason": reason,
            "preview_digest": prepared["preview_digest"],
            "requires_confirmation": True,
            "confirmation_hint": "回复“确认修改”后再应用这批变更",
        }

    def _apply_mutation(
        self,
        connection: sqlite3.Connection,
        target_type: str,
        items: list[dict[str, Any]],
        patch: dict[str, Any],
        *,
        reason: str,
        actor_hash: str,
        idempotency_key: str,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for item in items:
            object_id = item["id"]
            before = item["before"]
            if target_type == "transaction":
                row = connection.execute(
                    "SELECT * FROM transactions WHERE id=? AND type='payment' AND status='active'",
                    (object_id,),
                ).fetchone()
                if row is None:
                    raise LedgerError("payment_not_found", "付款不存在或已撤销", status=404)
                columns = {
                    field: value
                    for field, value in patch.items()
                    if field not in {"project_id", "stage_id", "area_id", "tags", "grouped_tags"}
                }
                if columns:
                    columns["updated_at"] = utc_now()
                    assignments = ",".join(f"{field}=?" for field in columns)
                    connection.execute(
                        f"UPDATE transactions SET {assignments} WHERE id=?",
                        (*columns.values(), object_id),
                    )
                if "tags" in patch:
                    self._set_tags(connection, object_id, patch["tags"])
                if "grouped_tags" in patch:
                    self._set_tags(
                        connection,
                        object_id,
                        normalize_grouped_tags(patch["grouped_tags"])[0],
                        ledger_format_version=2,
                    )
                context_patch = {field for field in patch if field in {"project_id", "stage_id", "area_id"}}
                current_context = connection.execute(
                    "SELECT project_id,stage_id,area_id,version FROM transaction_context WHERE transaction_id=?",
                    (object_id,),
                ).fetchone()
                if context_patch or (columns or "tags" in patch or "grouped_tags" in patch):
                    candidate_context = item["after"].get("context")
                    if candidate_context is None:
                        connection.execute("DELETE FROM transaction_context WHERE transaction_id=?", (object_id,))
                    elif current_context:
                        connection.execute(
                            "UPDATE transaction_context SET project_id=?,stage_id=?,area_id=?,version=?,updated_at=? WHERE transaction_id=?",
                            (
                                candidate_context["project_id"],
                                candidate_context.get("stage_id"),
                                candidate_context.get("area_id"),
                                int(current_context["version"]) + 1,
                                utc_now(),
                                object_id,
                            ),
                        )
                    else:
                        connection.execute(
                            "INSERT INTO transaction_context(transaction_id,project_id,stage_id,area_id,version,updated_at) VALUES (?,?,?,?,1,?)",
                            (
                                object_id,
                                candidate_context["project_id"],
                                candidate_context.get("stage_id"),
                                candidate_context.get("area_id"),
                                utc_now(),
                            ),
                        )
                    if context_patch:
                        self._cascade_transaction_context(
                            connection,
                            object_id,
                            candidate_context["project_id"] if candidate_context else None,
                            candidate_context.get("stage_id") if candidate_context else None,
                            candidate_context.get("area_id") if candidate_context else None,
                            actor_hash=actor_hash,
                            idempotency_key=idempotency_key,
                            reason=reason,
                        )
                updated = connection.execute("SELECT * FROM transactions WHERE id=?", (object_id,)).fetchone()
                after = self._row_json(connection, updated)
                self._audit(
                    connection,
                    action="mutate_transaction",
                    target_id=object_id,
                    actor_hash=actor_hash,
                    idempotency_key=idempotency_key,
                    reason=reason,
                    before=before,
                    after=after,
                )
                results.append(after)
                continue

            table = {"project": "projects", "stage": "stages", "area": "areas", "event": "events"}[target_type]
            expected_version = int(before["version"])
            self._validate_mutation_candidate(connection, target_type, before, item["after"], patch)
            assignments = ",".join(f"{field}=?" for field in patch)
            values = list(patch.values()) + [expected_version + 1, utc_now(), object_id, expected_version]
            try:
                cursor = connection.execute(
                    f"UPDATE {table} SET {assignments},version=?,updated_at=? WHERE id=? AND version=?",
                    values,
                )
            except sqlite3.IntegrityError as exc:
                raise LedgerError(f"{target_type}_conflict", "对象名称或状态冲突", status=409) from exc
            if cursor.rowcount != 1:
                raise LedgerError("version_conflict", "对象已被其他请求修改", status=409)
            after = self._object(connection, table, object_id)
            self._domain_audit(
                connection,
                action=f"mutate_{target_type}",
                target_type=target_type,
                target_id=object_id,
                actor_hash=actor_hash,
                idempotency_key=idempotency_key,
                before=before,
                after=after,
                reason=reason,
            )
            results.append(after)
        return results

    def _update_simple(
        self,
        *,
        table: str,
        target_type: str,
        payload: dict[str, Any],
        actor_hash: str,
        allowed: dict[str, Any],
        validate_dates: bool = False,
        validate_context: bool = False,
    ) -> dict[str, Any]:
        key = _idempotency_key(payload.get("idempotency_key"))
        object_id = _text(payload.get(f"{target_type}_id"), f"{target_type}_id", 64, required=True)
        expected_version = _version(payload.get("version"))
        changes = payload.get("changes")
        if not isinstance(changes, dict) or not changes or set(changes) - set(allowed):
            raise LedgerError("invalid_input", "changes 为空或包含不允许字段")
        clean = {field: allowed[field](value) for field, value in changes.items()}

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            before = self._object(connection, table, object_id)
            if before["version"] != expected_version:
                raise LedgerError("version_conflict", "对象已被其他请求修改", status=409)
            candidate = {**before, **clean}
            if validate_dates:
                self._validate_date_order(candidate)
            if validate_context:
                self._validate_context_refs(connection, before["project_id"], candidate.get("stage_id"), candidate.get("area_id"))
            if table == "stages" and candidate.get("status") == "active":
                conflict = connection.execute("SELECT 1 FROM stages WHERE project_id=? AND status='active' AND id<>?", (before["project_id"], object_id)).fetchone()
                if conflict:
                    raise LedgerError("stage_active_conflict", "同一项目只能有一个进行中阶段", status=409)
            assignments = ",".join(f"{field}=?" for field in clean)
            values = list(clean.values()) + [expected_version + 1, utc_now(), object_id, expected_version]
            try:
                cursor = connection.execute(
                    f"UPDATE {table} SET {assignments},version=?,updated_at=? WHERE id=? AND version=?",
                    values,
                )
            except sqlite3.IntegrityError as exc:
                raise LedgerError(f"{target_type}_conflict", "对象名称或状态冲突", status=409) from exc
            if cursor.rowcount != 1:
                raise LedgerError("version_conflict", "对象已被其他请求修改", status=409)
            after = self._object(connection, table, object_id)
            self._domain_audit(connection, action=f"update_{target_type}", target_type=target_type, target_id=object_id, actor_hash=actor_hash, idempotency_key=key, before=before, after=after)
            return after

        result, replayed = self._run_idempotent(
            key=key,
            request={"tool": f"renovation_{target_type}_update", f"{target_type}_id": object_id, "version": expected_version, "changes": clean},
            operation=operation,
        )
        return {target_type: result, "idempotent_replay": replayed}

    @staticmethod
    def _validate_date_order(values: dict[str, Any]) -> None:
        for start, end in (("planned_start", "planned_end"), ("actual_start", "actual_end")):
            if values.get(start) and values.get(end) and values[start] > values[end]:
                raise LedgerError("invalid_date_range", f"{start} 不能晚于 {end}")

    def list_projects(self, filters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        filters = filters or {}
        status = filters.get("status")
        if status is not None:
            status = _choice(status, "status", PROJECT_STATUSES)
        with self._connect() as connection:
            if status:
                rows = connection.execute("SELECT * FROM projects WHERE status=? ORDER BY created_at", (status,))
            else:
                rows = connection.execute("SELECT * FROM projects ORDER BY created_at")
            return [dict(row) for row in rows]

    def list_stages(self, project_id: str) -> list[dict[str, Any]]:
        project_id = _text(project_id, "project_id", 64, required=True)
        with self._connect() as connection:
            self._validate_context_refs(connection, project_id, None, None)
            return [dict(row) for row in connection.execute("SELECT * FROM stages WHERE project_id=? ORDER BY position,name", (project_id,))]

    def list_areas(self, project_id: str) -> list[dict[str, Any]]:
        project_id = _text(project_id, "project_id", 64, required=True)
        with self._connect() as connection:
            self._validate_context_refs(connection, project_id, None, None)
            return [dict(row) for row in connection.execute("SELECT * FROM areas WHERE project_id=? ORDER BY position,name", (project_id,))]

    @staticmethod
    def _payment_plan_nodes(value: Any) -> list[dict[str, Any]]:
        if value is None:
            return []
        if not isinstance(value, list) or len(value) > 32:
            raise LedgerError("invalid_input", "payment_nodes 必须是最多 32 个节点")
        result: list[dict[str, Any]] = []
        names: set[str] = set()
        for index, raw in enumerate(value):
            if not isinstance(raw, dict):
                raise LedgerError("invalid_input", "payment_nodes 条目必须是对象")
            name = _text(raw.get("name"), "payment_node.name", 120, required=True)
            if name.casefold() in names:
                raise LedgerError("invalid_input", "付款节点名称不能重复")
            names.add(name.casefold())
            result.append(
                {
                    "name": name,
                    "amount_cents": _positive_cents(raw.get("amount_cents")),
                    "due_on": None if raw.get("due_on") in {None, ""} else _validate_date(raw["due_on"], "due_on"),
                    "position": _position(raw.get("position", index)),
                }
            )
        return result

    @staticmethod
    def _payment_plan_document(connection: sqlite3.Connection, plan_id: str) -> dict[str, Any]:
        plan_row = connection.execute("SELECT * FROM payment_plans WHERE id=?", (plan_id,)).fetchone()
        if plan_row is None:
            raise LedgerError("payment_plan_not_found", "付款计划不存在", status=404)
        nodes = [dict(row) for row in connection.execute(
            "SELECT * FROM payment_plan_nodes WHERE payment_plan_id=? ORDER BY position,name,id",
            (plan_id,),
        )]
        for node in nodes:
            node["paid_amount_cents"] = 0

        allocation_rows = connection.execute(
            """
            SELECT a.id,a.payment_plan_node_id,a.transaction_id,a.amount_cents,
                   t.amount_cents AS payment_amount_cents,t.status,
                   COALESCE((SELECT SUM(r.amount_cents) FROM transactions r
                             WHERE r.type='refund' AND r.status='active'
                               AND r.original_payment_id=t.id),0) AS refunded_amount_cents
            FROM payment_plan_allocations a
            JOIN payment_plan_nodes n ON n.id=a.payment_plan_node_id
            JOIN transactions t ON t.id=a.transaction_id AND t.type='payment'
            WHERE n.payment_plan_id=?
            ORDER BY a.transaction_id,a.id
            """,
            (plan_id,),
        ).fetchall()
        node_by_id = {node["id"]: node for node in nodes}
        remaining_by_payment: dict[str, int] = {}
        for row in allocation_rows:
            payment_id = str(row["transaction_id"])
            if payment_id not in remaining_by_payment:
                remaining_by_payment[payment_id] = max(
                    0,
                    int(row["payment_amount_cents"])
                    - int(row["refunded_amount_cents"])
                    if row["status"] == "active"
                    else 0,
                )
            contribution = min(int(row["amount_cents"]), remaining_by_payment[payment_id])
            remaining_by_payment[payment_id] -= contribution
            node_by_id[str(row["payment_plan_node_id"])] ["paid_amount_cents"] += contribution

        paid_amount = 0
        for node in nodes:
            paid = int(node["paid_amount_cents"])
            total = int(node["amount_cents"])
            node["remaining_amount_cents"] = max(0, total - paid)
            node["payment_status"] = "paid" if node["remaining_amount_cents"] == 0 else "partial" if paid else "pending"
            paid_amount += paid
        total_amount = int(plan_row["total_amount_cents"])
        remaining_amount = max(0, total_amount - paid_amount)
        result = dict(plan_row)
        result.update(
            {
                "total_amount_cents": total_amount,
                "paid_amount_cents": paid_amount,
                "remaining_amount_cents": remaining_amount,
                "payment_status": "paid" if remaining_amount == 0 else "partial" if paid_amount else "pending",
                "payment_nodes": nodes,
            }
        )
        return result

    def create_payment_plan(self, payload: dict[str, Any], *, actor_hash: str = "system") -> dict[str, Any]:
        key = _idempotency_key(payload.get("idempotency_key"))
        clean = {
            "project_id": _text(payload.get("project_id"), "project_id", 64, required=True),
            "name": _text(payload.get("name"), "name", 120, required=True),
            "total_amount_cents": _positive_cents(payload.get("total_amount_cents")),
            "payment_nodes": self._payment_plan_nodes(payload.get("payment_nodes")),
        }
        if sum(item["amount_cents"] for item in clean["payment_nodes"]) > clean["total_amount_cents"]:
            raise LedgerError("payment_plan_nodes_exceed_total", "付款节点合计不能超过计划总额", status=409)

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            self._validate_context_refs(connection, clean["project_id"], None, None)
            plan_id = str(uuid.uuid4())
            now = utc_now()
            try:
                connection.execute(
                    "INSERT INTO payment_plans(id,project_id,name,total_amount_cents,version,created_at,updated_at) VALUES (?,?,?,?,1,?,?)",
                    (plan_id, clean["project_id"], clean["name"], clean["total_amount_cents"], now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise LedgerError("payment_plan_conflict", "同一项目下付款计划名称不能重复", status=409) from exc
            for node in clean["payment_nodes"]:
                node_id = str(uuid.uuid4())
                connection.execute(
                    "INSERT INTO payment_plan_nodes(id,payment_plan_id,name,amount_cents,due_on,position,version,created_at,updated_at) VALUES (?,?,?,?,?,?,1,?,?)",
                    (node_id, plan_id, node["name"], node["amount_cents"], node["due_on"], node["position"], now, now),
                )
            result = self._payment_plan_document(connection, plan_id)
            self._domain_audit(connection, action="create_payment_plan", target_type="payment_plan", target_id=plan_id, actor_hash=actor_hash, idempotency_key=key, before=None, after=result)
            return result

        result, replayed = self._run_idempotent(
            key=key,
            request={"tool": "payment_plan.create", **clean},
            operation=operation,
        )
        return {"payment_plan": result, "idempotent_replay": replayed}

    def show_payment_plan(self, plan_id: str) -> dict[str, Any]:
        plan_id = _text(plan_id, "payment_plan_id", 64, required=True)
        with self._connect() as connection:
            return self._payment_plan_document(connection, plan_id)

    def list_payment_plans(self, filters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        filters = filters or {}
        project_id = filters.get("project_id")
        if project_id:
            project_id = _text(project_id, "project_id", 64, required=True)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id FROM payment_plans" + (" WHERE project_id=?" if project_id else "") + " ORDER BY created_at,id",
                (project_id,) if project_id else (),
            ).fetchall()
            items = [self._payment_plan_document(connection, row["id"]) for row in rows]
        return items

    def allocate_payment_plan(self, payload: dict[str, Any], *, actor_hash: str = "system") -> dict[str, Any]:
        key = _idempotency_key(payload.get("idempotency_key"))
        plan_id = _text(payload.get("payment_plan_id"), "payment_plan_id", 64, required=True)
        node_id = _text(payload.get("payment_node_id"), "payment_node_id", 64, required=True)
        transaction_id = _text(payload.get("transaction_id"), "transaction_id", 64, required=True)
        amount_cents = _positive_cents(payload.get("amount_cents"))
        reason = _text(payload.get("reason"), "reason", 500, required=True)

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            plan = connection.execute("SELECT * FROM payment_plans WHERE id=?", (plan_id,)).fetchone()
            node = connection.execute("SELECT * FROM payment_plan_nodes WHERE id=? AND payment_plan_id=?", (node_id, plan_id)).fetchone()
            payment = connection.execute("SELECT * FROM transactions WHERE id=? AND type='payment' AND status='active'", (transaction_id,)).fetchone()
            if plan is None:
                raise LedgerError("payment_plan_not_found", "付款计划不存在", status=404)
            if node is None:
                raise LedgerError("payment_node_not_found", "付款节点不存在或不属于该计划", status=404)
            if payment is None:
                raise LedgerError("payment_not_found", "待关联付款不存在或已撤销", status=404)
            context = connection.execute("SELECT project_id FROM transaction_context WHERE transaction_id=?", (transaction_id,)).fetchone()
            if context is None or context["project_id"] != plan["project_id"]:
                raise LedgerError("payment_plan_project_mismatch", "付款必须先归属于付款计划所属项目", status=409)
            allocated_payment = connection.execute(
                "SELECT COALESCE(SUM(amount_cents),0) FROM payment_plan_allocations WHERE transaction_id=?",
                (transaction_id,),
            ).fetchone()[0]
            if int(allocated_payment) + amount_cents > int(payment["amount_cents"]):
                raise LedgerError("payment_allocation_exceeds_payment", "付款计划分配不能超过原付款金额", status=409)
            allocated_node = connection.execute(
                "SELECT COALESCE(SUM(amount_cents),0) FROM payment_plan_allocations WHERE payment_plan_node_id=?",
                (node_id,),
            ).fetchone()[0]
            if int(allocated_node) + amount_cents > int(node["amount_cents"]):
                raise LedgerError("payment_allocation_exceeds_node", "付款计划分配不能超过节点金额", status=409)
            now = utc_now()
            try:
                connection.execute(
                    "INSERT INTO payment_plan_allocations(payment_plan_node_id,transaction_id,amount_cents,created_at) VALUES (?,?,?,?)",
                    (node_id, transaction_id, amount_cents, now),
                )
            except sqlite3.IntegrityError as exc:
                raise LedgerError("payment_allocation_conflict", "同一付款不能重复分配到同一节点", status=409) from exc
            result = self._payment_plan_document(connection, plan_id)
            self._domain_audit(connection, action="allocate_payment_plan", target_type="payment_plan", target_id=plan_id, actor_hash=actor_hash, idempotency_key=key, before=None, after={"payment_plan": result, "transaction_id": transaction_id, "payment_node_id": node_id, "amount_cents": amount_cents}, reason=reason)
            return result

        result, replayed = self._run_idempotent(
            key=key,
            request={"tool": "payment_plan.allocate", "payment_plan_id": plan_id, "payment_node_id": node_id, "transaction_id": transaction_id, "amount_cents": amount_cents, "reason": reason},
            operation=operation,
        )
        return {"payment_plan": result, "idempotent_replay": replayed}

    def timeline(self, filters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        filters = filters or {}
        project_id = _text(filters.get("project_id"), "project_id", 64, required=True)
        clauses = ["events.project_id=?"]
        values: list[Any] = [project_id]
        for field in ("stage_id", "area_id", "event_type", "status"):
            value = filters.get(field)
            if value:
                if field == "event_type":
                    value = _choice(value, field, EVENT_TYPES)
                if field == "status":
                    value = _choice(value, field, EVENT_STATUSES)
                clauses.append(f"events.{field}=?")
                values.append(value)
        if filters.get("start"):
            clauses.append("events.occurred_at>=?")
            values.append(_datetime(filters["start"], "start"))
        if filters.get("end"):
            clauses.append("events.occurred_at<=?")
            values.append(_datetime(filters["end"], "end"))
        limit = filters.get("limit", 200)
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > 1000:
            raise LedgerError("invalid_input", "limit 必须为 1 到 1000")
        values.append(limit)
        sql = f"""
            SELECT events.*, stages.name AS stage_name, areas.name AS area_name
            FROM events
            LEFT JOIN stages ON stages.id=events.stage_id
            LEFT JOIN areas ON areas.id=events.area_id
            WHERE {' AND '.join(clauses)}
            ORDER BY events.occurred_at DESC, events.id DESC
            LIMIT ?
        """
        keyword = _text(filters.get("keyword"), "keyword", 100)
        with self._connect() as connection:
            self._validate_context_refs(connection, project_id, None, None)
            items = [dict(row) for row in connection.execute(sql, values)]
        if keyword:
            folded = keyword.casefold()
            items = [item for item in items if folded in f"{item['title']} {item['description']}".casefold()]
        return items

    def query(self, filters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        filters = dict(filters or {})
        context_filters = {
            "project_id": filters.get("project_id"),
            "stage_id": filters.get("stage_id"),
            "area_id": filters.get("area_id"),
        }
        if not any(context_filters.values()):
            return super().query(filters)
        requested_limit = filters.get("limit", 200)
        expanded = dict(filters)
        expanded["limit"] = 1000
        items = super().query(expanded)
        for field, value in context_filters.items():
            if value:
                value = _text(value, field, 64, required=True)
                items = [item for item in items if item.get("context") and item["context"].get(field) == value]
        if isinstance(requested_limit, bool) or not isinstance(requested_limit, int) or requested_limit < 1 or requested_limit > 1000:
            raise LedgerError("invalid_input", "limit 必须为 1 到 1000")
        return items[:requested_limit]

    def dashboard(self, project_id: str) -> dict[str, Any]:
        project_id = _text(project_id, "project_id", 64, required=True)
        with self._connect() as connection:
            project = self._object(connection, "projects", project_id)
            active_stage = connection.execute("SELECT * FROM stages WHERE project_id=? AND status='active'", (project_id,)).fetchone()
            counts = {
                "stages": connection.execute("SELECT count(*) FROM stages WHERE project_id=? AND status!='archived'", (project_id,)).fetchone()[0],
                "areas": connection.execute("SELECT count(*) FROM areas WHERE project_id=? AND status!='archived'", (project_id,)).fetchone()[0],
                "events": connection.execute("SELECT count(*) FROM events WHERE project_id=? AND status='active'", (project_id,)).fetchone()[0],
            }
        ledger = self.summary({"project_id": project_id})
        budget = project["budget_cents"]
        return {
            "project": project,
            "active_stage": dict(active_stage) if active_stage else None,
            "counts": counts,
            "ledger": ledger,
            "budget_remaining_cents": budget - ledger["net_amount_cents"],
            "budget_used_ratio": (ledger["net_amount_cents"] / budget) if budget else None,
            "recent_events": self.timeline({"project_id": project_id, "status": "active", "limit": 8}),
        }
