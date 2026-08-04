"""Passkey-backed authorization requests with no operation execution path."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from .contract import (
    DEFAULT_ADAPTER_SCHEMA_VERSION,
    DEFAULT_ADAPTER_VERSION,
    DEFAULT_POLICY_EPOCH,
    DEFAULT_POLICY_HASH,
    BACKUP_EVIDENCE_ID_RE,
    NATIVE_PROPOSAL_HASH_FIELDS,
    PROPOSAL_HASH_FIELDS,
    SHA256_RE,
    ContractError,
    allowlist_fingerprint,
    canonical_json,
    parse_timestamp,
    sha256_text,
    validate_envelope,
    validate_backup_evidence,
    validate_native_authorization_request,
    validate_native_intent,
)


class AuthorizationError(ValueError):
    """Raised when an authorization request must fail closed."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class PasskeyBackend(Protocol):
    def registration_begin(
        self, *, user_handle: bytes, existing_credentials: list[bytes]
    ) -> tuple[dict[str, Any], Any]: ...

    def registration_complete(self, *, state: Any, response: Any) -> dict[str, Any]: ...

    def authentication_begin(
        self, *, credentials: list[bytes]
    ) -> tuple[dict[str, Any], Any]: ...

    def authentication_complete(
        self, *, state: Any, credentials: list[bytes], response: Any
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class StoredCredential:
    credential_id: bytes
    credential_data: bytes
    user_id_hash: str
    sign_count: int


@dataclass(frozen=True)
class PendingFlow:
    kind: str
    user_id_hash: str
    state: Any
    expires_at: datetime
    approval_id: str | None = None
    proposal_hash: str | None = None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def hash_ha_user_id(user_id: str) -> str:
    if not isinstance(user_id, str):
        raise AuthorizationError("ingress_user_required", "Authenticated HA user is required")
    value = user_id.strip()
    if not value or len(value) > 256 or any(ord(character) < 33 for character in value):
        raise AuthorizationError("ingress_user_invalid", "Authenticated HA user is invalid")
    return hashlib.sha256(f"ha-user:{value}".encode("utf-8")).hexdigest()


class AuthorizationStore:
    """Private SQLite state for passkeys, immutable requests, and receipts."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("PRAGMA synchronous=FULL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS passkeys (
                    credential_id BLOB PRIMARY KEY,
                    credential_data BLOB NOT NULL,
                    user_id_hash TEXT NOT NULL,
                    sign_count INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    last_used_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_passkeys_user
                    ON passkeys(user_id_hash);

                CREATE TABLE IF NOT EXISTS authorization_requests (
                    approval_id TEXT PRIMARY KEY,
                    action_id TEXT NOT NULL UNIQUE,
                    action_type TEXT NOT NULL,
                    target TEXT NOT NULL,
                    proposal_hash TEXT NOT NULL,
                    risk_level TEXT NOT NULL,
                    requires_backup INTEGER NOT NULL,
                    parameter_summary_json TEXT NOT NULL,
                    expected_change TEXT NOT NULL,
                    validation_plan_json TEXT NOT NULL,
                    rollback_plan_json TEXT NOT NULL,
                    structural_owner_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('pending', 'authorized', 'expired'))
                );

                CREATE TABLE IF NOT EXISTS authorization_receipts (
                    receipt_id TEXT PRIMARY KEY,
                    approval_id TEXT NOT NULL UNIQUE,
                    action_id TEXT NOT NULL,
                    proposal_hash TEXT NOT NULL,
                    authorized_user_hash TEXT NOT NULL,
                    credential_id_hash TEXT NOT NULL,
                    authorized_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    assurance TEXT NOT NULL CHECK(assurance = 'passkey_verified'),
                    consumed_at TEXT,
                    FOREIGN KEY(approval_id) REFERENCES authorization_requests(approval_id)
                );

                CREATE TABLE IF NOT EXISTS operation_proposals (
                    action_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    action_type TEXT NOT NULL CHECK(action_type = 'restart_addon'),
                    target TEXT NOT NULL,
                    proposal_hash TEXT NOT NULL UNIQUE,
                    risk_level TEXT NOT NULL CHECK(risk_level = 'L3'),
                    requires_backup INTEGER NOT NULL CHECK(requires_backup = 1),
                    parameter_summary_json TEXT NOT NULL,
                    expected_change TEXT NOT NULL,
                    validation_plan_json TEXT NOT NULL,
                    rollback_plan_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS operation_executions (
                    action_id TEXT PRIMARY KEY,
                    receipt_id TEXT NOT NULL UNIQUE,
                    proposal_hash TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    action_type TEXT NOT NULL CHECK(action_type = 'restart_addon'),
                    target TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN (
                        'authorized', 'executing', 'verifying', 'succeeded',
                        'failed', 'recovery_required'
                    )),
                    preflight_json TEXT,
                    postflight_json TEXT,
                    error_code TEXT,
                    recovery_resolution TEXT CHECK(
                        recovery_resolution IN ('confirmed_healthy', 'compensated')
                    ),
                    recovery_evidence_hash TEXT,
                    recovery_resolved_at TEXT,
                    started_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    finished_at TEXT,
                    FOREIGN KEY(receipt_id) REFERENCES authorization_receipts(receipt_id),
                    FOREIGN KEY(action_id) REFERENCES operation_proposals(action_id)
                );

                CREATE TABLE IF NOT EXISTS operation_leases (
                    lease_id TEXT PRIMARY KEY,
                    resource TEXT NOT NULL,
                    action_id TEXT NOT NULL,
                    instance_id TEXT NOT NULL,
                    epoch INTEGER NOT NULL,
                    heartbeat_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN (
                        'active', 'recovery_required', 'released'
                    )),
                    created_at TEXT NOT NULL,
                    released_at TEXT,
                    UNIQUE(resource, epoch),
                    FOREIGN KEY(action_id) REFERENCES operation_executions(action_id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_operation_leases_held_resource
                    ON operation_leases(resource)
                    WHERE state IN ('active', 'recovery_required');

                CREATE TABLE IF NOT EXISTS backup_evidence (
                    logical_id TEXT PRIMARY KEY,
                    scope TEXT NOT NULL CHECK(scope IN ('full', 'addon', 'dashboard', 'recorder')),
                    completed INTEGER NOT NULL CHECK(completed = 1),
                    created_at TEXT NOT NULL,
                    size INTEGER NOT NULL CHECK(size > 0),
                    sha256 TEXT NOT NULL,
                    off_device_sha256 TEXT NOT NULL,
                    readable INTEGER NOT NULL CHECK(readable = 1),
                    baseline TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    registered_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_backup_evidence_selection
                    ON backup_evidence(scope, baseline, expires_at, created_at);
                CREATE TRIGGER IF NOT EXISTS backup_evidence_no_update
                    BEFORE UPDATE ON backup_evidence
                    BEGIN
                        SELECT RAISE(ABORT, 'backup_evidence_immutable');
                    END;
                CREATE TRIGGER IF NOT EXISTS backup_evidence_no_delete
                    BEFORE DELETE ON backup_evidence
                    BEGIN
                        SELECT RAISE(ABORT, 'backup_evidence_immutable');
                    END;
                """
            )
            self._ensure_column(
                connection,
                "authorization_requests",
                "proposal_origin",
                "TEXT NOT NULL DEFAULT 'legacy_envelope'",
            )
            self._ensure_column(
                connection,
                "operation_executions",
                "recovery_resolution",
                "TEXT CHECK(recovery_resolution IN ('confirmed_healthy', 'compensated'))",
            )
            self._ensure_column(
                connection,
                "operation_executions",
                "recovery_evidence_hash",
                "TEXT",
            )
            self._ensure_column(
                connection,
                "operation_executions",
                "recovery_resolved_at",
                "TEXT",
            )
            binding_columns = (
                ("proposal_version", "INTEGER NOT NULL DEFAULT 1"),
                ("policy_epoch", "INTEGER"),
                ("policy_hash", "TEXT"),
                ("allowlist_hash", "TEXT"),
                ("adapter_version", "TEXT"),
                ("adapter_schema_version", "INTEGER"),
                ("baseline_etag", "TEXT"),
                ("backup_evidence_id", "TEXT"),
            )
            for table in (
                "operation_proposals",
                "authorization_requests",
                "authorization_receipts",
                "operation_executions",
            ):
                for column, declaration in binding_columns:
                    self._ensure_column(connection, table, column, declaration)
            self._ensure_column(
                connection,
                "operation_executions",
                "lease_instance_id",
                "TEXT",
            )
            self._ensure_column(
                connection,
                "operation_executions",
                "lease_epoch",
                "INTEGER",
            )
            connection.execute("PRAGMA user_version=6")
        os.chmod(self.path, 0o600)

    @staticmethod
    def _ensure_column(
        connection: sqlite3.Connection, table: str, column: str, declaration: str
    ) -> None:
        columns = {
            row["name"] for row in connection.execute(f"PRAGMA table_info({table})")
        }
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    def credential_count(self) -> int:
        with self._connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM passkeys").fetchone()
        return int(row["count"])

    def register_backup_evidence(
        self, evidence: Any, *, registered_at: datetime
    ) -> dict[str, Any]:
        try:
            validated = validate_backup_evidence(evidence, now=registered_at)
        except ContractError as exc:
            raise AuthorizationError(exc.code, str(exc)) from exc
        registered_text = iso_timestamp(registered_at)
        fields = (
            "logical_id",
            "scope",
            "completed",
            "created_at",
            "size",
            "sha256",
            "off_device_sha256",
            "readable",
            "baseline",
            "expires_at",
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM backup_evidence WHERE logical_id = ?",
                (validated["logical_id"],),
            ).fetchone()
            if existing is not None:
                stored = self._backup_evidence_document(existing)
                if any(stored[field] != validated[field] for field in fields):
                    raise AuthorizationError(
                        "backup_evidence_conflict",
                        "Backup evidence logical ID already has different content",
                    )
                return stored
            connection.execute(
                """
                INSERT INTO backup_evidence(
                    logical_id, scope, completed, created_at, size, sha256,
                    off_device_sha256, readable, baseline, expires_at, registered_at
                ) VALUES (?, ?, 1, ?, ?, ?, ?, 1, ?, ?, ?)
                """,
                (
                    validated["logical_id"],
                    validated["scope"],
                    validated["created_at"],
                    validated["size"],
                    validated["sha256"],
                    validated["off_device_sha256"],
                    validated["baseline"],
                    validated["expires_at"],
                    registered_text,
                ),
            )
            row = connection.execute(
                "SELECT * FROM backup_evidence WHERE logical_id = ?",
                (validated["logical_id"],),
            ).fetchone()
        return self._backup_evidence_document(row)

    def get_backup_evidence(self, logical_id: str) -> dict[str, Any]:
        if not isinstance(logical_id, str) or not BACKUP_EVIDENCE_ID_RE.fullmatch(logical_id):
            raise AuthorizationError("backup_evidence_not_found", "Backup evidence was not found")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM backup_evidence WHERE logical_id = ?", (logical_id,)
            ).fetchone()
        if row is None:
            raise AuthorizationError("backup_evidence_not_found", "Backup evidence was not found")
        return self._backup_evidence_document(row)

    def select_backup_evidence(
        self,
        *,
        scopes: tuple[str, ...],
        baseline: str,
        now: datetime,
        valid_until: datetime,
    ) -> dict[str, Any]:
        if not scopes or any(scope not in {"full", "addon", "dashboard", "recorder"} for scope in scopes):
            raise AuthorizationError("backup_evidence_scope_invalid", "Backup evidence scope is invalid")
        now_text = iso_timestamp(now)
        valid_until_text = iso_timestamp(valid_until)
        placeholders = ",".join("?" for _scope in scopes)
        priority = " ".join(
            f"WHEN '{scope}' THEN {index}" for index, scope in enumerate(scopes)
        )
        with self._connect() as connection:
            row = connection.execute(
                f"""
                SELECT * FROM backup_evidence
                WHERE scope IN ({placeholders}) AND baseline = ?
                    AND completed = 1 AND readable = 1
                    AND created_at <= ? AND expires_at >= ?
                ORDER BY CASE scope {priority} ELSE {len(scopes)} END,
                         created_at DESC, logical_id ASC
                LIMIT 1
                """,
                (*scopes, baseline, now_text, valid_until_text),
            ).fetchone()
        if row is None:
            raise AuthorizationError(
                "backup_evidence_required",
                "A completed, readable, unexpired backup evidence record is required",
            )
        self._assert_backup_evidence_row(
            row,
            logical_id=row["logical_id"],
            scopes=scopes,
            baseline=baseline,
            now=now,
            valid_until=valid_until,
        )
        return self._backup_evidence_document(row)

    def assert_backup_evidence_valid(
        self,
        *,
        logical_id: str | None,
        scopes: tuple[str, ...],
        baseline: str,
        now: datetime,
        valid_until: datetime,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM backup_evidence WHERE logical_id = ?", (logical_id,)
            ).fetchone()
        self._assert_backup_evidence_row(
            row,
            logical_id=logical_id,
            scopes=scopes,
            baseline=baseline,
            now=now,
            valid_until=valid_until,
        )
        return self._backup_evidence_document(row)

    def credentials_for_user(self, user_id_hash: str) -> list[StoredCredential]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT credential_id, credential_data, user_id_hash, sign_count
                FROM passkeys WHERE user_id_hash = ? ORDER BY created_at
                """,
                (user_id_hash,),
            ).fetchall()
        return [
            StoredCredential(
                credential_id=bytes(row["credential_id"]),
                credential_data=bytes(row["credential_data"]),
                user_id_hash=row["user_id_hash"],
                sign_count=int(row["sign_count"]),
            )
            for row in rows
        ]

    def add_credential(
        self,
        *,
        credential_id: bytes,
        credential_data: bytes,
        user_id_hash: str,
        sign_count: int,
        created_at: datetime,
    ) -> None:
        if not credential_id or not credential_data or sign_count < 0:
            raise AuthorizationError("credential_invalid", "Passkey credential is invalid")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT 1 FROM passkeys WHERE user_id_hash = ?", (user_id_hash,)
            ).fetchone()
            if existing:
                raise AuthorizationError(
                    "passkey_already_enrolled", "This HA user already has a passkey"
                )
            try:
                connection.execute(
                    """
                    INSERT INTO passkeys(
                        credential_id, credential_data, user_id_hash, sign_count, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        credential_id,
                        credential_data,
                        user_id_hash,
                        sign_count,
                        iso_timestamp(created_at),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise AuthorizationError(
                    "credential_conflict", "Passkey credential already exists"
                ) from exc

    def update_counter(
        self, *, credential_id: bytes, old_count: int, new_count: int, used_at: datetime
    ) -> None:
        if new_count < 0:
            raise AuthorizationError("counter_invalid", "Passkey counter is invalid")
        if old_count > 0 and new_count <= old_count:
            raise AuthorizationError("counter_rollback", "Passkey counter did not advance")
        if old_count == 0 and new_count == 0:
            next_count = 0
        elif new_count > old_count:
            next_count = new_count
        else:
            raise AuthorizationError("counter_rollback", "Passkey counter regressed")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE passkeys SET sign_count = ?, last_used_at = ?
                WHERE credential_id = ? AND sign_count = ?
                """,
                (next_count, iso_timestamp(used_at), credential_id, old_count),
            )
            if cursor.rowcount != 1:
                raise AuthorizationError(
                    "credential_changed", "Passkey state changed during verification"
                )

    def create_native_proposal(
        self,
        intent: Any,
        *,
        restart_addon_allowlist: frozenset[str],
        policy_epoch: int,
        policy_hash: str,
        adapter_version: str,
        adapter_schema_version: int,
        baseline_etag: str,
        created_at: datetime,
        ttl_seconds: int,
    ) -> dict[str, Any]:
        try:
            validated = validate_native_intent(
                intent,
                restart_addon_allowlist=restart_addon_allowlist,
            )
        except ContractError as exc:
            raise AuthorizationError(exc.code, str(exc)) from exc
        if not isinstance(baseline_etag, str) or not SHA256_RE.fullmatch(baseline_etag):
            raise AuthorizationError("baseline_invalid", "baseline_etag is invalid")
        allowlist_hash = allowlist_fingerprint(restart_addon_allowlist)
        now = created_at.astimezone(timezone.utc)
        expires_at = now + timedelta(seconds=ttl_seconds)
        backup_evidence = self.select_backup_evidence(
            scopes=("addon", "full"),
            baseline=baseline_etag,
            now=now,
            valid_until=expires_at,
        )
        proposal = {
            "version": 2,
            "action_id": f"OPS-{now.strftime('%Y%m%d')}-{secrets.token_hex(6).upper()}",
            "action_type": "restart_addon",
            "target": validated["target"],
            "parameter_summary": {
                "idempotency_key": validated["idempotency_key"],
            },
            "risk_level": "L3",
            "requires_backup": True,
            "expected_change": "重启精确白名单中的 Home Assistant Add-on。",
            "validation_plan": [
                "执行前读取精确 Add-on 状态与版本",
                "执行后确认状态恢复为 started 且版本未变化",
            ],
            "rollback_plan": [
                "若结果不确定则停止自动操作并标记 recovery_required",
                "后续恢复或备份还原必须单独授权",
            ],
            "policy_epoch": policy_epoch,
            "policy_hash": policy_hash,
            "allowlist_hash": allowlist_hash,
            "adapter_version": adapter_version,
            "adapter_schema_version": adapter_schema_version,
            "baseline_etag": baseline_etag,
            "backup_evidence_id": backup_evidence["logical_id"],
            "created_at": iso_timestamp(now),
            "expires_at": iso_timestamp(expires_at),
            "state": "awaiting_approval",
        }
        parameter_hash = sha256_text(canonical_json(proposal["parameter_summary"]))
        proposal_hash = sha256_text(
            canonical_json(
                {field: proposal[field] for field in NATIVE_PROPOSAL_HASH_FIELDS}
            )
        )
        proposal["parameter_summary_hash"] = f"sha256:{parameter_hash}"
        proposal["proposal_hash"] = f"sha256:{proposal_hash}"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM operation_proposals WHERE idempotency_key = ?",
                (validated["idempotency_key"],),
            ).fetchone()
            if existing:
                if (
                    existing["action_type"] != validated["action_type"]
                    or existing["target"] != validated["target"]
                ):
                    raise AuthorizationError(
                        "idempotency_conflict",
                        "Idempotency key already belongs to another operation intent",
                    )
                return self._proposal_document(existing, now=now)
            connection.execute(
                """
                INSERT INTO operation_proposals(
                    action_id, idempotency_key, action_type, target, proposal_hash,
                    risk_level, requires_backup, parameter_summary_json,
                    expected_change, validation_plan_json, rollback_plan_json,
                    created_at, expires_at, proposal_version, policy_epoch,
                    policy_hash, allowlist_hash, adapter_version,
                    adapter_schema_version, baseline_etag, backup_evidence_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    proposal["action_id"],
                    validated["idempotency_key"],
                    proposal["action_type"],
                    proposal["target"],
                    proposal["proposal_hash"],
                    proposal["risk_level"],
                    1,
                    canonical_json(proposal["parameter_summary"]),
                    proposal["expected_change"],
                    canonical_json(proposal["validation_plan"]),
                    canonical_json(proposal["rollback_plan"]),
                    proposal["created_at"],
                    proposal["expires_at"],
                    proposal["version"],
                    proposal["policy_epoch"],
                    proposal["policy_hash"],
                    proposal["allowlist_hash"],
                    proposal["adapter_version"],
                    proposal["adapter_schema_version"],
                    proposal["baseline_etag"],
                    proposal["backup_evidence_id"],
                ),
            )
            row = connection.execute(
                "SELECT * FROM operation_proposals WHERE action_id = ?",
                (proposal["action_id"],),
            ).fetchone()
        return self._proposal_document(row, now=now)

    def existing_native_proposal(
        self, validated_intent: dict[str, Any], *, now: datetime
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM operation_proposals WHERE idempotency_key = ?",
                (validated_intent["idempotency_key"],),
            ).fetchone()
        if row is None:
            return None
        if (
            row["action_type"] != validated_intent["action_type"]
            or row["target"] != validated_intent["target"]
        ):
            raise AuthorizationError(
                "idempotency_conflict",
                "Idempotency key already belongs to another operation intent",
            )
        return self._proposal_document(row, now=now)

    def get_native_proposal(self, action_id: str, *, now: datetime) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM operation_proposals WHERE action_id = ?", (action_id,)
            ).fetchone()
        if row is None:
            raise AuthorizationError("proposal_not_found", "Operation proposal was not found")
        return self._proposal_document(row, now=now)

    def create_request_from_native_proposal(
        self, request: Any, *, created_at: datetime
    ) -> dict[str, Any]:
        try:
            action_id = validate_native_authorization_request(request)
        except ContractError as exc:
            raise AuthorizationError(exc.code, str(exc)) from exc
        now = created_at.astimezone(timezone.utc)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            proposal = connection.execute(
                "SELECT * FROM operation_proposals WHERE action_id = ?", (action_id,)
            ).fetchone()
            if proposal is None:
                raise AuthorizationError("proposal_not_found", "Operation proposal was not found")
            if now >= parse_timestamp(proposal["expires_at"], field="expires_at"):
                raise AuthorizationError("proposal_expired", "Operation proposal expired")
            existing = connection.execute(
                "SELECT * FROM authorization_requests WHERE action_id = ?", (action_id,)
            ).fetchone()
            if existing:
                if existing["proposal_hash"] != proposal["proposal_hash"]:
                    raise AuthorizationError(
                        "action_conflict", "Action ID already has another proposal hash"
                    )
                return self._request_document(existing)
            approval_id = f"AUTH-{secrets.token_hex(16).upper()}"
            connection.execute(
                """
                INSERT INTO authorization_requests(
                    approval_id, action_id, action_type, target, proposal_hash,
                    risk_level, requires_backup, parameter_summary_json,
                    expected_change, validation_plan_json, rollback_plan_json,
                    structural_owner_hash, created_at, expires_at, state, proposal_origin,
                    proposal_version, policy_epoch, policy_hash, allowlist_hash,
                    adapter_version, adapter_schema_version, baseline_etag,
                    backup_evidence_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 'broker_native', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    approval_id,
                    proposal["action_id"],
                    proposal["action_type"],
                    proposal["target"],
                    proposal["proposal_hash"],
                    proposal["risk_level"],
                    proposal["requires_backup"],
                    proposal["parameter_summary_json"],
                    proposal["expected_change"],
                    proposal["validation_plan_json"],
                    proposal["rollback_plan_json"],
                    "broker-native-passkey",
                    iso_timestamp(now),
                    proposal["expires_at"],
                    proposal["proposal_version"],
                    proposal["policy_epoch"],
                    proposal["policy_hash"],
                    proposal["allowlist_hash"],
                    proposal["adapter_version"],
                    proposal["adapter_schema_version"],
                    proposal["baseline_etag"],
                    proposal["backup_evidence_id"],
                ),
            )
            row = connection.execute(
                "SELECT * FROM authorization_requests WHERE approval_id = ?", (approval_id,)
            ).fetchone()
        return self._request_document(row)

    def create_request(self, validated: dict[str, Any], *, created_at: datetime) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM authorization_requests WHERE action_id = ?",
                (validated["action_id"],),
            ).fetchone()
            if existing:
                if existing["proposal_hash"] != validated["proposal_hash"]:
                    raise AuthorizationError(
                        "action_conflict", "Action ID already exists with another proposal hash"
                    )
                return self._request_document(existing)
            approval_id = f"AUTH-{secrets.token_hex(16).upper()}"
            connection.execute(
                """
                INSERT INTO authorization_requests(
                    approval_id, action_id, action_type, target, proposal_hash,
                    risk_level, requires_backup, parameter_summary_json,
                    expected_change, validation_plan_json, rollback_plan_json,
                    structural_owner_hash, created_at, expires_at, state
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
                """,
                (
                    approval_id,
                    validated["action_id"],
                    validated["action_type"],
                    validated["target"],
                    validated["proposal_hash"],
                    validated["risk_level"],
                    int(validated["requires_backup"]),
                    canonical_json(validated["parameter_summary"]),
                    validated["expected_change"],
                    canonical_json(validated["validation_plan"]),
                    canonical_json(validated["rollback_plan"]),
                    validated["approved_by_hash"],
                    iso_timestamp(created_at),
                    validated["expires_at"],
                ),
            )
            row = connection.execute(
                "SELECT * FROM authorization_requests WHERE approval_id = ?", (approval_id,)
            ).fetchone()
        return self._request_document(row)

    def get_request(self, approval_id: str, *, now: datetime) -> dict[str, Any]:
        if not isinstance(approval_id, str) or not approval_id.startswith("AUTH-"):
            raise AuthorizationError("approval_not_found", "Authorization request was not found")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM authorization_requests WHERE approval_id = ?", (approval_id,)
            ).fetchone()
            if row is None:
                raise AuthorizationError("approval_not_found", "Authorization request was not found")
            expires_at = parse_timestamp(row["expires_at"], field="expires_at")
            if row["state"] == "pending" and now.astimezone(timezone.utc) >= expires_at:
                connection.execute(
                    "UPDATE authorization_requests SET state = 'expired' WHERE approval_id = ?",
                    (approval_id,),
                )
                row = connection.execute(
                    "SELECT * FROM authorization_requests WHERE approval_id = ?", (approval_id,)
                ).fetchone()
        return self._request_document(row)

    def authorize(
        self,
        *,
        approval_id: str,
        expected_proposal_hash: str,
        user_id_hash: str,
        credential_id: bytes,
        authorized_at: datetime,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            request = connection.execute(
                "SELECT * FROM authorization_requests WHERE approval_id = ?", (approval_id,)
            ).fetchone()
            if request is None:
                raise AuthorizationError("approval_not_found", "Authorization request was not found")
            if request["proposal_hash"] != expected_proposal_hash:
                raise AuthorizationError("proposal_changed", "Proposal changed during authorization")
            if request["state"] != "pending":
                raise AuthorizationError(
                    "approval_not_pending", "Authorization request is not pending"
                )
            expires_at = parse_timestamp(request["expires_at"], field="expires_at")
            if authorized_at.astimezone(timezone.utc) >= expires_at:
                connection.execute(
                    "UPDATE authorization_requests SET state = 'expired' WHERE approval_id = ?",
                    (approval_id,),
                )
                raise AuthorizationError("approval_expired", "Authorization request expired")
            receipt_id = f"RCPT-{secrets.token_hex(16).upper()}"
            credential_hash = hashlib.sha256(credential_id).hexdigest()
            connection.execute(
                """
                INSERT INTO authorization_receipts(
                    receipt_id, approval_id, action_id, proposal_hash,
                    authorized_user_hash, credential_id_hash, authorized_at,
                    expires_at, assurance, proposal_version, policy_epoch,
                    policy_hash, allowlist_hash, adapter_version,
                    adapter_schema_version, baseline_etag, backup_evidence_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'passkey_verified', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt_id,
                    approval_id,
                    request["action_id"],
                    request["proposal_hash"],
                    user_id_hash,
                    credential_hash,
                    iso_timestamp(authorized_at),
                    request["expires_at"],
                    request["proposal_version"],
                    request["policy_epoch"],
                    request["policy_hash"],
                    request["allowlist_hash"],
                    request["adapter_version"],
                    request["adapter_schema_version"],
                    request["baseline_etag"],
                    request["backup_evidence_id"],
                ),
            )
            connection.execute(
                "UPDATE authorization_requests SET state = 'authorized' WHERE approval_id = ?",
                (approval_id,),
            )
            receipt = connection.execute(
                "SELECT * FROM authorization_receipts WHERE approval_id = ?", (approval_id,)
            ).fetchone()
        return self._receipt_document(receipt)

    def receipt_for_request(self, approval_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM authorization_receipts WHERE approval_id = ?", (approval_id,)
            ).fetchone()
        return None if row is None else self._receipt_document(row)

    def claim_execution(
        self,
        *,
        receipt_id: str,
        action_id: str,
        proposal_hash: str,
        idempotency_key: str,
        policy_epoch: int,
        policy_hash: str,
        allowlist_hash: str,
        adapter_version: str,
        adapter_schema_version: int,
        baseline_etag: str,
        backup_evidence_id: str | None,
        instance_id: str,
        lease_ttl_seconds: int,
        claimed_at: datetime,
    ) -> tuple[dict[str, Any], bool]:
        now = claimed_at.astimezone(timezone.utc)
        now_text = iso_timestamp(now)
        expires_text = iso_timestamp(now + timedelta(seconds=lease_ttl_seconds))
        expired_lease_action: str | None = None
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM operation_executions WHERE action_id = ?", (action_id,)
            ).fetchone()
            if existing:
                if (
                    existing["receipt_id"] != receipt_id
                    or existing["proposal_hash"] != proposal_hash
                    or existing["idempotency_key"] != idempotency_key
                ):
                    raise AuthorizationError(
                        "execution_conflict", "Action already has another execution claim"
                    )
                return self._execution_document(existing), True
            expired = connection.execute(
                """
                SELECT action_id FROM operation_leases
                WHERE state = 'active' AND expires_at <= ?
                ORDER BY created_at LIMIT 1
                """,
                (now_text,),
            ).fetchone()
            if expired is not None:
                expired_lease_action = expired["action_id"]
                connection.execute(
                    """
                    UPDATE operation_executions
                    SET state = 'recovery_required', error_code = 'lease_expired',
                        updated_at = ?, finished_at = COALESCE(finished_at, ?)
                    WHERE action_id = ?
                        AND state IN ('authorized', 'executing', 'verifying')
                    """,
                    (now_text, now_text, expired_lease_action),
                )
                connection.execute(
                    """
                    UPDATE operation_leases
                    SET state = 'recovery_required', heartbeat_at = ?
                    WHERE action_id = ? AND state = 'active'
                    """,
                    (now_text, expired_lease_action),
                )
            if expired_lease_action is not None:
                pass
            else:
                held = connection.execute(
                    """
                    SELECT action_id FROM operation_leases
                    WHERE state IN ('active', 'recovery_required')
                    LIMIT 1
                    """
                ).fetchone()
                if held is not None:
                    raise AuthorizationError(
                        "execution_busy", "A persistent operation lease is already held"
                    )
            unresolved = connection.execute(
                """
                SELECT action_id FROM operation_executions
                WHERE state = 'recovery_required' AND recovery_resolution IS NULL
                LIMIT 1
                """
            ).fetchone()
            if expired_lease_action is None and unresolved is not None:
                raise AuthorizationError(
                    "unresolved_recovery",
                    "A previous execution still requires recovery resolution",
                )
            if expired_lease_action is not None:
                row = None
            else:
                row = connection.execute(
                """
                SELECT r.*, q.state AS request_state, q.proposal_origin,
                       p.action_type, p.target,
                       p.idempotency_key AS proposal_idempotency_key,
                       p.proposal_version AS stored_proposal_version,
                       p.policy_epoch AS stored_policy_epoch,
                       p.policy_hash AS stored_policy_hash,
                       p.allowlist_hash AS stored_allowlist_hash,
                       p.adapter_version AS stored_adapter_version,
                       p.adapter_schema_version AS stored_adapter_schema_version,
                       p.baseline_etag AS stored_baseline_etag,
                       p.backup_evidence_id AS stored_backup_evidence_id
                FROM authorization_receipts r
                JOIN authorization_requests q ON q.approval_id = r.approval_id
                JOIN operation_proposals p ON p.action_id = r.action_id
                WHERE r.receipt_id = ?
                """,
                (receipt_id,),
                ).fetchone()
            if expired_lease_action is None and row is None:
                raise AuthorizationError("receipt_not_found", "Authorization receipt was not found")
            if expired_lease_action is None and row["proposal_origin"] != "broker_native":
                raise AuthorizationError(
                    "legacy_receipt_not_executable",
                    "Only Broker-native proposals can be executed",
                )
            if expired_lease_action is None and (
                row["action_id"] != action_id
                or row["proposal_hash"] != proposal_hash
                or row["proposal_idempotency_key"] != idempotency_key
            ):
                raise AuthorizationError(
                    "receipt_mismatch", "Authorization receipt does not match the execution request"
                )
            if expired_lease_action is None and (
                row["request_state"] != "authorized" or row["assurance"] != "passkey_verified"
            ):
                raise AuthorizationError(
                    "receipt_not_authorized", "Authorization receipt is not executable"
                )
            if expired_lease_action is None and row["consumed_at"] is not None:
                raise AuthorizationError("receipt_consumed", "Authorization receipt was consumed")
            if expired_lease_action is None and now >= parse_timestamp(
                row["expires_at"], field="receipt.expires_at"
            ):
                raise AuthorizationError("receipt_expired", "Authorization receipt expired")
            if expired_lease_action is None:
                evidence = connection.execute(
                    "SELECT * FROM backup_evidence WHERE logical_id = ?",
                    (backup_evidence_id,),
                ).fetchone()
                self._assert_backup_evidence_row(
                    evidence,
                    logical_id=backup_evidence_id,
                    scopes=("addon", "full"),
                    baseline=baseline_etag,
                    now=now,
                    valid_until=parse_timestamp(row["expires_at"], field="receipt.expires_at"),
                )
                expected_binding = {
                    "stored_proposal_version": 2,
                    "stored_policy_epoch": policy_epoch,
                    "stored_policy_hash": policy_hash,
                    "stored_allowlist_hash": allowlist_hash,
                    "stored_adapter_version": adapter_version,
                    "stored_adapter_schema_version": adapter_schema_version,
                    "stored_baseline_etag": baseline_etag,
                    "stored_backup_evidence_id": backup_evidence_id,
                }
                drift_codes = {
                    "stored_proposal_version": "proposal_version_changed",
                    "stored_policy_epoch": "policy_changed",
                    "stored_policy_hash": "policy_changed",
                    "stored_allowlist_hash": "allowlist_changed",
                    "stored_adapter_version": "adapter_changed",
                    "stored_adapter_schema_version": "adapter_changed",
                    "stored_baseline_etag": "baseline_changed",
                    "stored_backup_evidence_id": "backup_evidence_changed",
                }
                for field, expected in expected_binding.items():
                    if row[field] != expected:
                        raise AuthorizationError(
                            drift_codes[field], "Operation binding changed before execution"
                        )
                receipt_binding_fields = (
                    ("proposal_version", 2),
                    ("policy_epoch", policy_epoch),
                    ("policy_hash", policy_hash),
                    ("allowlist_hash", allowlist_hash),
                    ("adapter_version", adapter_version),
                    ("adapter_schema_version", adapter_schema_version),
                    ("baseline_etag", baseline_etag),
                    ("backup_evidence_id", backup_evidence_id),
                )
                for field, expected in receipt_binding_fields:
                    if row[field] != expected:
                        raise AuthorizationError(
                            "receipt_binding_changed",
                            "Authorization receipt binding changed before execution",
                        )
            if expired_lease_action is not None:
                claimed = None
            else:
                cursor = connection.execute(
                    """
                    UPDATE authorization_receipts SET consumed_at = ?
                    WHERE receipt_id = ? AND consumed_at IS NULL
                    """,
                    (now_text, receipt_id),
                )
                if cursor.rowcount != 1:
                    raise AuthorizationError("receipt_consumed", "Authorization receipt was consumed")
                resource = f"addon:{row['target']}"
                epochs = {}
                for lease_resource in ("singleton:operations", resource):
                    epoch_row = connection.execute(
                        "SELECT COALESCE(MAX(epoch), 0) + 1 AS next_epoch "
                        "FROM operation_leases WHERE resource = ?",
                        (lease_resource,),
                    ).fetchone()
                    epochs[lease_resource] = int(epoch_row["next_epoch"])
                connection.execute(
                    """
                    INSERT INTO operation_executions(
                        action_id, receipt_id, proposal_hash, idempotency_key,
                        action_type, target, state, started_at, updated_at,
                        proposal_version, policy_epoch, policy_hash, allowlist_hash,
                        adapter_version, adapter_schema_version, baseline_etag,
                        backup_evidence_id, lease_instance_id, lease_epoch
                    ) VALUES (?, ?, ?, ?, ?, ?, 'authorized', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        action_id,
                        receipt_id,
                        proposal_hash,
                        idempotency_key,
                        row["action_type"],
                        row["target"],
                        now_text,
                        now_text,
                        2,
                        policy_epoch,
                        policy_hash,
                        allowlist_hash,
                        adapter_version,
                        adapter_schema_version,
                        baseline_etag,
                        backup_evidence_id,
                        instance_id,
                        epochs["singleton:operations"],
                    ),
                )
                for lease_resource in ("singleton:operations", resource):
                    connection.execute(
                        """
                        INSERT INTO operation_leases(
                            lease_id, resource, action_id, instance_id, epoch,
                            heartbeat_at, expires_at, state, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?)
                        """,
                        (
                            f"LEASE-{secrets.token_hex(16).upper()}",
                            lease_resource,
                            action_id,
                            instance_id,
                            epochs[lease_resource],
                            now_text,
                            expires_text,
                            now_text,
                        ),
                    )
                claimed = connection.execute(
                    "SELECT * FROM operation_executions WHERE action_id = ?", (action_id,)
                ).fetchone()
        if expired_lease_action is not None:
            raise AuthorizationError(
                "lease_recovery_required",
                "An expired persistent lease requires recovery before new execution",
            )
        return self._execution_document(claimed), False

    def update_execution(
        self,
        *,
        action_id: str,
        expected_states: frozenset[str],
        state: str,
        updated_at: datetime,
        preflight: dict[str, Any] | None = None,
        postflight: dict[str, Any] | None = None,
        error_code: str | None = None,
        finished: bool = False,
    ) -> dict[str, Any]:
        now = iso_timestamp(updated_at)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM operation_executions WHERE action_id = ?", (action_id,)
            ).fetchone()
            if row is None:
                raise AuthorizationError("execution_not_found", "Execution was not found")
            if row["state"] not in expected_states:
                raise AuthorizationError(
                    "execution_state_conflict", "Execution state changed unexpectedly"
                )
            connection.execute(
                """
                UPDATE operation_executions
                SET state = ?,
                    preflight_json = COALESCE(?, preflight_json),
                    postflight_json = COALESCE(?, postflight_json),
                    error_code = ?, updated_at = ?,
                    finished_at = CASE WHEN ? THEN ? ELSE finished_at END
                WHERE action_id = ?
                """,
                (
                    state,
                    None if preflight is None else canonical_json(preflight),
                    None if postflight is None else canonical_json(postflight),
                    error_code,
                    now,
                    int(finished),
                    now,
                    action_id,
                ),
            )
            if state == "recovery_required":
                connection.execute(
                    """
                    UPDATE operation_leases
                    SET state = 'recovery_required', heartbeat_at = ?
                    WHERE action_id = ? AND state = 'active'
                    """,
                    (now, action_id),
                )
            elif finished:
                connection.execute(
                    """
                    UPDATE operation_leases
                    SET state = 'released', heartbeat_at = ?, released_at = ?
                    WHERE action_id = ? AND state = 'active'
                    """,
                    (now, now, action_id),
                )
            updated = connection.execute(
                "SELECT * FROM operation_executions WHERE action_id = ?", (action_id,)
            ).fetchone()
        return self._execution_document(updated)

    def heartbeat_execution_leases(
        self,
        *,
        action_id: str,
        instance_id: str,
        heartbeat_at: datetime,
        lease_ttl_seconds: int,
    ) -> None:
        now = heartbeat_at.astimezone(timezone.utc)
        now_text = iso_timestamp(now)
        expires_text = iso_timestamp(now + timedelta(seconds=lease_ttl_seconds))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT instance_id, state FROM operation_leases
                WHERE action_id = ?
                """,
                (action_id,),
            ).fetchall()
            if len(rows) != 2 or any(
                row["instance_id"] != instance_id or row["state"] != "active"
                for row in rows
            ):
                raise AuthorizationError(
                    "lease_lost", "Persistent operation lease is unavailable"
                )
            connection.execute(
                """
                UPDATE operation_leases
                SET heartbeat_at = ?, expires_at = ?
                WHERE action_id = ? AND instance_id = ? AND state = 'active'
                """,
                (now_text, expires_text, action_id, instance_id),
            )

    def leases_for_action(self, action_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT resource, action_id, instance_id, epoch, heartbeat_at,
                       expires_at, state, created_at, released_at
                FROM operation_leases WHERE action_id = ? ORDER BY resource
                """,
                (action_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_execution(self, action_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM operation_executions WHERE action_id = ?", (action_id,)
            ).fetchone()
        if row is None:
            raise AuthorizationError("execution_not_found", "Execution was not found")
        return self._execution_document(row)

    def assert_execution_interlock_clear(self) -> None:
        with self._connect() as connection:
            unresolved = connection.execute(
                """
                SELECT action_id FROM operation_executions
                WHERE state = 'recovery_required' AND recovery_resolution IS NULL
                LIMIT 1
                """
            ).fetchone()
            if unresolved is not None:
                raise AuthorizationError(
                    "unresolved_recovery",
                    "A previous execution still requires recovery resolution",
                )

    def resolve_recovery(
        self,
        *,
        action_id: str,
        resolution: str,
        evidence_hash: str,
        resolved_at: datetime,
    ) -> dict[str, Any]:
        now = iso_timestamp(resolved_at)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM operation_executions WHERE action_id = ?", (action_id,)
            ).fetchone()
            if row is None:
                raise AuthorizationError("execution_not_found", "Execution was not found")
            if row["state"] != "recovery_required":
                raise AuthorizationError(
                    "recovery_not_required", "Execution does not require recovery"
                )
            if row["recovery_resolution"] is not None:
                raise AuthorizationError(
                    "recovery_already_resolved", "Execution recovery is already resolved"
                )
            cursor = connection.execute(
                """
                UPDATE operation_executions
                SET recovery_resolution = ?, recovery_evidence_hash = ?,
                    recovery_resolved_at = ?, updated_at = ?
                WHERE action_id = ? AND state = 'recovery_required'
                    AND recovery_resolution IS NULL
                """,
                (resolution, evidence_hash, now, now, action_id),
            )
            if cursor.rowcount != 1:
                raise AuthorizationError(
                    "recovery_already_resolved", "Execution recovery is already resolved"
                )
            connection.execute(
                """
                UPDATE operation_leases
                SET state = 'released', heartbeat_at = ?, released_at = ?
                WHERE action_id = ? AND state = 'recovery_required'
                """,
                (now, now, action_id),
            )
            updated = connection.execute(
                "SELECT * FROM operation_executions WHERE action_id = ?", (action_id,)
            ).fetchone()
        return self._execution_document(updated)

    def recover_incomplete_executions(self, *, recovered_at: datetime) -> int:
        now = iso_timestamp(recovered_at)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE operation_executions
                SET state = 'recovery_required', error_code = 'broker_restarted',
                    updated_at = ?, finished_at = ?
                WHERE state IN ('authorized', 'executing', 'verifying')
                """,
                (now, now),
            )
            connection.execute(
                """
                UPDATE operation_leases
                SET state = 'recovery_required', heartbeat_at = ?
                WHERE state = 'active'
                """,
                (now,),
            )
        return cursor.rowcount

    @staticmethod
    def _proposal_document(row: sqlite3.Row, *, now: datetime) -> dict[str, Any]:
        expired = now.astimezone(timezone.utc) >= parse_timestamp(
            row["expires_at"], field="proposal.expires_at"
        )
        document = {
            "version": int(row["proposal_version"]),
            "action_id": row["action_id"],
            "action_type": row["action_type"],
            "target": row["target"],
            "idempotency_key": row["idempotency_key"],
            "parameter_summary": json.loads(row["parameter_summary_json"]),
            "risk_level": row["risk_level"],
            "requires_backup": bool(row["requires_backup"]),
            "expected_change": row["expected_change"],
            "validation_plan": json.loads(row["validation_plan_json"]),
            "rollback_plan": json.loads(row["rollback_plan_json"]),
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
            "proposal_hash": row["proposal_hash"],
            "state": "expired" if expired else "awaiting_approval",
            "execution_allowed": False,
        }
        if int(row["proposal_version"]) >= 2:
            document.update(AuthorizationStore._binding_document(row))
        return document

    @staticmethod
    def _request_document(row: sqlite3.Row) -> dict[str, Any]:
        document = {
            "version": int(row["proposal_version"]),
            "approval_id": row["approval_id"],
            "action_id": row["action_id"],
            "action_type": row["action_type"],
            "target": row["target"],
            "proposal_hash": row["proposal_hash"],
            "risk_level": row["risk_level"],
            "requires_backup": bool(row["requires_backup"]),
            "parameter_summary": json.loads(row["parameter_summary_json"]),
            "expected_change": row["expected_change"],
            "validation_plan": json.loads(row["validation_plan_json"]),
            "rollback_plan": json.loads(row["rollback_plan_json"]),
            "expires_at": row["expires_at"],
            "state": row["state"],
            "authorization_assurance": (
                "passkey_verified" if row["state"] == "authorized" else "structural_only"
            ),
            "proposal_origin": row["proposal_origin"],
            "execution_allowed": False,
        }
        if int(row["proposal_version"]) >= 2:
            document.update(AuthorizationStore._binding_document(row))
        return document

    @staticmethod
    def _receipt_document(row: sqlite3.Row) -> dict[str, Any]:
        document = {
            "version": int(row["proposal_version"]),
            "receipt_id": row["receipt_id"],
            "approval_id": row["approval_id"],
            "action_id": row["action_id"],
            "proposal_hash": row["proposal_hash"],
            "authorized_user_hash": row["authorized_user_hash"],
            "credential_id_hash": row["credential_id_hash"],
            "authorized_at": row["authorized_at"],
            "expires_at": row["expires_at"],
            "authorization_assurance": row["assurance"],
            "consumed": row["consumed_at"] is not None,
            "execution_allowed": False,
        }
        if int(row["proposal_version"]) >= 2:
            document.update(AuthorizationStore._binding_document(row))
        return document

    @staticmethod
    def _execution_document(row: sqlite3.Row) -> dict[str, Any]:
        recovery = None
        if row["state"] == "recovery_required":
            recovery = {
                "resolved": row["recovery_resolution"] is not None,
                "resolution": row["recovery_resolution"],
                "evidence_hash": row["recovery_evidence_hash"],
                "resolved_at": row["recovery_resolved_at"],
            }
        document = {
            "version": int(row["proposal_version"]),
            "receipt_id": row["receipt_id"],
            "action_id": row["action_id"],
            "proposal_hash": row["proposal_hash"],
            "idempotency_key": row["idempotency_key"],
            "action_type": row["action_type"],
            "target": row["target"],
            "state": row["state"],
            "started_at": row["started_at"],
            "updated_at": row["updated_at"],
            "finished_at": row["finished_at"],
            "preflight": (
                None if row["preflight_json"] is None else json.loads(row["preflight_json"])
            ),
            "postflight": (
                None if row["postflight_json"] is None else json.loads(row["postflight_json"])
            ),
            "error_code": row["error_code"],
            "recovery": recovery,
        }
        if int(row["proposal_version"]) >= 2:
            document.update(AuthorizationStore._binding_document(row))
            document["lease"] = {
                "instance_id": row["lease_instance_id"],
                "epoch": row["lease_epoch"],
            }
        return document

    @staticmethod
    def _binding_document(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "policy_epoch": row["policy_epoch"],
            "policy_hash": row["policy_hash"],
            "allowlist_hash": row["allowlist_hash"],
            "adapter_version": row["adapter_version"],
            "adapter_schema_version": row["adapter_schema_version"],
            "baseline_etag": row["baseline_etag"],
            "backup_evidence_id": row["backup_evidence_id"],
        }

    @staticmethod
    def _backup_evidence_document(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "version": 1,
            "scope": row["scope"],
            "logical_id": row["logical_id"],
            "completed": bool(row["completed"]),
            "created_at": row["created_at"],
            "size": int(row["size"]),
            "sha256": row["sha256"],
            "off_device_sha256": row["off_device_sha256"],
            "readable": bool(row["readable"]),
            "baseline": row["baseline"],
            "expires_at": row["expires_at"],
        }

    @staticmethod
    def _assert_backup_evidence_row(
        row: sqlite3.Row | None,
        *,
        logical_id: str | None,
        scopes: tuple[str, ...],
        baseline: str,
        now: datetime,
        valid_until: datetime,
    ) -> None:
        if row is None or logical_id is None:
            raise AuthorizationError("backup_evidence_required", "Backup evidence is missing")
        if (
            row["logical_id"] != logical_id
            or row["scope"] not in scopes
            or row["baseline"] != baseline
        ):
            raise AuthorizationError(
                "backup_evidence_changed", "Backup evidence binding changed"
            )
        if (
            row["completed"] != 1
            or row["readable"] != 1
            or not isinstance(row["size"], int)
            or row["size"] < 1
            or not SHA256_RE.fullmatch(row["sha256"] or "")
            or not SHA256_RE.fullmatch(row["off_device_sha256"] or "")
            or not SHA256_RE.fullmatch(row["baseline"] or "")
        ):
            raise AuthorizationError(
                "backup_evidence_changed", "Backup evidence is no longer usable"
            )
        current = now.astimezone(timezone.utc)
        required_until = valid_until.astimezone(timezone.utc)
        try:
            created_at = parse_timestamp(
                row["created_at"], field="backup_evidence.created_at"
            )
            expires_at = parse_timestamp(
                row["expires_at"], field="backup_evidence.expires_at"
            )
        except ContractError as exc:
            raise AuthorizationError(
                "backup_evidence_changed", "Backup evidence timestamps are invalid"
            ) from exc
        if created_at > current:
            raise AuthorizationError(
                "backup_evidence_changed", "Backup evidence creation time changed"
            )
        if expires_at <= current or expires_at < required_until:
            raise AuthorizationError(
                "backup_evidence_expired", "Backup evidence does not cover the authorization window"
            )


