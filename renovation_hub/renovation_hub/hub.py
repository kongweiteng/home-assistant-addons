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
    _text,
    _validate_date,
    canonical_json,
    utc_now,
)


HUB_SCHEMA_VERSION = 1
PROJECT_STATUSES = {"active", "completed", "archived"}
STAGE_STATUSES = {"planned", "active", "completed", "archived"}
AREA_STATUSES = {"active", "archived"}
EVENT_TYPES = {"progress", "note", "decision", "inspection", "milestone"}
EVENT_STATUSES = {"active", "voided"}


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
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO metadata(key,value) VALUES ('hub_schema_version',?)",
                (str(HUB_SCHEMA_VERSION),),
            )
        from .media import initialize_media_schema

        initialize_media_schema(self)

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
        if original_payment_id and not project_id:
            inherited = connection.execute(
                "SELECT project_id,stage_id,area_id FROM transaction_context WHERE transaction_id=?",
                (original_payment_id,),
            ).fetchone()
            if inherited:
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
        if "version" not in payload:
            return
        expected = _version(payload.get("version"))
        row = connection.execute("SELECT version FROM transaction_context WHERE transaction_id=?", (transaction_id,)).fetchone()
        if row is None or row["version"] != expected:
            raise LedgerError("version_conflict", "账目已被其他请求修改", status=409)

    def _after_transaction_update(
        self,
        connection: sqlite3.Connection,
        transaction_id: str,
        payload: dict[str, Any],
    ) -> None:
        if "version" not in payload:
            return
        expected = _version(payload.get("version"))
        cursor = connection.execute(
            "UPDATE transaction_context SET version=version+1,updated_at=? WHERE transaction_id=? AND version=?",
            (utc_now(), transaction_id, expected),
        )
        if cursor.rowcount != 1:
            raise LedgerError("version_conflict", "账目已被其他请求修改", status=409)

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
