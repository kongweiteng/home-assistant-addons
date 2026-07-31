"""Passkey-backed authorization requests with no operation execution path."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from .contract import ContractError, canonical_json, parse_timestamp, validate_envelope


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
                """
            )
        os.chmod(self.path, 0o600)

    def credential_count(self) -> int:
        with self._connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM passkeys").fetchone()
        return int(row["count"])

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
                    expires_at, assurance
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'passkey_verified')
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

    @staticmethod
    def _request_document(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "version": 1,
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
            "execution_allowed": False,
        }

    @staticmethod
    def _receipt_document(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "version": 1,
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
        self.store = store
        self.passkeys = passkeys
        self.trusted_owner_hashes = trusted_owner_hashes
        self.enrollment_token = enrollment_token
        self.challenge_ttl = timedelta(seconds=challenge_ttl_seconds)
        self.max_passkeys = max_passkeys
        self.max_pending_flows = max_pending_flows
        self.clock = clock
        self._flows: dict[str, PendingFlow] = {}
        self._lock = threading.Lock()

    def create_request(self, envelope: Any) -> dict[str, Any]:
        try:
            validated = validate_envelope(
                envelope,
                trusted_owner_hashes=self.trusted_owner_hashes,
                clock=self.clock,
            )
        except ContractError as exc:
            raise AuthorizationError(exc.code, str(exc)) from exc
        return self.store.create_request(validated, created_at=self.clock())

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
