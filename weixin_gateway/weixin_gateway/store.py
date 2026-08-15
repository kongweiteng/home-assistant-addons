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
import re
import secrets
import sqlite3
import tempfile
from typing import Any, BinaryIO
import zipfile

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .protocol import canonical_json, validate_cdn_base_url, validate_ilink_base_url


IDENTITY_FORMAT = "weixin-ilink-identity@1"
OWNER_PAIRING_FORMAT = "weixin-owner-pairing@1"
OWNER_PAIRING_TTL_SECONDS = 15 * 60
MEMBER_INVITATION_TTL_SECONDS = 15 * 60
ONBOARDING_TTL_SECONDS = 15 * 60
MAX_ONBOARDING_ATTEMPTS = 5
MAX_WEIXIN_USERS = 32
DEFAULT_MAX_ACTIVE_IDENTITIES = 5
USER_ROLES = frozenset({"owner", "member"})
USER_STATES = frozenset({"active", "suspended", "revoked"})
IDENTITY_STATES = frozenset({"active", "pending_pairing", "paused", "session_expired", "revoked"})
IDENTITY_RUNTIME_STATES = frozenset(
    {"disabled", "stopped", "starting", "pairing", "polling", "session_expired", "token_conflict", "error"}
)
MEDIA_ARCHIVE_PENDING_TTL_SECONDS = 15 * 60
MEDIA_ARCHIVE_MAX_ATTACHMENTS = 16
MEDIA_ARCHIVE_ARCHIVE_ACTION_RE = re.compile(r"(?:归档|存档|归入|收录)")
MEDIA_ARCHIVE_TARGETED_ACTION_RE = re.compile(r"(?:保存到|加入|添加到|记录到)")
MEDIA_ARCHIVE_ACTION_RE = re.compile(r"(?:归档|存档|归入|收录|保存到|加入|添加到|记录到)")
MEDIA_ARCHIVE_CANCEL_RE = re.compile(r"(?:取消|停止|结束|不要|不用|不再|别).{0,8}(?:归档|存档|归入|收录|保存|加入|添加|记录)")
MEDIA_ARCHIVE_FUTURE_RE = re.compile(r"(?:接下来|随后|稍后|后面|待会|下一张|下几张|我会发|将要发)")
MEDIA_ARCHIVE_BACKWARD_RE = re.compile(r"(?:刚才|刚刚|上面|前面|之前|这些|这几|全部|都|一共|共)")
MEDIA_ARCHIVE_MEDIA_RE = re.compile(r"(?:图片|照片|视频|媒体|文件)")
MEDIA_ARCHIVE_TARGET_RE = re.compile(r"(?:装修|施工|工地|现场|水电|泥木|瓦工|木工|油漆).{0,16}(?:档案|记录|媒体库|资料库)")
MEDIA_ARCHIVE_COUNT_RE = re.compile(r"(?:(\d{1,2})|([一二两三四五六七八九十]{1,3}))(?=张|个|幅|段|份)")
MEDIA_ARCHIVE_CONTEXT_STATUSES = frozenset(
    {
        "authorized",
        "intent_registered",
        "awaiting_more_attachments",
        "cancelled",
        "nothing_to_cancel",
        "no_recent_attachments",
        "attachment_count_mismatch",
        "too_many_attachments",
    }
)


def _chinese_media_count(value: str) -> int | None:
    digits = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if value == "十":
        return 10
    if "十" in value:
        left, right = value.split("十", 1)
        tens = digits.get(left, 1) if left else 1
        ones = digits.get(right, 0) if right else 0
        result = tens * 10 + ones
        return result if 1 <= result <= MEDIA_ARCHIVE_MAX_ATTACHMENTS else None
    return digits.get(value)


def media_archive_instruction(text: str, *, has_attachments: bool) -> dict[str, Any] | None:
    """Recognize only explicit bounded cross-message archive controls."""

    if not isinstance(text, str) or not text.strip():
        return None
    normalized = re.sub(r"\s+", "", text)
    if MEDIA_ARCHIVE_CANCEL_RE.search(normalized):
        return {"kind": "cancel", "expected_count": None}
    has_target = MEDIA_ARCHIVE_TARGET_RE.search(normalized) is not None
    if not MEDIA_ARCHIVE_ARCHIVE_ACTION_RE.search(normalized) and not (
        MEDIA_ARCHIVE_TARGETED_ACTION_RE.search(normalized) and has_target
    ):
        return None
    count_match = MEDIA_ARCHIVE_COUNT_RE.search(normalized)
    expected_count: int | None = None
    if count_match is not None:
        expected_count = int(count_match.group(1)) if count_match.group(1) else _chinese_media_count(count_match.group(2))
        if expected_count is None or not 1 <= expected_count <= MEDIA_ARCHIVE_MAX_ATTACHMENTS:
            return None
    has_media = MEDIA_ARCHIVE_MEDIA_RE.search(normalized) is not None
    if not has_attachments and MEDIA_ARCHIVE_FUTURE_RE.search(normalized) and (has_media or has_target):
        return {"kind": "intent_first", "expected_count": expected_count}
    if (
        not has_attachments
        and (has_media or expected_count is not None)
        and (expected_count is not None or MEDIA_ARCHIVE_BACKWARD_RE.search(normalized))
    ):
        return {"kind": "image_first", "expected_count": expected_count}
    return None


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


def principal_conversation_key(principal_id: str) -> str:
    return "sha256:" + hashlib.sha256(f"weixin-principal-v2:{principal_id}".encode("utf-8")).hexdigest()


def identity_id(account_id: str) -> str:
    return "ID-" + account_hash(account_id)[:32]


def routed_message_id(identity_identifier: str, upstream_message_id: str) -> str:
    digest = hashlib.sha256(f"{identity_identifier}\0{upstream_message_id}".encode("utf-8")).hexdigest()
    return f"wx2-{digest}"


