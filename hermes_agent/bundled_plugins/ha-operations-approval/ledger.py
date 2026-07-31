"""Deterministic proposal and approval ledger for Home Assistant operations.

This module deliberately has no Home Assistant, Supervisor, HACS, network, or
process-execution capability.  It produces immutable proposal hashes and an
auditable approval state that a later isolated broker must re-validate before
performing any production action.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit


ACTION_RISKS = {
    "check_ha_config": "L1",
    "install_addon": "L3",
    "configure_addon": "L3",
    "start_addon": "L3",
    "stop_addon": "L3",
    "restart_addon": "L3",
    "update_addon": "L3",
    "repair_addon": "L3",
    "uninstall_addon": "L3",
    "install_hacs": "L3",
    "update_hacs": "L3",
    "repair_hacs": "L3",
    "remove_hacs": "L3",
    "start_config_flow": "L3",
    "enable_integration": "L3",
    "disable_integration": "L3",
    "reload_integration": "L3",
    "remove_integration": "L3",
    "restart_core": "L3",
    "create_backup": "L3",
    "purge_recorder": "L3",
    "delete_expired_backup": "L3",
    "cleanup_allowlisted_cache": "L3",
}

ACTION_ID_RE = re.compile(r"^OPS-[0-9]{8}-[A-F0-9]{12}$")
TARGET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")
PARAMETER_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,63}$")
IDENTITY_HASH_RE = re.compile(r"^[a-f0-9]{64}$")
CHALLENGE_RE = re.compile(r"^[A-F0-9]{8}$")

SENSITIVE_KEY_PARTS = (
    "accesskey",
    "apikey",
    "authorization",
    "cookie",
    "credential",
    "password",
    "privatekey",
    "refreshtoken",
    "secret",
    "token",
)
SENSITIVE_VALUE_RE = re.compile(
    r"(-----BEGIN [A-Z ]*PRIVATE KEY-----|\bBearer\s+\S+|\bsk-[A-Za-z0-9_-]{16,}|\bAKIA[A-Z0-9]{16}\b)",
    re.IGNORECASE,
)
NONTERMINAL_STATES = frozenset(
    {"awaiting_approval", "awaiting_confirmation", "approved"}
)
TERMINAL_STATES = frozenset({"cancelled", "expired"})


class ProposalError(ValueError):
    """Raised when proposal input or a state transition is rejected."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def isoformat(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ProposalError("invalid_timestamp", "Stored timestamp is missing timezone")
    return parsed.astimezone(timezone.utc)


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def identity_hash(platform: str, user_id: str) -> str:
    return sha256_text(f"{platform.strip().lower()}:{user_id.strip()}")


def configured_owner_hashes(raw: str | None = None) -> frozenset[str]:
    values = raw if raw is not None else os.getenv("HA_OPERATIONS_OWNER_IDENTITY_HASHES", "")
    hashes = frozenset(item.strip().lower() for item in values.split(",") if item.strip())
    if any(not IDENTITY_HASH_RE.fullmatch(item) for item in hashes):
        raise ProposalError("invalid_owner_config", "Owner identity hash configuration is invalid")
    return hashes


def _safe_text(value: Any, *, field: str, maximum: int = 300) -> str:
    if not isinstance(value, str):
        raise ProposalError("invalid_field", f"{field} must be a string")
    text = value.strip()
    if not text or len(text) > maximum:
        raise ProposalError("invalid_field", f"{field} must be 1-{maximum} characters")
    if any(ord(character) < 32 and character not in "\t" for character in text):
        raise ProposalError("invalid_field", f"{field} contains control characters")
    if SENSITIVE_VALUE_RE.search(text):
        raise ProposalError("sensitive_value", f"{field} contains a secret-like value")
    return text


def _safe_text_list(value: Any, *, field: str) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ProposalError("invalid_json", f"{field} must be a JSON array") from exc
    if not isinstance(value, list) or not 1 <= len(value) <= 10:
        raise ProposalError("invalid_field", f"{field} must contain 1-10 items")
    return [_safe_text(item, field=field) for item in value]


