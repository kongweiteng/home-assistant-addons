"""Private identity, cursor, context, queue, attachment and single-poller state."""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path, PurePosixPath
import secrets
import sqlite3
import tempfile
from typing import Any
import zipfile

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .protocol import canonical_json, validate_cdn_base_url, validate_ilink_base_url


IDENTITY_FORMAT = "weixin-ilink-identity@1"
OWNER_PAIRING_FORMAT = "weixin-owner-pairing@1"
OWNER_PAIRING_TTL_SECONDS = 15 * 60
MEMBER_INVITATION_TTL_SECONDS = 15 * 60
MAX_WEIXIN_USERS = 32
USER_ROLES = frozenset({"owner", "member"})
USER_STATES = frozenset({"active", "suspended", "revoked"})


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class StoreError(RuntimeError):
    def __init__(self, code: str, message: str, *, status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


def atomic_json_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(canonical_json(payload) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def account_hash(account_id: str) -> str:
    return hashlib.sha256(f"weixin-account:{account_id}".encode("utf-8")).hexdigest()


def user_hash(user_id: str) -> str:
    return hashlib.sha256(f"weixin-user:{user_id}".encode("utf-8")).hexdigest()


def conversation_key(user_id: str) -> str:
    return "sha256:" + hashlib.sha256(f"weixin:{user_id}".encode("utf-8")).hexdigest()


def re_fullmatch_short(prefix: str, value: str) -> bool:
    expected_prefix = f"{prefix}-"
    suffix = value[len(expected_prefix) :] if value.startswith(expected_prefix) else ""
    return len(suffix) == 10 and all(character in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567" for character in suffix)


class IdentityStore:
    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self.accounts_dir = self.data_dir / "accounts"
        self.migration_dir = self.data_dir / "migration"
        self.locks_dir = self.data_dir / "locks"
        self.pairing_dir = self.data_dir / "pairing"
        for directory in (self.accounts_dir, self.migration_dir, self.locks_dir, self.pairing_dir):
            directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.active_path = self.accounts_dir / "active.json"
        self.owner_pairing_path = self.pairing_dir / "owner.json"

    def save_identity(self, identity: dict[str, Any]) -> dict[str, Any]:
        normalized = self.validate_identity(identity)
        digest = account_hash(normalized["account_id"])
        atomic_json_write(self.accounts_dir / f"{digest}.json", normalized)
        atomic_json_write(self.active_path, {"account_hash": digest, "updated_at": utc_now()})
        return self.public_summary(normalized)

    def load_identity(self) -> dict[str, Any] | None:
        if not self.active_path.is_file() or self.active_path.is_symlink():
            return None
        try:
            pointer = json.loads(self.active_path.read_text(encoding="utf-8"))
            digest = pointer.get("account_hash")
            if not isinstance(digest, str) or len(digest) != 64:
                return None
            path = self.accounts_dir / f"{digest}.json"
            if path.is_symlink() or not path.is_file():
                return None
            return self.validate_identity(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, StoreError):
            return None

    def bootstrap(self, identity: dict[str, Any]) -> dict[str, Any] | None:
        existing = self.load_identity()
        if existing is not None:
            return existing
        if identity.get("account_id") and identity.get("token"):
            self.save_identity(identity)
            return self.load_identity()
        return None

    @staticmethod
    def validate_identity(identity: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(identity, dict):
            raise StoreError("identity_invalid", "身份必须是对象")
        account_id = identity.get("account_id")
        token = identity.get("token")
        user_id = identity.get("user_id", "")
        allowlist = identity.get("allowed_user_ids", [])
        contexts = identity.get("context_tokens", {})
        if not isinstance(account_id, str) or not account_id or len(account_id) > 256:
            raise StoreError("identity_invalid", "account_id 无效")
        if not isinstance(token, str) or len(token) < 16 or len(token) > 4096:
            raise StoreError("identity_invalid", "iLink token 无效")
        if not isinstance(user_id, str) or len(user_id) > 256:
            raise StoreError("identity_invalid", "user_id 无效")
        if not isinstance(allowlist, list) or len(allowlist) > 32 or any(not isinstance(value, str) or not value or len(value) > 256 for value in allowlist):
            raise StoreError("identity_invalid", "allowed_user_ids 无效")
        if not isinstance(contexts, dict) or len(contexts) > 256:
            raise StoreError("identity_invalid", "context_tokens 无效")
        clean_contexts: dict[str, str] = {}
        for key, value in contexts.items():
            if not isinstance(key, str) or not isinstance(value, str) or not key or not value or len(key) > 256 or len(value) > 8192:
                raise StoreError("identity_invalid", "context_token 条目无效")
            clean_contexts[key] = value
        cursor = identity.get("get_updates_buf", "")
        if not isinstance(cursor, str) or len(cursor) > 1024 * 1024:
            raise StoreError("identity_invalid", "同步游标无效")
        return {
            "format_id": IDENTITY_FORMAT,
            "account_id": account_id,
            "token": token,
            "base_url": validate_ilink_base_url(str(identity.get("base_url") or "https://ilinkai.weixin.qq.com")),
            "cdn_base_url": validate_cdn_base_url(str(identity.get("cdn_base_url") or "https://novac2c.cdn.weixin.qq.com/c2c")),
            "user_id": user_id,
            "allowed_user_ids": sorted(set(allowlist)),
            "get_updates_buf": cursor,
            "context_tokens": clean_contexts,
            "saved_at": str(identity.get("saved_at") or utc_now()),
        }

    @staticmethod
    def public_summary(identity: dict[str, Any]) -> dict[str, Any]:
        return {
            "format_id": IDENTITY_FORMAT,
            "account_hash": account_hash(identity["account_id"]),
            "allowed_user_count": len(identity.get("allowed_user_ids", [])),
            "has_cursor": bool(identity.get("get_updates_buf")),
            "context_count": len(identity.get("context_tokens", {})),
        }

    def cursor(self, identity: dict[str, Any]) -> str:
        return str(identity.get("get_updates_buf") or "")

    def set_cursor(self, identity: dict[str, Any], cursor: str) -> None:
        updated = dict(identity)
        updated["get_updates_buf"] = cursor
        self.save_identity(updated)
        identity["get_updates_buf"] = cursor

    def context(self, identity: dict[str, Any], user_id: str) -> str | None:
        return identity.get("context_tokens", {}).get(user_id)

    def set_context(self, identity: dict[str, Any], user_id: str, token: str) -> None:
        updated = dict(identity)
        contexts = dict(updated.get("context_tokens", {}))
        contexts[user_id] = token
        updated["context_tokens"] = contexts
        self.save_identity(updated)
        identity["context_tokens"] = contexts

    def clear_context(self, identity: dict[str, Any], user_id: str) -> None:
        updated = dict(identity)
        contexts = dict(updated.get("context_tokens", {}))
        contexts.pop(user_id, None)
        updated["context_tokens"] = contexts
        self.save_identity(updated)
        identity["context_tokens"] = contexts

    def mirror_owner(self, identity: dict[str, Any], user_id: str) -> None:
        """Keep the legacy allowlist as a one-owner compatibility mirror."""
        updated = dict(identity)
        updated["allowed_user_ids"] = [user_id]
        self.save_identity(updated)
        identity.clear()
        identity.update(updated)

    def clear_owner_pairing(self) -> None:
        if self.owner_pairing_path.is_file() and not self.owner_pairing_path.is_symlink():
            self.owner_pairing_path.unlink()

    def start_owner_pairing(self, identity: dict[str, Any]) -> dict[str, Any]:
        normalized = self.validate_identity(identity)
        if normalized["allowed_user_ids"]:
            self.clear_owner_pairing()
            raise StoreError("owner_already_bound", "微信 owner 已完成绑定", status=409)
        code = "绑定-CODEX-" + secrets.token_hex(6).upper()
        salt = secrets.token_bytes(16)
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=OWNER_PAIRING_TTL_SECONDS)
        atomic_json_write(
            self.owner_pairing_path,
            {
                "format_id": OWNER_PAIRING_FORMAT,
                "account_hash": account_hash(normalized["account_id"]),
                "salt": base64.urlsafe_b64encode(salt).decode("ascii"),
                "code_sha256": hashlib.sha256(salt + code.encode("utf-8")).hexdigest(),
                "expires_at": expires_at.isoformat(),
            },
        )
        return {"state": "waiting", "code": code, "expires_at": expires_at.isoformat()}

    def owner_pairing_summary(self, identity: dict[str, Any] | None) -> dict[str, Any]:
        if identity is None:
            return {"state": "credential_required"}
        normalized = self.validate_identity(identity)
        if normalized["allowed_user_ids"]:
            self.clear_owner_pairing()
            return {"state": "bound", "owner_count": len(normalized["allowed_user_ids"])}
        document = self._owner_pairing_document(normalized)
        if document is None:
            return {"state": "required"}
        return {"state": "waiting", "expires_at": document["expires_at"]}

    def claim_owner(
        self,
        identity: dict[str, Any],
        *,
        user_id: str,
        text: str,
        context_token: str | None,
    ) -> bool:
        normalized = self.validate_identity(identity)
        if normalized["allowed_user_ids"] or not user_id or not text:
            return False
        document = self._owner_pairing_document(normalized)
        if document is None:
            return False
        try:
            salt = base64.urlsafe_b64decode(str(document["salt"]).encode("ascii"))
        except Exception:
            self.clear_owner_pairing()
            return False
        actual = hashlib.sha256(salt + text.strip().encode("utf-8")).hexdigest()
        if not hmac.compare_digest(actual, str(document.get("code_sha256") or "")):
            return False
        updated = dict(normalized)
        updated["allowed_user_ids"] = [user_id]
        contexts = dict(updated.get("context_tokens", {}))
        if context_token:
            contexts[user_id] = context_token
        updated["context_tokens"] = contexts
        self.save_identity(updated)
        identity.clear()
        identity.update(updated)
        self.clear_owner_pairing()
        return True

    def _owner_pairing_document(self, identity: dict[str, Any]) -> dict[str, Any] | None:
        if not self.owner_pairing_path.is_file() or self.owner_pairing_path.is_symlink():
            return None
        try:
            read_document = json.loads(self.owner_pairing_path.read_text(encoding="utf-8"))
            document = read_document if isinstance(read_document, dict) else None
            if (
                document is None
                or document.get("format_id") != OWNER_PAIRING_FORMAT
                or document.get("account_hash") != account_hash(identity["account_id"])
                or parse_time(str(document.get("expires_at") or "")) <= datetime.now(timezone.utc)
                or not isinstance(document.get("salt"), str)
                or not isinstance(document.get("code_sha256"), str)
            ):
                self.clear_owner_pairing()
                return None
        except (OSError, ValueError, json.JSONDecodeError):
            self.clear_owner_pairing()
            return None
        return document

    def acquire_token_lock(self, token: str) -> "TokenLock":
        return TokenLock(self.locks_dir / f"{hashlib.sha256(token.encode('utf-8')).hexdigest()}.lock")

    def inspect_migration(self, package_path: str | Path) -> dict[str, Any]:
        path = self._migration_path(package_path)
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            self._validate_zip_names(names)
            if set(names) != {"manifest.json", "identity.enc"}:
                raise StoreError("migration_invalid", "身份包文件集合无效")
            manifest = json.loads(archive.read("manifest.json"))
            ciphertext = archive.read("identity.enc")
        if manifest.get("format_id") != IDENTITY_FORMAT or manifest.get("cipher") != "AES-256-GCM":
            raise StoreError("migration_invalid", "身份包格式或加密算法无效")
        if hashlib.sha256(ciphertext).hexdigest() != manifest.get("identity_sha256"):
            raise StoreError("migration_invalid", "身份包密文摘要无效")
        return {
            "valid_envelope": True,
            "format_id": IDENTITY_FORMAT,
            "cipher": "AES-256-GCM",
            "created_at": manifest.get("created_at"),
            "size_bytes": len(ciphertext),
        }

    def import_migration(self, package_path: str | Path, key_b64: str) -> dict[str, Any]:
        path = self._migration_path(package_path)
        self.inspect_migration(path.name)
        try:
            key = base64.urlsafe_b64decode(key_b64.encode("ascii"))
        except Exception as exc:
            raise StoreError("migration_key_invalid", "一次性迁移密钥无效") from exc
        if len(key) != 32:
            raise StoreError("migration_key_invalid", "一次性迁移密钥必须为 32 字节")
        with zipfile.ZipFile(path) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            ciphertext = archive.read("identity.enc")
        try:
            nonce = base64.urlsafe_b64decode(str(manifest["nonce"]).encode("ascii"))
            plaintext = AESGCM(key).decrypt(nonce, ciphertext, IDENTITY_FORMAT.encode("utf-8"))
            identity = json.loads(plaintext)
        except Exception as exc:
            raise StoreError("migration_decrypt_failed", "身份包解密或认证失败") from exc
        summary = self.save_identity(identity)
        return {"state": "credential_ready", **summary}

    @staticmethod
    def build_migration_package(identity: dict[str, Any], key: bytes, destination: str | Path) -> dict[str, Any]:
        normalized = IdentityStore.validate_identity(identity)
        if len(key) != 32:
            raise StoreError("migration_key_invalid", "一次性迁移密钥必须为 32 字节")
        nonce = secrets.token_bytes(12)
        ciphertext = AESGCM(key).encrypt(nonce, canonical_json(normalized).encode("utf-8"), IDENTITY_FORMAT.encode("utf-8"))
        manifest = {
            "format_id": IDENTITY_FORMAT,
            "cipher": "AES-256-GCM",
            "nonce": base64.urlsafe_b64encode(nonce).decode("ascii"),
            "identity_sha256": hashlib.sha256(ciphertext).hexdigest(),
            "created_at": utc_now(),
        }
        destination = Path(destination)
        destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", canonical_json(manifest) + "\n")
            archive.writestr("identity.enc", ciphertext)
        os.chmod(destination, 0o600)
        return manifest

    def _migration_path(self, package_path: str | Path) -> Path:
        reference = Path(package_path)
        if reference.name != str(reference) or not reference.name.endswith(".zip"):
            raise StoreError("migration_invalid", "迁移包引用无效")
        path = self.migration_dir / reference.name
        resolved = path.resolve(strict=True)
        if resolved.parent != self.migration_dir.resolve() or not resolved.is_file():
            raise StoreError("migration_invalid", "迁移包不在固定目录")
        return resolved

    @staticmethod
    def _validate_zip_names(names: list[str]) -> None:
        if len(names) > 8 or len(names) != len(set(names)):
            raise StoreError("migration_invalid", "迁移包路径数量或重复项无效")
        for name in names:
            pure = PurePosixPath(name)
            if pure.is_absolute() or ".." in pure.parts or name.startswith("/"):
                raise StoreError("migration_invalid", "迁移包包含不安全路径")


class TokenLock:
    def __init__(self, path: Path):
        self.path = path
        self.handle: Any = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.handle = self.path.open("a+")
        os.chmod(self.path, 0o600)
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.handle.close()
            self.handle = None
            raise StoreError("token_in_use", "同一 iLink token 已被本地 poller 占用", status=409) from exc

    def release(self) -> None:
        if self.handle is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None

    def __enter__(self) -> "TokenLock":
        self.acquire()
        return self

    def __exit__(self, *_args: Any) -> None:
        self.release()


class GatewayStore:
    def __init__(self, database_path: str | Path, *, data_dir: str | Path, spool_ttl_seconds: int = 86400):
        self.database_path = Path(database_path)
        self.data_dir = Path(data_dir)
        self.spool_dir = self.data_dir / "spool" / "inbound"
        self.spool_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.database_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.spool_ttl_seconds = spool_ttl_seconds
        self.display_secret_path = self.data_dir / "display-secret.bin"
        self.display_secret = self._load_display_secret()
        self._initialize()

    def _load_display_secret(self) -> bytes:
        if self.display_secret_path.is_file() and not self.display_secret_path.is_symlink():
            secret = self.display_secret_path.read_bytes()
            if len(secret) == 32:
                return secret
            raise StoreError("display_secret_invalid", "短标识密钥无效", status=500)
        secret = secrets.token_bytes(32)
        descriptor, temporary_name = tempfile.mkstemp(prefix=".display-secret.", dir=self.data_dir)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(secret)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.display_secret_path)
        finally:
            if temporary.exists():
                temporary.unlink()
        return secret

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
                CREATE TABLE IF NOT EXISTS inbound_messages (
                    message_id TEXT PRIMARY KEY,
                    sender_id TEXT NOT NULL,
                    conversation_key TEXT NOT NULL,
                    text TEXT NOT NULL,
                    attachments_json TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('pending_controller','controller_submitted','completed','failed')),
                    controller_job_id TEXT,
                    received_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    error_code TEXT
                );
                CREATE INDEX IF NOT EXISTS inbound_state_idx ON inbound_messages(state, received_at);
                CREATE TABLE IF NOT EXISTS attachments (
                    attachment_ref TEXT PRIMARY KEY,
                    message_id TEXT NOT NULL,
                    storage_name TEXT NOT NULL,
                    original_filename TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    consumed_at TEXT,
                    FOREIGN KEY(message_id) REFERENCES inbound_messages(message_id)
                );
                CREATE TABLE IF NOT EXISTS outbound_chunks (
                    job_id TEXT NOT NULL,
                    chunk_index INTEGER NOT NULL,
                    client_id TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL CHECK(state IN ('pending','sent','failed')),
                    sent_at TEXT,
                    error_code TEXT,
                    PRIMARY KEY(job_id,chunk_index)
                );
                CREATE TABLE IF NOT EXISTS gateway_meta (
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    users_revision INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS weixin_users (
                    user_hash TEXT PRIMARY KEY,
                    private_user_id TEXT NOT NULL UNIQUE,
                    conversation_key TEXT NOT NULL UNIQUE,
                    alias TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('owner','member')),
                    status TEXT NOT NULL CHECK(status IN ('active','suspended','revoked')),
                    revision INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    revoked_at TEXT,
                    last_seen_at TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS weixin_single_owner_idx
                    ON weixin_users(role) WHERE role='owner';
                CREATE INDEX IF NOT EXISTS weixin_users_status_idx
                    ON weixin_users(status, role, updated_at);
                CREATE TABLE IF NOT EXISTS pairing_invitations (
                    invite_id TEXT PRIMARY KEY,
                    code_salt TEXT NOT NULL,
                    code_hash TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('waiting','claimed','expired','cancelled')),
                    expires_at TEXT NOT NULL,
                    claimed_user_hash TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS pairing_invitations_state_idx
                    ON pairing_invitations(state, expires_at);
                CREATE TABLE IF NOT EXISTS conversation_links (
                    user_hash TEXT PRIMARY KEY,
                    conversation_short TEXT NOT NULL UNIQUE,
                    thread_short TEXT,
                    last_job_short TEXT,
                    last_seen_at TEXT,
                    FOREIGN KEY(user_hash) REFERENCES weixin_users(user_hash)
                );
                CREATE TABLE IF NOT EXISTS admin_mutations (
                    request_id TEXT PRIMARY KEY,
                    scope TEXT NOT NULL,
                    request_digest TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO gateway_meta(id,users_revision,updated_at) VALUES (1,0,?)",
                (utc_now(),),
            )
            self._ensure_column(connection, "inbound_messages", "user_hash", "TEXT")
            self._ensure_column(
                connection,
                "inbound_messages",
                "capability_profile",
                "TEXT NOT NULL DEFAULT 'owner_legacy'",
            )

    @staticmethod
    def _ensure_column(connection: sqlite3.Connection, table: str, column: str, declaration: str) -> None:
        columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    def short_id(self, prefix: str, value: str) -> str:
        digest = hmac.new(self.display_secret, f"{prefix}:{value}".encode("utf-8"), hashlib.sha256).digest()
        encoded = base64.b32encode(digest).decode("ascii").rstrip("=")[:10]
        return f"{prefix}-{encoded}"

    def users_revision(self, connection: sqlite3.Connection | None = None) -> int:
        if connection is not None:
            return int(connection.execute("SELECT users_revision FROM gateway_meta WHERE id=1").fetchone()[0])
        with self._connect() as current:
            return self.users_revision(current)

    def _next_users_revision(self, connection: sqlite3.Connection) -> int:
        revision = self.users_revision(connection) + 1
        connection.execute(
            "UPDATE gateway_meta SET users_revision=?,updated_at=? WHERE id=1",
            (revision, utc_now()),
        )
        return revision

    @staticmethod
    def _request_digest(scope: str, payload: dict[str, Any]) -> str:
        return hashlib.sha256(f"{scope}\n{canonical_json(payload)}".encode("utf-8")).hexdigest()

    def _idempotent_response(
        self,
        connection: sqlite3.Connection,
        *,
        request_id: str,
        scope: str,
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        if not isinstance(request_id, str) or not 16 <= len(request_id) <= 128 or any(
            character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for character in request_id
        ):
            raise StoreError("request_id_invalid", "request_id 无效")
        digest = self._request_digest(scope, payload)
        row = connection.execute(
            "SELECT scope,request_digest,response_json FROM admin_mutations WHERE request_id=?",
            (request_id,),
        ).fetchone()
        if row is None:
            return None
        if row["scope"] != scope or not hmac.compare_digest(row["request_digest"], digest):
            raise StoreError("idempotency_conflict", "request_id 已用于不同请求", status=409)
        return json.loads(row["response_json"])

    def _record_mutation(
        self,
        connection: sqlite3.Connection,
        *,
        request_id: str,
        scope: str,
        payload: dict[str, Any],
        response: dict[str, Any],
    ) -> None:
        connection.execute(
            "INSERT INTO admin_mutations(request_id,scope,request_digest,response_json,created_at) VALUES (?,?,?,?,?)",
            (request_id, scope, self._request_digest(scope, payload), canonical_json(response), utc_now()),
        )

    def _assert_revision(self, connection: sqlite3.Connection, expected_revision: int) -> None:
        current = self.users_revision(connection)
        if not isinstance(expected_revision, int) or isinstance(expected_revision, bool) or expected_revision != current:
            raise StoreError("revision_conflict", "用户目录已变化，请刷新后重试", status=409)

    def migrate_identity_allowlist(self, allowed_user_ids: list[str]) -> dict[str, Any]:
        """Create the initial owner once while preserving legacy allowlist semantics."""
        if len(allowed_user_ids) > 1:
            raise StoreError("owner_migration_ambiguous", "旧 allowlist 包含多个用户，无法确定唯一 owner", status=409)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT * FROM weixin_users WHERE role='owner' ORDER BY created_at"
            ).fetchall()
            if len(rows) > 1:
                raise StoreError("owner_invariant_broken", "用户目录存在多个 owner", status=500)
            if rows:
                owner = rows[0]
                if owner["status"] != "active":
                    raise StoreError("owner_invariant_broken", "唯一 owner 不是 active", status=500)
                self._backfill_legacy_owner_messages(connection, owner)
                return {
                    "state": "existing",
                    "owner_private_id": owner["private_user_id"],
                    "revision": self.users_revision(connection),
                }
            count = int(connection.execute("SELECT COUNT(*) FROM weixin_users").fetchone()[0])
            if count:
                raise StoreError("owner_invariant_broken", "用户目录有成员但没有 owner", status=500)
            if not allowed_user_ids:
                return {"state": "empty", "owner_private_id": None, "revision": self.users_revision(connection)}
            owner_id = allowed_user_ids[0]
            now = utc_now()
            revision = self._next_users_revision(connection)
            digest = user_hash(owner_id)
            key = conversation_key(owner_id)
            connection.execute(
                "INSERT INTO weixin_users(user_hash,private_user_id,conversation_key,alias,role,status,revision,created_at,updated_at,last_seen_at) VALUES (?,?,?,?,?,'active',?,?,?,NULL)",
                (digest, owner_id, key, "管理员", "owner", revision, now, now),
            )
            connection.execute(
                "INSERT INTO conversation_links(user_hash,conversation_short) VALUES (?,?)",
                (digest, self.short_id("CV", key)),
            )
            owner = connection.execute(
                "SELECT * FROM weixin_users WHERE user_hash=?",
                (digest,),
            ).fetchone()
            assert owner is not None
            self._backfill_legacy_owner_messages(connection, owner)
            return {"state": "migrated", "owner_private_id": owner_id, "revision": revision}

    @staticmethod
    def _backfill_legacy_owner_messages(
        connection: sqlite3.Connection,
        owner: sqlite3.Row,
    ) -> None:
        """Attach pre-0.2.0 messages to the migrated owner for later role revalidation."""
        connection.execute(
            """
            UPDATE inbound_messages
            SET user_hash=?, capability_profile='owner'
            WHERE user_hash IS NULL
              AND sender_id=?
              AND capability_profile='owner_legacy'
            """,
            (owner["user_hash"], owner["private_user_id"]),
        )

    def register_paired_owner(self, user_id: str) -> dict[str, Any]:
        migration = self.migrate_identity_allowlist([user_id])
        return self.user_by_private_id(user_id) or migration

    def user_by_private_id(self, private_user_id: str) -> dict[str, Any] | None:
        digest = user_hash(private_user_id)
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM weixin_users WHERE user_hash=?", (digest,)).fetchone()
            return None if row is None else self._private_user_document(row)

    def user_by_short(self, wx_short: str) -> dict[str, Any]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM weixin_users").fetchall()
            for row in rows:
                if hmac.compare_digest(self.short_id("WX", row["user_hash"]), wx_short):
                    return self._private_user_document(row)
        raise StoreError("user_not_found", "微信用户不存在", status=404)

    def active_owner(self) -> dict[str, Any]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM weixin_users WHERE role='owner' AND status='active'"
            ).fetchall()
            if len(rows) != 1:
                raise StoreError("notification_owner_unavailable", "微信通知要求精确绑定一个 active owner", status=409)
            return self._private_user_document(rows[0])

    def list_users(self) -> dict[str, Any]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM weixin_users ORDER BY CASE role WHEN 'owner' THEN 0 ELSE 1 END, created_at"
            ).fetchall()
            return {
                "revision": self.users_revision(connection),
                "users": [self._public_user_document(connection, row) for row in rows],
                "limits": {"max_users": MAX_WEIXIN_USERS},
            }

    def list_conversations(self) -> dict[str, Any]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT u.user_hash,u.alias,u.role,u.status,l.conversation_short,l.thread_short,l.last_job_short,l.last_seen_at
                FROM weixin_users u LEFT JOIN conversation_links l ON l.user_hash=u.user_hash
                ORDER BY CASE u.role WHEN 'owner' THEN 0 ELSE 1 END,u.created_at
                """
            ).fetchall()
            return {
                "revision": self.users_revision(connection),
                "conversations": [
                    {
                        "wx_short": self.short_id("WX", row["user_hash"]),
                        "alias": row["alias"],
                        "role": row["role"],
                        "status": row["status"],
                        "conversation_short": row["conversation_short"],
                        "thread_short": row["thread_short"],
                        "last_job_short": row["last_job_short"],
                        "last_seen_at": row["last_seen_at"],
                    }
                    for row in rows
                ],
            }

    def create_member_invitation(
        self,
        *,
        expected_revision: int,
        request_id: str,
        ttl_seconds: int = MEMBER_INVITATION_TTL_SECONDS,
    ) -> dict[str, Any]:
        if not isinstance(ttl_seconds, int) or isinstance(ttl_seconds, bool) or not 60 <= ttl_seconds <= 3600:
            raise StoreError("invitation_ttl_invalid", "邀请码有效期必须在 60 到 3600 秒之间")
        scope = "member_invitation_create"
        payload = {"revision": expected_revision, "ttl_seconds": ttl_seconds}
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            replay = self._idempotent_response(
                connection, request_id=request_id, scope=scope, payload=payload
            )
            if replay is not None:
                return replay
            self._assert_revision(connection, expected_revision)
            count = int(connection.execute("SELECT COUNT(*) FROM weixin_users WHERE status!='revoked'").fetchone()[0])
            if count >= MAX_WEIXIN_USERS:
                raise StoreError("user_limit_reached", "微信用户数量已达到上限", status=409)
            now_dt = datetime.now(timezone.utc)
            now = now_dt.isoformat()
            connection.execute(
                "UPDATE pairing_invitations SET state='expired',updated_at=? WHERE state='waiting' AND expires_at<=?",
                (now, now),
            )
            code = "加入-CODEX-" + secrets.token_hex(16).upper()
            salt = secrets.token_bytes(16)
            invite_id = secrets.token_urlsafe(24)
            expires_at = (now_dt + timedelta(seconds=ttl_seconds)).isoformat()
            connection.execute(
                "INSERT INTO pairing_invitations(invite_id,code_salt,code_hash,state,expires_at,created_at,updated_at) VALUES (?,?,?,'waiting',?,?,?)",
                (
                    invite_id,
                    base64.urlsafe_b64encode(salt).decode("ascii"),
                    hashlib.sha256(salt + code.encode("utf-8")).hexdigest(),
                    expires_at,
                    now,
                    now,
                ),
            )
            revision = self._next_users_revision(connection)
            replay_response = {
                "state": "created_code_already_shown",
                "invite_short": self.short_id("IV", invite_id),
                "expires_at": expires_at,
                "revision": revision,
            }
            self._record_mutation(
                connection,
                request_id=request_id,
                scope=scope,
                payload=payload,
                response=replay_response,
            )
            return {**replay_response, "state": "waiting", "code": code}

    def cancel_member_invitation(
        self,
        *,
        invite_short: str,
        expected_revision: int,
        request_id: str,
    ) -> dict[str, Any]:
        scope = "member_invitation_cancel"
        payload = {"invite_short": invite_short, "revision": expected_revision}
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            replay = self._idempotent_response(connection, request_id=request_id, scope=scope, payload=payload)
            if replay is not None:
                return replay
            self._assert_revision(connection, expected_revision)
            row = self._invitation_by_short(connection, invite_short)
            if row["state"] != "waiting":
                raise StoreError("invitation_not_waiting", "邀请码已不可取消", status=409)
            now = utc_now()
            connection.execute(
                "UPDATE pairing_invitations SET state='cancelled',updated_at=? WHERE invite_id=?",
                (now, row["invite_id"]),
            )
            revision = self._next_users_revision(connection)
            response = {"state": "cancelled", "invite_short": invite_short, "revision": revision}
            self._record_mutation(connection, request_id=request_id, scope=scope, payload=payload, response=response)
            return response

    def claim_member_invitation(self, *, user_id: str, text: str) -> dict[str, Any] | None:
        if not user_id or not text:
            return None
        digest = user_hash(user_id)
        key = conversation_key(user_id)
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT * FROM weixin_users WHERE user_hash=?", (digest,)).fetchone()
            if existing is not None:
                return None
            connection.execute(
                "UPDATE pairing_invitations SET state='expired',updated_at=? WHERE state='waiting' AND expires_at<=?",
                (now, now),
            )
            rows = connection.execute(
                "SELECT * FROM pairing_invitations WHERE state='waiting' ORDER BY created_at"
            ).fetchall()
            matched = None
            for row in rows:
                try:
                    salt = base64.urlsafe_b64decode(row["code_salt"].encode("ascii"))
                except Exception:
                    continue
                actual = hashlib.sha256(salt + text.encode("utf-8")).hexdigest()
                if hmac.compare_digest(actual, row["code_hash"]):
                    matched = row
                    break
            if matched is None:
                return None
            count = int(connection.execute("SELECT COUNT(*) FROM weixin_users WHERE status!='revoked'").fetchone()[0])
            if count >= MAX_WEIXIN_USERS:
                raise StoreError("user_limit_reached", "微信用户数量已达到上限", status=409)
            revision = self._next_users_revision(connection)
            alias = f"成员 {count}"
            connection.execute(
                "INSERT INTO weixin_users(user_hash,private_user_id,conversation_key,alias,role,status,revision,created_at,updated_at,last_seen_at) VALUES (?,?,?,?,?,'active',?,?,?,?)",
                (digest, user_id, key, alias, "member", revision, now, now, now),
            )
            connection.execute(
                "INSERT INTO conversation_links(user_hash,conversation_short,last_seen_at) VALUES (?,?,?)",
                (digest, self.short_id("CV", key), now),
            )
            changed = connection.execute(
                "UPDATE pairing_invitations SET state='claimed',claimed_user_hash=?,updated_at=? WHERE invite_id=? AND state='waiting'",
                (digest, now, matched["invite_id"]),
            ).rowcount
            if changed != 1:
                raise StoreError("invitation_already_claimed", "邀请码已被领取", status=409)
            row = connection.execute("SELECT * FROM weixin_users WHERE user_hash=?", (digest,)).fetchone()
            assert row is not None
            return self._private_user_document(row)

    def update_alias(
        self,
        *,
        wx_short: str,
        alias: str,
        expected_revision: int,
        request_id: str,
    ) -> dict[str, Any]:
        clean_alias = alias.strip()
        if not 1 <= len(clean_alias) <= 40 or any(ord(character) < 32 for character in clean_alias):
            raise StoreError("alias_invalid", "别名长度必须为 1 到 40 个可见字符")
        return self._mutate_user(
            action="alias",
            wx_short=wx_short,
            expected_revision=expected_revision,
            request_id=request_id,
            extra={"alias": clean_alias},
        )

    def change_user_state(
        self,
        *,
        wx_short: str,
        action: str,
        expected_revision: int,
        request_id: str,
    ) -> dict[str, Any]:
        if action not in {"suspend", "resume", "revoke"}:
            raise StoreError("user_action_invalid", "用户操作无效")
        return self._mutate_user(
            action=action,
            wx_short=wx_short,
            expected_revision=expected_revision,
            request_id=request_id,
            extra={},
        )

    def _mutate_user(
        self,
        *,
        action: str,
        wx_short: str,
        expected_revision: int,
        request_id: str,
        extra: dict[str, Any],
    ) -> dict[str, Any]:
        scope = f"user_{action}"
        payload = {"wx_short": wx_short, "revision": expected_revision, **extra}
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            replay = self._idempotent_response(connection, request_id=request_id, scope=scope, payload=payload)
            if replay is not None:
                return replay
            self._assert_revision(connection, expected_revision)
            row = self._user_row_by_short(connection, wx_short)
            if action in {"suspend", "revoke"} and row["role"] == "owner":
                raise StoreError("owner_protected", "唯一 owner 不能暂停或移除", status=409)
            now = utc_now()
            if action == "alias":
                connection.execute(
                    "UPDATE weixin_users SET alias=?,updated_at=? WHERE user_hash=?",
                    (extra["alias"], now, row["user_hash"]),
                )
            elif action == "suspend":
                if row["status"] != "active":
                    raise StoreError("user_state_conflict", "只有 active 成员可以暂停", status=409)
                connection.execute(
                    "UPDATE weixin_users SET status='suspended',updated_at=? WHERE user_hash=?",
                    (now, row["user_hash"]),
                )
            elif action == "resume":
                if row["role"] != "member" or row["status"] != "suspended":
                    raise StoreError("user_state_conflict", "只有 suspended 成员可以恢复", status=409)
                connection.execute(
                    "UPDATE weixin_users SET status='active',updated_at=? WHERE user_hash=?",
                    (now, row["user_hash"]),
                )
            else:
                if row["status"] == "revoked":
                    raise StoreError("user_state_conflict", "成员已经移除", status=409)
                connection.execute(
                    "UPDATE weixin_users SET status='revoked',revoked_at=?,updated_at=? WHERE user_hash=?",
                    (now, now, row["user_hash"]),
                )
            revision = self._next_users_revision(connection)
            connection.execute(
                "UPDATE weixin_users SET revision=? WHERE user_hash=?",
                (revision, row["user_hash"]),
            )
            updated = connection.execute("SELECT * FROM weixin_users WHERE user_hash=?", (row["user_hash"],)).fetchone()
            assert updated is not None
            response = {"revision": revision, "user": self._public_user_document(connection, updated)}
            self._record_mutation(connection, request_id=request_id, scope=scope, payload=payload, response=response)
            return response

    def transfer_owner(
        self,
        *,
        target_wx_short: str,
        expected_revision: int,
        request_id: str,
        confirmation: str,
    ) -> dict[str, Any]:
        if confirmation != "TRANSFER_OWNER":
            raise StoreError("owner_transfer_confirmation_required", "Owner 转移确认词无效", status=409)
        scope = "owner_transfer"
        payload = {
            "target_wx_short": target_wx_short,
            "revision": expected_revision,
            "confirmation": confirmation,
        }
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            replay = self._idempotent_response(connection, request_id=request_id, scope=scope, payload=payload)
            if replay is not None:
                replay["replayed"] = True
                return replay
            self._assert_revision(connection, expected_revision)
            owners = connection.execute("SELECT * FROM weixin_users WHERE role='owner' AND status='active'").fetchall()
            if len(owners) != 1:
                raise StoreError("owner_invariant_broken", "Owner 转移前不满足唯一 active owner", status=500)
            old_owner = owners[0]
            target = self._user_row_by_short(connection, target_wx_short)
            if target["role"] != "member" or target["status"] != "active":
                raise StoreError("owner_transfer_target_invalid", "目标必须是 active member", status=409)
            now = utc_now()
            connection.execute(
                "UPDATE weixin_users SET role='member',updated_at=? WHERE user_hash=?",
                (now, old_owner["user_hash"]),
            )
            connection.execute(
                "UPDATE weixin_users SET role='owner',updated_at=? WHERE user_hash=?",
                (now, target["user_hash"]),
            )
            revision = self._next_users_revision(connection)
            connection.execute(
                "UPDATE weixin_users SET revision=? WHERE user_hash IN (?,?)",
                (revision, old_owner["user_hash"], target["user_hash"]),
            )
            response = {
                "revision": revision,
                "owner": self._public_user_document(
                    connection,
                    connection.execute("SELECT * FROM weixin_users WHERE user_hash=?", (target["user_hash"],)).fetchone(),
                ),
                "previous_owner": self.short_id("WX", old_owner["user_hash"]),
                "owner_private_id": target["private_user_id"],
                "previous_owner_private_id": old_owner["private_user_id"],
            }
            stored_response = {key: value for key, value in response.items() if not key.endswith("private_id")}
            self._record_mutation(connection, request_id=request_id, scope=scope, payload=payload, response=stored_response)
            return response

    def restore_owner_after_mirror_failure(
        self,
        previous_owner_private_id: str,
        target_private_id: str,
        request_id: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = user_hash(previous_owner_private_id)
            target = user_hash(target_private_id)
            connection.execute("UPDATE weixin_users SET role='member' WHERE user_hash=?", (target,))
            connection.execute("UPDATE weixin_users SET role='owner' WHERE user_hash=?", (previous,))
            revision = self._next_users_revision(connection)
            connection.execute(
                "UPDATE weixin_users SET revision=?,updated_at=? WHERE user_hash IN (?,?)",
                (revision, utc_now(), previous, target),
            )
            connection.execute("DELETE FROM admin_mutations WHERE request_id=?", (request_id,))

    def touch_user(self, private_user_id: str) -> dict[str, Any] | None:
        digest = user_hash(private_user_id)
        now = utc_now()
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM weixin_users WHERE user_hash=?", (digest,)).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE weixin_users SET last_seen_at=?,updated_at=? WHERE user_hash=?",
                (now, now, digest),
            )
            connection.execute(
                "UPDATE conversation_links SET last_seen_at=? WHERE user_hash=?",
                (now, digest),
            )
            updated = connection.execute("SELECT * FROM weixin_users WHERE user_hash=?", (digest,)).fetchone()
            assert updated is not None
            return self._private_user_document(updated)

    def user_is_active(self, digest: str | None) -> bool:
        if not digest:
            return False
        with self._connect() as connection:
            row = connection.execute("SELECT status FROM weixin_users WHERE user_hash=?", (digest,)).fetchone()
            return row is not None and row["status"] == "active"

    def authorize_stored_message(self, digest: str | None, capability_profile: str) -> dict[str, Any]:
        """Revalidate a queued message without ever upgrading its stored authority."""
        if capability_profile not in {"owner", "owner_legacy", "member_read_only"}:
            return {"allowed": False, "error_code": "message_capability_invalid", "capability_profile": None}
        if not digest:
            return {
                "allowed": capability_profile == "owner_legacy",
                "error_code": None if capability_profile == "owner_legacy" else "message_user_missing",
                "capability_profile": "owner_legacy" if capability_profile == "owner_legacy" else None,
            }
        with self._connect() as connection:
            row = connection.execute(
                "SELECT role,status FROM weixin_users WHERE user_hash=?",
                (digest,),
            ).fetchone()
        if row is None or row["status"] != "active":
            return {"allowed": False, "error_code": "message_user_inactive", "capability_profile": None}
        current_profile = "owner" if row["role"] == "owner" else "member_read_only"
        effective_profile = (
            "member_read_only"
            if "member_read_only" in {capability_profile, current_profile}
            else "owner"
        )
        return {"allowed": True, "error_code": None, "capability_profile": effective_profile}

    def update_conversation_link(self, digest: str | None, *, thread_short: str | None, job_id: str | None) -> None:
        if not digest:
            return
        if thread_short is not None and not re_fullmatch_short("TH", thread_short):
            thread_short = None
        job_short = None if not job_id else self.short_id("JB", job_id)
        with self._connect() as connection:
            connection.execute(
                "UPDATE conversation_links SET thread_short=COALESCE(?,thread_short),last_job_short=COALESCE(?,last_job_short),last_seen_at=? WHERE user_hash=?",
                (thread_short, job_short, utc_now(), digest),
            )

    def invitation_summary(self) -> dict[str, int]:
        now = utc_now()
        with self._connect() as connection:
            connection.execute(
                "UPDATE pairing_invitations SET state='expired',updated_at=? WHERE state='waiting' AND expires_at<=?",
                (now, now),
            )
            counts = {
                row["state"]: int(row["count"])
                for row in connection.execute("SELECT state,COUNT(*) AS count FROM pairing_invitations GROUP BY state")
            }
            return {state: counts.get(state, 0) for state in ("waiting", "claimed", "expired", "cancelled")}

    def reset_access_directory_for_identity_replacement(self) -> int:
        """Fail closed and remove principals that belong to a different bot identity."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            now = utc_now()
            connection.execute(
                "UPDATE inbound_messages SET state='failed',error_code='identity_replaced',updated_at=? WHERE state IN ('pending_controller','controller_submitted')",
                (now,),
            )
            connection.execute("DELETE FROM conversation_links")
            connection.execute("DELETE FROM pairing_invitations")
            connection.execute("DELETE FROM weixin_users")
            connection.execute("DELETE FROM admin_mutations")
            return self._next_users_revision(connection)

    def _user_row_by_short(self, connection: sqlite3.Connection, wx_short: str) -> sqlite3.Row:
        if not isinstance(wx_short, str) or not re_fullmatch_short("WX", wx_short):
            raise StoreError("user_not_found", "微信用户不存在", status=404)
        for row in connection.execute("SELECT * FROM weixin_users"):
            if hmac.compare_digest(self.short_id("WX", row["user_hash"]), wx_short):
                return row
        raise StoreError("user_not_found", "微信用户不存在", status=404)

    def _invitation_by_short(self, connection: sqlite3.Connection, invite_short: str) -> sqlite3.Row:
        if not isinstance(invite_short, str) or not re_fullmatch_short("IV", invite_short):
            raise StoreError("invitation_not_found", "邀请码不存在", status=404)
        for row in connection.execute("SELECT * FROM pairing_invitations"):
            if hmac.compare_digest(self.short_id("IV", row["invite_id"]), invite_short):
                return row
        raise StoreError("invitation_not_found", "邀请码不存在", status=404)

    def _public_user_document(self, connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        link = connection.execute("SELECT * FROM conversation_links WHERE user_hash=?", (row["user_hash"],)).fetchone()
        return {
            "wx_short": self.short_id("WX", row["user_hash"]),
            "alias": row["alias"],
            "role": row["role"],
            "status": row["status"],
            "revision": row["revision"],
            "has_context": False,
            "conversation_short": None if link is None else link["conversation_short"],
            "thread_short": None if link is None else link["thread_short"],
            "last_job_short": None if link is None else link["last_job_short"],
            "last_seen_at": row["last_seen_at"],
        }

    @staticmethod
    def _private_user_document(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "user_hash": row["user_hash"],
            "private_user_id": row["private_user_id"],
            "conversation_key": row["conversation_key"],
            "alias": row["alias"],
            "role": row["role"],
            "status": row["status"],
            "revision": row["revision"],
            "last_seen_at": row["last_seen_at"],
        }

    def message_exists(self, message_id: str) -> bool:
        with self._connect() as connection:
            return connection.execute("SELECT 1 FROM inbound_messages WHERE message_id=?", (message_id,)).fetchone() is not None

    def store_message(
        self,
        *,
        message_id: str,
        sender_id: str,
        conversation_key: str,
        text: str,
        media: list[tuple[dict[str, Any], bytes]],
        user_digest: str | None = None,
        capability_profile: str = "owner_legacy",
    ) -> dict[str, Any]:
        if capability_profile not in {"owner", "owner_legacy", "member_read_only"}:
            raise StoreError("capability_profile_invalid", "会话权限画像无效")
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT * FROM inbound_messages WHERE message_id=?", (message_id,)).fetchone()
            if existing:
                return self._message_document(existing)
            attachment_documents: list[dict[str, Any]] = []
            staged: list[tuple[Path, Path]] = []
            created_targets: list[Path] = []
            try:
                connection.execute(
                    "INSERT INTO inbound_messages(message_id,sender_id,conversation_key,text,attachments_json,state,received_at,updated_at,user_hash,capability_profile) VALUES (?,?,?,?,?,'pending_controller',?,?,?,?)",
                    (message_id, sender_id, conversation_key, text, "[]", now, now, user_digest, capability_profile),
                )
                for spec, content in media:
                    digest = hashlib.sha256(content).hexdigest()
                    storage_name = digest
                    target = self.spool_dir / storage_name
                    if not target.exists():
                        descriptor, temp_name = tempfile.mkstemp(prefix=".media.", dir=self.spool_dir)
                        temporary = Path(temp_name)
                        with os.fdopen(descriptor, "wb") as handle:
                            handle.write(content)
                            handle.flush()
                            os.fsync(handle.fileno())
                        os.chmod(temporary, 0o600)
                        staged.append((temporary, target))
                    reference = secrets.token_urlsafe(32)
                    document = {
                        "attachment_ref": reference,
                        "media_type": spec["media_type"],
                        "size_bytes": len(content),
                        "sha256": f"sha256:{digest}",
                        "display_name": spec["filename"],
                    }
                    attachment_documents.append(document)
                    connection.execute(
                        "INSERT INTO attachments(attachment_ref,message_id,storage_name,original_filename,mime_type,size_bytes,sha256,expires_at) VALUES (?,?,?,?,?,?,?,?)",
                        (
                            reference,
                            message_id,
                            storage_name,
                            spec["filename"],
                            spec["mime_type"],
                            len(content),
                            digest,
                            (datetime.now(timezone.utc) + timedelta(seconds=self.spool_ttl_seconds)).isoformat(),
                        ),
                    )
                connection.execute(
                    "UPDATE inbound_messages SET attachments_json=?,updated_at=? WHERE message_id=?",
                    (canonical_json(attachment_documents), now, message_id),
                )
                for temporary, target in staged:
                    if target.exists():
                        temporary.unlink()
                    else:
                        os.replace(temporary, target)
                        created_targets.append(target)
                connection.commit()
            except Exception:
                for temporary, _target in staged:
                    if temporary.exists():
                        temporary.unlink()
                for target in created_targets:
                    if target.is_file() and not target.is_symlink():
                        target.unlink()
                raise
        return self.get_message(message_id)

    def get_message(self, message_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM inbound_messages WHERE message_id=?", (message_id,)).fetchone()
            if row is None:
                raise StoreError("message_not_found", "消息不存在", status=404)
            return self._message_document(row)

    def pending_controller(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM inbound_messages WHERE state='pending_controller' ORDER BY received_at LIMIT ?", (limit,)
            ).fetchall()
            return [self._message_document(row) for row in rows]

    def submitted(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM inbound_messages WHERE state='controller_submitted' ORDER BY updated_at LIMIT ?", (limit,)
            ).fetchall()
            return [self._message_document(row) for row in rows]

    def mark_submitted(self, message_id: str, job_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE inbound_messages SET state='controller_submitted',controller_job_id=?,updated_at=?,error_code=NULL WHERE message_id=? AND state='pending_controller'",
                (job_id, utc_now(), message_id),
            )

    def mark_finished(self, message_id: str, *, success: bool, error_code: str | None = None) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE inbound_messages SET state=?,updated_at=?,error_code=? WHERE message_id=?",
                ("completed" if success else "failed", utc_now(), error_code, message_id),
            )

    def prepare_chunk(self, job_id: str, chunk_index: int) -> tuple[str, bool]:
        client_id = "codex-weixin-" + hashlib.sha256(f"{job_id}:{chunk_index}".encode("utf-8")).hexdigest()[:32]
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO outbound_chunks(job_id,chunk_index,client_id,state) VALUES (?,?,?,'pending')",
                (job_id, chunk_index, client_id),
            )
            row = connection.execute("SELECT state FROM outbound_chunks WHERE job_id=? AND chunk_index=?", (job_id, chunk_index)).fetchone()
            return client_id, row["state"] == "sent"

    def mark_chunk(self, job_id: str, chunk_index: int, *, success: bool, error_code: str | None = None) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE outbound_chunks SET state=?,sent_at=?,error_code=? WHERE job_id=? AND chunk_index=?",
                ("sent" if success else "failed", utc_now() if success else None, error_code, job_id, chunk_index),
            )

    def preview_attachment(self, reference: str) -> tuple[dict[str, Any], bytes]:
        """Read and verify an attachment without consuming its one-time reference."""
        return self._read_attachment(reference, consume=False)

    def consume_attachment(self, reference: str) -> tuple[dict[str, Any], bytes]:
        """Read and atomically consume an attachment reference."""
        return self._read_attachment(reference, consume=True)

    def _read_attachment(self, reference: str, *, consume: bool) -> tuple[dict[str, Any], bytes]:
        with self._connect() as connection:
            if consume:
                connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM attachments WHERE attachment_ref=?", (reference,)).fetchone()
            if row is None or row["consumed_at"] is not None or parse_time(row["expires_at"]) <= datetime.now(timezone.utc):
                raise StoreError("attachment_unavailable", "附件引用不存在、已消费或已过期", status=404)
            path = (self.spool_dir / row["storage_name"]).resolve(strict=True)
            if path.parent != self.spool_dir.resolve() or not path.is_file() or path.is_symlink():
                raise StoreError("attachment_unavailable", "附件存储越界或缺失", status=404)
            content = path.read_bytes()
            if len(content) != row["size_bytes"] or hashlib.sha256(content).hexdigest() != row["sha256"]:
                raise StoreError("attachment_invalid", "附件大小或摘要不一致", status=409)
            if consume:
                connection.execute("UPDATE attachments SET consumed_at=? WHERE attachment_ref=?", (utc_now(), reference))
            return {
                "original_filename": row["original_filename"],
                "mime_type": row["mime_type"],
                "size_bytes": row["size_bytes"],
                "sha256": f"sha256:{row['sha256']}",
            }, content

    def cleanup_spool(self) -> int:
        now = utc_now()
        removed = 0
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT attachment_ref,storage_name FROM attachments WHERE expires_at<=? OR consumed_at IS NOT NULL", (now,)
            ).fetchall()
            for row in rows:
                connection.execute("DELETE FROM attachments WHERE attachment_ref=?", (row["attachment_ref"],))
                remaining = connection.execute("SELECT 1 FROM attachments WHERE storage_name=? LIMIT 1", (row["storage_name"],)).fetchone()
                if remaining is None:
                    path = self.spool_dir / row["storage_name"]
                    if path.is_file() and not path.is_symlink():
                        path.unlink()
                        removed += 1
        return removed

    def status(self) -> dict[str, Any]:
        with self._connect() as connection:
            message_counts = {row["state"]: row["count"] for row in connection.execute("SELECT state,COUNT(*) AS count FROM inbound_messages GROUP BY state")}
            return {
                "messages": {state: message_counts.get(state, 0) for state in ("pending_controller", "controller_submitted", "completed", "failed")},
                "attachments": connection.execute("SELECT COUNT(*) FROM attachments WHERE consumed_at IS NULL").fetchone()[0],
                "spool_bytes": connection.execute("SELECT COALESCE(SUM(size_bytes),0) FROM attachments WHERE consumed_at IS NULL").fetchone()[0],
            }

    @staticmethod
    def _message_document(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "message_id": row["message_id"],
            "sender_id": row["sender_id"],
            "conversation_key": row["conversation_key"],
            "text": row["text"],
            "attachments": json.loads(row["attachments_json"]),
            "state": row["state"],
            "controller_job_id": row["controller_job_id"],
            "received_at": row["received_at"],
            "error_code": row["error_code"],
            "user_hash": row["user_hash"],
            "capability_profile": row["capability_profile"],
        }