def new_principal_id() -> str:
    return "PR-" + secrets.token_hex(16)


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

    def save_identity(self, identity: dict[str, Any], *, make_active: bool = True) -> dict[str, Any]:
        normalized = self.validate_identity(identity)
        digest = account_hash(normalized["account_id"])
        atomic_json_write(self.accounts_dir / f"{digest}.json", normalized)
        if make_active:
            atomic_json_write(self.active_path, {"account_hash": digest, "updated_at": utc_now()})
        return self.public_summary(normalized)

    def set_active_identity(self, identity: dict[str, Any]) -> None:
        normalized = self.validate_identity(identity)
        digest = account_hash(normalized["account_id"])
        path = self.accounts_dir / f"{digest}.json"
        if path.is_symlink() or not path.is_file():
            raise StoreError("identity_missing", "iLink 身份文件不存在", status=404)
        atomic_json_write(self.active_path, {"account_hash": digest, "updated_at": utc_now()})

    def active_account_hash(self) -> str | None:
        if not self.active_path.is_file() or self.active_path.is_symlink():
            return None
        try:
            document = json.loads(self.active_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        digest = document.get("account_hash") if isinstance(document, dict) else None
        return digest if isinstance(digest, str) and len(digest) == 64 else None

    def load_identity_by_hash(self, digest: str) -> dict[str, Any] | None:
        if not isinstance(digest, str) or len(digest) != 64:
            return None
        path = self.accounts_dir / f"{digest}.json"
        if path.is_symlink() or not path.is_file():
            return None
        try:
            return self.validate_identity(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, StoreError):
            return None

    def load_all_identities(self) -> list[dict[str, Any]]:
        identities: list[dict[str, Any]] = []
        for path in sorted(self.accounts_dir.glob("*.json")):
            if path.name == self.active_path.name or path.is_symlink() or not path.is_file():
                continue
            if not re.fullmatch(r"[a-f0-9]{64}\.json", path.name):
                continue
            loaded = self.load_identity_by_hash(path.stem)
            if loaded is not None:
                identities.append(loaded)
        return identities

    def remove_identity(self, identity: dict[str, Any]) -> None:
        """Delete a non-owner credential file after a terminal revoke or failed pairing."""
        normalized = self.validate_identity(identity)
        digest = account_hash(normalized["account_id"])
        if hmac.compare_digest(self.active_account_hash() or "", digest):
            raise StoreError("owner_identity_delete_forbidden", "不能删除当前 Owner ClawBot 凭据", status=409)
        path = self.accounts_dir / f"{digest}.json"
        if path.is_symlink():
            raise StoreError("identity_invalid", "身份文件不能是符号链接", status=500)
        if path.is_file():
            path.unlink()

    def recent_tokens(self, limit: int = 10) -> list[str]:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 10:
            raise StoreError("identity_token_limit_invalid", "本地 Token 列表上限无效")
        identities = self.load_all_identities()
        identities.sort(key=lambda item: str(item.get("saved_at") or ""))
        tokens: list[str] = []
        for identity in reversed(identities):
            token = str(identity.get("token") or "").strip()
            if token and token not in tokens:
                tokens.append(token)
            if len(tokens) >= limit:
                break
        return tokens

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
        expected_identity_id = identity_id(account_id)
        provided_identity_id = identity.get("identity_id")
        if provided_identity_id not in {None, "", expected_identity_id}:
            raise StoreError("identity_invalid", "identity_id 与 account_id 不匹配")
        return {
            "format_id": IDENTITY_FORMAT,
            "identity_id": expected_identity_id,
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
        self.save_identity(updated, make_active=self.active_account_hash() == account_hash(updated["account_id"]))
        identity["get_updates_buf"] = cursor

    def context(self, identity: dict[str, Any], user_id: str) -> str | None:
        return identity.get("context_tokens", {}).get(user_id)

    def set_context(self, identity: dict[str, Any], user_id: str, token: str) -> None:
        updated = dict(identity)
        contexts = dict(updated.get("context_tokens", {}))
        contexts[user_id] = token
        updated["context_tokens"] = contexts
        self.save_identity(updated, make_active=self.active_account_hash() == account_hash(updated["account_id"]))
        identity["context_tokens"] = contexts

    def clear_context(self, identity: dict[str, Any], user_id: str) -> None:
        updated = dict(identity)
        contexts = dict(updated.get("context_tokens", {}))
        contexts.pop(user_id, None)
        updated["context_tokens"] = contexts
        self.save_identity(updated, make_active=self.active_account_hash() == account_hash(updated["account_id"]))
        identity["context_tokens"] = contexts

    def mirror_owner(self, identity: dict[str, Any], user_id: str) -> None:
        """Keep the legacy allowlist as a one-owner compatibility mirror."""
        updated = dict(identity)
        updated["allowed_user_ids"] = [user_id]
        self.save_identity(updated, make_active=True)
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

    def import_migration(
        self,
        package_path: str | Path,
        key_b64: str,
        *,
        make_active: bool = True,
        expected_account_id: str | None = None,
    ) -> dict[str, Any]:
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
        normalized = self.validate_identity(identity)
        if expected_account_id is not None and not hmac.compare_digest(
            normalized["account_id"], expected_account_id
        ):
            raise StoreError(
                "owner_identity_mismatch",
                "迁移包只允许刷新当前 Owner ClawBot；新成员请使用成员接入流程。",
                status=409,
            )
        summary = self.save_identity(normalized, make_active=make_active)
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
        self.outbound_dir = self.data_dir / "spool" / "outbound"
        self.spool_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.outbound_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
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
                CREATE TABLE IF NOT EXISTS media_archive_requests (
                    request_id TEXT PRIMARY KEY,
                    identity_scope TEXT NOT NULL,
                    principal_scope TEXT NOT NULL,
                    conversation_key TEXT NOT NULL,
                    source TEXT NOT NULL CHECK(source IN ('image_first','intent_first')),
                    state TEXT NOT NULL CHECK(state IN ('pending','bound','completed','failed','cancelled','expired')),
                    intent_message_id TEXT NOT NULL,
                    intent_text TEXT NOT NULL,
                    expected_count INTEGER CHECK(expected_count BETWEEN 1 AND 16 OR expected_count IS NULL),
                    bound_message_id TEXT,
                    idempotency_scope TEXT,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS media_archive_request_scope_idx
                    ON media_archive_requests(identity_scope,principal_scope,conversation_key,state,created_at);
                CREATE UNIQUE INDEX IF NOT EXISTS media_archive_request_pending_idx
                    ON media_archive_requests(identity_scope,principal_scope,conversation_key)
                    WHERE state='pending';
                CREATE TABLE IF NOT EXISTS media_archive_request_attachments (
                    request_id TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    attachment_ref TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    PRIMARY KEY(request_id,position),
                    FOREIGN KEY(request_id) REFERENCES media_archive_requests(request_id)
                );
                CREATE INDEX IF NOT EXISTS media_archive_request_attachment_ref_idx
                    ON media_archive_request_attachments(attachment_ref);
                CREATE TABLE IF NOT EXISTS outbound_chunks (
                    job_id TEXT NOT NULL,
                    chunk_index INTEGER NOT NULL,
                    client_id TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL CHECK(state IN ('pending','sent','failed')),
                    sent_at TEXT,
                    error_code TEXT,
                    PRIMARY KEY(job_id,chunk_index)
                );
                CREATE TABLE IF NOT EXISTS outbound_artifacts (
                    job_id TEXT NOT NULL,
                    artifact_id TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    client_id TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL CHECK(state IN ('pending','sent','failed')),
                    sent_at TEXT,
                    error_code TEXT,
                    fallback_client_id TEXT NOT NULL UNIQUE,
                    fallback_state TEXT NOT NULL CHECK(fallback_state IN ('pending','sent','failed')),
                    fallback_sent_at TEXT,
                    fallback_error_code TEXT,
                    PRIMARY KEY(job_id,artifact_id)
                );
                CREATE TABLE IF NOT EXISTS gateway_meta (
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    users_revision INTEGER NOT NULL,
                    poller_override TEXT CHECK(poller_override IN ('enabled','disabled') OR poller_override IS NULL),
                    poller_revision INTEGER NOT NULL DEFAULT 0,
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
                CREATE TABLE IF NOT EXISTS ilink_identities (
                    identity_id TEXT PRIMARY KEY,
                    account_hash TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL CHECK(state IN ('active','pending_pairing','paused','session_expired','revoked')),
                    runtime_state TEXT NOT NULL DEFAULT 'stopped',
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_seen_at TEXT
                );
                CREATE INDEX IF NOT EXISTS ilink_identities_state_idx
                    ON ilink_identities(state, updated_at);
                CREATE TABLE IF NOT EXISTS identity_bindings (
                    identity_id TEXT NOT NULL,
                    principal_id TEXT NOT NULL UNIQUE,
                    private_user_id TEXT NOT NULL,
                    binding_type TEXT NOT NULL CHECK(binding_type IN ('primary','legacy_shared')),
                    state TEXT NOT NULL CHECK(state IN ('active','suspended','revoked')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(identity_id,principal_id),
                    FOREIGN KEY(identity_id) REFERENCES ilink_identities(identity_id)
                );
                CREATE INDEX IF NOT EXISTS identity_bindings_route_idx
                    ON identity_bindings(identity_id,private_user_id,state);
                CREATE TABLE IF NOT EXISTS onboarding_sessions (
                    session_id TEXT PRIMARY KEY,
                    target_principal_id TEXT,
                    requested_alias TEXT NOT NULL,
                    code_salt TEXT NOT NULL,
                    code_hash TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN (
                        'waiting_qr','pending_pairing','claimed','expired','cancelled','failed','already_bound'
                    )),
                    identity_id TEXT,
                    account_hash TEXT,
                    scanned_private_user_id TEXT,
                    qr_state TEXT,
                    last_error TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS onboarding_sessions_state_idx
                    ON onboarding_sessions(state, expires_at);
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
                CREATE TABLE IF NOT EXISTS remote_work_tasks (
                    task_id TEXT PRIMARY KEY,
                    source_message_id TEXT NOT NULL UNIQUE,
                    sender_id TEXT NOT NULL,
                    user_hash TEXT NOT NULL,
                    project_alias TEXT NOT NULL,
                    instruction_digest TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN (
                        'waiting_mac','queued','running','awaiting_confirmation','completed',
                        'failed','cancelled','expired','recovery_required'
                    )),
                    run_seq INTEGER NOT NULL DEFAULT 0,
                    sequence INTEGER NOT NULL DEFAULT 0,
                    stage TEXT,
                    last_status_json TEXT,
                    last_result_json TEXT,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(user_hash) REFERENCES weixin_users(user_hash)
                );
                CREATE INDEX IF NOT EXISTS remote_work_tasks_state_idx
                    ON remote_work_tasks(state, updated_at);
                CREATE TABLE IF NOT EXISTS remote_work_outbox (
                    message_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    topic TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_digest TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('pending','published','failed')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    published_at TEXT,
                    FOREIGN KEY(task_id) REFERENCES remote_work_tasks(task_id)
                );
                CREATE INDEX IF NOT EXISTS remote_work_outbox_state_idx
                    ON remote_work_outbox(state, created_at);
                CREATE TABLE IF NOT EXISTS remote_work_events (
                    event_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    topic TEXT NOT NULL,
                    run_seq INTEGER NOT NULL,
                    sequence INTEGER NOT NULL,
                    payload_digest TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    reply_state TEXT NOT NULL CHECK(reply_state IN ('none','pending','sent','suppressed')),
                    reply_error TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(task_id,topic,run_seq,sequence),
                    FOREIGN KEY(task_id) REFERENCES remote_work_tasks(task_id)
                );
                CREATE INDEX IF NOT EXISTS remote_work_events_reply_idx
                    ON remote_work_events(reply_state, created_at);
                CREATE TABLE IF NOT EXISTS remote_work_agent (
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    online INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    payload_digest TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO gateway_meta(id,users_revision,updated_at) VALUES (1,0,?)",
                (utc_now(),),
            )
            self._ensure_column(connection, "gateway_meta", "poller_override", "TEXT")
            self._ensure_column(connection, "gateway_meta", "poller_revision", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(connection, "inbound_messages", "user_hash", "TEXT")
            self._ensure_column(
                connection,
                "inbound_messages",
                "capability_profile",
                "TEXT NOT NULL DEFAULT 'owner_legacy'",
            )
            self._ensure_column(connection, "weixin_users", "principal_id", "TEXT")
            self._ensure_column(connection, "inbound_messages", "identity_id", "TEXT")
            self._ensure_column(connection, "inbound_messages", "principal_id", "TEXT")
            self._ensure_column(connection, "inbound_messages", "upstream_message_id", "TEXT")
            self._ensure_column(
                connection,
                "inbound_messages",
                "media_archive_context_json",
                "TEXT NOT NULL DEFAULT '{}'",
            )
            self._ensure_column(connection, "remote_work_tasks", "identity_id", "TEXT")
            self._ensure_column(connection, "remote_work_tasks", "principal_id", "TEXT")
            self._ensure_column(connection, "onboarding_sessions", "qr_state", "TEXT")
            self._ensure_column(connection, "onboarding_sessions", "last_error", "TEXT")
            self._backfill_principal_ids(connection)
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS weixin_users_principal_idx ON weixin_users(principal_id)"
            )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS inbound_identity_message_idx "
                "ON inbound_messages(identity_id,upstream_message_id) "
                "WHERE identity_id IS NOT NULL AND upstream_message_id IS NOT NULL"
            )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS identity_bindings_sender_idx "
                "ON identity_bindings(identity_id,private_user_id)"
            )

    @staticmethod
    def _ensure_column(connection: sqlite3.Connection, table: str, column: str, declaration: str) -> None:
        columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    @staticmethod
    def _backfill_principal_ids(connection: sqlite3.Connection) -> None:
        rows = connection.execute(
            "SELECT user_hash FROM weixin_users WHERE principal_id IS NULL OR principal_id=''"
        ).fetchall()
        for row in rows:
            principal_id_value = "PR-" + hashlib.sha256(
                f"weixin-principal-legacy:{row['user_hash']}".encode("utf-8")
            ).hexdigest()[:32]
            connection.execute(
                "UPDATE weixin_users SET principal_id=? WHERE user_hash=?",
                (principal_id_value, row["user_hash"]),
            )

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

    def poller_control(self) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT poller_override,poller_revision FROM gateway_meta WHERE id=1"
            ).fetchone()
            if row is None:
                raise StoreError("gateway_meta_missing", "Gateway 控制状态不存在", status=500)
            override = row["poller_override"]
            if override not in {None, "enabled", "disabled"}:
                raise StoreError("poller_override_invalid", "Poller 持久化覆盖状态无效", status=500)
            return {"override": override, "revision": int(row["poller_revision"])}

    def set_poller_enabled(
        self,
        enabled: bool,
        *,
        expected_revision: int,
        request_id: str,
    ) -> dict[str, Any]:
        if not isinstance(enabled, bool):
            raise StoreError("poller_enabled_invalid", "Poller 开关值无效")
        payload = {"enabled": enabled, "revision": expected_revision}
        scope = "poller_control"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            replay = self._idempotent_response(
                connection,
                request_id=request_id,
                scope=scope,
                payload=payload,
            )
            if replay is not None:
                replay["replayed"] = True
                return replay
            row = connection.execute(
                "SELECT poller_revision FROM gateway_meta WHERE id=1"
            ).fetchone()
            if row is None:
                raise StoreError("gateway_meta_missing", "Gateway 控制状态不存在", status=500)
            current_revision = int(row["poller_revision"])
            if (
                not isinstance(expected_revision, int)
                or isinstance(expected_revision, bool)
                or expected_revision != current_revision
            ):
                raise StoreError("poller_revision_conflict", "Poller 状态已变化，请刷新页面后重试", status=409)
            revision = current_revision + 1
            response = {
                "enabled": enabled,
                "override": "enabled" if enabled else "disabled",
                "revision": revision,
            }
            connection.execute(
                "UPDATE gateway_meta SET poller_override=?,poller_revision=?,updated_at=? WHERE id=1",
                (response["override"], revision, utc_now()),
            )
            self._record_mutation(
                connection,
                request_id=request_id,
                scope=scope,
                payload=payload,
                response=response,
            )
            return response

    @staticmethod
    def validate_request_id(request_id: str) -> None:
        if not isinstance(request_id, str) or not 16 <= len(request_id) <= 128 or any(
            character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
            for character in request_id
        ):
            raise StoreError("request_id_invalid", "request_id 无效")

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
        self.validate_request_id(request_id)
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
            principal_id_value = "PR-" + hashlib.sha256(
                f"weixin-principal-legacy:{digest}".encode("utf-8")
            ).hexdigest()[:32]
            connection.execute(
                "INSERT INTO weixin_users(user_hash,private_user_id,conversation_key,alias,role,status,revision,created_at,updated_at,last_seen_at,principal_id) VALUES (?,?,?,?,?,'active',?,?,?,NULL,?)",
                (digest, owner_id, key, "管理员", "owner", revision, now, now, principal_id_value),
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

    @staticmethod
    def _validate_identity_reference(identity_identifier: str, account_digest: str) -> None:
        if not isinstance(account_digest, str) or not re.fullmatch(r"[a-f0-9]{64}", account_digest):
            raise StoreError("identity_invalid", "account_hash 无效")
        if (
            not isinstance(identity_identifier, str)
            or not re.fullmatch(r"ID-[a-f0-9]{32}", identity_identifier)
            or identity_identifier != f"ID-{account_digest[:32]}"
        ):
            raise StoreError("identity_invalid", "identity_id 无效")

    def migrate_legacy_identity(self, *, identity_identifier: str, account_digest: str) -> dict[str, Any]:
        """Register the current active identity without changing existing principals or conversations."""
        self._validate_identity_reference(identity_identifier, account_digest)
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            duplicate = connection.execute(
                "SELECT identity_id FROM ilink_identities WHERE account_hash=?",
                (account_digest,),
            ).fetchone()
            if duplicate is not None and duplicate["identity_id"] != identity_identifier:
                raise StoreError("identity_already_bound", "该 ClawBot 身份已经接入", status=409)
            connection.execute(
                """
                INSERT INTO ilink_identities(identity_id,account_hash,state,runtime_state,created_at,updated_at)
                VALUES (?,?,'active','stopped',?,?)
                ON CONFLICT(identity_id) DO UPDATE SET
                    account_hash=excluded.account_hash,
                    state=CASE WHEN ilink_identities.state='revoked' THEN ilink_identities.state ELSE 'active' END,
                    updated_at=excluded.updated_at
                """,
                (identity_identifier, account_digest, now, now),
            )
            users = connection.execute("SELECT * FROM weixin_users ORDER BY created_at").fetchall()
            for row in users:
                binding_type = "primary" if row["role"] == "owner" else "legacy_shared"
                connection.execute(
                    """
                    INSERT INTO identity_bindings(
                        identity_id,principal_id,private_user_id,binding_type,state,created_at,updated_at
                    ) VALUES (?,?,?,?,?,?,?)
                    ON CONFLICT(principal_id) DO UPDATE SET
                        identity_id=CASE
                            WHEN identity_bindings.binding_type='primary' THEN identity_bindings.identity_id
                            ELSE excluded.identity_id
                        END,
                        private_user_id=CASE
                            WHEN identity_bindings.binding_type='primary' THEN identity_bindings.private_user_id
                            ELSE excluded.private_user_id
                        END,
                        binding_type=CASE
                            WHEN identity_bindings.binding_type='primary' THEN identity_bindings.binding_type
                            ELSE excluded.binding_type
                        END,
                        state=CASE
                            WHEN identity_bindings.state='revoked' THEN identity_bindings.state
                            WHEN identity_bindings.binding_type='primary' THEN identity_bindings.state
                            ELSE excluded.state
                        END,
                        updated_at=excluded.updated_at
                    """,
                    (
                        identity_identifier,
                        row["principal_id"],
                        row["private_user_id"],
                        binding_type,
                        "active" if row["status"] == "active" else row["status"],
                        now,
                        now,
                    ),
                )
                connection.execute(
                    """
                    UPDATE inbound_messages
                    SET identity_id=COALESCE(identity_id,?),
                        principal_id=COALESCE(principal_id,?),
                        upstream_message_id=COALESCE(upstream_message_id,message_id)
                    WHERE sender_id=?
                    """,
                    (identity_identifier, row["principal_id"], row["private_user_id"]),
                )
            return {
                "identity_id": identity_identifier,
                "account_hash": account_digest,
                "bound_principals": len(users),
            }

    def register_pending_identity(self, *, identity_identifier: str, account_digest: str) -> dict[str, Any]:
        self._validate_identity_reference(identity_identifier, account_digest)
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM ilink_identities WHERE account_hash=?",
                (account_digest,),
            ).fetchone()
            if existing is not None and existing["identity_id"] != identity_identifier and existing["state"] != "revoked":
                raise StoreError("identity_already_bound", "该 ClawBot 身份已经接入", status=409)
            connection.execute(
                """
                INSERT INTO ilink_identities(identity_id,account_hash,state,runtime_state,created_at,updated_at)
                VALUES (?,?,'pending_pairing','stopped',?,?)
                ON CONFLICT(identity_id) DO UPDATE SET
                    account_hash=excluded.account_hash,state='pending_pairing',last_error=NULL,updated_at=excluded.updated_at
                """,
                (identity_identifier, account_digest, now, now),
            )
        return self.identity_record(identity_identifier)

    def identity_record(self, identity_identifier: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM ilink_identities WHERE identity_id=?",
                (identity_identifier,),
            ).fetchone()
            if row is None:
                raise StoreError("identity_not_found", "ClawBot 身份不存在", status=404)
            return dict(row)

    def identity_records(self, *, include_revoked: bool = False) -> list[dict[str, Any]]:
        with self._connect() as connection:
            where = "" if include_revoked else "WHERE state!='revoked'"
            rows = connection.execute(
                f"SELECT * FROM ilink_identities {where} ORDER BY created_at"
            ).fetchall()
            return [dict(row) for row in rows]

    def set_identity_runtime_state(
        self,
        identity_identifier: str,
        runtime_state: str,
        *,
        error_code: str | None = None,
        identity_state: str | None = None,
    ) -> None:
        if runtime_state not in IDENTITY_RUNTIME_STATES:
            raise StoreError("identity_runtime_state_invalid", "ClawBot 运行状态无效")
        if identity_state is not None and identity_state not in IDENTITY_STATES:
            raise StoreError("identity_state_invalid", "ClawBot 身份状态无效")
        with self._connect() as connection:
            changed = connection.execute(
                """
                UPDATE ilink_identities
                SET runtime_state=?,last_error=?,state=COALESCE(?,state),updated_at=?,
                    last_seen_at=CASE WHEN ?='polling' THEN ? ELSE last_seen_at END
                WHERE identity_id=?
                """,
                (runtime_state, error_code, identity_state, utc_now(), runtime_state, utc_now(), identity_identifier),
            ).rowcount
            if changed != 1:
                raise StoreError("identity_not_found", "ClawBot 身份不存在", status=404)

    def user_by_identity_sender(self, identity_identifier: str, private_user_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT u.* FROM identity_bindings b
                JOIN weixin_users u ON u.principal_id=b.principal_id
                WHERE b.identity_id=? AND b.private_user_id=? AND b.state='active'
                """,
                (identity_identifier, private_user_id),
            ).fetchone()
            return None if row is None else self._private_user_document(row)

    def identity_route_for_principal(
        self,
        principal_id_value: str,
        *,
        include_inactive: bool = False,
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            binding_filter = "" if include_inactive else "AND b.state='active'"
            row = connection.execute(
                f"""
                SELECT i.*,b.private_user_id,b.binding_type,b.state AS binding_state
                FROM identity_bindings b JOIN ilink_identities i ON i.identity_id=b.identity_id
                WHERE b.principal_id=? {binding_filter}
                """,
                (principal_id_value,),
            ).fetchone()
            return None if row is None else dict(row)

    def owner_identity_route(self) -> dict[str, Any]:
        owner = self.active_owner()
        route = self.identity_route_for_principal(owner["principal_id"])
        if route is None or route["binding_state"] != "active" or route["state"] == "revoked":
            raise StoreError("notification_owner_identity_unavailable", "当前 Owner 的 ClawBot 身份不可用", status=409)
        return {**route, "principal": owner}

    def list_identities(self) -> dict[str, Any]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT i.*,u.user_hash,u.alias,u.role,u.status,b.binding_type,b.private_user_id
                FROM ilink_identities i
                LEFT JOIN identity_bindings b ON b.identity_id=i.identity_id AND b.state!='revoked'
                LEFT JOIN weixin_users u ON u.principal_id=b.principal_id
                WHERE i.state!='revoked'
                ORDER BY CASE WHEN u.role='owner' THEN 0 ELSE 1 END,i.created_at
                """
            ).fetchall()
            identities: dict[str, dict[str, Any]] = {}
            for row in rows:
                document = identities.setdefault(
                    row["identity_id"],
                    {
                        "identity_short": self.short_id("CB", row["identity_id"]),
                        "state": row["state"],
                        "runtime_state": row["runtime_state"],
                        "last_error": row["last_error"],
                        "last_seen_at": row["last_seen_at"],
                        "bindings": [],
                    },
                )
                if row["user_hash"] is not None:
                    document["bindings"].append(
                        {
                            "wx_short": self.short_id("WX", row["user_hash"]),
                            "alias": row["alias"],
                            "role": row["role"],
                            "status": row["status"],
                            "binding_type": row["binding_type"],
                        }
                    )
            return {
                "identities": list(identities.values()),
                "limits": {
                    "max_users": MAX_WEIXIN_USERS,
                    "max_active_identities": DEFAULT_MAX_ACTIVE_IDENTITIES,
                },
            }

    def create_onboarding_session(
        self,
        *,
        expected_revision: int,
        request_id: str,
        alias: str,
        target_wx_short: str | None = None,
        ttl_seconds: int = ONBOARDING_TTL_SECONDS,
        max_active_identities: int = DEFAULT_MAX_ACTIVE_IDENTITIES,
    ) -> dict[str, Any]:
        clean_alias = alias.strip()
        if not 1 <= len(clean_alias) <= 40 or any(ord(character) < 32 for character in clean_alias):
            raise StoreError("alias_invalid", "别名长度必须为 1 到 40 个可见字符")
        if not isinstance(ttl_seconds, int) or isinstance(ttl_seconds, bool) or not 60 <= ttl_seconds <= 3600:
            raise StoreError("onboarding_ttl_invalid", "成员接入有效期必须在 60 到 3600 秒之间")
        if not isinstance(max_active_identities, int) or not 1 <= max_active_identities <= MAX_WEIXIN_USERS:
            raise StoreError("identity_limit_invalid", "活动 ClawBot 上限无效")
        scope = "onboarding_session_create"
        payload = {
            "revision": expected_revision,
            "alias": clean_alias,
            "target_wx_short": target_wx_short,
            "ttl_seconds": ttl_seconds,
        }
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            replay = self._idempotent_response(connection, request_id=request_id, scope=scope, payload=payload)
            if replay is not None:
                return replay
            self._assert_revision(connection, expected_revision)
            self._expire_onboarding_sessions(connection)
            owner_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM weixin_users WHERE role='owner' AND status='active'"
                ).fetchone()[0]
            )
            if owner_count != 1:
                raise StoreError("owner_required", "添加成员前必须存在唯一 active Owner", status=409)
            pending = int(
                connection.execute(
                    "SELECT COUNT(*) FROM onboarding_sessions WHERE state IN ('waiting_qr','pending_pairing')"
                ).fetchone()[0]
            )
            if pending:
                raise StoreError("onboarding_in_progress", "已有成员接入会话正在进行", status=409)
            identity_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM ilink_identities WHERE state!='revoked'"
                ).fetchone()[0]
            )
            if identity_count >= max_active_identities:
                raise StoreError("identity_limit_reached", "活动 ClawBot 数量已达到上限", status=409)
            target_principal_id = None
            if target_wx_short:
                target = self._user_row_by_short(connection, target_wx_short)
                if target["role"] != "member" or target["status"] != "active":
                    raise StoreError("onboarding_target_invalid", "目标必须是 active Member", status=409)
                bound = connection.execute(
                    "SELECT binding_type FROM identity_bindings WHERE principal_id=? AND state='active'",
                    (target["principal_id"],),
                ).fetchone()
                if bound is not None and bound["binding_type"] != "legacy_shared":
                    raise StoreError("identity_already_bound", "该成员已经绑定 ClawBot", status=409)
                target_principal_id = target["principal_id"]
                clean_alias = target["alias"]
            elif int(connection.execute("SELECT COUNT(*) FROM weixin_users WHERE status!='revoked'").fetchone()[0]) >= MAX_WEIXIN_USERS:
                raise StoreError("user_limit_reached", "微信用户数量已达到上限", status=409)
            now_dt = datetime.now(timezone.utc)
            now = now_dt.isoformat()
            code = "接入-CODEX-" + secrets.token_hex(16).upper()
            salt = secrets.token_bytes(16)
            session_id = secrets.token_urlsafe(24)
            expires_at = (now_dt + timedelta(seconds=ttl_seconds)).isoformat()
            connection.execute(
                """
                INSERT INTO onboarding_sessions(
                    session_id,target_principal_id,requested_alias,code_salt,code_hash,state,
                    qr_state,expires_at,created_at,updated_at
                ) VALUES (?,?,?,?,?,'waiting_qr','waiting',?,?,?)
                """,
                (
                    session_id,
                    target_principal_id,
                    clean_alias,
                    base64.urlsafe_b64encode(salt).decode("ascii"),
                    hashlib.sha256(salt + code.encode("utf-8")).hexdigest(),
                    expires_at,
                    now,
                    now,
                ),
            )
            revision = self._next_users_revision(connection)
            response = {
                "state": "created_code_already_shown",
                "session_short": self.short_id("OB", session_id),
                "expires_at": expires_at,
                "revision": revision,
            }
            self._record_mutation(connection, request_id=request_id, scope=scope, payload=payload, response=response)
            return {**response, "state": "waiting_qr", "code": code, "session_id": session_id}

    def onboarding_session(self, session_short: str) -> dict[str, Any]:
        with self._connect() as connection:
            self._expire_onboarding_sessions(connection)
            return dict(self._onboarding_by_short(connection, session_short))

    def onboarding_session_by_id(self, session_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            self._expire_onboarding_sessions(connection)
            row = connection.execute(
                "SELECT * FROM onboarding_sessions WHERE session_id=?",
                (session_id,),
            ).fetchone()
            return None if row is None else dict(row)

    def pending_onboarding_for_identity(self, identity_identifier: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            self._expire_onboarding_sessions(connection)
            row = connection.execute(
                "SELECT * FROM onboarding_sessions WHERE identity_id=? AND state='pending_pairing'",
                (identity_identifier,),
            ).fetchone()
            return None if row is None else dict(row)

    def set_onboarding_qr_state(
        self,
        *,
        session_id: str,
        qr_state: str,
        error_code: str | None = None,
        terminal_state: str | None = None,
    ) -> None:
        allowed_qr_states = {
            "waiting",
            "scanned",
            "need_verifycode",
            "verify_code_blocked",
            "redirecting",
            "confirmed",
            "expired",
            "cancelled",
            "failed",
            "already_bound",
        }
        if qr_state not in allowed_qr_states:
            raise StoreError("onboarding_qr_state_invalid", "二维码状态无效")
        if terminal_state is not None and terminal_state not in {
            "expired",
            "cancelled",
            "failed",
            "already_bound",
        }:
            raise StoreError("onboarding_state_invalid", "成员接入终态无效")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state FROM onboarding_sessions WHERE session_id=?",
                (session_id,),
            ).fetchone()
            if row is None:
                raise StoreError("onboarding_not_found", "成员接入会话不存在", status=404)
            if row["state"] not in {"waiting_qr", "pending_pairing"}:
                return
            next_state = terminal_state or row["state"]
            now = utc_now()
            connection.execute(
                "UPDATE onboarding_sessions SET state=?,qr_state=?,last_error=?,updated_at=? WHERE session_id=?",
                (next_state, qr_state, error_code, now, session_id),
            )
            if terminal_state is not None:
                connection.execute(
                    "UPDATE ilink_identities SET state='revoked',runtime_state='stopped',last_error=?,updated_at=? "
                    "WHERE identity_id=(SELECT identity_id FROM onboarding_sessions WHERE session_id=?) "
                    "AND state='pending_pairing'",
                    (error_code, now, session_id),
                )

    def attach_onboarding_identity(
        self,
        *,
        session_id: str,
        identity_identifier: str,
        account_digest: str,
        scanned_private_user_id: str,
    ) -> dict[str, Any]:
        if not scanned_private_user_id:
            raise StoreError("onboarding_sender_missing", "扫码用户身份缺失", status=409)
        self._validate_identity_reference(identity_identifier, account_digest)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._expire_onboarding_sessions(connection)
            row = connection.execute(
                "SELECT * FROM onboarding_sessions WHERE session_id=?",
                (session_id,),
            ).fetchone()
            if row is None or row["state"] != "waiting_qr":
                raise StoreError("onboarding_not_waiting", "成员接入会话已不可使用", status=409)
            duplicate = connection.execute(
                "SELECT principal_id,binding_type FROM identity_bindings WHERE private_user_id=? AND state='active'",
                (scanned_private_user_id,),
            ).fetchone()
            duplicate_is_target_legacy = bool(
                duplicate is not None
                and row["target_principal_id"]
                and duplicate["principal_id"] == row["target_principal_id"]
                and duplicate["binding_type"] == "legacy_shared"
            )
            if duplicate is not None and not duplicate_is_target_legacy:
                raise StoreError("identity_already_bound", "该微信用户已经绑定 ClawBot", status=409)
            existing_identity = connection.execute(
                "SELECT * FROM ilink_identities WHERE account_hash=? AND state!='revoked'",
                (account_digest,),
            ).fetchone()
            if existing_identity is not None and existing_identity["identity_id"] != identity_identifier:
                raise StoreError("identity_already_bound", "该 ClawBot 身份已经接入", status=409)
            now = utc_now()
            connection.execute(
                """
                INSERT INTO ilink_identities(identity_id,account_hash,state,runtime_state,created_at,updated_at)
                VALUES (?,?,'pending_pairing','stopped',?,?)
                ON CONFLICT(identity_id) DO UPDATE SET
                    account_hash=excluded.account_hash,state='pending_pairing',last_error=NULL,updated_at=excluded.updated_at
                """,
                (identity_identifier, account_digest, now, now),
            )
            connection.execute(
                """
                UPDATE onboarding_sessions
                SET state='pending_pairing',identity_id=?,account_hash=?,scanned_private_user_id=?,
                    qr_state='confirmed',last_error=NULL,updated_at=?
                WHERE session_id=? AND state='waiting_qr'
                """,
                (identity_identifier, account_digest, scanned_private_user_id, now, session_id),
            )
            return {
                "state": "pending_pairing",
                "session_short": self.short_id("OB", session_id),
                "identity_short": self.short_id("CB", identity_identifier),
                "expires_at": row["expires_at"],
            }

    def claim_onboarding(
        self,
        *,
        identity_identifier: str,
        private_user_id: str,
        text: str,
    ) -> dict[str, Any] | None:
        if not private_user_id or not text:
            return None
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._expire_onboarding_sessions(connection)
            session = connection.execute(
                "SELECT * FROM onboarding_sessions WHERE identity_id=? AND state='pending_pairing'",
                (identity_identifier,),
            ).fetchone()
            if session is None:
                return None
            if not hmac.compare_digest(str(session["scanned_private_user_id"]), private_user_id):
                return None
            try:
                salt = base64.urlsafe_b64decode(session["code_salt"].encode("ascii"))
            except Exception:
                connection.execute(
                    "UPDATE onboarding_sessions SET state='failed',updated_at=? WHERE session_id=?",
                    (now, session["session_id"]),
                )
                return None
            actual = hashlib.sha256(salt + text.strip().encode("utf-8")).hexdigest()
            if not hmac.compare_digest(actual, session["code_hash"]):
                attempts = int(session["attempts"]) + 1
                state = "failed" if attempts >= MAX_ONBOARDING_ATTEMPTS else "pending_pairing"
                connection.execute(
                    "UPDATE onboarding_sessions SET attempts=?,state=?,updated_at=? WHERE session_id=?",
                    (attempts, state, now, session["session_id"]),
                )
                if state == "failed":
                    connection.execute(
                        "UPDATE ilink_identities SET state='revoked',runtime_state='error',last_error='pairing_attempts_exceeded',updated_at=? "
                        "WHERE identity_id=? AND state='pending_pairing'",
                        (now, identity_identifier),
                    )
                return None
            principal_id_value = session["target_principal_id"]
            if principal_id_value:
                user = connection.execute(
                    "SELECT * FROM weixin_users WHERE principal_id=?",
                    (principal_id_value,),
                ).fetchone()
                if user is None or user["role"] != "member" or user["status"] != "active":
                    raise StoreError("onboarding_target_invalid", "目标成员已不可绑定", status=409)
                duplicate = connection.execute(
                    "SELECT binding_type FROM identity_bindings WHERE principal_id=? AND state='active'",
                    (principal_id_value,),
                ).fetchone()
                if duplicate is not None and duplicate["binding_type"] != "legacy_shared":
                    raise StoreError("identity_already_bound", "该成员已经绑定 ClawBot", status=409)
            else:
                existing = connection.execute(
                    "SELECT * FROM weixin_users WHERE private_user_id=? AND status!='revoked'",
                    (private_user_id,),
                ).fetchone()
                if existing is not None:
                    raise StoreError("identity_already_bound", "该微信用户已经接入", status=409)
                count = int(
                    connection.execute("SELECT COUNT(*) FROM weixin_users WHERE status!='revoked'").fetchone()[0]
                )
                if count >= MAX_WEIXIN_USERS:
                    raise StoreError("user_limit_reached", "微信用户数量已达到上限", status=409)
                principal_id_value = new_principal_id()
                digest = user_hash(private_user_id)
                key = principal_conversation_key(principal_id_value)
                revision = self._next_users_revision(connection)
                connection.execute(
                    """
                    INSERT INTO weixin_users(
                        user_hash,private_user_id,conversation_key,alias,role,status,revision,
                        created_at,updated_at,last_seen_at,principal_id
                    ) VALUES (?,?,?,?,'member','active',?,?,?,?,?)
                    """,
                    (
                        digest,
                        private_user_id,
                        key,
                        session["requested_alias"],
                        revision,
                        now,
                        now,
                        now,
                        principal_id_value,
                    ),
                )
                connection.execute(
                    "INSERT INTO conversation_links(user_hash,conversation_short,last_seen_at) VALUES (?,?,?)",
                    (digest, self.short_id("CV", key), now),
                )
                user = connection.execute(
                    "SELECT * FROM weixin_users WHERE principal_id=?",
                    (principal_id_value,),
                ).fetchone()
            assert user is not None
            existing_binding = connection.execute(
                "SELECT binding_type FROM identity_bindings WHERE principal_id=?",
                (principal_id_value,),
            ).fetchone()
            if existing_binding is not None:
                if existing_binding["binding_type"] != "legacy_shared":
                    raise StoreError("identity_already_bound", "该成员已经绑定 ClawBot", status=409)
                connection.execute(
                    """
                    UPDATE identity_bindings
                    SET identity_id=?,private_user_id=?,binding_type='primary',state='active',updated_at=?
                    WHERE principal_id=? AND binding_type='legacy_shared'
                    """,
                    (identity_identifier, private_user_id, now, principal_id_value),
                )
            else:
                connection.execute(
                    """
                    INSERT INTO identity_bindings(
                        identity_id,principal_id,private_user_id,binding_type,state,created_at,updated_at
                    ) VALUES (?,?,?,'primary','active',?,?)
                    """,
                    (identity_identifier, principal_id_value, private_user_id, now, now),
                )
            connection.execute(
                "UPDATE ilink_identities SET state='active',runtime_state='polling',last_error=NULL,updated_at=? WHERE identity_id=?",
                (now, identity_identifier),
            )
            connection.execute(
                "UPDATE onboarding_sessions SET state='claimed',updated_at=? WHERE session_id=? AND state='pending_pairing'",
                (now, session["session_id"]),
            )
            if session["target_principal_id"]:
                revision = self._next_users_revision(connection)
                connection.execute(
                    "UPDATE weixin_users SET revision=?,updated_at=?,last_seen_at=? WHERE principal_id=?",
                    (revision, now, now, principal_id_value),
                )
                user = connection.execute(
                    "SELECT * FROM weixin_users WHERE principal_id=?",
                    (principal_id_value,),
                ).fetchone()
            result = self._private_user_document(user)
            result["identity_id"] = identity_identifier
            result["session_short"] = self.short_id("OB", session["session_id"])
            return result

    def cancel_onboarding_session(
        self,
        *,
        session_short: str,
        expected_revision: int,
        request_id: str,
    ) -> dict[str, Any]:
        scope = "onboarding_session_cancel"
        payload = {"session_short": session_short, "revision": expected_revision}
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            replay = self._idempotent_response(connection, request_id=request_id, scope=scope, payload=payload)
            if replay is not None:
                return replay
            self._assert_revision(connection, expected_revision)
            session = self._onboarding_by_short(connection, session_short)
            if session["state"] not in {"waiting_qr", "pending_pairing"}:
                raise StoreError("onboarding_not_waiting", "成员接入会话已不可取消", status=409)
            now = utc_now()
            connection.execute(
                "UPDATE onboarding_sessions SET state='cancelled',updated_at=? WHERE session_id=?",
                (now, session["session_id"]),
            )
            if session["identity_id"]:
                connection.execute(
                    "UPDATE ilink_identities SET state='revoked',runtime_state='stopped',updated_at=? WHERE identity_id=? AND state='pending_pairing'",
                    (now, session["identity_id"]),
                )
            revision = self._next_users_revision(connection)
            response = {"state": "cancelled", "session_short": session_short, "revision": revision}
            self._record_mutation(connection, request_id=request_id, scope=scope, payload=payload, response=response)
            return response

    def onboarding_summary(self) -> dict[str, Any]:
        with self._connect() as connection:
            self._expire_onboarding_sessions(connection)
            counts = {
                row["state"]: int(row["count"])
                for row in connection.execute(
                    "SELECT state,COUNT(*) AS count FROM onboarding_sessions GROUP BY state"
                )
            }
            current = connection.execute(
                """
                SELECT * FROM onboarding_sessions
                WHERE state IN ('waiting_qr','pending_pairing')
                ORDER BY created_at LIMIT 1
                """
            ).fetchone()
            return {
                "counts": {
                    state: counts.get(state, 0)
                    for state in (
                        "waiting_qr",
                        "pending_pairing",
                        "claimed",
                        "expired",
                        "cancelled",
                        "failed",
                        "already_bound",
                    )
                },
                "current": None
                if current is None
                else {
                    "session_short": self.short_id("OB", current["session_id"]),
                    "state": current["state"],
                    "qr_state": current["qr_state"],
                    "last_error": current["last_error"],
                    "expires_at": current["expires_at"],
                },
            }

    def expire_onboarding_sessions(self) -> list[str]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            now = utc_now()
            rows = connection.execute(
                "SELECT identity_id FROM onboarding_sessions "
                "WHERE state IN ('waiting_qr','pending_pairing') AND expires_at<=? AND identity_id IS NOT NULL",
                (now,),
            ).fetchall()
            self._expire_onboarding_sessions(connection)
            return [str(row["identity_id"]) for row in rows]

    @staticmethod
    def _expire_onboarding_sessions(connection: sqlite3.Connection) -> int:
        now = utc_now()
        rows = connection.execute(
            "SELECT identity_id FROM onboarding_sessions WHERE state IN ('waiting_qr','pending_pairing') AND expires_at<=?",
            (now,),
        ).fetchall()
        changed = connection.execute(
            "UPDATE onboarding_sessions SET state='expired',updated_at=? WHERE state IN ('waiting_qr','pending_pairing') AND expires_at<=?",
            (now, now),
        ).rowcount
        for row in rows:
            if row["identity_id"]:
                connection.execute(
                    "UPDATE ilink_identities SET state='revoked',runtime_state='stopped',updated_at=? WHERE identity_id=? AND state='pending_pairing'",
                    (now, row["identity_id"]),
                )
        return int(changed)

    def _onboarding_by_short(self, connection: sqlite3.Connection, session_short: str) -> sqlite3.Row:
        if not isinstance(session_short, str) or not re_fullmatch_short("OB", session_short):
            raise StoreError("onboarding_not_found", "成员接入会话不存在", status=404)
        for row in connection.execute("SELECT * FROM onboarding_sessions"):
            if hmac.compare_digest(self.short_id("OB", row["session_id"]), session_short):
                return row
        raise StoreError("onboarding_not_found", "成员接入会话不存在", status=404)

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
            principal_id_value = new_principal_id()
            connection.execute(
                "INSERT INTO weixin_users(user_hash,private_user_id,conversation_key,alias,role,status,revision,created_at,updated_at,last_seen_at,principal_id) VALUES (?,?,?,?,?,'active',?,?,?,?,?)",
                (digest, user_id, key, alias, "member", revision, now, now, now, principal_id_value),
            )
            connection.execute(
                "INSERT INTO conversation_links(user_hash,conversation_short,last_seen_at) VALUES (?,?,?)",
                (digest, self.short_id("CV", key), now),
            )
            shared_identity = connection.execute(
                """
                SELECT b.identity_id FROM identity_bindings b
                JOIN weixin_users u ON u.principal_id=b.principal_id
                JOIN ilink_identities i ON i.identity_id=b.identity_id
                WHERE u.role='owner' AND u.status='active' AND b.state='active' AND i.state='active'
                """
            ).fetchone()
            if shared_identity is not None:
                connection.execute(
                    """
                    INSERT INTO identity_bindings(
                        identity_id,principal_id,private_user_id,binding_type,state,created_at,updated_at
                    ) VALUES (?,?,?,'legacy_shared','active',?,?)
                    """,
                    (shared_identity["identity_id"], principal_id_value, user_id, now, now),
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
                self._set_principal_binding_state(connection, row["principal_id"], "suspended", now)
            elif action == "resume":
                if row["role"] != "member" or row["status"] != "suspended":
                    raise StoreError("user_state_conflict", "只有 suspended 成员可以恢复", status=409)
                connection.execute(
                    "UPDATE weixin_users SET status='active',updated_at=? WHERE user_hash=?",
                    (now, row["user_hash"]),
                )
                self._set_principal_binding_state(connection, row["principal_id"], "active", now)
            else:
                if row["status"] == "revoked":
                    raise StoreError("user_state_conflict", "成员已经移除", status=409)
                connection.execute(
                    "UPDATE weixin_users SET status='revoked',revoked_at=?,updated_at=? WHERE user_hash=?",
                    (now, now, row["user_hash"]),
                )
                self._set_principal_binding_state(connection, row["principal_id"], "revoked", now)
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

    @staticmethod
    def _set_principal_binding_state(
        connection: sqlite3.Connection,
        principal_id_value: str,
        state: str,
        now: str,
    ) -> None:
        binding = connection.execute(
            "SELECT identity_id,binding_type FROM identity_bindings WHERE principal_id=?",
            (principal_id_value,),
        ).fetchone()
        if binding is None:
            return
        connection.execute(
            "UPDATE identity_bindings SET state=?,updated_at=? WHERE principal_id=?",
            (state, now, principal_id_value),
        )
        if binding["binding_type"] != "primary":
            return
        identity_state = {"active": "active", "suspended": "paused", "revoked": "revoked"}[state]
        connection.execute(
            "UPDATE ilink_identities SET state=?,runtime_state='stopped',updated_at=? WHERE identity_id=?",
            (identity_state, now, binding["identity_id"]),
        )

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
                "owner_principal_id": target["principal_id"],
                "previous_owner_principal_id": old_owner["principal_id"],
            }
            stored_response = {
                key: value
                for key, value in response.items()
                if not key.endswith("private_id") and not key.endswith("principal_id")
            }
            self._record_mutation(connection, request_id=request_id, scope=scope, payload=payload, response=stored_response)
            return response

    def restore_owner_after_mirror_failure(
        self,
        previous_owner_principal_id: str,
        target_principal_id: str,
        request_id: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE weixin_users SET role='member' WHERE principal_id=?",
                (target_principal_id,),
            )
            connection.execute(
                "UPDATE weixin_users SET role='owner' WHERE principal_id=?",
                (previous_owner_principal_id,),
            )
            revision = self._next_users_revision(connection)
            connection.execute(
                "UPDATE weixin_users SET revision=?,updated_at=? WHERE principal_id IN (?,?)",
                (revision, utc_now(), previous_owner_principal_id, target_principal_id),
            )
            connection.execute("DELETE FROM admin_mutations WHERE request_id=?", (request_id,))

    def touch_user(
        self,
        private_user_id: str,
        *,
        identity_identifier: str | None = None,
    ) -> dict[str, Any] | None:
        now = utc_now()
        with self._connect() as connection:
            if identity_identifier is None:
                digest = user_hash(private_user_id)
                row = connection.execute("SELECT * FROM weixin_users WHERE user_hash=?", (digest,)).fetchone()
            else:
                row = connection.execute(
                    """
                    SELECT u.* FROM identity_bindings b
                    JOIN weixin_users u ON u.principal_id=b.principal_id
                    WHERE b.identity_id=? AND b.private_user_id=? AND b.state='active'
                    """,
                    (identity_identifier, private_user_id),
                ).fetchone()
            if row is None:
                return None
            digest = row["user_hash"]
            connection.execute(
                "UPDATE weixin_users SET last_seen_at=?,updated_at=? WHERE user_hash=?",
                (now, now, digest),
            )
            connection.execute(
                "UPDATE conversation_links SET last_seen_at=? WHERE user_hash=?",
                (now, digest),
            )
            if identity_identifier is not None:
                connection.execute(
                    "UPDATE identity_bindings SET updated_at=? WHERE identity_id=? AND principal_id=?",
                    (now, identity_identifier, row["principal_id"]),
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
        """Legacy destructive replacement is forbidden after multi-identity migration."""
        raise StoreError(
            "identity_replacement_forbidden",
            "不能通过替换 ClawBot 身份清空用户目录；请使用成员接入或同账号重新认证。",
            status=409,
        )

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
        binding = connection.execute(
            """
            SELECT b.binding_type,b.state AS binding_state,i.identity_id,
                   i.state AS identity_state,i.runtime_state
            FROM identity_bindings b JOIN ilink_identities i ON i.identity_id=b.identity_id
            WHERE b.principal_id=?
            """,
            (row["principal_id"],),
        ).fetchone()
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
            "identity_short": None if binding is None else self.short_id("CB", binding["identity_id"]),
            "identity_state": None if binding is None else binding["identity_state"],
            "identity_runtime_state": None if binding is None else binding["runtime_state"],
            "binding_type": None if binding is None else binding["binding_type"],
            "binding_state": None if binding is None else binding["binding_state"],
        }

    @staticmethod
    def _private_user_document(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "principal_id": row["principal_id"],
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

    def enqueue_remote_work_command(
        self,
        *,
        topic: str,
        payload: dict[str, Any],
        sender_id: str,
        user_digest: str,
        identity_identifier: str | None = None,
        principal_id_value: str | None = None,
    ) -> dict[str, Any]:
        """Persist one request/control before MQTT publication."""
        if topic not in {"home/codex-work/v1/request", "home/codex-work/v1/control"}:
            raise StoreError("remote_work_topic_invalid", "Remote Work 出站主题无效")
        identifier_name = "message_id" if topic.endswith("/request") else "control_id"
        identifier = payload.get(identifier_name)
        task_id = payload.get("task_id")
        if not isinstance(identifier, str) or not isinstance(task_id, str):
            raise StoreError("remote_work_payload_invalid", "Remote Work 出站标识无效")
        payload_json = canonical_json(payload)
        payload_digest = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT payload_digest FROM remote_work_outbox WHERE message_id=?",
                (identifier,),
            ).fetchone()
            if existing is not None:
                if hmac.compare_digest(existing["payload_digest"], payload_digest):
                    task = connection.execute("SELECT * FROM remote_work_tasks WHERE task_id=?", (task_id,)).fetchone()
                    assert task is not None
                    result = self._remote_work_task_document(task)
                    result["duplicate"] = True
                    return result
                raise StoreError("remote_work_idempotency_conflict", "Remote Work 消息 ID 正文冲突", status=409)

            task = connection.execute("SELECT * FROM remote_work_tasks WHERE task_id=?", (task_id,)).fetchone()
            if topic.endswith("/request"):
                if task is not None:
                    raise StoreError("remote_work_task_conflict", "Remote Work task 已存在", status=409)
                project_alias = str(payload.get("project_alias") or "")
                instruction = str(payload.get("instruction") or "")
                instruction_digest = hashlib.sha256(instruction.encode("utf-8")).hexdigest()
                connection.execute(
                    """
                    INSERT INTO remote_work_tasks(
                        task_id,source_message_id,sender_id,user_hash,project_alias,instruction_digest,
                        state,created_at,expires_at,updated_at,identity_id,principal_id
                    ) VALUES (?,?,?,?,?,?,'waiting_mac',?,?,?,?,?)
                    """,
                    (
                        task_id,
                        identifier,
                        sender_id,
                        user_digest,
                        project_alias,
                        instruction_digest,
                        str(payload["created_at"]),
                        str(payload["expires_at"]),
                        now,
                        identity_identifier,
                        principal_id_value,
                    ),
                )
            else:
                if task is None:
                    raise StoreError("remote_work_task_not_found", "Remote Work task 不存在", status=404)
                if not hmac.compare_digest(str(task["user_hash"]), user_digest):
                    raise StoreError("remote_work_task_not_found", "Remote Work task 不存在", status=404)
                if identity_identifier is not None and task["identity_id"] != identity_identifier:
                    raise StoreError("remote_work_task_not_found", "Remote Work task 不存在", status=404)
                if principal_id_value is not None and task["principal_id"] != principal_id_value:
                    raise StoreError("remote_work_task_not_found", "Remote Work task 不存在", status=404)
            connection.execute(
                """
                INSERT INTO remote_work_outbox(
                    message_id,task_id,topic,payload_json,payload_digest,state,created_at
                ) VALUES (?,?,?,?,?,'pending',?)
                """,
                (identifier, task_id, topic, payload_json, payload_digest, now),
            )
            task = connection.execute("SELECT * FROM remote_work_tasks WHERE task_id=?", (task_id,)).fetchone()
            assert task is not None
            result = self._remote_work_task_document(task)
            result["duplicate"] = False
            return result

    def remote_work_command_replay(
        self,
        message_id: str,
        *,
        task_id: str,
        operation: str,
        project_alias: str | None,
        instruction: str | None,
        user_digest: str,
    ) -> dict[str, Any] | None:
        """Return an exact semantic replay without regenerating time-dependent fields."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json,task_id FROM remote_work_outbox WHERE message_id=?",
                (message_id,),
            ).fetchone()
            if row is None:
                return None
            payload = json.loads(row["payload_json"])
            actual_operation = payload.get("operation", payload.get("action"))
            semantic_match = (
                row["task_id"] == task_id
                and actual_operation == operation
                and payload.get("project_alias") == project_alias
                and payload.get("instruction") == instruction
            )
            task = connection.execute("SELECT * FROM remote_work_tasks WHERE task_id=?", (task_id,)).fetchone()
            if task is None or not hmac.compare_digest(str(task["user_hash"]), user_digest):
                raise StoreError("remote_work_task_not_found", "Remote Work task 不存在", status=404)
            if not semantic_match:
                raise StoreError("remote_work_idempotency_conflict", "Remote Work 消息 ID 正文冲突", status=409)
            result = self._remote_work_task_document(task)
            result["duplicate"] = True
            return result

    def remote_work_task(self, task_id: str, *, user_digest: str | None = None) -> dict[str, Any]:
        with self._connect() as connection:
            self._expire_remote_work_tasks(connection)
            row = connection.execute("SELECT * FROM remote_work_tasks WHERE task_id=?", (task_id,)).fetchone()
            if row is None or (user_digest is not None and not hmac.compare_digest(str(row["user_hash"]), user_digest)):
                raise StoreError("remote_work_task_not_found", "Remote Work task 不存在", status=404)
            return self._remote_work_task_document(row)

    def remote_work_pending_outbox(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as connection:
            self._expire_remote_work_tasks(connection)
            rows = connection.execute(
                """
                SELECT o.* FROM remote_work_outbox o
                JOIN remote_work_tasks t ON t.task_id=o.task_id
                WHERE o.state IN ('pending','failed') AND t.state!='expired'
                ORDER BY o.created_at LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]

    def mark_remote_work_outbox(self, message_id: str, *, success: bool, error_code: str | None = None) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE remote_work_outbox
                SET state=?,attempts=attempts+1,last_error=?,published_at=?
                WHERE message_id=?
                """,
                ("published" if success else "failed", error_code, utc_now() if success else None, message_id),
            )

    def record_remote_work_event(self, topic: str, payload: dict[str, Any]) -> dict[str, Any]:
        payload_json = canonical_json(payload)
        payload_digest = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        if topic == "home/codex-work/v1/agent":
            with self._connect() as connection:
                existing = connection.execute("SELECT payload_digest FROM remote_work_agent WHERE id=1").fetchone()
                if existing is not None and hmac.compare_digest(existing["payload_digest"], payload_digest):
                    return {"outcome": "duplicate"}
                connection.execute(
                    """
                    INSERT INTO remote_work_agent(id,online,updated_at,payload_digest,payload_json)
                    VALUES (1,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET
                        online=excluded.online,updated_at=excluded.updated_at,
                        payload_digest=excluded.payload_digest,payload_json=excluded.payload_json
                    """,
                    (1 if payload["online"] else 0, str(payload["updated_at"]), payload_digest, payload_json),
                )
            return {"outcome": "recorded", "agent": True}
        if topic not in {"home/codex-work/v1/status", "home/codex-work/v1/result"}:
            raise StoreError("remote_work_topic_invalid", "Remote Work 入站主题无效")

        task_id = str(payload["task_id"])
        run_seq = int(payload["run_seq"])
        sequence = int(payload["sequence"])
        event_id = hashlib.sha256(f"{topic}:{task_id}:{run_seq}:{sequence}".encode("utf-8")).hexdigest()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task = connection.execute("SELECT * FROM remote_work_tasks WHERE task_id=?", (task_id,)).fetchone()
            if task is None:
                raise StoreError("remote_work_task_unknown", "Remote Work 事件 task 未知", status=404)
            existing = connection.execute(
                "SELECT payload_digest FROM remote_work_events WHERE task_id=? AND topic=? AND run_seq=? AND sequence=?",
                (task_id, topic, run_seq, sequence),
            ).fetchone()
            if existing is not None:
                if hmac.compare_digest(existing["payload_digest"], payload_digest):
                    return {"outcome": "duplicate", "task_id": task_id}
                raise StoreError("remote_work_event_conflict", "Remote Work 事件序号正文冲突", status=409)
            current_pair = (int(task["run_seq"]), int(task["sequence"]))
            incoming_pair = (run_seq, sequence)
            if incoming_pair < current_pair:
                return {"outcome": "stale", "task_id": task_id}
            if incoming_pair == current_pair and current_pair != (0, 0):
                return {"outcome": "stale", "task_id": task_id}
            state = str(payload["state"])
            if str(task["state"]) in {"completed", "failed", "cancelled", "expired", "recovery_required"} and run_seq <= int(task["run_seq"]):
                raise StoreError("remote_work_state_conflict", "Remote Work 终态不能回退", status=409)
            reply_state = "pending" if topic.endswith("/result") else "none"
            connection.execute(
                """
                INSERT INTO remote_work_events(
                    event_id,task_id,topic,run_seq,sequence,payload_digest,payload_json,reply_state,created_at
                ) VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (event_id, task_id, topic, run_seq, sequence, payload_digest, payload_json, reply_state, utc_now()),
            )
            connection.execute(
                """
                UPDATE remote_work_tasks SET state=?,run_seq=?,sequence=?,stage=?,
                    last_status_json=CASE WHEN ? LIKE '%/status' THEN ? ELSE last_status_json END,
                    last_result_json=CASE WHEN ? LIKE '%/result' THEN ? ELSE last_result_json END,
                    updated_at=? WHERE task_id=?
                """,
                (
                    state,
                    run_seq,
                    sequence,
                    payload.get("stage"),
                    topic,
                    payload_json,
                    topic,
                    payload_json,
                    utc_now(),
                    task_id,
                ),
            )
        return {"outcome": "recorded", "task_id": task_id, "reply_pending": reply_state == "pending"}

    def remote_work_pending_replies(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT e.event_id,e.payload_json,t.task_id,t.sender_id,t.user_hash,
                       t.identity_id,t.principal_id
                FROM remote_work_events e JOIN remote_work_tasks t ON t.task_id=e.task_id
                WHERE e.reply_state='pending' ORDER BY e.created_at LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [
                {
                    "event_id": row["event_id"],
                    "payload": json.loads(row["payload_json"]),
                    "task_id": row["task_id"],
                    "sender_id": row["sender_id"],
                    "user_hash": row["user_hash"],
                    "identity_id": row["identity_id"],
                    "principal_id": row["principal_id"],
                }
                for row in rows
            ]

    def mark_remote_work_reply(self, event_id: str, *, sent: bool, error_code: str | None = None) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE remote_work_events SET reply_state=?,reply_error=? WHERE event_id=? AND reply_state='pending'",
                ("sent" if sent else "suppressed", error_code, event_id),
            )

    def remote_work_status(self) -> dict[str, Any]:
        with self._connect() as connection:
            self._expire_remote_work_tasks(connection)
            counts = {
                row["state"]: int(row["count"])
                for row in connection.execute("SELECT state,COUNT(*) AS count FROM remote_work_tasks GROUP BY state")
            }
            agent = connection.execute("SELECT * FROM remote_work_agent WHERE id=1").fetchone()
            return {
                "tasks": counts,
                "pending_outbox": int(
                    connection.execute("SELECT COUNT(*) FROM remote_work_outbox WHERE state IN ('pending','failed')").fetchone()[0]
                ),
                "pending_replies": int(
                    connection.execute("SELECT COUNT(*) FROM remote_work_events WHERE reply_state='pending'").fetchone()[0]
                ),
                "agent": None if agent is None else json.loads(agent["payload_json"]),
            }

    def expire_remote_work_tasks(self) -> int:
        with self._connect() as connection:
            return self._expire_remote_work_tasks(connection)

    @staticmethod
    def _expire_remote_work_tasks(connection: sqlite3.Connection) -> int:
        cursor = connection.execute(
            """
            UPDATE remote_work_tasks SET state='expired',updated_at=?
            WHERE state IN ('waiting_mac','queued') AND expires_at<=?
            """,
            (utc_now(), utc_now()),
        )
        return int(cursor.rowcount)

    @staticmethod
    def _remote_work_task_document(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "task_id": row["task_id"],
            "identity_id": row["identity_id"],
            "principal_id": row["principal_id"],
            "project_alias": row["project_alias"],
            "state": row["state"],
            "run_seq": int(row["run_seq"]),
            "sequence": int(row["sequence"]),
            "stage": row["stage"],
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
            "updated_at": row["updated_at"],
            "last_status": None if row["last_status_json"] is None else json.loads(row["last_status_json"]),
            "last_result": None if row["last_result_json"] is None else json.loads(row["last_result_json"]),
        }

    @staticmethod
    def _media_archive_scope(
        *,
        sender_id: str,
        user_digest: str | None,
        identity_identifier: str | None,
        principal_id_value: str | None,
    ) -> tuple[str, str]:
        identity_scope = identity_identifier or "legacy"
        principal_scope = principal_id_value or user_digest
        if not principal_scope:
            principal_scope = "sha256:" + hashlib.sha256(f"legacy-media:{sender_id}".encode("utf-8")).hexdigest()
        return identity_scope, principal_scope

    @staticmethod
    def _media_archive_request_id(
        identity_scope: str,
        principal_scope: str,
        conversation_key_value: str,
        intent_message_id: str,
    ) -> str:
        digest = hashlib.sha256(
            f"{identity_scope}\0{principal_scope}\0{conversation_key_value}\0{intent_message_id}".encode("utf-8")
        ).hexdigest()
        return f"MAR-{digest}"

    @staticmethod
    def _media_archive_idempotency_scope(
        intent_message_id: str,
        source_attachments: list[dict[str, str]],
    ) -> str:
        canonical = canonical_json(
            {
                "intent_message_id": intent_message_id,
                "source_attachments": source_attachments,
            }
        )
        return "sha256:" + hashlib.sha256(f"weixin-media-archive-v1\n{canonical}".encode("utf-8")).hexdigest()

    @staticmethod
    def _media_archive_context(
        *,
        source: str,
        status: str,
        intent_message_id: str,
        intent_text: str,
        expected_count: int | None,
        source_attachments: list[dict[str, str]],
        idempotency_scope: str | None = None,
    ) -> dict[str, Any]:
        if status not in MEDIA_ARCHIVE_CONTEXT_STATUSES:
            raise RuntimeError("媒体归档上下文状态无效")
        authorized = status == "authorized"
        context: dict[str, Any] = {
            "version": 1,
            "source": source,
            "status": status,
            "authorized": authorized,
            "intent_message_id": intent_message_id,
            "intent_text": intent_text,
            "expected_count": expected_count,
            "selected_count": len(source_attachments),
            "source_attachments": source_attachments,
        }
        if authorized:
            if not idempotency_scope:
                raise RuntimeError("媒体归档授权缺少幂等作用域")
            context["idempotency_scope"] = idempotency_scope
        return context

    @staticmethod
    def _attachment_document_from_row(row: sqlite3.Row) -> dict[str, Any]:
        mime_type = str(row["mime_type"])
        if mime_type.startswith("image/"):
            media_type = "image"
        elif mime_type.startswith("video/"):
            media_type = "video"
        elif mime_type.startswith("audio/"):
            media_type = "audio"
        else:
            media_type = "file"
        return {
            "attachment_ref": row["attachment_ref"],
            "media_type": media_type,
            "size_bytes": row["size_bytes"],
            "sha256": f"sha256:{row['sha256']}",
            "display_name": row["original_filename"],
        }

    @staticmethod
    def _source_attachment_from_row(row: sqlite3.Row) -> dict[str, str]:
        digest = str(row["sha256"])
        return {
            "message_id": str(row["message_id"]),
            "sha256": digest if digest.startswith("sha256:") else f"sha256:{digest}",
        }

    def _expire_media_archive_requests(self, connection: sqlite3.Connection, now: str) -> None:
        connection.execute(
            "UPDATE media_archive_requests SET state='expired',updated_at=? WHERE state='pending' AND expires_at<=?",
            (now, now),
        )

    @staticmethod
    def _pending_media_archive_request(
        connection: sqlite3.Connection,
        identity_scope: str,
        principal_scope: str,
        conversation_key_value: str,
    ) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM media_archive_requests "
            "WHERE identity_scope=? AND principal_scope=? AND conversation_key=? AND state='pending' "
            "ORDER BY created_at DESC LIMIT 1",
            (identity_scope, principal_scope, conversation_key_value),
        ).fetchone()

    @staticmethod
    def _media_archive_attachment_rows(
        connection: sqlite3.Connection,
        request_id: str,
    ) -> list[sqlite3.Row]:
        return connection.execute(
            "SELECT m.position,m.message_id,m.sha256,a.attachment_ref,a.original_filename,a.mime_type,a.size_bytes "
            "FROM media_archive_request_attachments m JOIN attachments a ON a.attachment_ref=m.attachment_ref "
            "WHERE m.request_id=? ORDER BY m.position",
            (request_id,),
        ).fetchall()

    @staticmethod
    def _reserve_media_archive_attachments(
        connection: sqlite3.Connection,
        request_id: str,
        rows: list[sqlite3.Row],
        *,
        start_position: int = 0,
    ) -> None:
        for offset, row in enumerate(rows):
            connection.execute(
                "INSERT INTO media_archive_request_attachments(request_id,position,attachment_ref,message_id,sha256) "
                "VALUES (?,?,?,?,?)",
                (
                    request_id,
                    start_position + offset,
                    row["attachment_ref"],
                    row["message_id"],
                    f"sha256:{row['sha256']}",
                ),
            )

    def _available_message_attachments(
        self,
        connection: sqlite3.Connection,
        message_id: str,
        now: str,
    ) -> list[sqlite3.Row]:
        return connection.execute(
            "SELECT a.* FROM attachments a "
            "WHERE a.message_id=? AND a.consumed_at IS NULL AND a.expires_at>? "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM media_archive_request_attachments m "
            "  JOIN media_archive_requests r ON r.request_id=m.request_id "
            "  WHERE m.attachment_ref=a.attachment_ref AND r.state IN ('pending','bound','completed')"
            ") ORDER BY a.rowid",
            (message_id, now),
        ).fetchall()

    def _recent_media_archive_batch(
        self,
        connection: sqlite3.Connection,
        *,
        current_message_id: str,
        identity_scope: str,
        principal_scope: str,
        conversation_key_value: str,
        expected_count: int | None,
        now: str,
    ) -> list[sqlite3.Row]:
        current = connection.execute(
            "SELECT rowid FROM inbound_messages WHERE message_id=?",
            (current_message_id,),
        ).fetchone()
        if current is None:
            return []
        cutoff = (parse_time(now) - timedelta(seconds=MEDIA_ARCHIVE_PENDING_TTL_SECONDS)).isoformat()
        messages = connection.execute(
            "SELECT rowid,* FROM inbound_messages WHERE conversation_key=? AND rowid<? AND received_at>=? "
            "ORDER BY rowid DESC LIMIT 64",
            (conversation_key_value, current["rowid"], cutoff),
        ).fetchall()
        groups: list[list[sqlite3.Row]] = []
        selected_count = 0
        for message in messages:
            row_identity, row_principal = self._media_archive_scope(
                sender_id=str(message["sender_id"]),
                user_digest=message["user_hash"],
                identity_identifier=message["identity_id"],
                principal_id_value=message["principal_id"],
            )
            if row_identity != identity_scope or row_principal != principal_scope:
                continue
            available = self._available_message_attachments(connection, str(message["message_id"]), now)
            if available:
                groups.append(available)
                selected_count += len(available)
                if expected_count is not None and selected_count >= expected_count:
                    break
                if expected_count is None and selected_count > MEDIA_ARCHIVE_MAX_ATTACHMENTS:
                    break
                continue
            total_attachments = connection.execute(
                "SELECT COUNT(*) FROM attachments WHERE message_id=?",
                (message["message_id"],),
            ).fetchone()[0]
            if total_attachments:
                break
            if str(message["text"] or "").strip():
                request = connection.execute(
                    "SELECT state FROM media_archive_requests WHERE bound_message_id=? ORDER BY created_at DESC LIMIT 1",
                    (message["message_id"],),
                ).fetchone()
                if request is not None and request["state"] == "failed":
                    continue
                break
        return [row for group in reversed(groups) for row in group]

    def _correlate_media_archive(
        self,
        connection: sqlite3.Connection,
        *,
        message_id: str,
        sender_id: str,
        conversation_key_value: str,
        text: str,
        attachment_documents: list[dict[str, Any]],
        user_digest: str | None,
        identity_identifier: str | None,
        principal_id_value: str | None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        now = utc_now()
        self._expire_media_archive_requests(connection, now)
        identity_scope, principal_scope = self._media_archive_scope(
            sender_id=sender_id,
            user_digest=user_digest,
            identity_identifier=identity_identifier,
            principal_id_value=principal_id_value,
        )
        instruction = media_archive_instruction(text, has_attachments=bool(attachment_documents))
        pending = self._pending_media_archive_request(
            connection,
            identity_scope,
            principal_scope,
            conversation_key_value,
        )

        if instruction is not None and instruction["kind"] == "cancel":
            if pending is None:
                return attachment_documents, self._media_archive_context(
                    source="intent_first",
                    status="nothing_to_cancel",
                    intent_message_id=message_id,
                    intent_text=text,
                    expected_count=None,
                    source_attachments=[],
                )
            connection.execute(
                "UPDATE media_archive_requests SET state='cancelled',updated_at=? WHERE request_id=?",
                (now, pending["request_id"]),
            )
            rows = self._media_archive_attachment_rows(connection, str(pending["request_id"]))
            return attachment_documents, self._media_archive_context(
                source=str(pending["source"]),
                status="cancelled",
                intent_message_id=str(pending["intent_message_id"]),
                intent_text=str(pending["intent_text"]),
                expected_count=pending["expected_count"],
                source_attachments=[self._source_attachment_from_row(row) for row in rows],
            )

        if attachment_documents and pending is not None:
            if MEDIA_ARCHIVE_ACTION_RE.search(re.sub(r"\s+", "", text or "")):
                connection.execute(
                    "UPDATE media_archive_requests SET state='cancelled',updated_at=? WHERE request_id=?",
                    (now, pending["request_id"]),
                )
                return attachment_documents, {}
            current_rows = connection.execute(
                "SELECT * FROM attachments WHERE message_id=? ORDER BY rowid",
                (message_id,),
            ).fetchall()
            existing_rows = self._media_archive_attachment_rows(connection, str(pending["request_id"]))
            would_count = len(existing_rows) + len(current_rows)
            expected_count = pending["expected_count"]
            if would_count > MEDIA_ARCHIVE_MAX_ATTACHMENTS or (
                expected_count is not None and would_count > expected_count
            ):
                connection.execute(
                    "UPDATE media_archive_requests SET state='failed',updated_at=? WHERE request_id=?",
                    (now, pending["request_id"]),
                )
                combined = [self._source_attachment_from_row(row) for row in existing_rows + current_rows]
                return attachment_documents, self._media_archive_context(
                    source="intent_first",
                    status="too_many_attachments"
                    if would_count > MEDIA_ARCHIVE_MAX_ATTACHMENTS
                    else "attachment_count_mismatch",
                    intent_message_id=str(pending["intent_message_id"]),
                    intent_text=str(pending["intent_text"]),
                    expected_count=expected_count,
                    source_attachments=combined,
                )
            self._reserve_media_archive_attachments(
                connection,
                str(pending["request_id"]),
                current_rows,
                start_position=len(existing_rows),
            )
            rows = self._media_archive_attachment_rows(connection, str(pending["request_id"]))
            source_attachments = [self._source_attachment_from_row(row) for row in rows]
            if expected_count is not None and len(rows) < expected_count:
                return attachment_documents, self._media_archive_context(
                    source="intent_first",
                    status="awaiting_more_attachments",
                    intent_message_id=str(pending["intent_message_id"]),
                    intent_text=str(pending["intent_text"]),
                    expected_count=expected_count,
                    source_attachments=source_attachments,
                )
            idempotency_scope = self._media_archive_idempotency_scope(
                str(pending["intent_message_id"]),
                source_attachments,
            )
            connection.execute(
                "UPDATE media_archive_requests SET state='bound',bound_message_id=?,idempotency_scope=?,updated_at=? "
                "WHERE request_id=?",
                (message_id, idempotency_scope, now, pending["request_id"]),
            )
            return [self._attachment_document_from_row(row) for row in rows], self._media_archive_context(
                source="intent_first",
                status="authorized",
                intent_message_id=str(pending["intent_message_id"]),
                intent_text=str(pending["intent_text"]),
                expected_count=expected_count,
                source_attachments=source_attachments,
                idempotency_scope=idempotency_scope,
            )

        if instruction is not None and instruction["kind"] == "intent_first":
            if pending is not None:
                connection.execute(
                    "UPDATE media_archive_requests SET state='cancelled',updated_at=? WHERE request_id=?",
                    (now, pending["request_id"]),
                )
            request_id = self._media_archive_request_id(
                identity_scope,
                principal_scope,
                conversation_key_value,
                message_id,
            )
            expires_at = (parse_time(now) + timedelta(seconds=MEDIA_ARCHIVE_PENDING_TTL_SECONDS)).isoformat()
            connection.execute(
                "INSERT INTO media_archive_requests(request_id,identity_scope,principal_scope,conversation_key,source,state,intent_message_id,intent_text,expected_count,created_at,expires_at,updated_at) "
                "VALUES (?,?,?,?,?,'pending',?,?,?,?,?,?)",
                (
                    request_id,
                    identity_scope,
                    principal_scope,
                    conversation_key_value,
                    "intent_first",
                    message_id,
                    text,
                    instruction["expected_count"],
                    now,
                    expires_at,
                    now,
                ),
            )
            return attachment_documents, self._media_archive_context(
                source="intent_first",
                status="intent_registered",
                intent_message_id=message_id,
                intent_text=text,
                expected_count=instruction["expected_count"],
                source_attachments=[],
            )

        if instruction is not None and instruction["kind"] == "image_first":
            expected_count = instruction["expected_count"]
            rows = self._recent_media_archive_batch(
                connection,
                current_message_id=message_id,
                identity_scope=identity_scope,
                principal_scope=principal_scope,
                conversation_key_value=conversation_key_value,
                expected_count=expected_count,
                now=now,
            )
            source_attachments = [self._source_attachment_from_row(row) for row in rows]
            if not rows:
                return attachment_documents, self._media_archive_context(
                    source="image_first",
                    status="no_recent_attachments",
                    intent_message_id=message_id,
                    intent_text=text,
                    expected_count=expected_count,
                    source_attachments=[],
                )
            if expected_count is not None and len(rows) != expected_count:
                return attachment_documents, self._media_archive_context(
                    source="image_first",
                    status="attachment_count_mismatch",
                    intent_message_id=message_id,
                    intent_text=text,
                    expected_count=expected_count,
                    source_attachments=source_attachments,
                )
            if len(rows) > MEDIA_ARCHIVE_MAX_ATTACHMENTS:
                return attachment_documents, self._media_archive_context(
                    source="image_first",
                    status="too_many_attachments",
                    intent_message_id=message_id,
                    intent_text=text,
                    expected_count=expected_count,
                    source_attachments=source_attachments,
                )
            request_id = self._media_archive_request_id(
                identity_scope,
                principal_scope,
                conversation_key_value,
                message_id,
            )
            idempotency_scope = self._media_archive_idempotency_scope(message_id, source_attachments)
            expires_at = min(str(row["expires_at"]) for row in rows)
            connection.execute(
                "INSERT INTO media_archive_requests(request_id,identity_scope,principal_scope,conversation_key,source,state,intent_message_id,intent_text,expected_count,bound_message_id,idempotency_scope,created_at,expires_at,updated_at) "
                "VALUES (?,?,?,?,?,'bound',?,?,?,?,?,?,?,?)",
                (
                    request_id,
                    identity_scope,
                    principal_scope,
                    conversation_key_value,
                    "image_first",
                    message_id,
                    text,
                    expected_count,
                    message_id,
                    idempotency_scope,
                    now,
                    expires_at,
                    now,
                ),
            )
            self._reserve_media_archive_attachments(connection, request_id, rows)
            return [self._attachment_document_from_row(row) for row in rows], self._media_archive_context(
                source="image_first",
                status="authorized",
                intent_message_id=message_id,
                intent_text=text,
                expected_count=expected_count,
                source_attachments=source_attachments,
                idempotency_scope=idempotency_scope,
            )

        return attachment_documents, {}

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
        identity_identifier: str | None = None,
        principal_id_value: str | None = None,
        upstream_message_id: str | None = None,
    ) -> dict[str, Any]:
        if capability_profile not in {"owner", "owner_legacy", "member_read_only"}:
            raise StoreError("capability_profile_invalid", "会话权限画像无效")
        route_values = (identity_identifier, principal_id_value, upstream_message_id)
        if any(value is not None for value in route_values) and not all(
            isinstance(value, str) and value for value in route_values
        ):
            raise StoreError("message_route_invalid", "消息身份路由不完整")
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if identity_identifier is not None:
                route = connection.execute(
                    """
                    SELECT u.user_hash FROM identity_bindings b
                    JOIN ilink_identities i ON i.identity_id=b.identity_id
                    JOIN weixin_users u ON u.principal_id=b.principal_id
                    WHERE b.identity_id=? AND b.principal_id=? AND b.private_user_id=?
                      AND b.state='active' AND i.state='active' AND u.status='active'
                    """,
                    (identity_identifier, principal_id_value, sender_id),
                ).fetchone()
                if route is None or (user_digest is not None and route["user_hash"] != user_digest):
                    raise StoreError("message_route_invalid", "消息身份路由无效", status=409)
                existing_route = connection.execute(
                    "SELECT * FROM inbound_messages WHERE identity_id=? AND upstream_message_id=?",
                    (identity_identifier, upstream_message_id),
                ).fetchone()
                if existing_route is not None:
                    return self._message_document(existing_route)
            existing = connection.execute("SELECT * FROM inbound_messages WHERE message_id=?", (message_id,)).fetchone()
            if existing:
                return self._message_document(existing)
            attachment_documents: list[dict[str, Any]] = []
            staged: list[tuple[Path, Path]] = []
            created_targets: list[Path] = []
            try:
                connection.execute(
                    "INSERT INTO inbound_messages(message_id,sender_id,conversation_key,text,attachments_json,state,received_at,updated_at,user_hash,capability_profile,identity_id,principal_id,upstream_message_id) VALUES (?,?,?,?,?,'pending_controller',?,?,?,?,?,?,?)",
                    (
                        message_id,
                        sender_id,
                        conversation_key,
                        text,
                        "[]",
                        now,
                        now,
                        user_digest,
                        capability_profile,
                        identity_identifier,
                        principal_id_value,
                        upstream_message_id,
                    ),
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
                attachment_documents, media_archive_context = self._correlate_media_archive(
                    connection,
                    message_id=message_id,
                    sender_id=sender_id,
                    conversation_key_value=conversation_key,
                    text=text,
                    attachment_documents=attachment_documents,
                    user_digest=user_digest,
                    identity_identifier=identity_identifier,
                    principal_id_value=principal_id_value,
                )
                connection.execute(
                    "UPDATE inbound_messages SET attachments_json=?,media_archive_context_json=?,updated_at=? WHERE message_id=?",
                    (
                        canonical_json(attachment_documents),
                        canonical_json(media_archive_context),
                        now,
                        message_id,
                    ),
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
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE inbound_messages SET state=?,updated_at=?,error_code=? WHERE message_id=?",
                ("completed" if success else "failed", utc_now(), error_code, message_id),
            )
            connection.execute(
                "UPDATE media_archive_requests SET state=?,updated_at=? WHERE bound_message_id=? AND state='bound'",
                ("completed" if success else "failed", utc_now(), message_id),
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

    def prepare_artifact(self, job_id: str, artifact: dict[str, Any]) -> dict[str, Any]:
        artifact_id = artifact.get("artifact_id")
        digest = artifact.get("sha256")
        mime_type = artifact.get("mime_type")
        size_bytes = artifact.get("size_bytes")
        if not isinstance(job_id, str) or not job_id:
            raise StoreError("artifact_invalid", "artifact job_id 无效")
        if not isinstance(artifact_id, str) or not re.fullmatch(r"AR-[A-Z2-7]{26}", artifact_id):
            raise StoreError("artifact_invalid", "artifact_id 无效")
        if not isinstance(digest, str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
            raise StoreError("artifact_invalid", "artifact sha256 无效")
        if mime_type not in {"image/png", "application/zip"}:
            raise StoreError("artifact_invalid", "artifact MIME 无效")
        filename = artifact.get("filename") or ("artifact.png" if mime_type == "image/png" else "artifact.zip")
        if not isinstance(filename, str) or not filename or len(filename) > 255 or Path(filename).name != filename:
            raise StoreError("artifact_invalid", "artifact 文件名无效")
        if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes < 1:
            raise StoreError("artifact_invalid", "artifact 大小无效")
        client_id = "codex-weixin-" + hashlib.sha256(
            f"media:{job_id}:{artifact_id}".encode("utf-8")
        ).hexdigest()[:32]
        fallback_client_id = "codex-weixin-" + hashlib.sha256(
            f"fallback:{job_id}:{artifact_id}".encode("utf-8")
        ).hexdigest()[:32]
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT OR IGNORE INTO outbound_artifacts(job_id,artifact_id,sha256,mime_type,size_bytes,client_id,state,fallback_client_id,fallback_state) "
                "VALUES (?,?,?,?,?,?,'pending',?,'pending')",
                (job_id, artifact_id, digest, mime_type, size_bytes, client_id, fallback_client_id),
            )
            row = connection.execute(
                "SELECT * FROM outbound_artifacts WHERE job_id=? AND artifact_id=?",
                (job_id, artifact_id),
            ).fetchone()
            if row is None:
                raise StoreError("artifact_state_invalid", "artifact 状态不可用", status=500)
            if row["sha256"] != digest or row["mime_type"] != mime_type or row["size_bytes"] != size_bytes:
                raise StoreError("artifact_idempotency_conflict", "同一 artifact_id 的元数据发生变化", status=409)
            state = dict(row)
            state["delivery_state"] = (
                "delivery_state_unknown"
                if state.get("error_code") == "delivery_state_unknown"
                else state.get("state")
            )
            return state

    def mark_artifact(self, job_id: str, artifact_id: str, *, success: bool, error_code: str | None = None) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE outbound_artifacts SET state=?,sent_at=?,error_code=? WHERE job_id=? AND artifact_id=?",
                ("sent" if success else "failed", utc_now() if success else None, error_code, job_id, artifact_id),
            )

    def mark_artifact_fallback(
        self,
        job_id: str,
        artifact_id: str,
        *,
        success: bool,
        error_code: str | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE outbound_artifacts SET fallback_state=?,fallback_sent_at=?,fallback_error_code=? "
                "WHERE job_id=? AND artifact_id=?",
                (
                    "sent" if success else "failed",
                    utc_now() if success else None,
                    error_code,
                    job_id,
                    artifact_id,
                ),
            )

    def stage_outbound_artifact(self, artifact: dict[str, Any], content: bytes) -> Path:
        mime_type = artifact.get("mime_type")
        if mime_type not in {"image/png", "application/zip"}:
            raise StoreError("artifact_content_invalid", "出站 artifact MIME 无效", status=502)
        if mime_type == "image/png" and not content.startswith(b"\x89PNG\r\n\x1a\n"):
            raise StoreError("artifact_content_invalid", "出站图片 artifact 不是有效 PNG", status=502)
        if mime_type == "application/zip" and not content.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
            raise StoreError("artifact_content_invalid", "出站文件 artifact 不是有效 ZIP", status=502)
        if len(content) != artifact.get("size_bytes"):
            raise StoreError("artifact_size_invalid", "出站 artifact 大小不一致", status=502)
        digest = hashlib.sha256(content).hexdigest()
        if artifact.get("sha256") != f"sha256:{digest}":
            raise StoreError("artifact_digest_invalid", "出站 artifact 摘要不一致", status=502)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".artifact.",
            suffix=".png" if mime_type == "image/png" else ".zip",
            dir=self.outbound_dir,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            return temporary
        except Exception:
            if temporary.exists():
                temporary.unlink()
            raise

    def cleanup_outbound_artifacts(self, *, older_than_seconds: int = 3600) -> int:
        threshold = datetime.now(timezone.utc).timestamp() - max(300, int(older_than_seconds))
        removed = 0
        for path in self.outbound_dir.iterdir():
            if path.is_file() and not path.is_symlink() and path.stat().st_mtime <= threshold:
                path.unlink()
                removed += 1
        return removed

    def preview_attachment(self, reference: str) -> tuple[dict[str, Any], bytes]:
        """Read and verify an attachment without consuming its one-time reference."""
        return self._read_attachment(reference, consume=False)

    def consume_attachment(self, reference: str) -> tuple[dict[str, Any], bytes]:
        """Read and atomically consume an attachment reference."""
        return self._read_attachment(reference, consume=True)

    def open_stream_attachment(self, reference: str) -> tuple[dict[str, Any], BinaryIO]:
        """Open an attachment for a non-consuming, authenticated media stream."""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM attachments WHERE attachment_ref=?",
                (reference,),
            ).fetchone()
        if row is None:
            raise StoreError("attachment_not_found", "附件引用不存在", status=404)
        if row["consumed_at"] is not None:
            raise StoreError("attachment_consumed", "附件已经消费", status=409)
        if parse_time(row["expires_at"]) <= datetime.now(timezone.utc):
            raise StoreError("attachment_expired", "附件已过期", status=410)
        raw_path = self.spool_dir / row["storage_name"]
        spool_root = self.spool_dir.resolve()
        try:
            if raw_path.name != row["storage_name"] or raw_path.is_symlink():
                raise StoreError("attachment_invalid", "附件存储越界或为符号链接", status=409)
            path = raw_path.resolve(strict=True)
            path.relative_to(spool_root)
            if path.parent != spool_root or not path.is_file() or path.is_symlink():
                raise StoreError("attachment_invalid", "附件存储缺失或越界", status=409)
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            handle = os.fdopen(os.open(path, flags), "rb")
        except (OSError, ValueError) as exc:
            raise StoreError("attachment_missing", "附件存储缺失或越界", status=404) from exc
        try:
            if path.is_symlink() or not path.is_file() or os.fstat(handle.fileno()).st_size != row["size_bytes"]:
                raise StoreError("attachment_invalid", "附件大小或存储状态不一致", status=409)
        except Exception:
            handle.close()
            raise
        return (
            {
                "original_filename": row["original_filename"],
                "mime_type": row["mime_type"],
                "size_bytes": row["size_bytes"],
                "sha256": f"sha256:{row['sha256']}",
            },
            handle,
        )

    def acknowledge_attachment(self, reference: str, sha256: str) -> dict[str, Any]:
        """Consume a streamed attachment only after downstream ingestion succeeds."""

        if not isinstance(sha256, str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", sha256):
            raise StoreError("attachment_invalid", "附件摘要无效", status=400)
        expected = sha256.split(":", 1)[1]
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT sha256,consumed_at,expires_at FROM attachments WHERE attachment_ref=?",
                (reference,),
            ).fetchone()
            if row is None:
                raise StoreError("attachment_not_found", "附件引用不存在", status=404)
            if row["sha256"] != expected:
                raise StoreError("attachment_digest_mismatch", "附件摘要不一致", status=409)
            if row["consumed_at"] is not None:
                return {"consumed": True, "idempotent_replay": True}
            if parse_time(row["expires_at"]) <= datetime.now(timezone.utc):
                raise StoreError("attachment_expired", "附件已过期", status=410)
            connection.execute(
                "UPDATE attachments SET consumed_at=? WHERE attachment_ref=? AND consumed_at IS NULL",
                (utc_now(), reference),
            )
            return {"consumed": True, "idempotent_replay": False}

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
            archive_counts = {
                row["state"]: row["count"]
                for row in connection.execute(
                    "SELECT state,COUNT(*) AS count FROM media_archive_requests GROUP BY state"
                )
            }
            return {
                "messages": {state: message_counts.get(state, 0) for state in ("pending_controller", "controller_submitted", "completed", "failed")},
                "attachments": connection.execute("SELECT COUNT(*) FROM attachments WHERE consumed_at IS NULL").fetchone()[0],
                "spool_bytes": connection.execute("SELECT COALESCE(SUM(size_bytes),0) FROM attachments WHERE consumed_at IS NULL").fetchone()[0],
                "media_archive_requests": {
                    state: archive_counts.get(state, 0)
                    for state in ("pending", "bound", "completed", "failed", "cancelled", "expired")
                },
            }

    @staticmethod
    def _message_document(row: sqlite3.Row) -> dict[str, Any]:
        media_archive_context = json.loads(row["media_archive_context_json"] or "{}")
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
            "identity_id": row["identity_id"],
            "principal_id": row["principal_id"],
            "upstream_message_id": row["upstream_message_id"],
            "media_archive_context": media_archive_context if isinstance(media_archive_context, dict) else {},
        }
