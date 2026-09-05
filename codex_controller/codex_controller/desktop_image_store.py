"""Authenticated Controller image staging; opaque IDs, bounded capacity and TTL."""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import sqlite3
import threading
import uuid
from contextlib import contextmanager

from .desktop_images import IMAGE_REF_RE, MAX_IMAGES, decode_image
from .store import StoreError


class DesktopImageStore:
    def __init__(self, database_path):
        self.database_path = database_path
        self.lock = threading.Lock()
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS desktop_images (image_ref TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL, host_ref TEXT NOT NULL, mime_type TEXT NOT NULL, body BLOB NOT NULL, digest TEXT NOT NULL, expires_at TEXT NOT NULL)")

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.database_path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def upload(self, payload, *, now):
        try:
            body = decode_image(payload.get("mime_type"), payload.get("data_base64"))
        except (ValueError, TypeError) as exc:
            raise StoreError("desktop_image_invalid", "图片须为有效的 PNG、JPEG 或 WebP，且不超过 64 KiB", status=400) from exc
        digest = hashlib.sha256(body).hexdigest()
        with self.lock, self._connect() as db:
            db.execute("DELETE FROM desktop_images WHERE expires_at<=?", (now.isoformat(),))
            row = db.execute("SELECT * FROM desktop_images WHERE request_id=?", (payload["request_id"],)).fetchone()
            if row is not None:
                if row["digest"] != digest or row["host_ref"] != payload["host_ref"] or row["mime_type"] != payload["mime_type"]:
                    raise StoreError("desktop_image_request_conflict", "同一上传请求的图片已变化", status=409)
                return self._public(row)
            if db.execute("SELECT COUNT(*) FROM desktop_images").fetchone()[0] >= 1024:
                raise StoreError("desktop_image_capacity", "图片暂存空间已满，请稍后再试", status=507)
            image_ref = "IM-" + uuid.uuid4().hex
            expires_at = (now + dt.timedelta(hours=24)).isoformat()
            db.execute("INSERT INTO desktop_images VALUES(?,?,?,?,?,?,?)", (image_ref, payload["request_id"], payload["host_ref"], payload["mime_type"], body, digest, expires_at))
            return {"image_ref": image_ref, "mime_type": payload["mime_type"], "byte_size": len(body), "expires_at": expires_at}

    def get(self, image_ref, *, now, host_ref=None):
        if not isinstance(image_ref, str) or not IMAGE_REF_RE.fullmatch(image_ref):
            raise StoreError("desktop_image_ref_invalid", "图片引用无效", status=400)
        with self._connect() as db:
            row = db.execute("SELECT * FROM desktop_images WHERE image_ref=? AND expires_at>?", (image_ref, now.isoformat())).fetchone()
        if row is None or (host_ref is not None and row["host_ref"] != host_ref):
            raise StoreError("desktop_image_unavailable", "图片已过期或不属于当前主机，请重新添加", status=404)
        return {**self._public(row), "data_base64": base64.b64encode(row["body"]).decode("ascii")}

    def sweep(self, *, now):
        with self.lock, self._connect() as db:
            return db.execute("DELETE FROM desktop_images WHERE expires_at<=?", (now.isoformat(),)).rowcount

    def resolve(self, refs, *, host_ref, now):
        if not isinstance(refs, list) or not 1 <= len(refs) <= MAX_IMAGES or any(not isinstance(ref, str) for ref in refs) or len(set(refs)) != len(refs):
            raise StoreError("desktop_image_refs_invalid", "每条消息最多添加 4 张不同图片", status=400)
        return [
            {key: item[key] for key in ("image_ref", "mime_type", "data_base64")}
            for item in (self.get(ref, now=now, host_ref=host_ref) for ref in refs)
        ]

    @staticmethod
    def _public(row):
        return {"image_ref": row["image_ref"], "mime_type": row["mime_type"], "byte_size": len(row["body"]), "expires_at": row["expires_at"]}


def journal_command(command):
    """Keep replay digests and metadata, never duplicate image bytes in journals."""
    result = dict(command)
    images = result.pop("images", None)
    if images:
        result["image_refs"] = [image["image_ref"] for image in images]
    return result
