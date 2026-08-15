"""Persistent Runner Center v2 registry, task, lease, and audit store."""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import secrets
import sqlite3
from typing import Any, Callable, Iterable

from .store import StoreError


SCHEMA_VERSION = 4
ADMIN_STATES = frozenset({"pending", "enabled", "draining", "disabled", "revoked"})
WORK_STATES = frozenset({"idle", "busy", "recovery_required", "error"})
RUNNER_CAPABILITIES = frozenset(
    {
        "registered_projects",
        "worktree",
        "codex_exec_json",
        "codex_resume",
        "continue",
        "cancel",
        "expiry",
        "recovery_required",
        "summary_only_result",
        "non_force_git",
        "assignment_epoch",
        "lease",
        "self_check",
    }
)
TASK_STATES = frozenset(
    {
        "waiting_runner",
        "leased",
        "dispatched",
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
TERMINAL_TASK_STATES = frozenset({"completed", "failed", "cancelled", "expired"})
ACTIVE_TASK_STATES = frozenset({"leased", "dispatched", "queued", "running", "awaiting_confirmation"})
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
RUNNER_ID_RE = re.compile(r"^RN-[A-Z2-7]{20,32}$")
TASK_ID_RE = re.compile(r"^RW-[A-Za-z0-9][A-Za-z0-9._:-]{0,124}$")
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
SHA256_RE = re.compile(r"^sha256:[a-f0-9]{64}$")


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _parse_time(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timezone required")
    return parsed.astimezone(dt.timezone.utc)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _secret_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _opaque(prefix: str, size: int = 16) -> str:
    encoded = base64.b32encode(secrets.token_bytes(size)).decode("ascii").rstrip("=")
    return f"{prefix}-{encoded}"


def _bounded_text(value: Any, name: str, *, maximum: int, required: bool = True) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or (required and not value.strip()) or len(value) > maximum:
        raise StoreError("runner_payload_invalid", f"{name} 无效")
    if any(ord(character) < 32 and character not in {"\n", "\r", "\t"} for character in value):
        raise StoreError("runner_payload_invalid", f"{name} 包含无效控制字符")
    return value.strip()


def _safe_values(value: Any, name: str, pattern: re.Pattern[str], *, maximum: int = 32) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum:
        raise StoreError("runner_payload_invalid", f"{name} 无效")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not pattern.fullmatch(item):
            raise StoreError("runner_payload_invalid", f"{name} 无效")
        if item not in result:
            result.append(item)
    return sorted(result)


def _capabilities(value: Any) -> list[str]:
    result = _safe_values(value, "capabilities", LABEL_RE, maximum=32)
    if not set(result).issubset(RUNNER_CAPABILITIES):
        raise StoreError("runner_payload_invalid", "capabilities 包含未知能力")
    return result


class RunnerStore:
    """SQLite source of truth for Runner Center v2.

    The class intentionally uses its own tables and connections so enabling the
    feature cannot alter Controller v1 job scheduling or app-server state.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        online_after_seconds: int = 30,
        offline_after_seconds: int = 90,
        lease_ttl_seconds: int = 60,
        task_ttl_seconds: int = 1800,
        clock: Callable[[], str] = utc_now,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not 1 <= online_after_seconds < offline_after_seconds:
            raise ValueError("Runner heartbeat thresholds are invalid")
        if not 5 <= lease_ttl_seconds <= 3600 or not 60 <= task_ttl_seconds <= 86400:
            raise ValueError("Runner TTL configuration is invalid")
        self.online_after_seconds = online_after_seconds
        self.offline_after_seconds = offline_after_seconds
        self.lease_ttl_seconds = lease_ttl_seconds
        self.task_ttl_seconds = task_ttl_seconds
        self.clock = clock
        self._migrate()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _migrate(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS runner_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runner_registry (
                    runner_id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    admin_state TEXT NOT NULL,
                    work_state TEXT NOT NULL,
                    protocol_version INTEGER NOT NULL DEFAULT 2,
                    agent_version TEXT,
                    codex_version TEXT,
                    os TEXT NOT NULL,
                    arch TEXT NOT NULL,
                    labels_json TEXT NOT NULL,
                    allowed_projects_json TEXT NOT NULL,
                    capabilities_json TEXT NOT NULL DEFAULT '[]',
                    max_concurrency INTEGER NOT NULL DEFAULT 1,
                    policy_revision INTEGER NOT NULL DEFAULT 1,
                    revision INTEGER NOT NULL DEFAULT 1,
                    self_check_json TEXT,
                    last_heartbeat_at TEXT,
                    heartbeat_sequence INTEGER NOT NULL DEFAULT 0,
                    heartbeat_digest TEXT,
                    current_task_id TEXT,
                    archived_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runner_credentials (
                    credential_id TEXT PRIMARY KEY,
                    runner_id TEXT NOT NULL,
                    secret_digest TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    revoked_at TEXT,
                    FOREIGN KEY(runner_id) REFERENCES runner_registry(runner_id)
                );
                CREATE INDEX IF NOT EXISTS idx_runner_credentials_runner
                    ON runner_credentials(runner_id,state);
                CREATE TABLE IF NOT EXISTS runner_enrollments (
                    enrollment_id TEXT PRIMARY KEY,
                    runner_id TEXT NOT NULL,
                    token_digest TEXT NOT NULL UNIQUE,
                    request_id TEXT NOT NULL UNIQUE,
                    request_digest TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    claimed_at TEXT,
                    revoked_at TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(runner_id) REFERENCES runner_registry(runner_id)
                );
                CREATE TABLE IF NOT EXISTS runner_tasks (
                    task_id TEXT PRIMARY KEY,
                    create_request_id TEXT NOT NULL UNIQUE,
                    principal_hash TEXT NOT NULL,
                    project_alias TEXT NOT NULL,
                    instruction TEXT NOT NULL,
                    instruction_digest TEXT NOT NULL,
                    state TEXT NOT NULL,
                    stage TEXT,
                    summary TEXT,
                    candidate_id TEXT,
                    branch TEXT,
                    commits_json TEXT NOT NULL DEFAULT '[]',
                    result_hash TEXT,
                    test_summary TEXT,
                    changed_path_count INTEGER,
                    next_actions_json TEXT NOT NULL DEFAULT '[]',
                    error_code TEXT,
                    action_required TEXT,
                    preferred_runner_id TEXT,
                    required_labels_json TEXT NOT NULL DEFAULT '[]',
                    required_capabilities_json TEXT NOT NULL DEFAULT '[]',
                    assigned_runner_id TEXT,
                    assignment_epoch INTEGER NOT NULL DEFAULT 0,
                    lease_id TEXT,
                    lease_expires_at TEXT,
                    ever_running INTEGER NOT NULL DEFAULT 0,
                    sequence INTEGER NOT NULL DEFAULT 0,
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_runner_tasks_state
                    ON runner_tasks(state,created_at);
                CREATE TABLE IF NOT EXISTS runner_leases (
                    lease_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    runner_id TEXT NOT NULL,
                    assignment_epoch INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    released_at TEXT,
                    FOREIGN KEY(task_id) REFERENCES runner_tasks(task_id),
                    FOREIGN KEY(runner_id) REFERENCES runner_registry(runner_id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_runner_active_lease
                    ON runner_leases(task_id) WHERE state='active';
                CREATE TABLE IF NOT EXISTS runner_messages (
                    runner_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    assignment_epoch INTEGER NOT NULL,
                    sequence INTEGER NOT NULL,
                    body_digest TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(runner_id,task_id,kind,assignment_epoch,sequence)
                );
                CREATE TABLE IF NOT EXISTS runner_command_requests (
                    request_id TEXT PRIMARY KEY,
                    operation TEXT NOT NULL,
                    body_digest TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runner_admin_requests (
                    request_id TEXT PRIMARY KEY,
                    action TEXT NOT NULL,
                    body_digest TEXT NOT NULL,
                    runner_id TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runner_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    runner_id TEXT,
                    task_id TEXT,
                    event_type TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(runner_tasks)").fetchall()
            }
            for name, declaration in (
                ("branch", "TEXT"),
                ("commits_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("result_hash", "TEXT"),
            ):
                if name not in columns:
                    connection.execute(f"ALTER TABLE runner_tasks ADD COLUMN {name} {declaration}")
            enrollment_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(runner_enrollments)").fetchall()
            }
            if "revoked_at" not in enrollment_columns:
                connection.execute("ALTER TABLE runner_enrollments ADD COLUMN revoked_at TEXT")
            connection.execute(
                "INSERT INTO runner_meta(key,value) VALUES('schema_version',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )

    def schema_version(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM runner_meta WHERE key='schema_version'"
            ).fetchone()
        return 0 if row is None else int(row["value"])

    def create_enrollment(self, payload: dict[str, Any]) -> dict[str, Any]:
        required = {"display_name", "os", "arch", "labels", "allowed_projects", "max_concurrency", "request_id"}
        if set(payload) != required:
            raise StoreError("runner_payload_invalid", "新增 Runner 字段无效")
        display_name = _bounded_text(payload.get("display_name"), "display_name", maximum=80)
        os_name = payload.get("os")
        arch = payload.get("arch")
        if os_name not in {"macos", "linux"} or arch not in {"amd64", "aarch64"}:
            raise StoreError("runner_payload_invalid", "Runner 平台无效")
        labels = _safe_values(payload.get("labels"), "labels", LABEL_RE)
        projects = _safe_values(payload.get("allowed_projects"), "allowed_projects", PROJECT_RE)
        if not projects:
            raise StoreError("runner_payload_invalid", "Runner 至少需要一个项目白名单")
        max_concurrency = payload.get("max_concurrency")
        if max_concurrency != 1:
            raise StoreError("runner_payload_invalid", "v2 首期每个 Runner 并发必须为 1")
        request_id = self._request_id(payload.get("request_id"))
        body_digest = _digest(payload)
        now = self.clock()
        expires_at = (_parse_time(now) + dt.timedelta(minutes=15)).isoformat()
        runner_id = _opaque("RN")
        enrollment_id = _opaque("ENR", 12)
        enrollment_token = _opaque("ENROLL", 24)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM runner_enrollments WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if existing is not None:
                if existing["request_digest"] != body_digest:
                    connection.rollback()
                    raise StoreError("idempotency_conflict", "request_id 已用于不同 Runner 注册", status=409)
                row = connection.execute(
                    "SELECT * FROM runner_registry WHERE runner_id=?", (existing["runner_id"],)
                ).fetchone()
                connection.commit()
                result = self._public_runner(row, now=now)
                enrollment = self._public_enrollment(existing, now=now)
                result["enrollment"] = enrollment
                return {"runner": result, "enrollment": enrollment}
            connection.execute(
                "INSERT INTO runner_registry(runner_id,display_name,admin_state,work_state,os,arch,labels_json,allowed_projects_json,capabilities_json,max_concurrency,created_at,updated_at) "
                "VALUES(?,?,'pending','idle',?,?,?,?, '[]',1,?,?)",
                (runner_id, display_name, os_name, arch, _canonical(labels), _canonical(projects), now, now),
            )
            connection.execute(
                "INSERT INTO runner_enrollments(enrollment_id,runner_id,token_digest,request_id,request_digest,expires_at,created_at) VALUES(?,?,?,?,?,?,?)",
                (enrollment_id, runner_id, _secret_digest(enrollment_token), request_id, body_digest, expires_at, now),
            )
            self._event(connection, "runner_enrollment_created", runner_id=runner_id, metadata={"os": os_name, "arch": arch})
            row = connection.execute("SELECT * FROM runner_registry WHERE runner_id=?", (runner_id,)).fetchone()
            connection.commit()
        runner = self._public_runner(row, now=now)
        runner["enrollment"] = {
            "state": "pending",
            "expires_at": expires_at,
            "secret_available": False,
        }
        return {
            "runner": runner,
            "enrollment": {
                "state": "pending",
                "secret_available": True,
                "token": enrollment_token,
                "expires_at": expires_at,
                "runner_id": runner_id,
            },
        }

    def redeem_enrollment(self, payload: dict[str, Any]) -> dict[str, Any]:
        required = {"token", "runner_id", "protocol_version", "agent_version", "codex_version", "os", "arch", "capabilities", "projects", "labels", "policy_revision", "self_check"}
        if set(payload) != required:
            raise StoreError("runner_payload_invalid", "Runner enrollment 字段无效")
        token = _bounded_text(payload.get("token"), "token", maximum=256)
        runner_id = self._runner_id(payload.get("runner_id"))
        if payload.get("protocol_version") != 2:
            raise StoreError("runner_protocol_unsupported", "Runner 协议版本不兼容", status=409)
        agent_version = _bounded_text(payload.get("agent_version"), "agent_version", maximum=64)
        codex_version = _bounded_text(payload.get("codex_version"), "codex_version", maximum=64)
        capabilities = _capabilities(payload.get("capabilities"))
        projects = _safe_values(payload.get("projects"), "projects", PROJECT_RE)
        labels = _safe_values(payload.get("labels"), "labels", LABEL_RE)
        policy_revision = payload.get("policy_revision")
        if not isinstance(policy_revision, int) or isinstance(policy_revision, bool) or policy_revision < 1:
            raise StoreError("runner_payload_invalid", "policy_revision 无效")
        self_check = payload.get("self_check")
        if not isinstance(self_check, dict) or set(self_check) - {"ok", "checks", "error_code"}:
            raise StoreError("runner_payload_invalid", "self_check 无效")
        if not isinstance(self_check.get("ok"), bool):
            raise StoreError("runner_payload_invalid", "self_check.ok 无效")
        checks = self_check.get("checks", [])
        if not isinstance(checks, list) or len(checks) > 32 or any(not isinstance(value, str) or not LABEL_RE.fullmatch(value) for value in checks):
            raise StoreError("runner_payload_invalid", "self_check.checks 无效")
        now = self.clock()
        credential = _opaque("CRED", 24)
        credential_id = _opaque("CR", 12)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            enrollment = connection.execute(
                "SELECT * FROM runner_enrollments WHERE token_digest=? AND runner_id=?",
                (_secret_digest(token), runner_id),
            ).fetchone()
            if enrollment is None:
                connection.rollback()
                raise StoreError("enrollment_invalid", "Runner enrollment 无效", status=404)
            if enrollment["claimed_at"] is not None:
                connection.rollback()
                raise StoreError("enrollment_replayed", "Runner enrollment 已领取", status=409)
            if enrollment["revoked_at"] is not None:
                connection.rollback()
                raise StoreError("enrollment_revoked", "Runner enrollment 已撤销", status=409)
            if _parse_time(enrollment["expires_at"]) <= _parse_time(now):
                connection.rollback()
                raise StoreError("enrollment_expired", "Runner enrollment 已过期", status=409)
            runner = connection.execute("SELECT * FROM runner_registry WHERE runner_id=?", (runner_id,)).fetchone()
            assert runner is not None
            if runner["admin_state"] == "revoked" or runner["os"] != payload.get("os") or runner["arch"] != payload.get("arch"):
                connection.rollback()
                raise StoreError("enrollment_identity_mismatch", "Runner enrollment 身份不匹配", status=409)
            allowed = set(json.loads(runner["allowed_projects_json"]))
            if not set(projects).issubset(allowed):
                connection.rollback()
                raise StoreError("runner_policy_rejected", "Runner 上报项目超出白名单", status=409)
            registered_labels = set(json.loads(runner["labels_json"]))
            if not set(labels).issubset(registered_labels):
                connection.rollback()
                raise StoreError("runner_policy_rejected", "Runner 上报标签超出策略", status=409)
            if policy_revision != runner["policy_revision"]:
                connection.rollback()
                raise StoreError("runner_policy_rejected", "Runner policy revision 已过期", status=409)
            connection.execute("UPDATE runner_enrollments SET claimed_at=? WHERE enrollment_id=?", (now, enrollment["enrollment_id"]))
            connection.execute(
                "INSERT INTO runner_credentials(credential_id,runner_id,secret_digest,state,created_at) VALUES(?,?,?,'active',?)",
                (credential_id, runner_id, _secret_digest(credential), now),
            )
            connection.execute(
                "UPDATE runner_registry SET protocol_version=2,agent_version=?,codex_version=?,capabilities_json=?,self_check_json=?,updated_at=?,revision=revision+1 WHERE runner_id=?",
                (agent_version, codex_version, _canonical(capabilities), _canonical({"ok": self_check["ok"], "checks": sorted(set(checks)), "error_code": self_check.get("error_code")}), now, runner_id),
            )
            self._event(connection, "runner_enrollment_redeemed", runner_id=runner_id, metadata={"self_check_ok": self_check["ok"]})
            row = connection.execute("SELECT * FROM runner_registry WHERE runner_id=?", (runner_id,)).fetchone()
            connection.commit()
        runner = self._public_runner(row, now=now)
        runner["enrollment"] = {
            "state": "claimed",
            "expires_at": enrollment["expires_at"],
            "secret_available": False,
        }
        return {
            "runner": runner,
            "credential": {"credential_id": credential_id, "secret": credential, "secret_available": True},
        }

    def inspect_enrollment(self, token: str) -> dict[str, Any]:
        """Resolve one pending enrollment for Relay bootstrap without consuming it."""

        if not isinstance(token, str) or not 32 <= len(token) <= 512 or any(character.isspace() for character in token):
            raise StoreError("enrollment_invalid", "Runner enrollment 无效", status=404)
        now = self.clock()
        with self._connect() as connection:
            enrollment = connection.execute(
                "SELECT * FROM runner_enrollments WHERE token_digest=? ORDER BY created_at DESC LIMIT 1",
                (_secret_digest(token),),
            ).fetchone()
            if enrollment is None:
                raise StoreError("enrollment_invalid", "Runner enrollment 无效", status=404)
            runner = connection.execute(
                "SELECT * FROM runner_registry WHERE runner_id=?", (enrollment["runner_id"],)
            ).fetchone()
        assert runner is not None
        if enrollment["claimed_at"] is not None:
            raise StoreError("enrollment_replayed", "Runner enrollment 已领取", status=409)
        if enrollment["revoked_at"] is not None:
            raise StoreError("enrollment_revoked", "Runner enrollment 已撤销", status=409)
        if _parse_time(enrollment["expires_at"]) <= _parse_time(now):
            raise StoreError("enrollment_expired", "Runner enrollment 已过期", status=409)
        if runner["admin_state"] not in {"pending", "disabled"} or runner["archived_at"] is not None:
            raise StoreError("enrollment_state_conflict", "Runner 当前不可安装", status=409)
        return {
            "runner_id": runner["runner_id"],
            "os": runner["os"],
            "arch": runner["arch"],
            "allowed_projects": json.loads(runner["allowed_projects_json"]),
            "labels": json.loads(runner["labels_json"]),
            "policy_revision": int(runner["policy_revision"]),
            "expires_at": enrollment["expires_at"],
            "token": token,
        }

    def list_runners(self, *, include_archived: bool = False) -> dict[str, Any]:
        now = self.clock()
        where = "" if include_archived else "WHERE archived_at IS NULL"
        with self._connect() as connection:
            rows = connection.execute(f"SELECT * FROM runner_registry {where} ORDER BY created_at,runner_id").fetchall()
        runners = [self._public_runner(row, now=now) for row in rows]
        for runner in runners:
            runner["enrollment"] = self._latest_enrollment(str(runner["runner_id"]), now=now)
        return {
            "revision": max((runner["revision"] for runner in runners), default=0),
            "summary": {
                "total": len(runners),
                "enabled": sum(runner["admin_state"] == "enabled" for runner in runners),
                "online": sum(runner["connectivity_state"] == "online" for runner in runners),
                "busy": sum(runner["work_state"] == "busy" for runner in runners),
                "recovery_required": sum(runner["work_state"] == "recovery_required" for runner in runners),
            },
            "runners": runners,
        }

    def runner(self, runner_id: str) -> dict[str, Any]:
        runner_id = self._runner_id(runner_id)
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM runner_registry WHERE runner_id=?", (runner_id,)).fetchone()
            if row is None:
                raise StoreError("runner_not_found", "Runner 不存在", status=404)
            events = connection.execute(
                "SELECT event_type,metadata_json,created_at FROM runner_events WHERE runner_id=? ORDER BY event_id DESC LIMIT 50",
                (runner_id,),
            ).fetchall()
        result = self._public_runner(row, now=self.clock())
        result["enrollment"] = self._latest_enrollment(runner_id, now=self.clock())
        result["events"] = [
            {"event_type": event["event_type"], "metadata": json.loads(event["metadata_json"]), "created_at": event["created_at"]}
            for event in events
        ]
        return result

    def update_runner(self, runner_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        allowed = {"admin_state", "display_name", "labels", "allowed_projects", "revision", "request_id"}
        if not set(payload).issubset(allowed) or not {"revision", "request_id"}.issubset(payload):
            raise StoreError("runner_payload_invalid", "Runner 更新字段无效")
        runner_id = self._runner_id(runner_id)
        revision = self._revision(payload.get("revision"))
        request_id = self._request_id(payload.get("request_id"))
        body_digest = _digest(payload)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            replay = self._admin_replay(connection, request_id, "update", body_digest, runner_id)
            if replay is not None:
                connection.commit()
                return replay
            row = self._locked_runner(connection, runner_id, revision)
            updates: dict[str, Any] = {}
            event_metadata: dict[str, Any] = {}
            if "display_name" in payload:
                updates["display_name"] = _bounded_text(payload["display_name"], "display_name", maximum=80)
            if "labels" in payload:
                updates["labels_json"] = _canonical(_safe_values(payload["labels"], "labels", LABEL_RE))
                event_metadata["labels_changed"] = True
            if "allowed_projects" in payload:
                projects = _safe_values(payload["allowed_projects"], "allowed_projects", PROJECT_RE)
                if not projects:
                    connection.rollback()
                    raise StoreError("runner_payload_invalid", "Runner 至少需要一个项目白名单")
                updates["allowed_projects_json"] = _canonical(projects)
                updates["policy_revision"] = int(row["policy_revision"]) + 1
                event_metadata["projects_changed"] = True
            if "admin_state" in payload:
                target = payload["admin_state"]
                if target not in {"enabled", "disabled"}:
                    connection.rollback()
                    raise StoreError("runner_state_invalid", "PATCH 只允许启用或停用", status=409)
                current = row["admin_state"]
                if target == "enabled":
                    self_check = json.loads(row["self_check_json"] or "null")
                    if current not in {"pending", "disabled"} or not isinstance(self_check, dict) or self_check.get("ok") is not True:
                        connection.rollback()
                        raise StoreError("runner_state_conflict", "Runner 未通过自检或当前状态不能启用", status=409)
                elif row["current_task_id"] or row["work_state"] == "recovery_required":
                    connection.rollback()
                    raise StoreError("runner_state_conflict", "活动或恢复中的 Runner 必须先排空", status=409)
                updates["admin_state"] = target
                event_metadata["admin_state"] = target
            if not updates:
                connection.rollback()
                raise StoreError("runner_payload_invalid", "没有可更新字段")
            now = self.clock()
            assignments = ",".join(f"{key}=?" for key in updates)
            connection.execute(
                f"UPDATE runner_registry SET {assignments},revision=revision+1,updated_at=? WHERE runner_id=?",
                (*updates.values(), now, runner_id),
            )
            self._event(connection, "runner_updated", runner_id=runner_id, metadata=event_metadata)
            updated = connection.execute("SELECT * FROM runner_registry WHERE runner_id=?", (runner_id,)).fetchone()
            result = self._public_runner(updated, now=now)
            self._save_admin_replay(connection, request_id, "update", body_digest, runner_id, result)
            connection.commit()
        return result

    def drain(self, runner_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._admin_action(runner_id, payload, "drain")

    def emergency_disable(self, runner_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._admin_action(runner_id, payload, "emergency_disable")

    def rotate_credential(self, runner_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._admin_action(runner_id, payload, "rotate_credential")

    def delete_runner(self, runner_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._admin_action(runner_id, payload, "delete")

    def request_self_check(self, runner_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._admin_action(runner_id, payload, "self_check")

    def revoke_enrollment(self, runner_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        if set(payload) != {"revision", "request_id"}:
            raise StoreError("runner_payload_invalid", "撤销注册请求字段无效")
        runner_id = self._runner_id(runner_id)
        revision = self._revision(payload.get("revision"))
        request_id = self._request_id(payload.get("request_id"))
        body_digest = _digest(payload)
        now = self.clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            replay = self._admin_replay(connection, request_id, "revoke_enrollment", body_digest, runner_id)
            if replay is not None:
                connection.commit()
                return replay
            row = self._locked_runner(connection, runner_id, revision)
            enrollment = connection.execute(
                "SELECT * FROM runner_enrollments WHERE runner_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1",
                (runner_id,),
            ).fetchone()
            if (
                row["admin_state"] == "revoked"
                or enrollment is None
                or self._public_enrollment(enrollment, now=now)["state"] != "pending"
            ):
                connection.rollback()
                raise StoreError("enrollment_state_conflict", "当前 Runner 没有可撤销的 enrollment", status=409)
            connection.execute(
                "UPDATE runner_enrollments SET revoked_at=? WHERE enrollment_id=?",
                (now, enrollment["enrollment_id"]),
            )
            connection.execute(
                "UPDATE runner_registry SET revision=revision+1,updated_at=? WHERE runner_id=?",
                (now, runner_id),
            )
            self._event(connection, "runner_enrollment_revoked", runner_id=runner_id, metadata={})
            updated = connection.execute(
                "SELECT * FROM runner_registry WHERE runner_id=?", (runner_id,)
            ).fetchone()
            revoked = connection.execute(
                "SELECT * FROM runner_enrollments WHERE enrollment_id=?",
                (enrollment["enrollment_id"],),
            ).fetchone()
            result = {"runner": self._public_runner(updated, now=now)}
            result["runner"]["enrollment"] = self._public_enrollment(revoked, now=now)
            self._save_admin_replay(
                connection, request_id, "revoke_enrollment", body_digest, runner_id, result
            )
            connection.commit()
        return result

    def regenerate_enrollment(self, runner_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        if set(payload) != {"revision", "request_id"}:
            raise StoreError("runner_payload_invalid", "重新生成注册请求字段无效")
        runner_id = self._runner_id(runner_id)
        revision = self._revision(payload.get("revision"))
        request_id = self._request_id(payload.get("request_id"))
        body_digest = _digest(payload)
        now = self.clock()
        expires_at = (_parse_time(now) + dt.timedelta(minutes=15)).isoformat()
        enrollment_id = _opaque("ENR", 12)
        enrollment_token = _opaque("ENROLL", 24)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            replay = self._admin_replay(
                connection, request_id, "regenerate_enrollment", body_digest, runner_id
            )
            if replay is not None:
                connection.commit()
                return replay
            row = self._locked_runner(connection, runner_id, revision)
            claimed = connection.execute(
                "SELECT 1 FROM runner_enrollments WHERE runner_id=? AND claimed_at IS NOT NULL LIMIT 1",
                (runner_id,),
            ).fetchone()
            active_credential = connection.execute(
                "SELECT 1 FROM runner_credentials WHERE runner_id=? AND state='active' LIMIT 1",
                (runner_id,),
            ).fetchone()
            if (
                row["admin_state"] not in {"pending", "disabled"}
                or row["current_task_id"] is not None
                or claimed is not None
                or active_credential is not None
            ):
                connection.rollback()
                raise StoreError(
                    "enrollment_state_conflict",
                    "已注册 Runner 不能重新生成 enrollment，请使用凭据轮换",
                    status=409,
                )
            connection.execute(
                "UPDATE runner_enrollments SET revoked_at=? WHERE runner_id=? AND claimed_at IS NULL AND revoked_at IS NULL",
                (now, runner_id),
            )
            connection.execute(
                "INSERT INTO runner_enrollments(enrollment_id,runner_id,token_digest,request_id,request_digest,expires_at,created_at) VALUES(?,?,?,?,?,?,?)",
                (
                    enrollment_id,
                    runner_id,
                    _secret_digest(enrollment_token),
                    request_id,
                    body_digest,
                    expires_at,
                    now,
                ),
            )
            connection.execute(
                "UPDATE runner_registry SET revision=revision+1,updated_at=? WHERE runner_id=?",
                (now, runner_id),
            )
            self._event(connection, "runner_enrollment_regenerated", runner_id=runner_id, metadata={})
            updated = connection.execute(
                "SELECT * FROM runner_registry WHERE runner_id=?", (runner_id,)
            ).fetchone()
            runner = self._public_runner(updated, now=now)
            runner["enrollment"] = {
                "state": "pending",
                "expires_at": expires_at,
                "secret_available": False,
            }
            result = {
                "runner": runner,
                "enrollment": {
                    "state": "pending",
                    "secret_available": True,
                    "token": enrollment_token,
                    "expires_at": expires_at,
                    "runner_id": runner_id,
                },
            }
            replay_result = {
                "runner": runner,
                "enrollment": {
                    "state": "pending",
                    "secret_available": False,
                    "expires_at": expires_at,
                    "runner_id": runner_id,
                },
            }
            self._save_admin_replay(
                connection,
                request_id,
                "regenerate_enrollment",
                body_digest,
                runner_id,
                replay_result,
            )
            connection.commit()
        return result

    def _admin_action(self, runner_id: str, payload: dict[str, Any], action: str) -> dict[str, Any]:
        if set(payload) != {"revision", "request_id"}:
            raise StoreError("runner_payload_invalid", "Runner 管理请求字段无效")
        runner_id = self._runner_id(runner_id)
        revision = self._revision(payload.get("revision"))
        request_id = self._request_id(payload.get("request_id"))
        body_digest = _digest(payload)
        now = self.clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            replay = self._admin_replay(connection, request_id, action, body_digest, runner_id)
            if replay is not None:
                connection.commit()
                return replay
            row = self._locked_runner(connection, runner_id, revision)
            extra: dict[str, Any] = {}
            if action == "drain":
                if row["admin_state"] != "enabled":
                    connection.rollback()
                    raise StoreError("runner_state_conflict", "只有 enabled Runner 可以排空", status=409)
                target = "draining" if row["current_task_id"] else "disabled"
                connection.execute(
                    "UPDATE runner_registry SET admin_state=?,revision=revision+1,updated_at=? WHERE runner_id=?",
                    (target, now, runner_id),
                )
            elif action == "emergency_disable":
                if row["admin_state"] == "revoked":
                    connection.rollback()
                    raise StoreError("runner_state_conflict", "已吊销 Runner 不能操作", status=409)
                work_state = row["work_state"]
                task_id = row["current_task_id"]
                if task_id:
                    task = connection.execute("SELECT * FROM runner_tasks WHERE task_id=?", (task_id,)).fetchone()
                    if task is not None and task["ever_running"]:
                        work_state = "recovery_required"
                        connection.execute(
                            "UPDATE runner_tasks SET state='recovery_required',stage='recovery',action_required='人工核对 Runner 与工作树后再继续',updated_at=? WHERE task_id=?",
                            (now, task_id),
                        )
                    else:
                        work_state = "idle"
                        connection.execute(
                            "UPDATE runner_tasks SET state='cancelled',stage='cancelled',assigned_runner_id=NULL,lease_id=NULL,lease_expires_at=NULL,updated_at=? WHERE task_id=?",
                            (now, task_id),
                        )
                        connection.execute(
                            "UPDATE runner_leases SET state='released',released_at=? WHERE task_id=? AND state='active'",
                            (now, task_id),
                        )
                        task_id = None
                connection.execute(
                    "UPDATE runner_registry SET admin_state='disabled',work_state=?,current_task_id=?,revision=revision+1,updated_at=? WHERE runner_id=?",
                    (work_state, task_id, now, runner_id),
                )
            elif action == "rotate_credential":
                if row["admin_state"] == "revoked":
                    connection.rollback()
                    raise StoreError("runner_state_conflict", "已吊销 Runner 不能轮换凭据", status=409)
                active_credential = connection.execute(
                    "SELECT 1 FROM runner_credentials WHERE runner_id=? AND state='active' LIMIT 1",
                    (runner_id,),
                ).fetchone()
                if active_credential is None:
                    connection.rollback()
                    raise StoreError(
                        "runner_state_conflict",
                        "Runner 尚未领取长期凭据，不能执行凭据轮换",
                        status=409,
                    )
                secret = _opaque("CRED", 24)
                credential_id = _opaque("CR", 12)
                connection.execute(
                    "UPDATE runner_credentials SET state='revoked',revoked_at=? WHERE runner_id=? AND state='active'",
                    (now, runner_id),
                )
                connection.execute(
                    "INSERT INTO runner_credentials(credential_id,runner_id,secret_digest,state,created_at) VALUES(?,?,?,'active',?)",
                    (credential_id, runner_id, _secret_digest(secret), now),
                )
                connection.execute(
                    "UPDATE runner_registry SET revision=revision+1,updated_at=? WHERE runner_id=?",
                    (now, runner_id),
                )
                extra["credential"] = {"credential_id": credential_id, "secret": secret, "secret_available": True}
            elif action == "delete":
                if row["admin_state"] != "disabled" or row["current_task_id"] or row["work_state"] != "idle":
                    connection.rollback()
                    raise StoreError("runner_delete_conflict", "仅允许删除已停用且无活动/恢复任务的 Runner", status=409)
                connection.execute(
                    "UPDATE runner_credentials SET state='revoked',revoked_at=? WHERE runner_id=? AND state='active'",
                    (now, runner_id),
                )
                connection.execute(
                    "UPDATE runner_registry SET admin_state='revoked',archived_at=?,revision=revision+1,updated_at=? WHERE runner_id=?",
                    (now, now, runner_id),
                )
            elif action == "self_check":
                if row["admin_state"] == "revoked":
                    connection.rollback()
                    raise StoreError("runner_state_conflict", "已吊销 Runner 不能自检", status=409)
                connection.execute(
                    "UPDATE runner_registry SET revision=revision+1,updated_at=? WHERE runner_id=?",
                    (now, runner_id),
                )
                extra["self_check_request"] = {"queued": True}
            else:
                connection.rollback()
                raise AssertionError(action)
            self._event(connection, f"runner_{action}", runner_id=runner_id, task_id=row["current_task_id"], metadata={})
            updated = connection.execute("SELECT * FROM runner_registry WHERE runner_id=?", (runner_id,)).fetchone()
            result = {"runner": self._public_runner(updated, now=now), **extra}
            replay_result = json.loads(_canonical(result))
            if "credential" in replay_result:
                replay_result["credential"] = {"credential_id": replay_result["credential"]["credential_id"], "secret_available": False}
            self._save_admin_replay(connection, request_id, action, body_digest, runner_id, replay_result)
            connection.commit()
        return result

    def verify_credential(self, runner_id: str, secret: str) -> None:
        runner_id = self._runner_id(runner_id)
        if not isinstance(secret, str) or len(secret) < 32:
            raise StoreError("runner_not_authorized", "Runner 凭据无效", status=401)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT r.admin_state FROM runner_credentials c JOIN runner_registry r ON r.runner_id=c.runner_id "
                "WHERE c.runner_id=? AND c.secret_digest=? AND c.state='active'",
                (runner_id, _secret_digest(secret)),
            ).fetchone()
        if row is None or row["admin_state"] == "revoked":
            raise StoreError("runner_not_authorized", "Runner 凭据已吊销或无效", status=401)

    def heartbeat(self, payload: dict[str, Any], *, credential: str) -> dict[str, Any]:
        required = {
            "version",
            "message_type",
            "runner_id",
            "task_id",
            "assignment_epoch",
            "sequence",
            "created_at",
            "expires_at",
            "body_digest",
            "online",
            "protocol_version",
            "agent_version",
            "codex_version",
            "os",
            "arch",
            "labels",
            "allowed_projects",
            "capabilities",
            "queue_depth",
            "work_state",
            "active_lease_id",
            "self_check",
            "policy_revision",
            "updated_at",
        }
        optional = {"error_code"}
        if required - set(payload) or set(payload) - required - optional:
            raise StoreError("runner_payload_invalid", "Runner heartbeat 字段无效")
        self._validate_envelope(payload, "heartbeat", heartbeat=True)
        try:
            _parse_time(str(payload["updated_at"]))
        except ValueError as exc:
            raise StoreError("runner_payload_invalid", "Runner heartbeat updated_at 无效") from exc
        runner_id = self._runner_id(payload["runner_id"])
        self.verify_credential(runner_id, credential)
        sequence = int(payload["sequence"])
        body_digest = str(payload["body_digest"])
        if payload.get("protocol_version") != 2 or payload.get("work_state") not in WORK_STATES:
            raise StoreError("runner_payload_invalid", "Runner heartbeat 状态无效")
        if payload.get("os") not in {"macos", "linux"} or payload.get("arch") not in {"amd64", "aarch64"}:
            raise StoreError("runner_payload_invalid", "Runner heartbeat 平台无效")
        if not isinstance(payload.get("online"), bool):
            raise StoreError("runner_payload_invalid", "Runner online 状态无效")
        if not isinstance(payload.get("queue_depth"), int) or isinstance(payload.get("queue_depth"), bool) or payload["queue_depth"] < 0:
            raise StoreError("runner_payload_invalid", "Runner queue_depth 无效")
        capabilities = _capabilities(payload.get("capabilities"))
        projects = _safe_values(payload.get("allowed_projects"), "allowed_projects", PROJECT_RE, maximum=64)
        labels = _safe_values(payload.get("labels"), "labels", LABEL_RE)
        self_check = payload.get("self_check")
        if self_check not in {"ok", "warning", "error"}:
            raise StoreError("runner_payload_invalid", "heartbeat self_check 无效")
        error_code = payload.get("error_code")
        if error_code is not None and (not isinstance(error_code, str) or not SAFE_ID_RE.fullmatch(error_code)):
            raise StoreError("runner_payload_invalid", "heartbeat error_code 无效")
        now = self.clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM runner_registry WHERE runner_id=?", (runner_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise StoreError("runner_not_found", "Runner 不存在", status=404)
            if sequence < row["heartbeat_sequence"]:
                connection.rollback()
                raise StoreError("runner_sequence_stale", "heartbeat sequence 已过期", status=409)
            if sequence == row["heartbeat_sequence"]:
                if body_digest != row["heartbeat_digest"]:
                    connection.rollback()
                    raise StoreError("runner_message_conflict", "同 sequence heartbeat 正文冲突", status=409)
                connection.commit()
                return self._public_runner(row, now=now)
            allowed = set(json.loads(row["allowed_projects_json"]))
            if not set(projects).issubset(allowed):
                connection.rollback()
                raise StoreError("runner_policy_rejected", "Runner 上报项目超出白名单", status=409)
            if not set(labels).issubset(set(json.loads(row["labels_json"]))):
                connection.rollback()
                raise StoreError("runner_policy_rejected", "Runner 上报标签超出策略", status=409)
            if payload["os"] != row["os"] or payload["arch"] != row["arch"]:
                connection.rollback()
                raise StoreError("runner_identity_mismatch", "Runner 平台身份不匹配", status=409)
            if payload["policy_revision"] != row["policy_revision"]:
                connection.rollback()
                raise StoreError("runner_policy_rejected", "Runner policy revision 已过期", status=409)
            active_task_id = payload.get("task_id")
            active_lease_id = payload.get("active_lease_id")
            if active_task_id is None:
                if payload["assignment_epoch"] != 0 or active_lease_id is not None:
                    connection.rollback()
                    raise StoreError("runner_assignment_stale", "Runner heartbeat assignment 无效", status=409)
            elif active_task_id != row["current_task_id"]:
                connection.rollback()
                raise StoreError("runner_assignment_stale", "Runner heartbeat task 不匹配", status=409)
            else:
                task = connection.execute(
                    "SELECT assignment_epoch,lease_id FROM runner_tasks WHERE task_id=?",
                    (active_task_id,),
                ).fetchone()
                if task is None or task["assignment_epoch"] != payload["assignment_epoch"] or task["lease_id"] != active_lease_id:
                    connection.rollback()
                    raise StoreError("runner_assignment_stale", "Runner heartbeat lease 不匹配", status=409)
            requested_work_state = payload["work_state"]
            if row["current_task_id"] and requested_work_state == "idle":
                requested_work_state = row["work_state"]
            connection.execute(
                "UPDATE runner_registry SET heartbeat_sequence=?,heartbeat_digest=?,last_heartbeat_at=?,protocol_version=2,agent_version=?,codex_version=?,capabilities_json=?,self_check_json=?,work_state=?,updated_at=? WHERE runner_id=?",
                (
                    sequence,
                    body_digest,
                    now if payload["online"] else None,
                    _bounded_text(payload["agent_version"], "agent_version", maximum=64),
                    _bounded_text(payload["codex_version"], "codex_version", maximum=64, required=False),
                    _canonical(capabilities),
                    _canonical(
                        {
                            "ok": self_check == "ok",
                            "status": self_check,
                            "error_code": error_code,
                        }
                    ),
                    requested_work_state,
                    now,
                    runner_id,
                ),
            )
            updated = connection.execute("SELECT * FROM runner_registry WHERE runner_id=?", (runner_id,)).fetchone()
            connection.commit()
        return self._public_runner(updated, now=now)

    def create_work_task(self, payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        required = {"version", "request_id", "operation", "source", "project_alias", "instruction"}
        if set(payload) != required or payload.get("version") != 2 or payload.get("operation") != "start":
            raise StoreError("runner_command_invalid", "Runner Manager start 请求无效")
        request_id = self._request_id(payload.get("request_id"))
        source = payload.get("source")
        if not isinstance(source, dict) or set(source) != {"channel", "principal_hash", "role"} or source.get("channel") != "weixin" or source.get("role") != "owner":
            raise StoreError("runner_command_invalid", "Runner Manager source 无效")
        principal_hash = source.get("principal_hash")
        if not isinstance(principal_hash, str) or not SHA256_RE.fullmatch(principal_hash):
            raise StoreError("runner_command_invalid", "Runner Manager principal 无效")
        project = payload.get("project_alias")
        if not isinstance(project, str) or not PROJECT_RE.fullmatch(project):
            raise StoreError("runner_command_invalid", "Runner Manager project 无效")
        instruction = _bounded_text(payload.get("instruction"), "instruction", maximum=12000)
        body_digest = _digest(payload)
        now = self.clock()
        task_id = "RW-" + hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:20].upper()
        expires_at = (_parse_time(now) + dt.timedelta(seconds=self.task_ttl_seconds)).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT * FROM runner_command_requests WHERE request_id=?", (request_id,)).fetchone()
            if existing is not None:
                if existing["body_digest"] != body_digest or existing["operation"] != "start":
                    connection.rollback()
                    raise StoreError("idempotency_conflict", "request_id 已用于不同 Runner 命令", status=409)
                task = connection.execute("SELECT * FROM runner_tasks WHERE task_id=?", (existing["task_id"],)).fetchone()
                connection.commit()
                return self._public_task(task), True
            connection.execute(
                "INSERT INTO runner_tasks(task_id,create_request_id,principal_hash,project_alias,instruction,instruction_digest,state,expires_at,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,'waiting_runner',?,?,?)",
                (task_id, request_id, principal_hash, project, instruction, _digest(instruction), expires_at, now, now),
            )
            connection.execute(
                "INSERT INTO runner_command_requests(request_id,operation,body_digest,task_id,created_at) VALUES(?,?,?,?,?)",
                (request_id, "start", body_digest, task_id, now),
            )
            self._event(connection, "runner_task_created", task_id=task_id, metadata={"project_alias": project})
            task = connection.execute("SELECT * FROM runner_tasks WHERE task_id=?", (task_id,)).fetchone()
            connection.commit()
        return self._public_task(task), False

    def command_task(
        self,
        payload: dict[str, Any],
        *,
        control_available: bool = True,
    ) -> tuple[dict[str, Any], bool]:
        operation = payload.get("operation")
        expected = {
            "status": {"version", "request_id", "operation", "source", "task_id"},
            "cancel": {"version", "request_id", "operation", "source", "task_id"},
            "continue": {"version", "request_id", "operation", "source", "task_id", "instruction"},
        }.get(operation)
        if expected is None or set(payload) != expected or payload.get("version") != 2:
            raise StoreError("runner_command_invalid", "Runner Manager 命令无效")
        request_id = self._request_id(payload.get("request_id"))
        task_id = self._task_id(payload.get("task_id"))
        source = payload.get("source")
        principal_hash = source.get("principal_hash") if isinstance(source, dict) else None
        if not isinstance(source, dict) or source.get("channel") != "weixin" or source.get("role") != "owner" or not isinstance(principal_hash, str) or not SHA256_RE.fullmatch(principal_hash):
            raise StoreError("runner_command_invalid", "Runner Manager source 无效")
        body_digest = _digest(payload)
        now = self.clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            replay = connection.execute("SELECT * FROM runner_command_requests WHERE request_id=?", (request_id,)).fetchone()
            if replay is not None:
                if replay["body_digest"] != body_digest or replay["operation"] != operation:
                    connection.rollback()
                    raise StoreError("idempotency_conflict", "request_id 已用于不同 Runner 命令", status=409)
                task = connection.execute("SELECT * FROM runner_tasks WHERE task_id=?", (replay["task_id"],)).fetchone()
                connection.commit()
                return self._public_task(task), True
            task = connection.execute("SELECT * FROM runner_tasks WHERE task_id=?", (task_id,)).fetchone()
            if task is None or task["principal_hash"] != principal_hash:
                connection.rollback()
                raise StoreError("runner_task_not_found", "Runner task 不存在", status=404)
            if operation == "cancel":
                if task["state"] in TERMINAL_TASK_STATES:
                    pass
                elif task["state"] == "awaiting_confirmation":
                    connection.execute(
                        "UPDATE runner_tasks SET state='cancelled',stage='cancelled',action_required=NULL,updated_at=? WHERE task_id=?",
                        (now, task_id),
                    )
                elif task["assigned_runner_id"]:
                    if not control_available:
                        connection.rollback()
                        raise StoreError("runner_relay_unavailable", "Runner Relay 当前不可用", status=503)
                    connection.execute(
                        "UPDATE runner_tasks SET action_required='等待 Runner 确认取消；未知结果不得转移',updated_at=? WHERE task_id=?",
                        (now, task_id),
                    )
                else:
                    connection.execute(
                        "UPDATE runner_tasks SET state='cancelled',stage='cancelled',assigned_runner_id=NULL,lease_id=NULL,lease_expires_at=NULL,updated_at=? WHERE task_id=?",
                        (now, task_id),
                    )
                    if task["assigned_runner_id"]:
                        connection.execute(
                            "UPDATE runner_registry SET work_state='idle',current_task_id=NULL,updated_at=? WHERE runner_id=?",
                            (now, task["assigned_runner_id"]),
                        )
                    connection.execute(
                        "UPDATE runner_leases SET state='released',released_at=? WHERE task_id=? AND state='active'",
                        (now, task_id),
                    )
            elif operation == "continue":
                if not control_available:
                    connection.rollback()
                    raise StoreError("runner_relay_unavailable", "Runner Relay 当前不可用", status=503)
                instruction = _bounded_text(payload.get("instruction"), "instruction", maximum=12000)
                if task["state"] not in TERMINAL_TASK_STATES | {"awaiting_confirmation"}:
                    connection.rollback()
                    raise StoreError("runner_task_state_conflict", "仅终态 task 可以继续", status=409)
                if not task["assigned_runner_id"] or not task["lease_id"]:
                    connection.rollback()
                    raise StoreError("runner_task_state_conflict", "task 没有可恢复的 Runner Session", status=409)
                runner = connection.execute(
                    "SELECT * FROM runner_registry WHERE runner_id=?",
                    (task["assigned_runner_id"],),
                ).fetchone()
                if runner is None or runner["admin_state"] != "enabled" or self._connectivity(runner, _parse_time(now)) != "online":
                    connection.rollback()
                    raise StoreError("runner_unavailable", "原 Runner 当前不可用于继续任务", status=503)
                lease_expires_at = (_parse_time(now) + dt.timedelta(seconds=self.lease_ttl_seconds)).isoformat()
                connection.execute(
                    "UPDATE runner_tasks SET instruction=?,instruction_digest=?,state='dispatched',stage='continuation',summary=NULL,candidate_id=NULL,branch=NULL,commits_json='[]',result_hash=NULL,test_summary=NULL,changed_path_count=NULL,next_actions_json='[]',error_code=NULL,action_required=NULL,lease_expires_at=?,expires_at=?,updated_at=? WHERE task_id=?",
                    (
                        instruction,
                        _digest(instruction),
                        lease_expires_at,
                        (_parse_time(now) + dt.timedelta(seconds=self.task_ttl_seconds)).isoformat(),
                        now,
                        task_id,
                    ),
                )
                connection.execute(
                    "UPDATE runner_leases SET state='active',expires_at=?,released_at=NULL WHERE lease_id=?",
                    (lease_expires_at, task["lease_id"]),
                )
                connection.execute(
                    "UPDATE runner_registry SET work_state='busy',current_task_id=?,updated_at=? WHERE runner_id=?",
                    (task_id, now, task["assigned_runner_id"]),
                )
            connection.execute(
                "INSERT INTO runner_command_requests(request_id,operation,body_digest,task_id,created_at) VALUES(?,?,?,?,?)",
                (request_id, operation, body_digest, task_id, now),
            )
            self._event(connection, f"runner_task_{operation}", task_id=task_id, metadata={})
            updated = connection.execute("SELECT * FROM runner_tasks WHERE task_id=?", (task_id,)).fetchone()
            connection.commit()
        return self._public_task(updated), False

    def control_document(
        self,
        task_id: str,
        *,
        action: str,
        control_id: str,
        instruction: str | None = None,
    ) -> dict[str, Any]:
        task_id = self._task_id(task_id)
        if action not in {"continue", "cancel"} or not isinstance(control_id, str) or not SAFE_ID_RE.fullmatch(control_id):
            raise StoreError("runner_command_invalid", "Runner control 无效")
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM runner_tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None or not row["assigned_runner_id"] or not row["lease_id"]:
            raise StoreError("runner_task_state_conflict", "Runner task 没有活动 assignment", status=409)
        if action == "continue":
            instruction = _bounded_text(instruction, "instruction", maximum=12000)
        elif instruction is not None:
            raise StoreError("runner_command_invalid", "cancel 不能包含 instruction")
        now = self.clock()
        expires_at = (_parse_time(now) + dt.timedelta(minutes=10)).isoformat()
        document: dict[str, Any] = {
            "version": 2,
            "message_type": "control",
            "runner_id": row["assigned_runner_id"],
            "task_id": row["task_id"],
            "assignment_epoch": row["assignment_epoch"],
            "sequence": int(row["sequence"]) + 1,
            "created_at": now,
            "expires_at": expires_at,
            "control_id": control_id,
            "lease_id": row["lease_id"],
            "action": action,
            "source": {
                "channel": "weixin",
                "principal_hash": row["principal_hash"],
                "role": "owner",
            },
            "authority": "owner_runner_development_v2",
        }
        if instruction is not None:
            document["instruction"] = instruction
        document["body_digest"] = _digest(document)
        return document

    def claim_next(self) -> dict[str, Any] | None:
        now = self.clock()
        now_time = _parse_time(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            tasks = connection.execute(
                "SELECT * FROM runner_tasks WHERE state='waiting_runner' ORDER BY created_at,task_id"
            ).fetchall()
            runners = connection.execute(
                "SELECT * FROM runner_registry WHERE archived_at IS NULL AND admin_state='enabled' AND work_state='idle' AND current_task_id IS NULL ORDER BY created_at,runner_id"
            ).fetchall()
            selected_task: sqlite3.Row | None = None
            selected_runner: sqlite3.Row | None = None
            for task in tasks:
                if _parse_time(task["expires_at"]) <= now_time:
                    connection.execute("UPDATE runner_tasks SET state='expired',stage='expired',updated_at=? WHERE task_id=?", (now, task["task_id"]))
                    continue
                for runner in runners:
                    if self._connectivity(runner, now_time) != "online":
                        continue
                    if task["preferred_runner_id"] and task["preferred_runner_id"] != runner["runner_id"]:
                        continue
                    if task["project_alias"] not in set(json.loads(runner["allowed_projects_json"])):
                        continue
                    if not set(json.loads(task["required_labels_json"])).issubset(set(json.loads(runner["labels_json"]))):
                        continue
                    if not set(json.loads(task["required_capabilities_json"])).issubset(set(json.loads(runner["capabilities_json"]))):
                        continue
                    selected_task, selected_runner = task, runner
                    break
                if selected_task is not None:
                    break
            if selected_task is None or selected_runner is None:
                connection.commit()
                return None
            epoch = int(selected_task["assignment_epoch"]) + 1
            lease_id = _opaque("LS", 12)
            lease_expires_at = (now_time + dt.timedelta(seconds=self.lease_ttl_seconds)).isoformat()
            connection.execute(
                "INSERT INTO runner_leases(lease_id,task_id,runner_id,assignment_epoch,state,created_at,expires_at) VALUES(?,?,?,?, 'active',?,?)",
                (lease_id, selected_task["task_id"], selected_runner["runner_id"], epoch, now, lease_expires_at),
            )
            connection.execute(
                "UPDATE runner_tasks SET state='leased',stage='dispatch',assigned_runner_id=?,assignment_epoch=?,lease_id=?,lease_expires_at=?,sequence=1,updated_at=? WHERE task_id=? AND state='waiting_runner'",
                (selected_runner["runner_id"], epoch, lease_id, lease_expires_at, now, selected_task["task_id"]),
            )
            if connection.total_changes < 2:
                connection.rollback()
                return None
            connection.execute(
                "UPDATE runner_registry SET work_state='busy',current_task_id=?,updated_at=? WHERE runner_id=?",
                (selected_task["task_id"], now, selected_runner["runner_id"]),
            )
            self._event(connection, "runner_task_leased", runner_id=selected_runner["runner_id"], task_id=selected_task["task_id"], metadata={"assignment_epoch": epoch})
            task = connection.execute("SELECT * FROM runner_tasks WHERE task_id=?", (selected_task["task_id"],)).fetchone()
            connection.commit()
        request = {
            "version": 2,
            "message_type": "request",
            "runner_id": task["assigned_runner_id"],
            "task_id": task["task_id"],
            "assignment_epoch": task["assignment_epoch"],
            "sequence": 1,
            "created_at": now,
            "expires_at": task["lease_expires_at"],
            "message_id": f"MSG-{task['task_id']}-{task['assignment_epoch']}",
            "lease_id": task["lease_id"],
            "lease_expires_at": task["lease_expires_at"],
            "project_alias": task["project_alias"],
            "operation": "start",
            "instruction": task["instruction"],
            "policy_revision": selected_runner["policy_revision"],
            "required_labels": json.loads(task["required_labels_json"]),
            "source": {
                "channel": "weixin",
                "principal_hash": task["principal_hash"],
                "role": "owner",
            },
            "authority": "owner_runner_development_v2",
        }
        request["body_digest"] = _digest(request)
        return {"task": self._public_task(task), "request": request}

    def mark_dispatched(self, task_id: str) -> dict[str, Any]:
        return self._set_task_stage(task_id, from_states={"leased", "dispatched"}, state="dispatched", stage="relay")

    def record_status(self, payload: dict[str, Any], *, credential: str) -> dict[str, Any]:
        required = {
            "version",
            "message_type",
            "runner_id",
            "task_id",
            "assignment_epoch",
            "sequence",
            "created_at",
            "expires_at",
            "body_digest",
            "lease_id",
            "state",
            "stage",
            "updated_at",
        }
        optional = {"queue_position", "candidate_id", "error_code", "action_required"}
        if required - set(payload) or set(payload) - required - optional or payload.get("state") not in {"queued", "running", "awaiting_confirmation", "recovery_required"}:
            raise StoreError("runner_payload_invalid", "Runner status 字段无效")
        self._validate_envelope(payload, "status")
        try:
            _parse_time(str(payload["updated_at"]))
        except ValueError as exc:
            raise StoreError("runner_payload_invalid", "Runner status updated_at 无效") from exc
        runner_id = self._runner_id(payload.get("runner_id"))
        task_id = self._task_id(payload.get("task_id"))
        self.verify_credential(runner_id, credential)
        if payload.get("stage") not in {"waiting_runner", "preflight", "queued", "workspace", "codex", "verify", "git", "handoff", "recovery"}:
            raise StoreError("runner_payload_invalid", "Runner status stage 无效")
        candidate_id = payload.get("candidate_id")
        if candidate_id is not None and (not isinstance(candidate_id, str) or not SHA256_RE.fullmatch(candidate_id)):
            raise StoreError("runner_payload_invalid", "Runner status candidate_id 无效")
        error_code = payload.get("error_code")
        if error_code is not None and (not isinstance(error_code, str) or not SAFE_ID_RE.fullmatch(error_code)):
            raise StoreError("runner_payload_invalid", "Runner status error_code 无效")
        action_required = payload.get("action_required")
        if action_required is not None and action_required not in {"clarification", "production_confirmation", "recovery_review"}:
            raise StoreError("runner_payload_invalid", "Runner status action_required 无效")
        queue_position = payload.get("queue_position")
        if queue_position is not None and (not isinstance(queue_position, int) or isinstance(queue_position, bool) or queue_position < 0):
            raise StoreError("runner_payload_invalid", "Runner status queue_position 无效")
        outcome = self._record_message(payload, kind="status")
        if outcome == "duplicate":
            return self.work_task(task_id)
        now = self.clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task = self._locked_assignment(connection, payload)
            state = payload["state"]
            connection.execute(
                "UPDATE runner_tasks SET state=?,stage=?,candidate_id=COALESCE(?,candidate_id),error_code=?,action_required=?,ever_running=CASE WHEN ?='running' THEN 1 ELSE ever_running END,sequence=?,updated_at=? WHERE task_id=?",
                (
                    state,
                    _bounded_text(payload["stage"], "stage", maximum=64),
                    candidate_id,
                    error_code,
                    action_required,
                    state,
                    payload["sequence"],
                    now,
                    task_id,
                ),
            )
            if state == "recovery_required":
                connection.execute(
                    "UPDATE runner_registry SET work_state='recovery_required',current_task_id=?,updated_at=? WHERE runner_id=?",
                    (task_id, now, runner_id),
                )
            self._event(connection, f"runner_task_{state}", runner_id=runner_id, task_id=task_id, metadata={"assignment_epoch": task["assignment_epoch"]})
            updated = connection.execute("SELECT * FROM runner_tasks WHERE task_id=?", (task_id,)).fetchone()
            connection.commit()
        return self._public_task(updated)

    def record_result(self, payload: dict[str, Any], *, credential: str) -> dict[str, Any]:
        required = {
            "version",
            "message_type",
            "runner_id",
            "task_id",
            "assignment_epoch",
            "sequence",
            "created_at",
            "expires_at",
            "body_digest",
            "lease_id",
            "state",
            "finished_at",
            "summary",
            "commits",
            "changed_path_count",
            "next_actions",
            "candidate_id",
            "result_hash",
        }
        optional = {"branch", "test_summary", "error_code"}
        if required - set(payload) or set(payload) - required - optional or payload.get("state") not in TERMINAL_TASK_STATES | {"awaiting_confirmation", "recovery_required"}:
            raise StoreError("runner_payload_invalid", "Runner result 字段无效")
        self._validate_envelope(payload, "result")
        try:
            _parse_time(str(payload["finished_at"]))
        except ValueError as exc:
            raise StoreError("runner_payload_invalid", "Runner result finished_at 无效") from exc
        runner_id = self._runner_id(payload.get("runner_id"))
        task_id = self._task_id(payload.get("task_id"))
        self.verify_credential(runner_id, credential)
        summary = _bounded_text(payload.get("summary"), "summary", maximum=6000)
        test_summary = _bounded_text(payload.get("test_summary"), "test_summary", maximum=3000, required=False)
        candidate_id = payload.get("candidate_id")
        if not isinstance(candidate_id, str) or not SHA256_RE.fullmatch(candidate_id):
            raise StoreError("runner_payload_invalid", "candidate_id 无效")
        result_hash = payload.get("result_hash")
        result_identity = dict(payload)
        result_identity.pop("body_digest", None)
        result_identity.pop("result_hash", None)
        if not isinstance(result_hash, str) or result_hash != _digest(result_identity):
            raise StoreError("runner_digest_invalid", "result_hash 无效")
        changed_count = payload.get("changed_path_count")
        if not isinstance(changed_count, int) or isinstance(changed_count, bool) or not 0 <= changed_count <= 100000:
            raise StoreError("runner_payload_invalid", "changed_path_count 无效")
        actions = payload.get("next_actions")
        if not isinstance(actions, list) or len(actions) > 16 or any(not isinstance(value, str) or not value or len(value) > 500 for value in actions):
            raise StoreError("runner_payload_invalid", "next_actions 无效")
        commits = payload.get("commits")
        if not isinstance(commits, list) or len(commits) > 32 or len(commits) != len(set(commits)) or any(not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{7,64}", value) for value in commits):
            raise StoreError("runner_payload_invalid", "commits 无效")
        branch = payload.get("branch")
        if branch is not None and (not isinstance(branch, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,254}", branch)):
            raise StoreError("runner_payload_invalid", "branch 无效")
        error_code = payload.get("error_code")
        if error_code is not None and (not isinstance(error_code, str) or not SAFE_ID_RE.fullmatch(error_code)):
            raise StoreError("runner_payload_invalid", "error_code 无效")
        outcome = self._record_message(payload, kind="result")
        if outcome == "duplicate":
            return self.work_task(task_id)
        now = self.clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task = self._locked_assignment(connection, payload)
            state = payload["state"]
            connection.execute(
                "UPDATE runner_tasks SET state=?,stage='handoff',summary=?,candidate_id=?,branch=?,commits_json=?,result_hash=?,test_summary=?,changed_path_count=?,next_actions_json=?,error_code=?,action_required=?,sequence=?,updated_at=? WHERE task_id=?",
                (
                    state,
                    summary,
                    candidate_id,
                    branch,
                    _canonical(sorted(commits)),
                    result_hash,
                    test_summary,
                    changed_count,
                    _canonical(actions),
                    error_code,
                    "recovery_review" if state == "recovery_required" else "clarification" if state == "awaiting_confirmation" else None,
                    payload["sequence"],
                    now,
                    task_id,
                ),
            )
            connection.execute(
                "UPDATE runner_leases SET state='released',released_at=? WHERE lease_id=? AND state='active'",
                (now, task["lease_id"]),
            )
            runner = connection.execute("SELECT * FROM runner_registry WHERE runner_id=?", (runner_id,)).fetchone()
            next_admin = "disabled" if runner["admin_state"] == "draining" else runner["admin_state"]
            next_work = "recovery_required" if state == "recovery_required" else "idle"
            current_task = task_id if state == "recovery_required" else None
            connection.execute(
                "UPDATE runner_registry SET admin_state=?,work_state=?,current_task_id=?,revision=revision+1,updated_at=? WHERE runner_id=?",
                (next_admin, next_work, current_task, now, runner_id),
            )
            self._event(connection, f"runner_task_{state}", runner_id=runner_id, task_id=task_id, metadata={"candidate_present": candidate_id is not None})
            updated = connection.execute("SELECT * FROM runner_tasks WHERE task_id=?", (task_id,)).fetchone()
            connection.commit()
        return self._public_task(updated)

    def work_task(self, task_id: str) -> dict[str, Any]:
        task_id = self._task_id(task_id)
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM runner_tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            raise StoreError("runner_task_not_found", "Runner task 不存在", status=404)
        return self._public_task(row)

    def list_tasks(self, *, limit: int = 100) -> dict[str, Any]:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000:
            raise StoreError("runner_payload_invalid", "limit 无效")
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM runner_tasks ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return {"tasks": [self._public_task(row) for row in rows]}

    def sweep(self) -> dict[str, int]:
        now = self.clock()
        now_time = _parse_time(now)
        reset = recovery = expired = disabled = 0
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for task in connection.execute("SELECT * FROM runner_tasks WHERE state IN ('waiting_runner','leased','dispatched','queued','running','awaiting_confirmation')").fetchall():
                if _parse_time(task["expires_at"]) <= now_time and task["state"] == "waiting_runner":
                    connection.execute("UPDATE runner_tasks SET state='expired',stage='expired',updated_at=? WHERE task_id=?", (now, task["task_id"]))
                    expired += 1
                    continue
                if not task["assigned_runner_id"]:
                    continue
                runner = connection.execute("SELECT * FROM runner_registry WHERE runner_id=?", (task["assigned_runner_id"],)).fetchone()
                if runner is None or self._connectivity(runner, now_time) != "offline":
                    continue
                lease_expired = task["lease_expires_at"] is not None and _parse_time(task["lease_expires_at"]) <= now_time
                if task["ever_running"]:
                    connection.execute(
                        "UPDATE runner_tasks SET state='recovery_required',stage='recovery',action_required='Runner 失联且任务已运行，禁止自动转移',updated_at=? WHERE task_id=?",
                        (now, task["task_id"]),
                    )
                    connection.execute(
                        "UPDATE runner_registry SET work_state='recovery_required',updated_at=? WHERE runner_id=?",
                        (now, runner["runner_id"]),
                    )
                    recovery += 1
                elif lease_expired:
                    connection.execute(
                        "UPDATE runner_leases SET state='expired',released_at=? WHERE task_id=? AND state='active'",
                        (now, task["task_id"]),
                    )
                    connection.execute(
                        "UPDATE runner_tasks SET state='waiting_runner',stage='reschedule',assigned_runner_id=NULL,lease_id=NULL,lease_expires_at=NULL,updated_at=? WHERE task_id=?",
                        (now, task["task_id"]),
                    )
                    connection.execute(
                        "UPDATE runner_registry SET work_state='idle',current_task_id=NULL,updated_at=? WHERE runner_id=?",
                        (now, runner["runner_id"]),
                    )
                    reset += 1
            disabled = connection.execute(
                "UPDATE runner_registry SET admin_state='disabled',revision=revision+1,updated_at=? WHERE admin_state='draining' AND current_task_id IS NULL",
                (now,),
            ).rowcount
            connection.commit()
        return {"rescheduled": reset, "recovery_required": recovery, "expired": expired, "drained": disabled}

    def integrity(self) -> str:
        with self._connect() as connection:
            return str(connection.execute("PRAGMA integrity_check").fetchone()[0])

    def _record_message(self, payload: dict[str, Any], *, kind: str) -> str:
        runner_id = self._runner_id(payload.get("runner_id"))
        task_id = self._task_id(payload.get("task_id"))
        epoch = payload.get("assignment_epoch")
        sequence = payload.get("sequence")
        body_digest = payload.get("body_digest")
        if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 1 or not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
            raise StoreError("runner_sequence_invalid", "Runner epoch/sequence 无效")
        if not isinstance(body_digest, str) or not SHA256_RE.fullmatch(body_digest):
            raise StoreError("runner_digest_invalid", "Runner body digest 无效")
        now = self.clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT body_digest FROM runner_messages WHERE runner_id=? AND task_id=? AND kind=? AND assignment_epoch=? AND sequence=?",
                (runner_id, task_id, kind, epoch, sequence),
            ).fetchone()
            if existing is not None:
                connection.commit()
                if existing["body_digest"] == body_digest:
                    return "duplicate"
                raise StoreError("runner_message_conflict", "同 epoch/sequence 正文冲突", status=409)
            task = connection.execute(
                "SELECT assignment_epoch,assigned_runner_id,sequence,lease_id,state FROM runner_tasks WHERE task_id=?",
                (task_id,),
            ).fetchone()
            if task is None:
                connection.rollback()
                raise StoreError("runner_task_not_found", "Runner task 不存在", status=404)
            if (
                task["assigned_runner_id"] != runner_id
                or epoch != task["assignment_epoch"]
                or task["lease_id"] != payload.get("lease_id")
            ):
                connection.rollback()
                raise StoreError("runner_assignment_stale", "Runner assignment epoch 已过期", status=409)
            if sequence <= task["sequence"]:
                connection.rollback()
                raise StoreError("runner_sequence_stale", "Runner sequence 已过期", status=409)
            if task["state"] == "recovery_required" or (
                kind == "result"
                and task["state"] in TERMINAL_TASK_STATES | {"awaiting_confirmation"}
            ):
                connection.rollback()
                raise StoreError("runner_late_message", "Runner task 已进入不可覆盖状态，迟到消息被拒绝", status=409)
            connection.execute(
                "INSERT INTO runner_messages(runner_id,task_id,kind,assignment_epoch,sequence,body_digest,created_at) VALUES(?,?,?,?,?,?,?)",
                (runner_id, task_id, kind, epoch, sequence, body_digest, now),
            )
            connection.commit()
        return "recorded"

    def _locked_assignment(self, connection: sqlite3.Connection, payload: dict[str, Any]) -> sqlite3.Row:
        task = connection.execute("SELECT * FROM runner_tasks WHERE task_id=?", (payload["task_id"],)).fetchone()
        if task is None:
            raise StoreError("runner_task_not_found", "Runner task 不存在", status=404)
        if (
            task["assigned_runner_id"] != payload["runner_id"]
            or task["assignment_epoch"] != payload["assignment_epoch"]
            or task["lease_id"] != payload.get("lease_id")
        ):
            raise StoreError("runner_assignment_stale", "Runner assignment epoch 已过期", status=409)
        return task

    def _set_task_stage(self, task_id: str, *, from_states: set[str], state: str, stage: str) -> dict[str, Any]:
        task_id = self._task_id(task_id)
        now = self.clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM runner_tasks WHERE task_id=?", (task_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise StoreError("runner_task_not_found", "Runner task 不存在", status=404)
            if row["state"] not in from_states:
                connection.rollback()
                raise StoreError("runner_task_state_conflict", "Runner task 状态冲突", status=409)
            connection.execute("UPDATE runner_tasks SET state=?,stage=?,updated_at=? WHERE task_id=?", (state, stage, now, task_id))
            updated = connection.execute("SELECT * FROM runner_tasks WHERE task_id=?", (task_id,)).fetchone()
            connection.commit()
        return self._public_task(updated)

    def _public_runner(self, row: sqlite3.Row, *, now: str) -> dict[str, Any]:
        self_check = json.loads(row["self_check_json"]) if row["self_check_json"] else None
        return {
            "runner_id": row["runner_id"],
            "display_name": row["display_name"],
            "admin_state": row["admin_state"],
            "connectivity_state": self._connectivity(row, _parse_time(now)),
            "work_state": row["work_state"],
            "protocol_version": row["protocol_version"],
            "agent_version": row["agent_version"],
            "codex_version": row["codex_version"],
            "os": row["os"],
            "arch": row["arch"],
            "labels": json.loads(row["labels_json"]),
            "allowed_projects": json.loads(row["allowed_projects_json"]),
            "capabilities": json.loads(row["capabilities_json"]),
            "max_concurrency": row["max_concurrency"],
            "policy_revision": row["policy_revision"],
            "revision": row["revision"],
            "self_check": self_check,
            "last_heartbeat_at": row["last_heartbeat_at"],
            "current_task_id": row["current_task_id"],
            "archived": row["archived_at"] is not None,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _latest_enrollment(self, runner_id: str, *, now: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM runner_enrollments WHERE runner_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1",
                (runner_id,),
            ).fetchone()
        if row is None:
            return None
        return self._public_enrollment(row, now=now)

    @staticmethod
    def _public_enrollment(row: sqlite3.Row, *, now: str) -> dict[str, Any]:
        if row["claimed_at"] is not None:
            state = "claimed"
        elif row["revoked_at"] is not None:
            state = "revoked"
        elif _parse_time(row["expires_at"]) <= _parse_time(now):
            state = "expired"
        else:
            state = "pending"
        return {
            "state": state,
            "expires_at": row["expires_at"],
            "secret_available": False,
        }

    @staticmethod
    def _public_task(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "version": 2,
            "request_id": row["create_request_id"],
            "operation": "start",
            "task_id": row["task_id"],
            "state": row["state"],
            "updated_at": row["updated_at"],
            "stage": row["stage"],
            "summary": row["summary"],
            "candidate_id": row["candidate_id"],
            "branch": row["branch"],
            "commits": json.loads(row["commits_json"]),
            "result_hash": row["result_hash"],
            "test_summary": row["test_summary"],
            "changed_path_count": row["changed_path_count"],
            "next_actions": json.loads(row["next_actions_json"]),
            "error_code": row["error_code"],
            "action_required": row["action_required"],
            "assigned_runner_id": row["assigned_runner_id"],
            "assignment_epoch": row["assignment_epoch"],
            "lease_id": row["lease_id"],
            "lease_expires_at": row["lease_expires_at"],
            "project_alias": row["project_alias"],
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
        }

    def _validate_envelope(
        self,
        payload: dict[str, Any],
        message_type: str,
        *,
        heartbeat: bool = False,
    ) -> None:
        if payload.get("version") != 2 or payload.get("message_type") != message_type:
            raise StoreError("runner_protocol_unsupported", "Runner message version/type 无效", status=409)
        self._runner_id(payload.get("runner_id"))
        if heartbeat and payload.get("task_id") is None:
            if payload.get("assignment_epoch") != 0:
                raise StoreError("runner_assignment_stale", "Runner heartbeat epoch 无效", status=409)
        else:
            self._task_id(payload.get("task_id"))
            if not isinstance(payload.get("assignment_epoch"), int) or isinstance(payload.get("assignment_epoch"), bool) or payload["assignment_epoch"] < 1:
                raise StoreError("runner_sequence_invalid", "Runner assignment epoch 无效")
        if not isinstance(payload.get("sequence"), int) or isinstance(payload.get("sequence"), bool) or payload["sequence"] < 1:
            raise StoreError("runner_sequence_invalid", "Runner sequence 无效")
        try:
            created_at = _parse_time(str(payload.get("created_at")))
            expires_at = _parse_time(str(payload.get("expires_at")))
        except (TypeError, ValueError) as exc:
            raise StoreError("runner_payload_invalid", "Runner message 时间无效") from exc
        if expires_at <= created_at or (expires_at - created_at).total_seconds() > 3600:
            raise StoreError("runner_payload_invalid", "Runner message TTL 无效")
        if expires_at <= _parse_time(self.clock()):
            raise StoreError("runner_message_expired", "Runner message 已过期", status=409)
        digest = payload.get("body_digest")
        body = dict(payload)
        body.pop("body_digest", None)
        if not isinstance(digest, str) or digest != _digest(body):
            raise StoreError("runner_digest_invalid", "Runner body digest 无效")
        if len(_canonical(payload).encode("utf-8")) > 32 * 1024:
            raise StoreError("runner_payload_invalid", "Runner message 过大")

    def _connectivity(self, row: sqlite3.Row, now: dt.datetime) -> str:
        value = row["last_heartbeat_at"]
        if not value:
            return "offline"
        try:
            age = max(0.0, (now - _parse_time(value)).total_seconds())
        except ValueError:
            return "offline"
        if age <= self.online_after_seconds:
            return "online"
        if age <= self.offline_after_seconds:
            return "stale"
        return "offline"

    def _locked_runner(self, connection: sqlite3.Connection, runner_id: str, revision: int) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM runner_registry WHERE runner_id=?", (runner_id,)).fetchone()
        if row is None:
            raise StoreError("runner_not_found", "Runner 不存在", status=404)
        if row["revision"] != revision:
            raise StoreError("revision_conflict", "Runner revision 已变化，请刷新后重试", status=409)
        return row

    def _admin_replay(self, connection: sqlite3.Connection, request_id: str, action: str, body_digest: str, runner_id: str) -> dict[str, Any] | None:
        row = connection.execute("SELECT * FROM runner_admin_requests WHERE request_id=?", (request_id,)).fetchone()
        if row is None:
            return None
        if row["action"] != action or row["body_digest"] != body_digest or row["runner_id"] != runner_id:
            raise StoreError("idempotency_conflict", "request_id 已用于不同 Runner 管理请求", status=409)
        return json.loads(row["response_json"])

    def _save_admin_replay(self, connection: sqlite3.Connection, request_id: str, action: str, body_digest: str, runner_id: str, response: dict[str, Any]) -> None:
        connection.execute(
            "INSERT INTO runner_admin_requests(request_id,action,body_digest,runner_id,response_json,created_at) VALUES(?,?,?,?,?,?)",
            (request_id, action, body_digest, runner_id, _canonical(response), self.clock()),
        )

    def _event(self, connection: sqlite3.Connection, event_type: str, *, runner_id: str | None = None, task_id: str | None = None, metadata: dict[str, Any]) -> None:
        connection.execute(
            "INSERT INTO runner_events(runner_id,task_id,event_type,metadata_json,created_at) VALUES(?,?,?,?,?)",
            (runner_id, task_id, event_type, _canonical(metadata), self.clock()),
        )

    @staticmethod
    def _revision(value: Any) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise StoreError("runner_payload_invalid", "revision 无效")
        return value

    @staticmethod
    def _request_id(value: Any) -> str:
        if not isinstance(value, str) or not REQUEST_ID_RE.fullmatch(value):
            raise StoreError("runner_payload_invalid", "request_id 无效")
        return value

    @staticmethod
    def _runner_id(value: Any) -> str:
        if not isinstance(value, str) or not RUNNER_ID_RE.fullmatch(value):
            raise StoreError("runner_payload_invalid", "runner_id 无效")
        return value

    @staticmethod
    def _task_id(value: Any) -> str:
        if not isinstance(value, str) or not TASK_ID_RE.fullmatch(value):
            raise StoreError("runner_payload_invalid", "task_id 无效")
        return value


__all__ = [
    "ACTIVE_TASK_STATES",
    "ADMIN_STATES",
    "RunnerStore",
    "SCHEMA_VERSION",
    "TASK_STATES",
    "TERMINAL_TASK_STATES",
    "WORK_STATES",
    "utc_now",
]