class AuthorizationManager:
    """Coordinates structural proposals with independent Passkey assertions."""

    def __init__(
        self,
        *,
        store: AuthorizationStore,
        passkeys: PasskeyBackend,
        trusted_owner_hashes: frozenset[str],
        enrollment_token: str,
        challenge_ttl_seconds: int = 180,
        max_passkeys: int = 8,
        max_pending_flows: int = 100,
        restart_addon_allowlist: frozenset[str] = frozenset(),
        proposal_ttl_seconds: int = 600,
        policy_epoch: int = DEFAULT_POLICY_EPOCH,
        policy_hash: str = DEFAULT_POLICY_HASH,
        adapter_version: str = DEFAULT_ADAPTER_VERSION,
        adapter_schema_version: int = DEFAULT_ADAPTER_SCHEMA_VERSION,
        baseline_provider: Callable[[str], str] | None = None,
        clock: Callable[[], datetime] = utc_now,
    ):
        if enrollment_token and len(enrollment_token) < 32:
            raise AuthorizationError(
                "enrollment_token_invalid", "Enrollment token must be at least 32 characters"
            )
        if not 60 <= challenge_ttl_seconds <= 600:
            raise AuthorizationError("challenge_ttl_invalid", "Challenge TTL is invalid")
        if not 1 <= max_passkeys <= 32 or not 10 <= max_pending_flows <= 500:
            raise AuthorizationError("limit_invalid", "Authorization limits are invalid")
        if not 60 <= proposal_ttl_seconds <= 1800:
            raise AuthorizationError("proposal_ttl_invalid", "Proposal TTL is invalid")
        if not isinstance(policy_epoch, int) or isinstance(policy_epoch, bool) or policy_epoch < 1:
            raise AuthorizationError("policy_invalid", "Policy epoch is invalid")
        if not isinstance(policy_hash, str) or not SHA256_RE.fullmatch(policy_hash):
            raise AuthorizationError("policy_invalid", "Policy hash is invalid")
        if not isinstance(adapter_version, str) or not adapter_version:
            raise AuthorizationError("adapter_invalid", "Adapter version is invalid")
        if (
            not isinstance(adapter_schema_version, int)
            or isinstance(adapter_schema_version, bool)
            or adapter_schema_version < 1
        ):
            raise AuthorizationError("adapter_invalid", "Adapter schema version is invalid")
        self.store = store
        self.passkeys = passkeys
        self.trusted_owner_hashes = trusted_owner_hashes
        self.enrollment_token = enrollment_token
        self.challenge_ttl = timedelta(seconds=challenge_ttl_seconds)
        self.max_passkeys = max_passkeys
        self.max_pending_flows = max_pending_flows
        self.restart_addon_allowlist = restart_addon_allowlist
        self.proposal_ttl_seconds = proposal_ttl_seconds
        self.policy_epoch = policy_epoch
        self.policy_hash = policy_hash
        self.adapter_version = adapter_version
        self.adapter_schema_version = adapter_schema_version
        if baseline_provider is None:
            raise AuthorizationError("baseline_provider_missing", "Baseline provider is required")
        self.baseline_provider = baseline_provider
        self.clock = clock
        self._flows: dict[str, PendingFlow] = {}
        self._lock = threading.Lock()

    def create_request(self, envelope: Any) -> dict[str, Any]:
        """Legacy P4 compatibility path; legacy receipts are never executable."""
        try:
            validated = validate_envelope(
                envelope,
                trusted_owner_hashes=self.trusted_owner_hashes,
                clock=self.clock,
            )
        except ContractError as exc:
            raise AuthorizationError(exc.code, str(exc)) from exc
        return self.store.create_request(validated, created_at=self.clock())

    def create_proposal(self, intent: Any) -> dict[str, Any]:
        try:
            validated = validate_native_intent(
                intent, restart_addon_allowlist=self.restart_addon_allowlist
            )
            existing = self.store.existing_native_proposal(validated, now=self.clock())
            if existing is not None:
                return existing
            baseline_etag = self.baseline_provider(validated["target"])
        except ContractError as exc:
            raise AuthorizationError(exc.code, str(exc)) from exc
        return self.store.create_native_proposal(
            intent,
            restart_addon_allowlist=self.restart_addon_allowlist,
            policy_epoch=self.policy_epoch,
            policy_hash=self.policy_hash,
            adapter_version=self.adapter_version,
            adapter_schema_version=self.adapter_schema_version,
            baseline_etag=baseline_etag,
            created_at=self.clock(),
            ttl_seconds=self.proposal_ttl_seconds,
        )

    def register_backup_evidence(self, evidence: Any) -> dict[str, Any]:
        return self.store.register_backup_evidence(evidence, registered_at=self.clock())

    def backup_evidence(self, logical_id: str) -> dict[str, Any]:
        return self.store.get_backup_evidence(logical_id)

    def create_native_request(self, request: Any) -> dict[str, Any]:
        return self.store.create_request_from_native_proposal(
            request, created_at=self.clock()
        )

    def native_proposal(self, action_id: str) -> dict[str, Any]:
        return self.store.get_native_proposal(action_id, now=self.clock())

    def internal_status(self, approval_id: str) -> dict[str, Any]:
        request = self.store.get_request(approval_id, now=self.clock())
        receipt = self.store.receipt_for_request(approval_id)
        return {"request": request, "receipt": receipt, "execution_allowed": False}

    def ingress_context(self, *, approval_id: str | None, remote_user_id: str) -> dict[str, Any]:
        user_hash = hash_ha_user_id(remote_user_id)
        credentials = self.store.credentials_for_user(user_hash)
        request = None
        receipt = None
        if approval_id:
            request = self.store.get_request(approval_id, now=self.clock())
            stored_receipt = self.store.receipt_for_request(approval_id)
            if stored_receipt:
                receipt = {
                    "receipt_id": stored_receipt["receipt_id"],
                    "authorized_at": stored_receipt["authorized_at"],
                    "authorization_assurance": stored_receipt[
                        "authorization_assurance"
                    ],
                }
        return {
            "version": 1,
            "registered_for_user": bool(credentials),
            "enrollment_enabled": bool(self.enrollment_token),
            "request": request,
            "receipt": receipt,
            "execution_allowed": False,
        }

    def begin_registration(
        self, *, remote_user_id: str, enrollment_token: Any
    ) -> dict[str, Any]:
        user_hash = hash_ha_user_id(remote_user_id)
        self._require_enrollment_token(enrollment_token)
        if self.store.credentials_for_user(user_hash):
            raise AuthorizationError(
                "passkey_already_enrolled", "This HA user already has a passkey"
            )
        if self.store.credential_count() >= self.max_passkeys:
            raise AuthorizationError("passkey_limit", "Passkey limit has been reached")
        options, state = self.passkeys.registration_begin(
            user_handle=bytes.fromhex(user_hash), existing_credentials=[]
        )
        flow_id = self._put_flow(
            PendingFlow(
                kind="registration",
                user_id_hash=user_hash,
                state=state,
                expires_at=self.clock() + self.challenge_ttl,
            )
        )
        return {"flow_id": flow_id, "options": options, "execution_allowed": False}

    def complete_registration(
        self,
        *,
        remote_user_id: str,
        enrollment_token: Any,
        flow_id: Any,
        response: Any,
    ) -> dict[str, Any]:
        user_hash = hash_ha_user_id(remote_user_id)
        self._require_enrollment_token(enrollment_token)
        flow = self._take_flow(flow_id, kind="registration", user_id_hash=user_hash)
        try:
            material = self.passkeys.registration_complete(state=flow.state, response=response)
        except AuthorizationError:
            raise
        except Exception as exc:
            raise AuthorizationError(
                "passkey_registration_failed", "Passkey registration verification failed"
            ) from exc
        self.store.add_credential(
            credential_id=material["credential_id"],
            credential_data=material["credential_data"],
            user_id_hash=user_hash,
            sign_count=int(material["sign_count"]),
            created_at=self.clock(),
        )
        return {
            "registered": True,
            "credential_id_hash": hashlib.sha256(material["credential_id"]).hexdigest(),
            "execution_allowed": False,
        }

    def begin_authorization(
        self, *, approval_id: str, remote_user_id: str
    ) -> dict[str, Any]:
        user_hash = hash_ha_user_id(remote_user_id)
        request = self.store.get_request(approval_id, now=self.clock())
        if request["state"] == "expired":
            raise AuthorizationError("approval_expired", "Authorization request expired")
        if request["state"] != "pending":
            raise AuthorizationError(
                "approval_not_pending", "Authorization request is not pending"
            )
        credentials = self.store.credentials_for_user(user_hash)
        if not credentials:
            raise AuthorizationError(
                "passkey_not_enrolled", "This HA user has no registered passkey"
            )
        options, state = self.passkeys.authentication_begin(
            credentials=[credential.credential_data for credential in credentials]
        )
        flow_id = self._put_flow(
            PendingFlow(
                kind="authorization",
                user_id_hash=user_hash,
                state=state,
                expires_at=self.clock() + self.challenge_ttl,
                approval_id=approval_id,
                proposal_hash=request["proposal_hash"],
            )
        )
        return {
            "flow_id": flow_id,
            "options": options,
            "approval_id": approval_id,
            "proposal_hash": request["proposal_hash"],
            "execution_allowed": False,
        }

    def complete_authorization(
        self,
        *,
        approval_id: str,
        remote_user_id: str,
        flow_id: Any,
        response: Any,
    ) -> dict[str, Any]:
        user_hash = hash_ha_user_id(remote_user_id)
        flow = self._take_flow(
            flow_id,
            kind="authorization",
            user_id_hash=user_hash,
            approval_id=approval_id,
        )
        request = self.store.get_request(approval_id, now=self.clock())
        if request["state"] != "pending" or request["proposal_hash"] != flow.proposal_hash:
            raise AuthorizationError("proposal_changed", "Proposal is no longer pending")
        credentials = self.store.credentials_for_user(user_hash)
        try:
            verified = self.passkeys.authentication_complete(
                state=flow.state,
                credentials=[credential.credential_data for credential in credentials],
                response=response,
            )
        except AuthorizationError:
            raise
        except Exception as exc:
            raise AuthorizationError(
                "passkey_verification_failed", "Passkey assertion verification failed"
            ) from exc
        matching = next(
            (
                credential
                for credential in credentials
                if credential.credential_id == verified["credential_id"]
            ),
            None,
        )
        if matching is None:
            raise AuthorizationError("credential_unknown", "Passkey credential is unknown")
        now = self.clock()
        self.store.update_counter(
            credential_id=matching.credential_id,
            old_count=matching.sign_count,
            new_count=int(verified["sign_count"]),
            used_at=now,
        )
        receipt = self.store.authorize(
            approval_id=approval_id,
            expected_proposal_hash=flow.proposal_hash or "",
            user_id_hash=user_hash,
            credential_id=matching.credential_id,
            authorized_at=now,
        )
        return {"receipt": receipt, "execution_allowed": False}

    def _require_enrollment_token(self, value: Any) -> None:
        if not self.enrollment_token:
            raise AuthorizationError("enrollment_disabled", "Passkey enrollment is disabled")
        if not isinstance(value, str) or not hmac.compare_digest(value, self.enrollment_token):
            raise AuthorizationError("enrollment_denied", "Passkey enrollment was denied")

    def _put_flow(self, flow: PendingFlow) -> str:
        with self._lock:
            now = self.clock()
            self._flows = {
                key: value for key, value in self._flows.items() if value.expires_at > now
            }
            if len(self._flows) >= self.max_pending_flows:
                raise AuthorizationError("challenge_limit", "Too many pending challenges")
            flow_id = secrets.token_urlsafe(32)
            self._flows[flow_id] = flow
            return flow_id

    def _take_flow(
        self,
        flow_id: Any,
        *,
        kind: str,
        user_id_hash: str,
        approval_id: str | None = None,
    ) -> PendingFlow:
        if not isinstance(flow_id, str) or len(flow_id) > 128:
            raise AuthorizationError("challenge_invalid", "Passkey challenge is invalid")
        with self._lock:
            flow = self._flows.pop(flow_id, None)
        if flow is None or flow.kind != kind:
            raise AuthorizationError("challenge_invalid", "Passkey challenge is invalid")
        if flow.expires_at <= self.clock():
            raise AuthorizationError("challenge_expired", "Passkey challenge expired")
        if flow.user_id_hash != user_id_hash or flow.approval_id != approval_id:
            raise AuthorizationError("challenge_mismatch", "Passkey challenge does not match")
        return flow
