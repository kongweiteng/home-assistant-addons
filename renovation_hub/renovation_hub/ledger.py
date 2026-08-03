"""Legacy-compatible ledger module for Renovation Hub."""

from __future__ import annotations

import csv
import base64
from datetime import date, datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import tempfile
import unicodedata
import uuid
import zipfile
from typing import Any, Callable

from .portable import (
    FORMAT_ID as CANONICAL_PORTABLE_FORMAT_ID,
    FORMAT_VERSION as CANONICAL_PORTABLE_FORMAT_VERSION,
    PortableArchiveError,
    digest_json as portable_digest_json,
    monthly_summary as portable_monthly_summary,
    normalized_member_name,
    sha256_file as portable_sha256_file,
    summary_from_transactions as portable_summary_from_transactions,
    verify_and_extract as verify_and_extract_canonical,
    verify_extracted as verify_extracted_canonical,
)


FORMAT_ID = "kanhuwan-renovation-ledger@1"
SCHEMA_VERSION = 1
WRITER_MODES = {
    "uninitialized",
    "read_only",
    "shadow_validated",
    "cutover_ready",
    "primary_writer",
    "suspended",
}
WRITER_TRANSITIONS = {
    "uninitialized": {"read_only"},
    "read_only": {"shadow_validated", "suspended"},
    "shadow_validated": {"cutover_ready", "read_only", "suspended"},
    "cutover_ready": {"primary_writer", "read_only", "suspended"},
    "primary_writer": {"suspended", "read_only"},
    "suspended": {"read_only"},
}
HEX_64 = re.compile(r"^[a-f0-9]{64}$")


