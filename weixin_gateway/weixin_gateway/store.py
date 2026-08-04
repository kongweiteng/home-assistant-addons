"""Private identity, cursor, context, queue, attachment and single-poller state."""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
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


class IdentityStore:
    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self.accounts_dir = self.data_dir / "accounts"
        self.migration_dir = self.data_dir / "migration"
        self.locks_dir = self.data_dir / "locks"
        for directory in (self.accounts_dir, self.migration_dir, self.locks_dir):
            directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.active_path = self.accounts_dir / "active.json"

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
                """
            )

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
    ) -> dict[str, Any]:
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
                    "INSERT INTO inbound_messages(message_id,sender_id,conversation_key,text,attachments_json,state,received_at,updated_at) VALUES (?,?,?,?,?,'pending_controller',?,?)",
                    (message_id, sender_id, conversation_key, text, "[]", now, now),
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
        }
