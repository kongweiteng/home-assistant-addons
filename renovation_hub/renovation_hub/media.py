"""Streaming media persistence, metadata extraction and safe playback paths."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import sqlite3
import subprocess
from typing import Any
import uuid

from .ledger import HEX_64, LedgerError, _idempotency_key, _text, canonical_json, digest_json, utc_now


IMAGE_MIME_TYPES = {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif"}
VIDEO_MIME_TYPES = {"video/mp4", "video/quicktime", "video/webm"}
MEDIA_MIME_TYPES = IMAGE_MIME_TYPES | VIDEO_MIME_TYPES
LINK_TYPES = {"event", "transaction", "stage", "area"}
MEDIA_STATUSES = {"uploaded", "validating", "ready", "failed", "quarantined"}


def initialize_media_schema(store: Any) -> None:
    with store._connect() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS media_assets (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(id),
                media_type TEXT NOT NULL CHECK(media_type IN ('image','video')),
                mime_type TEXT NOT NULL,
                original_filename TEXT NOT NULL,
                storage_name TEXT NOT NULL,
                preview_name TEXT,
                size_bytes INTEGER NOT NULL CHECK(size_bytes > 0),
                sha256 TEXT NOT NULL,
                width INTEGER,
                height INTEGER,
                duration_ms INTEGER,
                captured_at TEXT,
                uploaded_at TEXT NOT NULL,
                source TEXT NOT NULL,
                source_ref_hash TEXT NOT NULL DEFAULT '',
                processing_status TEXT NOT NULL CHECK(processing_status IN ('uploaded','validating','ready','failed','quarantined')),
                error_code TEXT,
                version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS media_project_time ON media_assets(project_id, uploaded_at DESC);
            CREATE INDEX IF NOT EXISTS media_sha256 ON media_assets(sha256);
            CREATE TABLE IF NOT EXISTS media_links (
                media_id TEXT NOT NULL REFERENCES media_assets(id),
                target_type TEXT NOT NULL CHECK(target_type IN ('event','transaction','stage','area')),
                target_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(media_id, target_type, target_id)
            );
            CREATE TABLE IF NOT EXISTS uploads (
                id TEXT PRIMARY KEY,
                idempotency_key TEXT NOT NULL UNIQUE,
                source_ref_hash TEXT NOT NULL,
                original_filename TEXT NOT NULL,
                mime_type TEXT NOT NULL,
                expected_bytes INTEGER NOT NULL,
                received_bytes INTEGER NOT NULL DEFAULT 0,
                state TEXT NOT NULL CHECK(state IN ('created','uploading','uploaded','validating','ready','failed')),
                media_id TEXT REFERENCES media_assets(id),
                error_code TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS media_ingest_results (
                idempotency_key TEXT PRIMARY KEY,
                source_ref_hash TEXT NOT NULL,
                result_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        upload_columns = {row["name"] for row in connection.execute("PRAGMA table_info(uploads)")}
        for name, declaration in (
            ("expected_sha256", "TEXT"),
            ("received_sha256", "TEXT"),
            ("metadata_json", "TEXT"),
        ):
            if name not in upload_columns:
                connection.execute(f"ALTER TABLE uploads ADD COLUMN {name} {declaration}")
        connection.execute("INSERT OR IGNORE INTO metadata(key,value) VALUES ('media_schema_version','1')")


def _iso_datetime(value: Any, field: str = "captured_at") -> str | None:
    if value in {None, ""}:
        return None
    if not isinstance(value, str):
        raise LedgerError("invalid_datetime", f"{field} 必须是带时区 ISO 8601")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LedgerError("invalid_datetime", f"{field} 必须是带时区 ISO 8601") from exc
    if parsed.tzinfo is None:
        raise LedgerError("invalid_datetime", f"{field} 必须包含时区")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_filename(value: Any) -> str:
    filename = _text(value, "original_filename", 255, required=True)
    if Path(filename).name != filename or filename in {".", ".."} or "\x00" in filename:
        raise LedgerError("media_invalid", "媒体文件名无效")
    return filename


class MediaService:
    def __init__(
        self,
        store: Any,
        *,
        media_root: str | Path,
        preview_root: str | Path,
        staging_root: str | Path,
        max_media_bytes: int,
    ) -> None:
        self.store = store
        self.media_root = Path(media_root)
        self.preview_root = Path(preview_root)
        self.staging_root = Path(staging_root)
        self.max_media_bytes = max_media_bytes
        for path in (self.media_root, self.preview_root, self.staging_root):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)

    def prepare_upload(
        self,
        *,
        idempotency_key: Any,
        source_ref_hash: Any,
        original_filename: Any,
        mime_type: Any,
        expected_bytes: Any,
        resume_existing: bool = False,
        retry_failed: bool = False,
    ) -> dict[str, Any]:
        key = _idempotency_key(idempotency_key)
        ref_hash = _text(source_ref_hash, "source_ref_hash", 80, required=True)
        filename = _safe_filename(original_filename)
        mime = _text(mime_type, "mime_type", 120, required=True).lower()
        if mime not in MEDIA_MIME_TYPES:
            raise LedgerError("media_type_rejected", "媒体类型不在允许范围", status=415)
        if isinstance(expected_bytes, bool) or not isinstance(expected_bytes, int) or expected_bytes < 1 or expected_bytes > self.max_media_bytes:
            raise LedgerError("media_size_invalid", "媒体大小无效", status=413)
        with self.store._connect() as connection:
            existing = connection.execute("SELECT source_ref_hash,result_json FROM media_ingest_results WHERE idempotency_key=?", (key,)).fetchone()
            if existing:
                if existing["source_ref_hash"] != ref_hash:
                    raise LedgerError("idempotency_conflict", "同一幂等键对应不同媒体引用", status=409)
                return {"replay": True, "result": json.loads(existing["result_json"])}
            upload = connection.execute("SELECT * FROM uploads WHERE idempotency_key=?", (key,)).fetchone()
            if upload:
                if (
                    upload["source_ref_hash"] != ref_hash
                    or upload["original_filename"] != filename
                    or upload["mime_type"] != mime
                    or upload["expected_bytes"] != expected_bytes
                ):
                    raise LedgerError("idempotency_conflict", "同一幂等键对应不同媒体引用", status=409)
                if resume_existing and upload["state"] in {"created", "uploading", "uploaded"}:
                    return {
                        "replay": False,
                        "resumed": True,
                        "upload_id": upload["id"],
                        "path": self.staging_root / f"{upload['id']}.part",
                        "filename": filename,
                        "mime_type": mime,
                        "expected_bytes": expected_bytes,
                        "state": upload["state"],
                    }
                if retry_failed and upload["state"] == "failed":
                    path = self.staging_root / f"{upload['id']}.part"
                    path.unlink(missing_ok=True)
                    connection.execute(
                        "UPDATE uploads SET received_bytes=0,state='created',media_id=NULL,error_code=NULL,updated_at=? WHERE id=?",
                        (utc_now(), upload["id"]),
                    )
                    return {
                        "replay": False,
                        "resumed": True,
                        "upload_id": upload["id"],
                        "path": path,
                        "filename": filename,
                        "mime_type": mime,
                        "expected_bytes": expected_bytes,
                        "state": "created",
                    }
                raise LedgerError("upload_in_progress", "媒体上传仍在处理中", status=409)
            upload_id = str(uuid.uuid4())
            now = utc_now()
            connection.execute(
                "INSERT INTO uploads(id,idempotency_key,source_ref_hash,original_filename,mime_type,expected_bytes,state,created_at,updated_at) VALUES (?,?,?,?,?,?,'created',?,?)",
                (upload_id, key, ref_hash, filename, mime, expected_bytes, now, now),
            )
        return {"replay": False, "resumed": False, "upload_id": upload_id, "path": self.staging_root / f"{upload_id}.part", "filename": filename, "mime_type": mime, "expected_bytes": expected_bytes, "state": "created"}

    def create_browser_upload(self, payload: dict[str, Any]) -> dict[str, Any]:
        key = _idempotency_key(payload.get("idempotency_key"))
        project_id = _text(payload.get("project_id"), "project_id", 64, required=True)
        filename = _safe_filename(payload.get("original_filename"))
        mime_type = _text(payload.get("mime_type"), "mime_type", 120, required=True).lower()
        expected_bytes = payload.get("size_bytes")
        expected_sha256 = _text(payload.get("sha256"), "sha256", 64, required=True).lower()
        if not HEX_64.fullmatch(expected_sha256):
            raise LedgerError("media_invalid", "媒体摘要无效")
        links = self._clean_links(payload.get("links", []))
        metadata = {
            "idempotency_key": key,
            "project_id": project_id,
            "source": "page",
            "captured_at": _iso_datetime(payload.get("captured_at")),
            "links": links,
        }
        source_ref_hash = digest_json(
            {
                "project_id": project_id,
                "original_filename": filename,
                "mime_type": mime_type,
                "size_bytes": expected_bytes,
                "sha256": expected_sha256,
                "captured_at": metadata["captured_at"],
                "links": links,
            }
        )
        metadata["source_ref_hash"] = source_ref_hash
        with self.store._connect() as connection:
            self.store._validate_context_refs(connection, project_id, None, None)
            self._validate_links(connection, project_id, links)
        prepared = self.prepare_upload(
            idempotency_key=key,
            source_ref_hash=source_ref_hash,
            original_filename=filename,
            mime_type=mime_type,
            expected_bytes=expected_bytes,
            resume_existing=True,
        )
        if prepared["replay"]:
            return {"completed": True, "result": prepared["result"]}
        with self.store._connect() as connection:
            row = connection.execute("SELECT expected_sha256,metadata_json FROM uploads WHERE id=?", (prepared["upload_id"],)).fetchone()
            metadata_json = canonical_json(metadata)
            if prepared["resumed"] and row and (
                row["expected_sha256"] not in {None, expected_sha256}
                or row["metadata_json"] not in {None, metadata_json}
            ):
                raise LedgerError("idempotency_conflict", "同一幂等键对应不同上传元数据", status=409)
            connection.execute(
                "UPDATE uploads SET expected_sha256=?,metadata_json=?,updated_at=? WHERE id=?",
                (expected_sha256, metadata_json, utc_now(), prepared["upload_id"]),
            )
        return {
            "completed": False,
            "upload_id": prepared["upload_id"],
            "state": prepared["state"],
            "content_url": f"/api/v1/uploads/{prepared['upload_id']}/content",
            "complete_url": f"/api/v1/uploads/{prepared['upload_id']}/complete",
        }

    def browser_upload(self, upload_id: str) -> dict[str, Any]:
        upload_id = _text(upload_id, "upload_id", 64, required=True)
        with self.store._connect() as connection:
            row = connection.execute("SELECT * FROM uploads WHERE id=?", (upload_id,)).fetchone()
        if row is None or not row["metadata_json"]:
            raise LedgerError("upload_not_found", "上传会话不存在", status=404)
        result = dict(row)
        result["metadata"] = json.loads(result.pop("metadata_json"))
        result["path"] = self.staging_root / f"{upload_id}.part"
        return result

    def mark_browser_uploaded(self, upload_id: str, *, received_bytes: int, received_sha256: str) -> None:
        with self.store._connect() as connection:
            cursor = connection.execute(
                "UPDATE uploads SET received_bytes=?,received_sha256=?,state='uploaded',error_code=NULL,updated_at=? WHERE id=? AND state IN ('created','uploading')",
                (received_bytes, received_sha256, utc_now(), upload_id),
            )
        if cursor.rowcount != 1:
            raise LedgerError("upload_state_conflict", "上传会话状态已变化", status=409)

    def complete_browser_upload(self, upload_id: str, *, actor_hash: str) -> dict[str, Any]:
        upload = self.browser_upload(upload_id)
        if upload["state"] == "ready" and upload.get("media_id"):
            replay = self.replay(upload["idempotency_key"], upload["source_ref_hash"])
            if replay is not None:
                return replay
        if upload["state"] != "uploaded":
            raise LedgerError("upload_incomplete", "媒体正文尚未完整上传", status=409)
        prepared = {
            "upload_id": upload["id"],
            "path": upload["path"],
            "filename": upload["original_filename"],
            "mime_type": upload["mime_type"],
            "expected_bytes": upload["expected_bytes"],
        }
        return self.finalize_upload(
            prepared,
            received_bytes=upload["received_bytes"],
            sha256=upload["received_sha256"],
            expected_sha256=upload["expected_sha256"],
            metadata=upload["metadata"],
            actor_hash=actor_hash,
        )

    def mark_uploading(self, upload_id: str) -> None:
        with self.store._connect() as connection:
            connection.execute("UPDATE uploads SET state='uploading',updated_at=? WHERE id=?", (utc_now(), upload_id))

    def fail_upload(self, upload_id: str, code: str) -> None:
        with self.store._connect() as connection:
            connection.execute("UPDATE uploads SET state='failed',error_code=?,updated_at=? WHERE id=?", (code[:80], utc_now(), upload_id))
        (self.staging_root / f"{upload_id}.part").unlink(missing_ok=True)

    def finalize_upload(
        self,
        upload: dict[str, Any],
        *,
        received_bytes: int,
        sha256: str,
        expected_sha256: str,
        metadata: dict[str, Any],
        actor_hash: str,
    ) -> dict[str, Any]:
        upload_id = upload["upload_id"]
        staging_path = Path(upload["path"])
        if received_bytes != upload["expected_bytes"] or not staging_path.is_file():
            self.fail_upload(upload_id, "upload_incomplete")
            raise LedgerError("upload_incomplete", "媒体上传不完整", status=400)
        if expected_sha256 and expected_sha256 != sha256:
            self.fail_upload(upload_id, "sha256_mismatch")
            raise LedgerError("sha256_mismatch", "媒体摘要不一致", status=400)
        project_id = _text(metadata.get("project_id"), "project_id", 64, required=True)
        source = _text(metadata.get("source", "weixin"), "source", 40, required=True)
        captured_at = _iso_datetime(metadata.get("captured_at"))
        links = self._clean_links(metadata.get("links", []))
        media_type = "image" if upload["mime_type"] in IMAGE_MIME_TYPES else "video"
        extension = self._extension(upload["mime_type"], upload["filename"])
        storage_name = f"{sha256[:2]}/{sha256}{extension}"
        final_path = self.media_root / storage_name
        final_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        created_file = False
        if final_path.exists():
            if final_path.stat().st_size != received_bytes:
                self.fail_upload(upload_id, "content_address_conflict")
                raise LedgerError("content_address_conflict", "内容寻址文件冲突", status=409)
            staging_path.unlink(missing_ok=True)
        else:
            os.replace(staging_path, final_path)
            os.chmod(final_path, 0o600)
            created_file = True
        processing = self._process(final_path, media_type, upload["mime_type"], sha256)
        object_id = str(uuid.uuid4())
        now = utc_now()
        connection = self.store._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self.store._require_writer(connection)
            self.store._validate_context_refs(connection, project_id, None, None)
            self._validate_links(connection, project_id, links)
            connection.execute(
                "INSERT INTO media_assets(id,project_id,media_type,mime_type,original_filename,storage_name,preview_name,size_bytes,sha256,width,height,duration_ms,captured_at,uploaded_at,source,source_ref_hash,processing_status,error_code,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (object_id, project_id, media_type, upload["mime_type"], upload["filename"], storage_name, processing.get("preview_name"), received_bytes, sha256, processing.get("width"), processing.get("height"), processing.get("duration_ms"), captured_at, now, source, metadata.get("source_ref_hash", ""), processing["status"], processing.get("error_code"), now, now),
            )
            for link in links:
                connection.execute("INSERT INTO media_links(media_id,target_type,target_id,created_at) VALUES (?,?,?,?)", (object_id, link["target_type"], link["target_id"], now))
            capture_session_id = metadata.get("capture_session_id")
            capture_item_id = metadata.get("capture_item_id")
            if capture_session_id is not None or capture_item_id is not None:
                if not isinstance(capture_session_id, str) or not isinstance(capture_item_id, str):
                    raise LedgerError("capture_item_invalid", "采集媒体元数据不完整")
                event_links = [link["target_id"] for link in links if link["target_type"] == "event"]
                if len(event_links) != 1:
                    raise LedgerError("capture_item_invalid", "采集媒体必须关联唯一进度事件")
                from .progress_capture import complete_capture_item

                complete_capture_item(
                    connection,
                    session_id=capture_session_id,
                    item_id=capture_item_id,
                    project_id=project_id,
                    event_id=event_links[0],
                    source_ref_hash=str(metadata.get("source_ref_hash") or ""),
                    media_id=object_id,
                    processing_status=processing["status"],
                    error_code=processing.get("error_code"),
                )
            asset = self._asset(connection, object_id)
            result = {"media": asset, "idempotent_replay": False}
            self.store._domain_audit(connection, action="ingest_media", target_type="media", target_id=object_id, actor_hash=actor_hash, idempotency_key=metadata["idempotency_key"], before=None, after=asset)
            connection.execute("UPDATE uploads SET received_bytes=?,state='ready',media_id=?,updated_at=? WHERE id=?", (received_bytes, object_id, now, upload_id))
            connection.execute("INSERT INTO media_ingest_results(idempotency_key,source_ref_hash,result_json,created_at) VALUES (?,?,?,?)", (metadata["idempotency_key"], metadata["source_ref_hash"], canonical_json(result), now))
            connection.execute("UPDATE metadata SET value=? WHERE key='last_write_at'", (now,))
            connection.commit()
            return result
        except Exception:
            connection.rollback()
            if created_file:
                with self.store._connect() as check:
                    referenced = check.execute("SELECT 1 FROM media_assets WHERE storage_name=?", (storage_name,)).fetchone()
                if not referenced:
                    final_path.unlink(missing_ok=True)
            raise
        finally:
            connection.close()

    def reprocess(self, media_id: str) -> dict[str, Any]:
        """Retry metadata/preview processing without replacing the durable original."""

        media_id = _text(media_id, "media_id", 64, required=True)
        with self.store._connect() as connection:
            row = connection.execute("SELECT * FROM media_assets WHERE id=?", (media_id,)).fetchone()
            if row is None:
                raise LedgerError("media_not_found", "媒体不存在", status=404)
            asset = dict(row)
        path = self.media_root / asset["storage_name"]
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(self.media_root.resolve())
        except (FileNotFoundError, ValueError) as exc:
            raise LedgerError("media_missing", "媒体原件缺失", status=404) from exc
        processing = self._process(resolved, asset["media_type"], asset["mime_type"], asset["sha256"])
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self.store._require_writer(connection)
            now = utc_now()
            connection.execute(
                "UPDATE media_assets SET preview_name=?,width=?,height=?,duration_ms=?,processing_status=?,error_code=?,version=version+1,updated_at=? WHERE id=?",
                (
                    processing.get("preview_name"),
                    processing.get("width"),
                    processing.get("height"),
                    processing.get("duration_ms"),
                    processing["status"],
                    processing.get("error_code"),
                    now,
                    media_id,
                ),
            )
            item_state = "stored" if processing["status"] == "ready" else "failed"
            connection.execute(
                "UPDATE progress_capture_items SET state=?,error_code=?,attempts=attempts+1,updated_at=? WHERE media_id=?",
                (
                    item_state,
                    None if item_state == "stored" else (processing.get("error_code") or "media_processing_failed"),
                    now,
                    media_id,
                ),
            )
            return self._asset(connection, media_id)

    def replay(self, idempotency_key: str, source_ref_hash: str) -> dict[str, Any] | None:
        key = _idempotency_key(idempotency_key)
        ref_hash = _text(source_ref_hash, "source_ref_hash", 80, required=True)
        with self.store._connect() as connection:
            row = connection.execute("SELECT source_ref_hash,result_json FROM media_ingest_results WHERE idempotency_key=?", (key,)).fetchone()
        if not row:
            return None
        if row["source_ref_hash"] != ref_hash:
            raise LedgerError("idempotency_conflict", "同一幂等键对应不同媒体引用", status=409)
        result = json.loads(row["result_json"])
        result["idempotent_replay"] = True
        return result

    def list(self, filters: dict[str, Any]) -> list[dict[str, Any]]:
        project_id = _text(filters.get("project_id"), "project_id", 64, required=True)
        clauses = ["media_assets.project_id=?"]
        values: list[Any] = [project_id]
        for field, allowed in (("media_type", {"image", "video"}), ("processing_status", MEDIA_STATUSES)):
            value = filters.get(field)
            if value:
                if value not in allowed:
                    raise LedgerError("invalid_input", f"{field} 无效")
                clauses.append(f"media_assets.{field}=?")
                values.append(value)
        for target_type in ("stage", "area", "event", "transaction"):
            value = filters.get(f"{target_type}_id")
            if value:
                clauses.append("EXISTS (SELECT 1 FROM media_links ml WHERE ml.media_id=media_assets.id AND ml.target_type=? AND ml.target_id=?)")
                values.extend([target_type, value])
        if filters.get("start"):
            clauses.append("COALESCE(media_assets.captured_at,media_assets.uploaded_at)>=?")
            values.append(_iso_datetime(filters["start"], "start"))
        if filters.get("end"):
            clauses.append("COALESCE(media_assets.captured_at,media_assets.uploaded_at)<=?")
            values.append(_iso_datetime(filters["end"], "end"))
        keyword = _text(filters.get("keyword"), "keyword", 100)
        if keyword:
            escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{escaped}%"
            clauses.append(
                "(media_assets.original_filename LIKE ? ESCAPE '\\' "
                "OR media_assets.media_type LIKE ? ESCAPE '\\' "
                "OR media_assets.mime_type LIKE ? ESCAPE '\\' "
                "OR media_assets.source LIKE ? ESCAPE '\\')"
            )
            values.extend([pattern, pattern, pattern, pattern])
        limit = filters.get("limit", 200)
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > 1000:
            raise LedgerError("invalid_input", "limit 必须为 1 到 1000")
        values.append(limit)
        sql = f"SELECT * FROM media_assets WHERE {' AND '.join(clauses)} ORDER BY uploaded_at DESC,id DESC LIMIT ?"
        with self.store._connect() as connection:
            items = [self._asset(connection, row["id"]) for row in connection.execute(sql, values)]
        return items

    def get(self, media_id: str) -> dict[str, Any]:
        media_id = _text(media_id, "media_id", 64, required=True)
        with self.store._connect() as connection:
            return self._asset(connection, media_id)

    def content_path(self, media_id: str, *, preview: bool = False) -> tuple[Path, str]:
        asset = self.get(media_id)
        if asset["processing_status"] != "ready":
            raise LedgerError("media_not_ready", "媒体尚未就绪", status=409)
        if preview:
            if not asset["preview_name"]:
                raise LedgerError("preview_unavailable", "预览不可用", status=404)
            path = self.preview_root / asset["preview_name"]
            mime = "image/webp"
        else:
            path = self.media_root / asset["storage_name"]
            mime = asset["mime_type"]
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to((self.preview_root if preview else self.media_root).resolve())
        except (FileNotFoundError, ValueError) as exc:
            raise LedgerError("media_missing", "媒体文件缺失", status=404) from exc
        return resolved, mime

    @staticmethod
    def _clean_links(value: Any) -> list[dict[str, str]]:
        if value is None:
            return []
        if not isinstance(value, list) or len(value) > 16:
            raise LedgerError("invalid_input", "links 必须是最多 16 项")
        result: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for item in value:
            if not isinstance(item, dict) or item.get("target_type") not in LINK_TYPES:
                raise LedgerError("invalid_input", "媒体关联类型无效")
            target_id = _text(item.get("target_id"), "target_id", 64, required=True)
            pair = (item["target_type"], target_id)
            if pair not in seen:
                result.append({"target_type": pair[0], "target_id": pair[1]})
                seen.add(pair)
        return result

    @staticmethod
    def _validate_links(connection: sqlite3.Connection, project_id: str, links: list[dict[str, str]]) -> None:
        tables = {"event": "events", "transaction": "transactions", "stage": "stages", "area": "areas"}
        for link in links:
            table = tables[link["target_type"]]
            if table == "transactions":
                row = connection.execute(
                    "SELECT 1 FROM transactions JOIN transaction_context ON transaction_context.transaction_id=transactions.id WHERE transactions.id=? AND transaction_context.project_id=?",
                    (link["target_id"], project_id),
                ).fetchone()
            else:
                row = connection.execute(f"SELECT 1 FROM {table} WHERE id=? AND project_id=?", (link["target_id"], project_id)).fetchone()
            if row is None:
                raise LedgerError("media_link_invalid", "媒体关联对象不存在或不属于项目", status=404)

    @staticmethod
    def _asset(connection: sqlite3.Connection, media_id: str) -> dict[str, Any]:
        row = connection.execute("SELECT * FROM media_assets WHERE id=?", (media_id,)).fetchone()
        if row is None:
            raise LedgerError("media_not_found", "媒体不存在", status=404)
        result = dict(row)
        result["links"] = [dict(item) for item in connection.execute("SELECT target_type,target_id FROM media_links WHERE media_id=? ORDER BY target_type,target_id", (media_id,))]
        result["content_url"] = f"/api/v1/media/{media_id}/content"
        result["preview_url"] = f"/api/v1/media/{media_id}/preview" if result["preview_name"] else None
        return result

    def _process(self, path: Path, media_type: str, mime_type: str, sha256: str) -> dict[str, Any]:
        if media_type == "image":
            return self._process_image(path, mime_type, sha256)
        return self._process_video(path, sha256)

    def _process_image(self, path: Path, mime_type: str, sha256: str) -> dict[str, Any]:
        try:
            from PIL import Image, ImageOps

            with Image.open(path) as image:
                image = ImageOps.exif_transpose(image)
                width, height = image.size
                image.thumbnail((1600, 1600))
                preview_name = f"{sha256}.webp"
                preview_path = self.preview_root / preview_name
                image.convert("RGB").save(preview_path, "WEBP", quality=84, method=6)
                os.chmod(preview_path, 0o600)
            return {"status": "ready", "width": width, "height": height, "preview_name": preview_name}
        except Exception:
            if mime_type in {"image/heic", "image/heif"}:
                return {"status": "ready", "error_code": "preview_unavailable"}
            return {"status": "failed", "error_code": "image_decode_failed"}

    def _process_video(self, path: Path, sha256: str) -> dict[str, Any]:
        try:
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
            )
            document = json.loads(probe.stdout)
            video = next(stream for stream in document.get("streams", []) if stream.get("codec_type") == "video")
            width = int(video.get("width") or 0) or None
            height = int(video.get("height") or 0) or None
            duration = video.get("duration") or document.get("format", {}).get("duration")
            duration_ms = int(float(duration) * 1000) if duration is not None else None
            preview_name = f"{sha256}.webp"
            preview_path = self.preview_root / preview_name
            subprocess.run(
                ["ffmpeg", "-v", "error", "-ss", "0.2", "-i", str(path), "-frames:v", "1", "-vf", "scale='min(1280,iw)':-2", "-y", str(preview_path)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=60,
            )
            os.chmod(preview_path, 0o600)
            return {"status": "ready", "width": width, "height": height, "duration_ms": duration_ms, "preview_name": preview_name}
        except Exception:
            return {"status": "failed", "error_code": "video_probe_failed"}

    @staticmethod
    def _extension(mime_type: str, filename: str) -> str:
        explicit = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/heic": ".heic", "image/heif": ".heif", "video/mp4": ".mp4", "video/quicktime": ".mov", "video/webm": ".webm"}
        return explicit.get(mime_type) or Path(filename).suffix.lower() or mimetypes.guess_extension(mime_type) or ".bin"
