"""Bounded attachment transport; models select scoped references, never bytes or paths."""
from __future__ import annotations

import base64
import hashlib
import re
from pathlib import PurePosixPath
from typing import Any

MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_TOTAL_BYTES = 10 * 1024 * 1024
MAX_ATTACHMENTS = 10
MIME_SUFFIXES = {
    "application/pdf": {".pdf"}, "application/zip": {".zip"},
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": {".xlsx"},
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": {".docx"},
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": {".pptx"},
    "text/plain": {".txt"}, "text/csv": {".csv"},
    "image/png": {".png"}, "image/jpeg": {".jpg", ".jpeg"}, "image/webp": {".webp"},
}


def validate_file(filename: Any, mime_type: Any, content: bytes) -> None:
    if (not isinstance(filename, str) or not 1 <= len(filename) <= 255
            or any(ord(c) < 32 or ord(c) == 127 for c in filename)
            or "/" in filename or "\\" in filename or filename in {".", ".."}
            or mime_type not in MIME_SUFFIXES
            or PurePosixPath(filename).suffix.lower() not in MIME_SUFFIXES[mime_type]
            or not isinstance(content, bytes) or not 1 <= len(content) <= MAX_FILE_BYTES):
        raise ValueError("attachment_invalid")
    signatures = {"application/pdf": (b"%PDF-",), "application/zip": (b"PK\x03\x04", b"PK\x05\x06"),
                  "image/png": (b"\x89PNG\r\n\x1a\n",), "image/jpeg": (b"\xff\xd8",),
                  "image/webp": (b"RIFF",)}
    prefixes = signatures.get(mime_type, (b"PK\x03\x04",) if "openxmlformats" in mime_type else ())
    if prefixes and not content.startswith(prefixes):
        raise ValueError("attachment_content_invalid")


def encode_file(metadata: dict[str, Any], content: bytes) -> dict[str, Any]:
    filename = metadata.get("filename", metadata.get("original_filename"))
    mime_type = metadata.get("mime_type")
    validate_file(filename, mime_type, content)
    digest = "sha256:" + hashlib.sha256(content).hexdigest()
    if metadata.get("sha256") != digest or metadata.get("size_bytes") != len(content):
        raise ValueError("attachment_integrity_invalid")
    return {"filename": filename, "mime_type": mime_type, "size_bytes": len(content),
            "sha256": digest, "content_base64": base64.b64encode(content).decode("ascii")}


def decode_files(value: Any) -> list[tuple[dict[str, Any], bytes]]:
    if not isinstance(value, list) or len(value) > MAX_ATTACHMENTS:
        raise ValueError("attachments_invalid")
    total = 0
    result = []
    for item in value:
        if (not isinstance(item, dict)
                or set(item) != {"filename", "mime_type", "size_bytes", "sha256", "content_base64"}
                or not isinstance(item["content_base64"], str)
                or len(item["content_base64"]) > 4 * ((MAX_FILE_BYTES + 2) // 3)):
            raise ValueError("attachments_invalid")
        content = base64.b64decode(item["content_base64"], validate=True)
        normalized = encode_file(item, content)
        total += len(content)
        if total > MAX_TOTAL_BYTES:
            raise ValueError("attachments_too_large")
        result.append(({k: v for k, v in normalized.items() if k != "content_base64"}, content))
    return result