def _normalize_parameter_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


def _validate_url_summary(value: str, *, field: str) -> None:
    if "://" not in value:
        return
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ProposalError("invalid_url", f"{field} contains an unsupported URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ProposalError("sensitive_url", f"{field} URL must not contain credentials, query, or fragment")


def _sanitize_parameter_value(
    value: Any,
    *,
    field: str,
    depth: int,
    counter: list[int],
) -> Any:
    counter[0] += 1
    if counter[0] > 64 or depth > 4:
        raise ProposalError("parameters_too_large", "parameter_summary is too large or deeply nested")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and not math.isfinite(value):
            raise ProposalError("invalid_number", f"{field} must be finite")
        return value
    if isinstance(value, str):
        text = _safe_text(value, field=field, maximum=256)
        _validate_url_summary(text, field=field)
        return text
    if isinstance(value, list):
        if len(value) > 16:
            raise ProposalError("parameters_too_large", f"{field} has too many list items")
        return [
            _sanitize_parameter_value(
                item,
                field=f"{field}[]",
                depth=depth + 1,
                counter=counter,
            )
            for item in value
        ]
    if isinstance(value, dict):
        if len(value) > 32:
            raise ProposalError("parameters_too_large", f"{field} has too many keys")
        result: dict[str, Any] = {}
        for key in sorted(value):
            if not isinstance(key, str) or not PARAMETER_KEY_RE.fullmatch(key):
                raise ProposalError("invalid_parameter_key", f"Invalid parameter key in {field}")
            normalized = _normalize_parameter_key(key)
            if any(part in normalized for part in SENSITIVE_KEY_PARTS):
                raise ProposalError("sensitive_parameter", f"Sensitive parameter key rejected: {key}")
            result[key] = _sanitize_parameter_value(
                value[key],
                field=f"{field}.{key}",
                depth=depth + 1,
                counter=counter,
            )
        return result
    raise ProposalError("invalid_parameter_type", f"Unsupported value in {field}")


def sanitize_parameter_summary(value: Any) -> dict[str, Any]:
    if value in (None, ""):
        value = {}
    if isinstance(value, str):
        if len(value) > 4096:
            raise ProposalError("parameters_too_large", "parameter_summary JSON is too large")
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ProposalError("invalid_json", "parameter_summary must be a JSON object") from exc
    if not isinstance(value, dict):
        raise ProposalError("invalid_parameters", "parameter_summary must be a JSON object")
    return _sanitize_parameter_value(value, field="parameter_summary", depth=0, counter=[0])


def normalize_proposal_request(args: dict[str, Any]) -> dict[str, Any]:
    action_type = str(args.get("action_type", "")).strip()
    if action_type not in ACTION_RISKS:
        raise ProposalError("unsupported_action", "action_type is not in the operations allowlist")
    target = str(args.get("target", "")).strip()
    if not TARGET_RE.fullmatch(target) or ".." in target or target.startswith(("/", "\\")):
        raise ProposalError("invalid_target", "target must be a bounded logical identifier")
    parameters = sanitize_parameter_summary(args.get("parameter_summary"))
    expected_change = _safe_text(args.get("expected_change"), field="expected_change")
    validation_plan = _safe_text_list(args.get("validation_plan"), field="validation_plan")
    rollback_plan = _safe_text_list(args.get("rollback_plan"), field="rollback_plan")
    requires_backup = args.get("requires_backup")
    if not isinstance(requires_backup, bool):
        raise ProposalError("invalid_field", "requires_backup must be boolean")
    risk_level = ACTION_RISKS[action_type]
    if risk_level == "L3" and not requires_backup and action_type != "check_ha_config":
        raise ProposalError("backup_required", "L3 operation proposals must require a backup")
    return {
        "action_type": action_type,
        "target": target,
        "parameter_summary": parameters,
        "risk_level": risk_level,
        "requires_backup": requires_backup,
        "expected_change": expected_change,
        "validation_plan": validation_plan,
        "rollback_plan": rollback_plan,
    }


class ApprovalLedger:
    def __init__(
        self,
        path: str | Path,
        *,
        ttl_seconds: int = 600,
        max_pending: int = 20,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.path = Path(path)
        self.ttl_seconds = int(ttl_seconds)
        self.max_pending = int(max_pending)
        self.clock = clock
        if not 60 <= self.ttl_seconds <= 3600:
            raise ProposalError("invalid_ttl", "Proposal TTL must be 60-3600 seconds")
        if not 1 <= self.max_pending <= 100:
            raise ProposalError("invalid_limit", "max_pending must be 1-100")
        self._initialize()

    @classmethod
    def from_env(cls) -> "ApprovalLedger":
        path = os.getenv("HA_OPERATIONS_LEDGER_PATH", "").strip()
        if not path:
            hermes_home = os.getenv("HERMES_HOME", "").strip()
            if not hermes_home:
                raise ProposalError("missing_runtime", "HERMES_HOME is unavailable")
            path = str(Path(hermes_home) / "state" / "ha_operations_approval.sqlite3")
        return cls(
            path,
            ttl_seconds=int(os.getenv("HA_OPERATIONS_PROPOSAL_TTL_SECONDS", "600")),
            max_pending=int(os.getenv("HA_OPERATIONS_MAX_PENDING", "20")),
        )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        connection = self._connect()
        try:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS operations (
                    action_id TEXT PRIMARY KEY,
                    action_type TEXT NOT NULL,
                    target TEXT NOT NULL,
                    parameter_summary_hash TEXT NOT NULL,
                    proposal_hash TEXT NOT NULL UNIQUE,
                    risk_level TEXT NOT NULL,
                    requires_backup INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    state TEXT NOT NULL,
                    approval_started_at TEXT,
                    approved_at TEXT,
                    approved_by_hash TEXT,
                    cancelled_at TEXT,
                    cancelled_by_hash TEXT,
                    confirmation_hash TEXT,
                    error_code TEXT,
                    rollback_state TEXT NOT NULL DEFAULT 'not_required'
                )
                """
            )
            connection.execute("PRAGMA user_version=1")
        finally:
            connection.close()
        os.chmod(self.path, 0o600)

    def _transaction(self) -> sqlite3.Connection:
        connection = self._connect()
        connection.execute("BEGIN IMMEDIATE")
        return connection

    @staticmethod
    def _row_status(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "version": 1,
            "action_id": row["action_id"],
            "action_type": row["action_type"],
            "target": row["target"],
            "parameter_summary_hash": f"sha256:{row['parameter_summary_hash']}",
            "proposal_hash": f"sha256:{row['proposal_hash']}",
            "risk_level": row["risk_level"],
            "requires_backup": bool(row["requires_backup"]),
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
            "state": row["state"],
            "approval_started_at": row["approval_started_at"],
            "approved_at": row["approved_at"],
            "cancelled_at": row["cancelled_at"],
            "error_code": row["error_code"],
            "rollback_state": row["rollback_state"],
        }

    def _expire_if_needed(
        self, connection: sqlite3.Connection, row: sqlite3.Row, now: datetime
    ) -> sqlite3.Row:
        if row["state"] in NONTERMINAL_STATES and now >= parse_timestamp(row["expires_at"]):
            connection.execute(
                "UPDATE operations SET state='expired', error_code='approval_ttl_expired', confirmation_hash=NULL WHERE action_id=?",
                (row["action_id"],),
            )
            row = connection.execute(
                "SELECT * FROM operations WHERE action_id=?", (row["action_id"],)
            ).fetchone()
        return row

    def create(self, args: dict[str, Any]) -> dict[str, Any]:
        normalized = normalize_proposal_request(args)
        now = self.clock().astimezone(timezone.utc)
        expires_at = now + timedelta(seconds=self.ttl_seconds)
        parameter_json = canonical_json(normalized["parameter_summary"])
        parameter_hash = sha256_text(parameter_json)

        connection = self._transaction()
        try:
            active = connection.execute(
                "SELECT COUNT(*) FROM operations WHERE state IN ('awaiting_approval','awaiting_confirmation','approved') AND expires_at > ?",
                (isoformat(now),),
            ).fetchone()[0]
            if active >= self.max_pending:
                raise ProposalError("too_many_pending", "Too many pending operation proposals")

            for _attempt in range(8):
                action_id = f"OPS-{now.strftime('%Y%m%d')}-{secrets.token_hex(6).upper()}"
                proposal_document = {
                    "version": 1,
                    "action_id": action_id,
                    **normalized,
                    "created_at": isoformat(now),
                    "expires_at": isoformat(expires_at),
                    "state": "awaiting_approval",
                }
                proposal_hash = sha256_text(canonical_json(proposal_document))
                try:
                    connection.execute(
                        """
                        INSERT INTO operations (
                            action_id, action_type, target, parameter_summary_hash,
                            proposal_hash, risk_level, requires_backup, created_at,
                            expires_at, state
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'awaiting_approval')
                        """,
                        (
                            action_id,
                            normalized["action_type"],
                            normalized["target"],
                            parameter_hash,
                            proposal_hash,
                            normalized["risk_level"],
                            int(normalized["requires_backup"]),
                            isoformat(now),
                            isoformat(expires_at),
                        ),
                    )
                    connection.commit()
                    proposal_document["parameter_summary_hash"] = f"sha256:{parameter_hash}"
                    proposal_document["proposal_hash"] = f"sha256:{proposal_hash}"
                    return proposal_document
                except sqlite3.IntegrityError:
                    continue
            raise ProposalError("id_collision", "Unable to allocate a unique action ID")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get(self, action_id: str) -> dict[str, Any]:
        if not ACTION_ID_RE.fullmatch(action_id):
            raise ProposalError("invalid_action_id", "Invalid action ID")
        now = self.clock().astimezone(timezone.utc)
        connection = self._transaction()
        try:
            row = connection.execute(
                "SELECT * FROM operations WHERE action_id=?", (action_id,)
            ).fetchone()
            if row is None:
                raise ProposalError("not_found", "Operation proposal not found")
            row = self._expire_if_needed(connection, row, now)
            connection.commit()
            return self._row_status(row)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def approve(self, action_id: str, actor_hash: str) -> dict[str, Any]:
        return self._approval_transition(action_id, actor_hash, mode="approve")

    def confirm(self, action_id: str, actor_hash: str, challenge: str) -> dict[str, Any]:
        return self._approval_transition(
            action_id,
            actor_hash,
            mode="confirm",
            challenge=challenge.strip().upper(),
        )

    def cancel(self, action_id: str, actor_hash: str) -> dict[str, Any]:
        if not ACTION_ID_RE.fullmatch(action_id):
            raise ProposalError("invalid_action_id", "Invalid action ID")
        if not IDENTITY_HASH_RE.fullmatch(actor_hash):
            raise ProposalError("not_authorized", "Approval identity is not authorized")
        now = self.clock().astimezone(timezone.utc)
        connection = self._transaction()
        try:
            row = connection.execute(
                "SELECT * FROM operations WHERE action_id=?", (action_id,)
            ).fetchone()
            if row is None:
                raise ProposalError("not_found", "Operation proposal not found")
            row = self._expire_if_needed(connection, row, now)
            if row["state"] in TERMINAL_STATES:
                connection.commit()
                return self._row_status(row)
            if row["state"] not in {"awaiting_approval", "awaiting_confirmation"}:
                raise ProposalError("cannot_cancel", "Operation proposal can no longer be cancelled here")
            if row["approved_by_hash"] and not hmac.compare_digest(
                row["approved_by_hash"], actor_hash
            ):
                raise ProposalError("identity_mismatch", "The same owner must cancel the confirmation")
            connection.execute(
                """
                UPDATE operations
                SET state='cancelled', cancelled_at=?, cancelled_by_hash=?,
                    confirmation_hash=NULL, error_code='cancelled_by_owner'
                WHERE action_id=?
                """,
                (isoformat(now), actor_hash, action_id),
            )
            row = connection.execute(
                "SELECT * FROM operations WHERE action_id=?", (action_id,)
            ).fetchone()
            connection.commit()
            return self._row_status(row)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _approval_transition(
        self,
        action_id: str,
        actor_hash: str,
        *,
        mode: str,
        challenge: str = "",
    ) -> dict[str, Any]:
        if not ACTION_ID_RE.fullmatch(action_id):
            raise ProposalError("invalid_action_id", "Invalid action ID")
        if not IDENTITY_HASH_RE.fullmatch(actor_hash):
            raise ProposalError("not_authorized", "Approval identity is not authorized")
        now = self.clock().astimezone(timezone.utc)
        connection = self._transaction()
        try:
            row = connection.execute(
                "SELECT * FROM operations WHERE action_id=?", (action_id,)
            ).fetchone()
            if row is None:
                raise ProposalError("not_found", "Operation proposal not found")
            row = self._expire_if_needed(connection, row, now)
            if row["state"] in TERMINAL_STATES or row["state"] == "approved":
                connection.commit()
                return self._row_status(row)

            if mode == "approve" and row["state"] == "awaiting_approval":
                if row["risk_level"] == "L3":
                    confirmation = secrets.token_hex(4).upper()
                    connection.execute(
                        """
                        UPDATE operations
                        SET state='awaiting_confirmation', approval_started_at=?,
                            approved_by_hash=?, confirmation_hash=?
                        WHERE action_id=?
                        """,
                        (
                            isoformat(now),
                            actor_hash,
                            sha256_text(confirmation),
                            action_id,
                        ),
                    )
                    row = connection.execute(
                        "SELECT * FROM operations WHERE action_id=?", (action_id,)
                    ).fetchone()
                    connection.commit()
                    result = self._row_status(row)
                    result["confirmation_challenge"] = confirmation
                    return result
                connection.execute(
                    """
                    UPDATE operations
                    SET state='approved', approval_started_at=?, approved_at=?, approved_by_hash=?
                    WHERE action_id=?
                    """,
                    (isoformat(now), isoformat(now), actor_hash, action_id),
                )
            elif mode == "approve" and row["state"] == "awaiting_confirmation":
                if not hmac.compare_digest(row["approved_by_hash"] or "", actor_hash):
                    raise ProposalError("identity_mismatch", "The same owner must confirm this proposal")
                connection.commit()
                result = self._row_status(row)
                result["confirmation_required"] = True
                return result
            elif mode == "confirm" and row["state"] == "awaiting_confirmation":
                if not hmac.compare_digest(row["approved_by_hash"] or "", actor_hash):
                    raise ProposalError("identity_mismatch", "The same owner must confirm this proposal")
                if not CHALLENGE_RE.fullmatch(challenge) or not hmac.compare_digest(
                    row["confirmation_hash"] or "", sha256_text(challenge)
                ):
                    raise ProposalError("invalid_confirmation", "Confirmation challenge is invalid")
                connection.execute(
                    """
                    UPDATE operations
                    SET state='approved', approved_at=?, confirmation_hash=NULL
                    WHERE action_id=?
                    """,
                    (isoformat(now), action_id),
                )
            else:
                raise ProposalError("invalid_transition", "Operation proposal is not in the expected approval state")

            row = connection.execute(
                "SELECT * FROM operations WHERE action_id=?", (action_id,)
            ).fetchone()
            connection.commit()
            return self._row_status(row)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