class LedgerError(ValueError):
    def __init__(self, code: str, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_date(value: Any, field: str = "occurred_on") -> str:
    if not isinstance(value, str):
        raise LedgerError("invalid_date", f"{field} 必须是 YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise LedgerError("invalid_date", f"{field} 必须是 YYYY-MM-DD") from exc
    return parsed.isoformat()


def _positive_cents(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise LedgerError("invalid_amount", "金额必须是正整数分")
    if value > 10_000_000_000:
        raise LedgerError("invalid_amount", "金额超过允许上限")
    return value


def _text(value: Any, field: str, maximum: int, *, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise LedgerError("invalid_input", f"{field} 必须是文本")
    value = unicodedata.normalize("NFC", value).strip()
    if required and not value:
        raise LedgerError("invalid_input", f"{field} 不能为空")
    if len(value) > maximum:
        raise LedgerError("invalid_input", f"{field} 超过长度上限")
    return value


def normalize_tags(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 8:
        raise LedgerError("invalid_tags", "标签必须是最多 8 个文本值")
    result: list[str] = []
    seen: set[str] = set()
    for raw in value:
        tag = _text(raw, "tag", 20, required=True)
        key = tag.casefold()
        if key in seen:
            raise LedgerError("invalid_tags", "标签规范化后重复")
        seen.add(key)
        result.append(tag)
    return result


def _idempotency_key(value: Any) -> str:
    if not isinstance(value, str) or len(value) < 16 or len(value) > 256:
        raise LedgerError("invalid_idempotency_key", "幂等键长度必须为 16 到 256")
    return value


class LedgerStore:
    def __init__(
        self,
        database_path: str | Path,
        *,
        data_dir: str | Path | None = None,
        share_dir: str | Path | None = None,
        max_attachment_bytes: int = 20 * 1024 * 1024,
        portable_history_limit: int = 20,
    ) -> None:
        self.database_path = Path(database_path)
        self.data_dir = Path(data_dir or self.database_path.parent)
        self.share_dir = Path(share_dir or self.data_dir / "share")
        self.attachments_dir = self.data_dir / "attachments"
        self.charts_dir = self.data_dir / "charts"
        self.import_dir = self.data_dir / "import"
        self.shadow_dir = self.data_dir / "shadow"
        self.max_attachment_bytes = max_attachment_bytes
        self.portable_history_limit = portable_history_limit
        for path in (
            self.database_path.parent,
            self.attachments_dir,
            self.charts_dir,
            self.import_dir,
            self.shadow_dir,
            self.share_dir / "current",
            self.share_dir / "history",
        ):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS transactions (
                    id TEXT PRIMARY KEY,
                    legacy_id INTEGER,
                    type TEXT NOT NULL CHECK(type IN ('payment','refund')),
                    amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
                    occurred_on TEXT NOT NULL,
                    main_category TEXT NOT NULL DEFAULT '',
                    merchant TEXT NOT NULL DEFAULT '',
                    note TEXT NOT NULL DEFAULT '',
                    is_deposit INTEGER NOT NULL DEFAULT 0 CHECK(is_deposit IN (0,1)),
                    original_payment_id TEXT REFERENCES transactions(id),
                    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','voided')),
                    void_reason TEXT NOT NULL DEFAULT '',
                    source_ref TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS transactions_legacy_id
                    ON transactions(legacy_id) WHERE legacy_id IS NOT NULL;
                CREATE TABLE IF NOT EXISTS tags (
                    normalized TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS transaction_tags (
                    transaction_id TEXT NOT NULL REFERENCES transactions(id),
                    tag_normalized TEXT NOT NULL REFERENCES tags(normalized),
                    position INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(transaction_id, tag_normalized)
                );
                CREATE TABLE IF NOT EXISTS attachments (
                    id TEXT PRIMARY KEY,
                    transaction_id TEXT NOT NULL REFERENCES transactions(id),
                    storage_name TEXT NOT NULL,
                    original_filename TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','removed')),
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    target_type TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    actor_hash TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    before_json TEXT,
                    after_json TEXT,
                    result TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS idempotency_keys (
                    idempotency_key TEXT PRIMARY KEY,
                    request_digest TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            transaction_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(transactions)")
            }
            if "void_reason" not in transaction_columns:
                connection.execute(
                    "ALTER TABLE transactions ADD COLUMN void_reason TEXT NOT NULL DEFAULT ''"
                )
            tag_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(transaction_tags)")
            }
            if "position" not in tag_columns:
                connection.execute(
                    "ALTER TABLE transaction_tags ADD COLUMN position INTEGER NOT NULL DEFAULT 0"
                )
            defaults = {
                "schema_version": str(SCHEMA_VERSION),
                "format_id": FORMAT_ID,
                "writer_mode": "uninitialized",
                "portable_export_state": "never",
                "last_export_at": "",
                "last_write_at": "",
            }
            for key, value in defaults.items():
                connection.execute(
                    "INSERT OR IGNORE INTO metadata(key,value) VALUES (?,?)",
                    (key, value),
                )
        os.chmod(self.database_path, 0o600)

    def metadata(self) -> dict[str, str]:
        with self._connect() as connection:
            return {row["key"]: row["value"] for row in connection.execute("SELECT key,value FROM metadata")}

    def writer_mode(self) -> str:
        return self.metadata()["writer_mode"]

    def set_writer_mode(self, target: str, *, force_initial: bool = False) -> dict[str, str]:
        if target not in WRITER_MODES:
            raise LedgerError("invalid_writer_mode", "未知 writer mode")
        with self._connect() as connection:
            current = connection.execute("SELECT value FROM metadata WHERE key='writer_mode'").fetchone()[0]
            if current == target:
                return {"previous": current, "current": target}
            allowed = WRITER_TRANSITIONS.get(current, set())
            if not (force_initial and current == "uninitialized") and target not in allowed:
                raise LedgerError("invalid_writer_transition", f"不允许从 {current} 切换到 {target}")
            connection.execute("UPDATE metadata SET value=? WHERE key='writer_mode'", (target,))
        return {"previous": current, "current": target}

    def status(self) -> dict[str, Any]:
        meta = self.metadata()
        with self._connect() as connection:
            counts = {
                "payments": connection.execute("SELECT count(*) FROM transactions WHERE type='payment' AND status='active'").fetchone()[0],
                "refunds": connection.execute("SELECT count(*) FROM transactions WHERE type='refund' AND status='active'").fetchone()[0],
                "attachments": connection.execute("SELECT count(*) FROM attachments WHERE status='active'").fetchone()[0],
                "audit_events": connection.execute("SELECT count(*) FROM audit_log").fetchone()[0],
            }
        return {
            "service": "renovation_hub",
            "version": "0.1.2",
            "schema_version": int(meta["schema_version"]),
            "format_id": meta["format_id"],
            "writer_mode": meta["writer_mode"],
            "portable_export_state": meta["portable_export_state"],
            "last_export_at": meta["last_export_at"] or None,
            "last_write_at": meta["last_write_at"] or None,
            "counts": counts,
        }

    def _require_writer(self, connection: sqlite3.Connection) -> None:
        mode = connection.execute("SELECT value FROM metadata WHERE key='writer_mode'").fetchone()[0]
        if mode != "primary_writer":
            raise LedgerError("writer_disabled", f"当前 writer mode 为 {mode}", status=409)

    def _run_idempotent(
        self,
        *,
        key: str,
        request: dict[str, Any],
        operation: Callable[[sqlite3.Connection], dict[str, Any]],
    ) -> tuple[dict[str, Any], bool]:
        key = _idempotency_key(key)
        request_digest = digest_json(request)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT request_digest,result_json FROM idempotency_keys WHERE idempotency_key=?",
                (key,),
            ).fetchone()
            if existing:
                if existing["request_digest"] != request_digest:
                    raise LedgerError("idempotency_conflict", "同一幂等键对应不同请求", status=409)
                connection.rollback()
                return json.loads(existing["result_json"]), True
            self._require_writer(connection)
            result = operation(connection)
            connection.execute(
                "INSERT INTO idempotency_keys(idempotency_key,request_digest,result_json,created_at) VALUES (?,?,?,?)",
                (key, request_digest, canonical_json(result), utc_now()),
            )
            connection.execute("UPDATE metadata SET value=? WHERE key='last_write_at'", (utc_now(),))
            connection.commit()
            return result, False
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _set_tags(self, connection: sqlite3.Connection, transaction_id: str, tags: list[str]) -> None:
        connection.execute("DELETE FROM transaction_tags WHERE transaction_id=?", (transaction_id,))
        for position, tag in enumerate(tags):
            normalized = unicodedata.normalize("NFC", tag).casefold()
            connection.execute(
                "INSERT INTO tags(normalized,display_name) VALUES (?,?) ON CONFLICT(normalized) DO UPDATE SET display_name=excluded.display_name",
                (normalized, tag),
            )
            connection.execute(
                "INSERT INTO transaction_tags(transaction_id,tag_normalized,position) VALUES (?,?,?)",
                (transaction_id, normalized, position),
            )

    def _tags(self, connection: sqlite3.Connection, transaction_id: str) -> list[str]:
        rows = connection.execute(
            "SELECT tags.display_name FROM transaction_tags JOIN tags ON tags.normalized=transaction_tags.tag_normalized WHERE transaction_id=? ORDER BY transaction_tags.position,tags.display_name",
            (transaction_id,),
        )
        return [row[0] for row in rows]

    def _row_json(self, connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["is_deposit"] = bool(result["is_deposit"])
        if result["type"] == "refund" and result["original_payment_id"]:
            original = connection.execute("SELECT main_category FROM transactions WHERE id=?", (result["original_payment_id"],)).fetchone()
            result["main_category"] = original[0] if original else ""
            result["tags"] = self._tags(connection, result["original_payment_id"])
        else:
            result["tags"] = self._tags(connection, result["id"])
        result["amount"] = f"{result['amount_cents'] / 100:.2f}"
        return result

    def _audit(
        self,
        connection: sqlite3.Connection,
        *,
        action: str,
        target_id: str,
        actor_hash: str,
        idempotency_key: str,
        reason: str,
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
    ) -> None:
        connection.execute(
            "INSERT INTO audit_log(action,target_type,target_id,actor_hash,idempotency_key,reason,before_json,after_json,result,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                action,
                "transaction",
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
        """Extension hook for Hub-owned transaction context."""

    def _validate_transaction_version(
        self,
        connection: sqlite3.Connection,
        transaction_id: str,
        payload: dict[str, Any],
    ) -> None:
        """Extension hook for optimistic page edits without changing Ledger v1."""

    def _after_transaction_update(
        self,
        connection: sqlite3.Connection,
        transaction_id: str,
        payload: dict[str, Any],
    ) -> None:
        """Extension hook paired with ``_validate_transaction_version``."""

    def add_payment(self, payload: dict[str, Any], *, actor_hash: str = "system") -> dict[str, Any]:
        key = _idempotency_key(payload.get("idempotency_key"))
        clean = {
            "amount_cents": _positive_cents(payload.get("amount_cents")),
            "occurred_on": _validate_date(payload.get("occurred_on")),
            "main_category": _text(payload.get("main_category"), "main_category", 80, required=True),
            "merchant": _text(payload.get("merchant"), "merchant", 200),
            "note": _text(payload.get("note"), "note", 2000),
            "is_deposit": bool(payload.get("is_deposit", False)),
            "tags": normalize_tags(payload.get("tags", [])),
            "source_ref": _text(payload.get("source_ref"), "source_ref", 256),
        }

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            transaction_id = str(uuid.uuid4())
            now = utc_now()
            connection.execute(
                "INSERT INTO transactions(id,type,amount_cents,occurred_on,main_category,merchant,note,is_deposit,status,source_ref,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    transaction_id,
                    "payment",
                    clean["amount_cents"],
                    clean["occurred_on"],
                    clean["main_category"],
                    clean["merchant"],
                    clean["note"],
                    int(clean["is_deposit"]),
                    "active",
                    clean["source_ref"],
                    now,
                    now,
                ),
            )
            self._set_tags(connection, transaction_id, clean["tags"])
            self._after_transaction_insert(connection, transaction_id, payload)
            row = connection.execute("SELECT * FROM transactions WHERE id=?", (transaction_id,)).fetchone()
            result = self._row_json(connection, row)
            self._audit(connection, action="add_payment", target_id=transaction_id, actor_hash=actor_hash, idempotency_key=key, reason="", before=None, after=result)
            return result

        result, replayed = self._run_idempotent(
            key=key,
            request={
                "tool": "ledger_add_payment",
                **clean,
                "project_id": payload.get("project_id"),
                "stage_id": payload.get("stage_id"),
                "area_id": payload.get("area_id"),
            },
            operation=operation,
        )
        return self._after_write(result, replayed=replayed)

    def add_refund(self, payload: dict[str, Any], *, actor_hash: str = "system") -> dict[str, Any]:
        key = _idempotency_key(payload.get("idempotency_key"))
        clean = {
            "original_payment_id": _text(payload.get("original_payment_id"), "original_payment_id", 64, required=True),
            "amount_cents": _positive_cents(payload.get("amount_cents")),
            "occurred_on": _validate_date(payload.get("occurred_on")),
            "note": _text(payload.get("note"), "note", 2000),
            "source_ref": _text(payload.get("source_ref"), "source_ref", 256),
        }

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            payment = connection.execute(
                "SELECT * FROM transactions WHERE id=? AND type='payment' AND status='active'",
                (clean["original_payment_id"],),
            ).fetchone()
            if payment is None:
                raise LedgerError("payment_not_found", "原付款不存在或已撤销", status=404)
            refunded = connection.execute(
                "SELECT coalesce(sum(amount_cents),0) FROM transactions WHERE type='refund' AND status='active' AND original_payment_id=?",
                (payment["id"],),
            ).fetchone()[0]
            if refunded + clean["amount_cents"] > payment["amount_cents"]:
                raise LedgerError("refund_exceeds_payment", "累计退款超过原付款", status=409)
            transaction_id = str(uuid.uuid4())
            now = utc_now()
            connection.execute(
                "INSERT INTO transactions(id,type,amount_cents,occurred_on,main_category,note,original_payment_id,status,source_ref,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    transaction_id,
                    "refund",
                    clean["amount_cents"],
                    clean["occurred_on"],
                    "",
                    clean["note"],
                    payment["id"],
                    "active",
                    clean["source_ref"],
                    now,
                    now,
                ),
            )
            self._after_transaction_insert(
                connection,
                transaction_id,
                payload,
                original_payment_id=payment["id"],
            )
            row = connection.execute("SELECT * FROM transactions WHERE id=?", (transaction_id,)).fetchone()
            result = self._row_json(connection, row)
            self._audit(connection, action="add_refund", target_id=transaction_id, actor_hash=actor_hash, idempotency_key=key, reason="", before=None, after=result)
            return result

        result, replayed = self._run_idempotent(
            key=key,
            request={
                "tool": "ledger_add_refund",
                **clean,
                "project_id": payload.get("project_id"),
                "stage_id": payload.get("stage_id"),
                "area_id": payload.get("area_id"),
            },
            operation=operation,
        )
        return self._after_write(result, replayed=replayed)

    def correct_payment(self, payload: dict[str, Any], *, actor_hash: str = "system") -> dict[str, Any]:
        key = _idempotency_key(payload.get("idempotency_key"))
        payment_id = _text(payload.get("payment_id"), "payment_id", 64, required=True)
        changes = payload.get("changes")
        if not isinstance(changes, dict) or not changes:
            raise LedgerError("invalid_input", "changes 必须是非空对象")
        allowed = {"amount_cents", "occurred_on", "main_category", "merchant", "note", "is_deposit", "tags"}
        if set(changes) - allowed:
            raise LedgerError("invalid_input", "changes 包含不允许字段")
        reason = _text(payload.get("reason"), "reason", 500, required=True)
        clean_changes: dict[str, Any] = {}
        if "amount_cents" in changes:
            clean_changes["amount_cents"] = _positive_cents(changes["amount_cents"])
        if "occurred_on" in changes:
            clean_changes["occurred_on"] = _validate_date(changes["occurred_on"])
        if "main_category" in changes:
            clean_changes["main_category"] = _text(changes["main_category"], "main_category", 80, required=True)
        if "merchant" in changes:
            clean_changes["merchant"] = _text(changes["merchant"], "merchant", 200)
        if "note" in changes:
            clean_changes["note"] = _text(changes["note"], "note", 2000)
        if "is_deposit" in changes:
            clean_changes["is_deposit"] = bool(changes["is_deposit"])
        if "tags" in changes:
            clean_changes["tags"] = normalize_tags(changes["tags"])

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            row = connection.execute("SELECT * FROM transactions WHERE id=? AND type='payment' AND status='active'", (payment_id,)).fetchone()
            if row is None:
                raise LedgerError("payment_not_found", "付款不存在或已撤销", status=404)
            self._validate_transaction_version(connection, payment_id, payload)
            before = self._row_json(connection, row)
            if "amount_cents" in clean_changes:
                refunded = connection.execute("SELECT coalesce(sum(amount_cents),0) FROM transactions WHERE type='refund' AND status='active' AND original_payment_id=?", (payment_id,)).fetchone()[0]
                if clean_changes["amount_cents"] < refunded:
                    raise LedgerError("refund_exceeds_payment", "付款金额不能低于累计退款", status=409)
            columns = {key: value for key, value in clean_changes.items() if key != "tags"}
            if "is_deposit" in columns:
                columns["is_deposit"] = int(columns["is_deposit"])
            if columns:
                columns["updated_at"] = utc_now()
                sql = ",".join(f"{name}=?" for name in columns)
                connection.execute(f"UPDATE transactions SET {sql} WHERE id=?", (*columns.values(), payment_id))
            if "tags" in clean_changes:
                self._set_tags(connection, payment_id, clean_changes["tags"])
            self._after_transaction_update(connection, payment_id, payload)
            updated = connection.execute("SELECT * FROM transactions WHERE id=?", (payment_id,)).fetchone()
            result = self._row_json(connection, updated)
            self._audit(connection, action="correct_payment", target_id=payment_id, actor_hash=actor_hash, idempotency_key=key, reason=reason, before=before, after=result)
            return result

        result, replayed = self._run_idempotent(
            key=key,
            request={"tool": "ledger_correct_payment", "payment_id": payment_id, "version": payload.get("version"), "changes": clean_changes, "reason": reason},
            operation=operation,
        )
        return self._after_write(result, replayed=replayed)

    def undo(self, payload: dict[str, Any], *, actor_hash: str = "system") -> dict[str, Any]:
        key = _idempotency_key(payload.get("idempotency_key"))
        transaction_id = _text(payload.get("transaction_id"), "transaction_id", 64, required=True)
        reason = _text(payload.get("reason"), "reason", 500, required=True)

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            row = connection.execute("SELECT * FROM transactions WHERE id=? AND status='active'", (transaction_id,)).fetchone()
            if row is None:
                raise LedgerError("transaction_not_found", "流水不存在或已撤销", status=404)
            self._validate_transaction_version(connection, transaction_id, payload)
            before = self._row_json(connection, row)
            if row["type"] == "payment":
                refunds = connection.execute("SELECT count(*) FROM transactions WHERE type='refund' AND status='active' AND original_payment_id=?", (transaction_id,)).fetchone()[0]
                if refunds:
                    raise LedgerError("payment_has_refunds", "付款存在有效退款，不能直接撤销", status=409)
            connection.execute(
                "UPDATE transactions SET status='voided',void_reason=?,updated_at=? WHERE id=?",
                (reason, utc_now(), transaction_id),
            )
            self._after_transaction_update(connection, transaction_id, payload)
            updated = connection.execute("SELECT * FROM transactions WHERE id=?", (transaction_id,)).fetchone()
            result = self._row_json(connection, updated)
            self._audit(connection, action="undo", target_id=transaction_id, actor_hash=actor_hash, idempotency_key=key, reason=reason, before=before, after=result)
            return result

        result, replayed = self._run_idempotent(
            key=key,
            request={"tool": "ledger_undo", "transaction_id": transaction_id, "version": payload.get("version"), "reason": reason},
            operation=operation,
        )
        return self._after_write(result, replayed=replayed)

    def attach_content(self, payload: dict[str, Any], *, actor_hash: str = "system") -> dict[str, Any]:
        key = _idempotency_key(payload.get("idempotency_key"))
        transaction_id = _text(payload.get("transaction_id"), "transaction_id", 64, required=True)
        filename = _safe_filename(payload.get("original_filename"))
        mime_type = _text(payload.get("mime_type"), "mime_type", 120, required=True)
        if mime_type not in {"image/jpeg", "image/png", "image/webp", "application/pdf", "text/plain"}:
            raise LedgerError("attachment_invalid", "附件类型不在白名单")
        content_base64 = payload.get("content_base64")
        if not isinstance(content_base64, str):
            raise LedgerError("attachment_invalid", "缺少附件内容")
        try:
            content = base64.b64decode(content_base64, validate=True)
        except ValueError as exc:
            raise LedgerError("attachment_invalid", "附件 Base64 非法") from exc
        if not content or len(content) > self.max_attachment_bytes:
            raise LedgerError("attachment_invalid", "附件大小超出范围")
        content_digest = hashlib.sha256(content).hexdigest()
        extension = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "application/pdf": ".pdf", "text/plain": ".txt"}[mime_type]
        storage_name = f"{content_digest}{extension}"
        target = self.attachments_dir / storage_name
        if not target.exists():
            temporary = self.attachments_dir / f".{uuid.uuid4().hex}.tmp"
            temporary.write_bytes(content)
            os.chmod(temporary, 0o600)
            os.replace(temporary, target)

        clean_request = {
            "tool": "ledger_attach",
            "transaction_id": transaction_id,
            "original_filename": filename,
            "mime_type": mime_type,
            "size_bytes": len(content),
            "sha256": content_digest,
        }

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            transaction = connection.execute("SELECT * FROM transactions WHERE id=? AND status='active'", (transaction_id,)).fetchone()
            if transaction is None:
                raise LedgerError("transaction_not_found", "附件目标流水不存在或已撤销", status=404)
            attachment_id = str(uuid.uuid4())
            created_at = utc_now()
            connection.execute(
                "INSERT INTO attachments(id,transaction_id,storage_name,original_filename,mime_type,size_bytes,sha256,status,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (attachment_id, transaction_id, storage_name, filename, mime_type, len(content), content_digest, "active", created_at),
            )
            result = {"id": attachment_id, "transaction_id": transaction_id, "original_filename": filename, "mime_type": mime_type, "size_bytes": len(content), "sha256": content_digest, "status": "active", "created_at": created_at}
            self._audit(connection, action="attach", target_id=transaction_id, actor_hash=actor_hash, idempotency_key=key, reason="", before=None, after=result)
            return result

        result, replayed = self._run_idempotent(key=key, request=clean_request, operation=operation)
        response = {"attachment": result, "idempotent_replay": replayed}
        if not replayed:
            try:
                export = self.export_portable()
            except Exception:
                response["portable_export"] = "stale"
                response["warning_code"] = "portable_export_stale"
            else:
                response["portable_export"] = "current"
                response["export_sha256"] = export["sha256"]
        return response

    def _after_write(self, result: dict[str, Any], *, replayed: bool) -> dict[str, Any]:
        response = {"transaction": result, "idempotent_replay": replayed, "portable_export": "unchanged" if replayed else "pending"}
        if replayed:
            return response
        try:
            export = self.export_portable()
        except Exception:
            with self._connect() as connection:
                connection.execute("UPDATE metadata SET value='stale' WHERE key='portable_export_state'")
            response["portable_export"] = "stale"
            response["warning_code"] = "portable_export_stale"
        else:
            response["portable_export"] = "current"
            response["export_sha256"] = export["sha256"]
        return response

    def show(self, transaction_id: str) -> dict[str, Any]:
        transaction_id = _text(transaction_id, "transaction_id", 64, required=True)
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM transactions WHERE id=?", (transaction_id,)).fetchone()
            if row is None:
                raise LedgerError("transaction_not_found", "流水不存在", status=404)
            result = self._row_json(connection, row)
            result["attachments"] = [dict(item) for item in connection.execute("SELECT id,original_filename,mime_type,size_bytes,sha256,status,created_at FROM attachments WHERE transaction_id=? ORDER BY created_at", (transaction_id,))]
            return result

    def query(self, filters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        filters = filters or {}
        clauses: list[str] = []
        values: list[Any] = []
        for name in ("type", "status", "main_category"):
            value = filters.get(name)
            if value:
                clauses.append(f"{name}=?")
                values.append(value)
        if filters.get("start"):
            clauses.append("occurred_on>=?")
            values.append(_validate_date(filters["start"], "start"))
        if filters.get("end"):
            clauses.append("occurred_on<=?")
            values.append(_validate_date(filters["end"], "end"))
        sql = "SELECT * FROM transactions"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY occurred_on,id"
        limit = filters.get("limit", 200)
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > 1000:
            raise LedgerError("invalid_input", "limit 必须为 1 到 1000")
        sql += " LIMIT ?"
        values.append(limit)
        keyword = _text(filters.get("keyword"), "keyword", 100)
        tag = _text(filters.get("tag"), "tag", 20)
        with self._connect() as connection:
            items = [self._row_json(connection, row) for row in connection.execute(sql, values)]
        if keyword:
            folded = keyword.casefold()
            items = [item for item in items if folded in " ".join(str(item.get(key, "")) for key in ("merchant", "note", "main_category")).casefold()]
        if tag:
            items = [item for item in items if tag.casefold() in {value.casefold() for value in item["tags"]}]
        return items

    def summary(self, filters: dict[str, Any] | None = None) -> dict[str, Any]:
        query_filters = dict(filters or {})
        query_filters["status"] = "active"
        query_filters["limit"] = 1000
        category_totals: dict[str, int] = {}
        tag_totals: dict[str, int] = {}
        total = 0
        items = self.query(query_filters)
        for item in items:
            sign = -1 if item["type"] == "refund" else 1
            amount = sign * item["amount_cents"]
            total += amount
            category = item["main_category"] or "未分类"
            category_totals[category] = category_totals.get(category, 0) + amount
            for tag in item["tags"]:
                tag_totals[tag] = tag_totals.get(tag, 0) + amount
        return {
            "currency": "CNY",
            "net_amount_cents": total,
            "net_amount": f"{total / 100:.2f}",
            "category_totals": category_totals,
            "tag_totals": tag_totals,
            "tag_totals_overlap": True,
            "warning": "标签为交叉维度，金额不可相加作为总支出",
            "transaction_count": len(items),
        }

    def export_portable(self) -> dict[str, Any]:
        current_dir = self.share_dir / "current"
        history_dir = self.share_dir / "history"
        current_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        history_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.TemporaryDirectory(dir=self.data_dir) as temporary:
            root = Path(temporary) / "ledger"
            root.mkdir(mode=0o700)
            snapshot = root / "bookkeeping.sqlite3"
            source = self._connect()
            target = sqlite3.connect(snapshot)
            try:
                source.backup(target)
            finally:
                target.close()
                source.close()
            os.chmod(snapshot, 0o600)
            items = self.query({"limit": 1000})
            summary = self.summary()
            (root / "ledger.json").write_text(canonical_json({"format_id": FORMAT_ID, "schema_version": SCHEMA_VERSION, "currency": "CNY", "transactions": items, "summary": summary}) + "\n")
            self._write_csv(root / "transactions.csv", items)
            self._write_audit(root / "audit_log.jsonl")
            (root / "schema.json").write_text(canonical_json({"format_id": FORMAT_ID, "schema_version": SCHEMA_VERSION, "amount_unit": "cents", "currency": "CNY"}) + "\n")
            (root / "FORMAT.md").write_text("# 装修账本便携格式\n\n格式：`kanhuwan-renovation-ledger@1`。金额使用整数分，币种为 CNY。\n")
            (root / "verify.py").write_text(PORTABLE_VERIFY_SCRIPT)
            attachments_root = root / "attachments"
            attachments_root.mkdir()
            with self._connect() as connection:
                rows = connection.execute("SELECT storage_name FROM attachments WHERE status='active' ORDER BY storage_name")
                for row in rows:
                    source_path = self.attachments_dir / row["storage_name"]
                    if source_path.is_file():
                        shutil.copy2(source_path, attachments_root / row["storage_name"])
            manifest_files = []
            for path in sorted(root.rglob("*")):
                if path.is_file() and path.name != "manifest.json":
                    manifest_files.append({"path": path.relative_to(root).as_posix(), "size_bytes": path.stat().st_size, "sha256": sha256_file(path)})
            manifest = {"format_id": FORMAT_ID, "schema_version": SCHEMA_VERSION, "created_at": utc_now(), "files": manifest_files}
            (root / "manifest.json").write_text(canonical_json(manifest) + "\n")
            temporary_zip = Path(temporary) / "ledger.zip"
            with zipfile.ZipFile(temporary_zip, "w", zipfile.ZIP_DEFLATED) as archive:
                for path in sorted(root.rglob("*")):
                    if path.is_file():
                        archive.write(path, path.relative_to(root).as_posix())
            self.verify_portable(temporary_zip)
            digest = sha256_file(temporary_zip)
            history_path = history_dir / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{digest[:16]}.zip"
            shutil.copy2(temporary_zip, history_path)
            current_path = current_dir / "kanhuwan-renovation-ledger.zip"
            current_temporary = current_dir / f".{uuid.uuid4().hex}.tmp"
            shutil.copy2(temporary_zip, current_temporary)
            os.replace(current_temporary, current_path)
            os.chmod(current_path, 0o600)
        history = sorted(history_dir.glob("*.zip"), key=lambda path: path.stat().st_mtime, reverse=True)
        for old in history[self.portable_history_limit :]:
            old.unlink()
        with self._connect() as connection:
            now = utc_now()
            connection.execute("UPDATE metadata SET value='current' WHERE key='portable_export_state'")
            connection.execute("UPDATE metadata SET value=? WHERE key='last_export_at'", (now,))
        return {"path": str(current_path), "sha256": digest, "size_bytes": current_path.stat().st_size}

    def _write_csv(self, path: Path, items: list[dict[str, Any]]) -> None:
        with path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=["id", "legacy_id", "type", "amount_cents", "occurred_on", "main_category", "merchant", "note", "is_deposit", "original_payment_id", "status", "tags"])
            writer.writeheader()
            for item in items:
                writer.writerow({key: ";".join(item[key]) if key == "tags" else item.get(key) for key in writer.fieldnames})

    def _write_audit(self, path: Path) -> None:
        with self._connect() as connection, path.open("w", encoding="utf-8") as handle:
            for row in connection.execute("SELECT * FROM audit_log ORDER BY id"):
                handle.write(canonical_json(dict(row)) + "\n")

    def _read_portable_manifest(self, zip_path: Path) -> dict[str, Any]:
        if not zip_path.is_file():
            raise LedgerError("import_invalid", "便携包不存在", status=404)
        if zip_path.stat().st_size > 512 * 1024 * 1024:
            raise LedgerError("import_invalid", "便携包超过大小上限")
        try:
            with zipfile.ZipFile(zip_path) as archive:
                matches = [info for info in archive.infolist() if info.filename == "manifest.json"]
                if len(matches) != 1 or matches[0].file_size > 1024 * 1024:
                    raise LedgerError("import_invalid", "便携包 manifest 缺失或过大")
                manifest = json.loads(archive.read(matches[0]))
        except LedgerError:
            raise
        except (OSError, RuntimeError, zipfile.BadZipFile, json.JSONDecodeError) as exc:
            raise LedgerError("import_invalid", "便携包 manifest 无法读取") from exc
        if not isinstance(manifest, dict):
            raise LedgerError("import_invalid", "便携包 manifest 不是对象")
        return manifest

    @staticmethod
    def _canonical_counts(state: dict[str, Any]) -> dict[str, int]:
        invariants = state["invariants"]
        return {
            "transactions": int(invariants["transaction_count"]),
            "active_payments": int(invariants["active_payment_count"]),
            "active_refunds": int(invariants["active_refund_count"]),
            "active_deposits": int(invariants["active_deposit_count"]),
            "void_transactions": int(invariants["void_transaction_count"]),
            "tag_links": int(invariants["transaction_tag_count"]),
            "attachments": int(invariants["attachment_count"]),
            "audit_events": int(invariants["audit_count"]),
            "months": len(state["monthly_summary"]),
        }

    def _verify_canonical_portable(self, zip_path: Path) -> dict[str, Any]:
        temporary = Path(tempfile.mkdtemp(prefix=".verify-", dir=self.shadow_dir))
        try:
            result = verify_and_extract_canonical(zip_path, temporary / "source")
        except PortableArchiveError as exc:
            raise LedgerError("import_invalid", str(exc)) from exc
        finally:
            shutil.rmtree(temporary, ignore_errors=True)
        state = result.pop("state")
        return {
            "valid": True,
            "format_id": result["format_id"],
            "format_version": result["format_version"],
            "sha256": result["archive_sha256"],
            "size_bytes": result["archive_size_bytes"],
            "verified_file_count": result["verified_file_count"],
            "attachments_included": result["attachments_included"],
            "counts": self._canonical_counts(state),
            "digests": result["digests"],
        }

    def _verify_legacy_portable(self, zip_path: Path) -> dict[str, Any]:
        try:
            with zipfile.ZipFile(zip_path) as archive:
                infos = archive.infolist()
                names = [info.filename for info in infos]
                if len(names) > 10000 or len(names) != len(set(names)):
                    raise LedgerError("import_invalid", "便携包文件数量或重复路径非法")
                expanded = 0
                for info in infos:
                    normalized_member_name(info.filename, allow_directory=info.is_dir())
                    mode = (info.external_attr >> 16) & 0xFFFF
                    if mode and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                        raise LedgerError("import_invalid", "便携包只允许普通文件和目录")
                    expanded += info.file_size
                if expanded > 1024 * 1024 * 1024:
                    raise LedgerError("import_invalid", "便携包解压后大小超过上限")
                required = {
                    "manifest.json",
                    "ledger.json",
                    "bookkeeping.sqlite3",
                    "schema.json",
                    "FORMAT.md",
                    "verify.py",
                }
                if not required.issubset(names):
                    raise LedgerError("import_invalid", "便携包缺少必需文件")
                manifest = json.loads(archive.read("manifest.json"))
                if manifest.get("format_id") != FORMAT_ID or manifest.get("schema_version") != SCHEMA_VERSION:
                    raise LedgerError("import_invalid", "便携包格式或版本不兼容")
                entries = manifest.get("files", [])
                if not isinstance(entries, list):
                    raise LedgerError("import_invalid", "manifest 文件清单无效")
                expected_paths = {entry.get("path") for entry in entries if isinstance(entry, dict)}
                file_names = {name for name in names if not name.endswith("/")}
                if expected_paths != file_names - {"manifest.json"}:
                    raise LedgerError("import_invalid", "manifest 文件集合不一致")
                for entry in entries:
                    if not isinstance(entry, dict):
                        raise LedgerError("import_invalid", "manifest 文件项无效")
                    data = archive.read(str(entry["path"]))
                    if len(data) != entry.get("size_bytes") or hashlib.sha256(data).hexdigest() != entry.get("sha256"):
                        raise LedgerError("import_invalid", "便携包文件校验失败")
                ledger = json.loads(archive.read("ledger.json"))
        except LedgerError:
            raise
        except PortableArchiveError as exc:
            raise LedgerError("import_invalid", str(exc)) from exc
        except (OSError, RuntimeError, zipfile.BadZipFile, json.JSONDecodeError) as exc:
            raise LedgerError("import_invalid", "便携包无法读取") from exc
        return {
            "valid": True,
            "format_id": FORMAT_ID,
            "sha256": sha256_file(zip_path),
            "transaction_count": len(ledger.get("transactions", [])),
        }

    def verify_portable(self, zip_path: str | Path) -> dict[str, Any]:
        path = Path(zip_path)
        manifest = self._read_portable_manifest(path)
        if (
            manifest.get("format_id") == CANONICAL_PORTABLE_FORMAT_ID
            and manifest.get("format_version") == CANONICAL_PORTABLE_FORMAT_VERSION
        ):
            return self._verify_canonical_portable(path)
        return self._verify_legacy_portable(path)

    def inspect_import(self, zip_path: str | Path) -> dict[str, Any]:
        path = Path(zip_path)
        verified = self.verify_portable(path)
        if verified.get("format_id") == CANONICAL_PORTABLE_FORMAT_ID:
            return verified
        with zipfile.ZipFile(path) as archive:
            ledger = json.loads(archive.read("ledger.json"))
        payments = [item for item in ledger.get("transactions", []) if item.get("type") == "payment"]
        refunds = [item for item in ledger.get("transactions", []) if item.get("type") == "refund"]
        return {**verified, "payments": len(payments), "refunds": len(refunds)}

    def import_shadow(self, zip_path: str | Path) -> dict[str, Any]:
        path = Path(zip_path)
        manifest = self._read_portable_manifest(path)
        if (
            manifest.get("format_id") == CANONICAL_PORTABLE_FORMAT_ID
            and manifest.get("format_version") == CANONICAL_PORTABLE_FORMAT_VERSION
        ):
            return self._import_canonical_shadow(path)
        return self._import_legacy_shadow(path)

    def _import_legacy_shadow(self, zip_path: Path) -> dict[str, Any]:
        inspected = self.inspect_import(zip_path)
        digest = inspected["sha256"]
        destination = self.shadow_dir / digest
        if destination.exists():
            report_path = destination / "report.json"
            if not report_path.is_file():
                raise LedgerError("invariant_mismatch", "既有影子目录不完整")
            return {**json.loads(report_path.read_text()), "idempotent_replay": True}
        destination.mkdir(parents=True, mode=0o700)
        with zipfile.ZipFile(zip_path) as archive:
            ledger = json.loads(archive.read("ledger.json"))
        shadow = LedgerStore(
            destination / "ledger.sqlite3",
            data_dir=destination,
            share_dir=destination / "portable",
        )
        shadow.set_writer_mode("read_only", force_initial=True)
        shadow.set_writer_mode("shadow_validated")
        with shadow._connect() as connection:
            connection.execute("DELETE FROM audit_log")
            connection.execute("DELETE FROM attachments")
            connection.execute("DELETE FROM transaction_tags")
            connection.execute("DELETE FROM transactions")
            connection.execute("DELETE FROM tags")
            id_map: dict[str, str] = {}
            for item in ledger.get("transactions", []):
                if item.get("type") != "payment":
                    continue
                transaction_id = str(item.get("id") or uuid.uuid4())
                id_map[str(item.get("id") or item.get("legacy_id"))] = transaction_id
                now = str(item.get("created_at") or utc_now())
                connection.execute(
                    "INSERT INTO transactions(id,legacy_id,type,amount_cents,occurred_on,main_category,merchant,note,is_deposit,status,void_reason,source_ref,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        transaction_id,
                        item.get("legacy_id"),
                        "payment",
                        _positive_cents(item.get("amount_cents")),
                        _validate_date(item.get("occurred_on")),
                        _text(item.get("main_category"), "main_category", 80, required=True),
                        _text(item.get("merchant"), "merchant", 200),
                        _text(item.get("note"), "note", 2000),
                        int(bool(item.get("is_deposit"))),
                        item.get("status", "active"),
                        _text(item.get("void_reason"), "void_reason", 500),
                        _text(item.get("source_ref"), "source_ref", 256),
                        now,
                        str(item.get("updated_at") or now),
                    ),
                )
                shadow._set_tags(connection, transaction_id, normalize_tags(item.get("tags", [])))
            for item in ledger.get("transactions", []):
                if item.get("type") != "refund":
                    continue
                transaction_id = str(item.get("id") or uuid.uuid4())
                source_original = str(
                    item.get("original_payment_id") or item.get("original_legacy_id") or ""
                )
                original = id_map.get(source_original, source_original)
                if not original:
                    raise LedgerError("invariant_mismatch", "退款缺少原付款关系")
                now = str(item.get("created_at") or utc_now())
                connection.execute(
                    "INSERT INTO transactions(id,legacy_id,type,amount_cents,occurred_on,merchant,note,is_deposit,original_payment_id,status,void_reason,source_ref,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        transaction_id,
                        item.get("legacy_id"),
                        "refund",
                        _positive_cents(item.get("amount_cents")),
                        _validate_date(item.get("occurred_on")),
                        _text(item.get("merchant"), "merchant", 200),
                        _text(item.get("note"), "note", 2000),
                        int(bool(item.get("is_deposit"))),
                        original,
                        item.get("status", "active"),
                        _text(item.get("void_reason"), "void_reason", 500),
                        _text(item.get("source_ref"), "source_ref", 256),
                        now,
                        str(item.get("updated_at") or now),
                    ),
                )
        if len(shadow.query({"limit": 1000})) != inspected["transaction_count"]:
            raise LedgerError("invariant_mismatch", "影子导入流水数量不一致")
        report = {
            "state": "shadow_validated",
            "format_id": FORMAT_ID,
            "source_sha256": digest,
            "counts": {
                "transactions": inspected["transaction_count"],
                "payments": inspected["payments"],
                "refunds": inspected["refunds"],
            },
            "storage": {"shadow_database": "ledger.sqlite3"},
            "idempotent_replay": False,
        }
        report_path = destination / "report.json"
        report_path.write_text(canonical_json({key: value for key, value in report.items() if key != "idempotent_replay"}) + "\n")
        os.chmod(report_path, 0o600)
        return report

    def _import_canonical_shadow(self, zip_path: Path) -> dict[str, Any]:
        temporary = Path(tempfile.mkdtemp(prefix=".canonical-import-", dir=self.shadow_dir))
        moved = False
        try:
            try:
                verified = verify_and_extract_canonical(zip_path, temporary / "source")
            except PortableArchiveError as exc:
                raise LedgerError("import_invalid", str(exc)) from exc
            if verified["attachments_included"] is not True:
                raise LedgerError("import_invalid", "只读影子导入要求便携包包含全部附件")
            state = verified["state"]
            digest = verified["archive_sha256"]
            destination = self.shadow_dir / digest
            if destination.exists():
                report = self._validate_existing_canonical_shadow(destination, verified)
                return {**report, "idempotent_replay": True}
            self._restore_canonical_shadow(temporary, state, digest)
            report = self._canonical_shadow_report(verified)
            report_path = temporary / "report.json"
            report_path.write_text(canonical_json(report) + "\n")
            os.chmod(report_path, 0o600)
            os.replace(temporary, destination)
            moved = True
            return {**report, "idempotent_replay": False}
        finally:
            if not moved:
                shutil.rmtree(temporary, ignore_errors=True)

    def _restore_canonical_shadow(
        self,
        destination: Path,
        state: dict[str, Any],
        source_sha256: str,
    ) -> None:
        shadow = LedgerStore(
            destination / "ledger.sqlite3",
            data_dir=destination,
            share_dir=destination / "portable",
        )
        shadow.set_writer_mode("read_only", force_initial=True)
        shadow.set_writer_mode("shadow_validated")
        transactions = state["transactions"]
        with shadow._connect() as connection:
            connection.execute("DELETE FROM audit_log")
            connection.execute("DELETE FROM attachments")
            connection.execute("DELETE FROM transaction_tags")
            connection.execute("DELETE FROM transactions")
            connection.execute("DELETE FROM tags")
            connection.execute("DELETE FROM idempotency_keys")
            for item in transactions:
                if item["kind"] != "payment":
                    continue
                identifier = str(item["id"])
                connection.execute(
                    "INSERT INTO transactions(id,legacy_id,type,amount_cents,occurred_on,main_category,merchant,note,is_deposit,status,void_reason,source_ref,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        identifier,
                        int(item["id"]),
                        "payment",
                        int(item["amount_cents"]),
                        str(item["date"]),
                        str(item["category"]),
                        str(item["vendor"]),
                        str(item["description"]),
                        int(bool(item["is_deposit"])),
                        "active" if item["status"] == "active" else "voided",
                        str(item["void_reason"]),
                        f"portable:{source_sha256[:16]}:{identifier}",
                        str(item["created_at"]),
                        str(item["updated_at"]),
                    ),
                )
                shadow._set_tags(connection, identifier, normalize_tags(item["tags"]))
            for item in transactions:
                if item["kind"] != "refund":
                    continue
                identifier = str(item["id"])
                original = str(item["payment_id"])
                connection.execute(
                    "INSERT INTO transactions(id,legacy_id,type,amount_cents,occurred_on,main_category,merchant,note,is_deposit,original_payment_id,status,void_reason,source_ref,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        identifier,
                        int(item["id"]),
                        "refund",
                        int(item["amount_cents"]),
                        str(item["date"]),
                        "",
                        str(item["vendor"]),
                        str(item["description"]),
                        int(bool(item["is_deposit"])),
                        original,
                        "active" if item["status"] == "active" else "voided",
                        str(item["void_reason"]),
                        f"portable:{source_sha256[:16]}:{identifier}",
                        str(item["created_at"]),
                        str(item["updated_at"]),
                    ),
                )
            for item in state["attachments"]:
                relative = normalized_member_name(str(item["relative_path"]))
                relative_path = PurePosixPath(relative)
                source = destination.joinpath("source", "attachments", *relative_path.parts)
                target = shadow.attachments_dir.joinpath(*relative_path.parts)
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                shutil.copyfile(source, target)
                os.chmod(target, 0o600)
                connection.execute(
                    "INSERT INTO attachments(id,transaction_id,storage_name,original_filename,mime_type,size_bytes,sha256,status,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        str(item["id"]),
                        str(item["transaction_id"]),
                        relative,
                        str(item["original_filename"]),
                        str(item["media_type"]),
                        int(item["size_bytes"]),
                        str(item["sha256"]),
                        "active",
                        str(item["created_at"]),
                    ),
                )
            for item in state["audit_log"]:
                connection.execute(
                    "INSERT INTO audit_log(id,action,target_type,target_id,actor_hash,idempotency_key,reason,before_json,after_json,result,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        int(item["id"]),
                        str(item["action"]),
                        "transaction",
                        str(item["transaction_id"]),
                        str(item["actor"]),
                        f"portable-audit-{item['id']}",
                        str(item["reason"]),
                        canonical_json(item["before"]) if item["before"] is not None else None,
                        canonical_json(item["after"]),
                        "success",
                        str(item["created_at"]),
                    ),
                )
            source_metadata = {
                "shadow_source_format_id": CANONICAL_PORTABLE_FORMAT_ID,
                "shadow_source_format_version": str(CANONICAL_PORTABLE_FORMAT_VERSION),
                "shadow_source_sha256": source_sha256,
                "shadow_invariants_sha256": portable_digest_json(state["invariants"]),
            }
            for key, value in source_metadata.items():
                connection.execute(
                    "INSERT INTO metadata(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, value),
                )
        self._validate_canonical_shadow(shadow, state, source_sha256)

    def _canonical_shadow_transactions(self, shadow: "LedgerStore") -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        with shadow._connect() as connection:
            rows = connection.execute("SELECT * FROM transactions ORDER BY legacy_id")
            for row in rows:
                item = shadow._row_json(connection, row)
                amount_cents = int(item["amount_cents"])
                result.append(
                    {
                        "id": int(item["legacy_id"]),
                        "kind": item["type"],
                        "payment_id": int(item["original_payment_id"])
                        if item["original_payment_id"] is not None
                        else None,
                        "amount_cents": amount_cents,
                        "amount": f"{amount_cents // 100}.{amount_cents % 100:02d}",
                        "date": item["occurred_on"],
                        "category": item["main_category"] if item["type"] == "payment" else None,
                        "effective_category": item["main_category"],
                        "vendor": item["merchant"],
                        "description": item["note"],
                        "is_deposit": bool(item["is_deposit"]),
                        "status": "active" if item["status"] == "active" else "void",
                        "void_reason": item["void_reason"],
                        "created_at": item["created_at"],
                        "updated_at": item["updated_at"],
                        "tags": item["tags"],
                    }
                )
        return result

    def _validate_canonical_shadow(
        self,
        shadow: "LedgerStore",
        state: dict[str, Any],
        source_sha256: str,
    ) -> None:
        transactions = self._canonical_shadow_transactions(shadow)
        if transactions != state["transactions"]:
            raise LedgerError("invariant_mismatch", "影子流水字段与来源不一致")
        with shadow._connect() as connection:
            source_refs = [
                tuple(row)
                for row in connection.execute(
                    "SELECT legacy_id,source_ref FROM transactions ORDER BY legacy_id"
                )
            ]
            expected_refs = [
                (int(item["id"]), f"portable:{source_sha256[:16]}:{item['id']}")
                for item in state["transactions"]
            ]
            if source_refs != expected_refs:
                raise LedgerError("invariant_mismatch", "影子 legacy ID 或 source_ref 不一致")
            tag_count = connection.execute("SELECT count(*) FROM transaction_tags").fetchone()[0]
            if tag_count != len(state["transaction_tags"]):
                raise LedgerError("invariant_mismatch", "影子标签关联数量不一致")
            attachments = [
                {
                    "id": int(row["id"]),
                    "transaction_id": int(row["transaction_id"]),
                    "original_filename": row["original_filename"],
                    "relative_path": row["storage_name"],
                    "sha256": row["sha256"],
                    "size_bytes": int(row["size_bytes"]),
                    "media_type": row["mime_type"],
                    "created_at": row["created_at"],
                }
                for row in connection.execute(
                    "SELECT * FROM attachments ORDER BY CAST(id AS INTEGER)"
                )
            ]
            if attachments != state["attachments"]:
                raise LedgerError("invariant_mismatch", "影子附件元数据不一致")
            for item in attachments:
                relative = normalized_member_name(item["relative_path"])
                path = shadow.attachments_dir.joinpath(*PurePosixPath(relative).parts)
                digest, size = portable_sha256_file(path)
                if digest != item["sha256"] or size != item["size_bytes"]:
                    raise LedgerError("invariant_mismatch", "影子附件文件校验失败")
            audits = []
            for row in connection.execute("SELECT * FROM audit_log ORDER BY id"):
                audits.append(
                    {
                        "id": int(row["id"]),
                        "transaction_id": int(row["target_id"]),
                        "action": row["action"],
                        "actor": row["actor_hash"],
                        "before": json.loads(row["before_json"]) if row["before_json"] else None,
                        "after": json.loads(row["after_json"]),
                        "reason": row["reason"],
                        "created_at": row["created_at"],
                    }
                )
            if audits != state["audit_log"]:
                raise LedgerError("invariant_mismatch", "影子审计顺序或前后值不一致")
        category_order = [item["category"] for item in state["summary"]["categories"]]
        summary = portable_summary_from_transactions(transactions, category_order)
        months = portable_monthly_summary(transactions)
        if summary != state["summary"] or months != state["monthly_summary"]:
            raise LedgerError("invariant_mismatch", "影子分类、标签或月份汇总不一致")

    def _canonical_shadow_report(self, verified: dict[str, Any]) -> dict[str, Any]:
        state = verified["state"]
        checks = {
            "archive_paths_safe": True,
            "manifest_files_match": True,
            "sqlite_integrity": True,
            "sqlite_foreign_keys": True,
            "json_matches_sqlite": True,
            "csv_matches_sqlite": True,
            "audit_matches_sqlite": True,
            "attachments_match": True,
            "normalized_shadow_matches": True,
            "source_snapshot_preserved": True,
        }
        return {
            "state": "shadow_validated",
            "format_id": verified["format_id"],
            "format_version": verified["format_version"],
            "source_sha256": verified["archive_sha256"],
            "source_size_bytes": verified["archive_size_bytes"],
            "verified_file_count": verified["verified_file_count"],
            "attachments_included": verified["attachments_included"],
            "counts": self._canonical_counts(state),
            "digests": verified["digests"],
            "checks": checks,
            "verification_digest": portable_digest_json(
                {
                    "source_sha256": verified["archive_sha256"],
                    "counts": self._canonical_counts(state),
                    "digests": verified["digests"],
                    "checks": checks,
                }
            ),
            "storage": {
                "source_snapshot": "source/bookkeeping.sqlite3",
                "shadow_database": "ledger.sqlite3",
                "attachments": "attachments/",
            },
        }

    def _validate_existing_canonical_shadow(
        self,
        destination: Path,
        input_verified: dict[str, Any],
    ) -> dict[str, Any]:
        report_path = destination / "report.json"
        if not report_path.is_file():
            raise LedgerError("invariant_mismatch", "既有影子目录不完整")
        try:
            persisted = verify_extracted_canonical(destination / "source")
        except PortableArchiveError as exc:
            raise LedgerError("invariant_mismatch", "既有来源快照校验失败") from exc
        if persisted["digests"] != input_verified["digests"]:
            raise LedgerError("invariant_mismatch", "既有来源快照与输入包不一致")
        persisted["archive_sha256"] = input_verified["archive_sha256"]
        persisted["archive_size_bytes"] = input_verified["archive_size_bytes"]
        shadow = LedgerStore(
            destination / "ledger.sqlite3",
            data_dir=destination,
            share_dir=destination / "portable",
        )
        self._validate_canonical_shadow(
            shadow,
            persisted["state"],
            input_verified["archive_sha256"],
        )
        expected = self._canonical_shadow_report(persisted)
        actual = json.loads(report_path.read_text())
        if actual != expected:
            raise LedgerError("invariant_mismatch", "既有影子脱敏报告与数据不一致")
        return actual

    def generate_chart(self, filters: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            from PIL import Image, ImageDraw, ImageFont
        except ImportError as exc:
            raise LedgerError("chart_unavailable", "镜像缺少 Pillow") from exc
        summary = self.summary(filters)
        image = Image.new("RGB", (1280, 960), "#0b1220")
        draw = ImageDraw.Draw(image)
        font_path = _find_cjk_font()
        title_font = ImageFont.truetype(font_path, 44) if font_path else ImageFont.load_default()
        body_font = ImageFont.truetype(font_path, 30) if font_path else ImageFont.load_default()
        small_font = ImageFont.truetype(font_path, 22) if font_path else ImageFont.load_default()
        draw.text((64, 48), "装修支出统计", fill="#f3f7ff", font=title_font)
        draw.text((64, 118), f"净支出：¥{summary['net_amount']}", fill="#42d392", font=body_font)
        totals = sorted(summary["category_totals"].items(), key=lambda item: abs(item[1]), reverse=True)[:10]
        maximum = max([abs(value) for _, value in totals] or [1])
        y = 200
        for name, cents in totals:
            width = int(800 * abs(cents) / maximum)
            draw.rounded_rectangle((320, y, 320 + width, y + 42), radius=8, fill="#42a5f5" if cents >= 0 else "#ff7373")
            draw.text((64, y + 4), name[:12], fill="#edf4ff", font=small_font)
            draw.text((1140, y + 4), f"¥{cents / 100:.2f}", anchor="ra", fill="#edf4ff", font=small_font)
            y += 62
        draw.text((64, 900), "标签为交叉维度，金额不可相加作为总支出", fill="#91a4bd", font=small_font)
        path = self.charts_dir / f"summary-{uuid.uuid4().hex}.png"
        image.save(path, format="PNG", optimize=True)
        os.chmod(path, 0o600)
        return {"download_ref": path.name, "width": 1280, "height": 960, "size_bytes": path.stat().st_size, "sha256": sha256_file(path), "summary": summary}


def _find_cjk_font() -> str | None:
    candidates = (
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Medium.ttc",
    )
    return next((path for path in candidates if Path(path).is_file()), None)


def _safe_filename(value: Any) -> str:
    name = _text(value, "original_filename", 255, required=True)
    name = Path(name).name.replace("\x00", "")
    if name in {".", "..", ""}:
        raise LedgerError("attachment_invalid", "附件文件名非法")
    return name


PORTABLE_VERIFY_SCRIPT = r'''#!/usr/bin/env python3
import hashlib, json, pathlib, sys, zipfile
p = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "kanhuwan-renovation-ledger.zip")
with zipfile.ZipFile(p) as z:
    names = z.namelist()
    if any(pathlib.PurePosixPath(n).is_absolute() or ".." in pathlib.PurePosixPath(n).parts for n in names):
        raise SystemExit("unsafe path")
    manifest = json.loads(z.read("manifest.json"))
    if manifest.get("format_id") != "kanhuwan-renovation-ledger@1":
        raise SystemExit("wrong format")
    for item in manifest["files"]:
        data = z.read(item["path"])
        if len(data) != item["size_bytes"] or hashlib.sha256(data).hexdigest() != item["sha256"]:
            raise SystemExit("checksum failed: " + item["path"])
print("OK")
'''
