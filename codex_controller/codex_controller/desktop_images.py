"""Bounded raster inputs. No filesystem paths or remote URLs are accepted."""
from __future__ import annotations

import base64
import binascii
import re
import struct
import zlib
from typing import Any

MAX_IMAGE_BYTES = 64 * 1024
MAX_TOTAL_IMAGE_BYTES = 256 * 1024
MAX_IMAGES = 4
IMAGE_REF_RE = re.compile(r"^IM-[a-f0-9]{32}$")
MIME_TYPES = {"image/png", "image/jpeg", "image/webp"}


def decode_image(mime_type: Any, encoded: Any) -> bytes:
    if mime_type not in MIME_TYPES or not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_IMAGE_BYTES + 2) // 3):
        raise ValueError("image type or size is invalid")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("image encoding is invalid") from exc
    if not data or len(data) > MAX_IMAGE_BYTES or base64.b64encode(data).decode("ascii") != encoded:
        raise ValueError("image size or encoding is invalid")
    width = height = 0
    if mime_type == "image/png" and len(data) >= 45 and data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR" and data[-12:] == b"\x00\x00\x00\x00IEND\xaeB\x60\x82":
        width, height = struct.unpack(">II", data[16:24])
        cursor = 8
        has_pixels = False
        while cursor + 12 <= len(data):
            size = int.from_bytes(data[cursor:cursor + 4], "big")
            end = cursor + 12 + size
            if end > len(data):
                raise ValueError("PNG chunk is truncated")
            kind = data[cursor + 4:cursor + 8]
            content = data[cursor + 4:cursor + 8 + size]
            checksum = int.from_bytes(data[cursor + 8 + size:end], "big")
            if checksum != zlib.crc32(content):
                raise ValueError("PNG checksum is invalid")
            if cursor == 8 and (kind != b"IHDR" or size != 13):
                raise ValueError("PNG header is invalid")
            if kind == b"IDAT" and size:
                has_pixels = True
            if kind == b"acTL":
                raise ValueError("animated PNG input is unsupported")
            if kind == b"IEND" and (size or end != len(data)):
                raise ValueError("PNG end is invalid")
            cursor = end
        if cursor != len(data) or not has_pixels:
            raise ValueError("PNG pixels are missing")
    elif mime_type == "image/jpeg" and data[:2] == b"\xff\xd8" and data[-2:] == b"\xff\xd9":
        cursor = 2
        while cursor + 4 <= len(data):
            if data[cursor] != 255:
                break
            while cursor < len(data) and data[cursor] == 255:
                cursor += 1
            if cursor >= len(data):
                break
            marker = data[cursor]
            cursor += 1
            if marker in {0xD8, 0xD9, 0x01} or 0xD0 <= marker <= 0xD7:
                continue
            if cursor + 2 > len(data):
                break
            length = int.from_bytes(data[cursor:cursor + 2], "big")
            if length < 2 or cursor + length > len(data):
                break
            if marker in {0xC0, 0xC1, 0xC2} and length >= 8:
                height, width = struct.unpack(">HH", data[cursor + 3:cursor + 7])
                break
            if marker == 0xDA:
                break
            cursor += length
    elif mime_type == "image/webp" and len(data) >= 30 and data[:4] == b"RIFF" and data[8:12] == b"WEBP" and int.from_bytes(data[4:8], "little") == len(data) - 8:
        kind = data[12:16]
        if kind == b"VP8X":
            # Animated uploads are excluded; use a static canvas export.
            if not data[20] & 2:
                width = 1 + int.from_bytes(data[24:27], "little")
                height = 1 + int.from_bytes(data[27:30], "little")
        elif kind == b"VP8 " and data[23:26] == b"\x9d\x01\x2a":
            width, height = struct.unpack("<HH", data[26:30])
            width &= 0x3FFF
            height &= 0x3FFF
        elif kind == b"VP8L" and data[20] == 0x2F:
            bits = int.from_bytes(data[21:25], "little")
            width, height = (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if not 0 < width <= 8192 or not 0 < height <= 8192 or width * height > 20_000_000:
        raise ValueError("image signature or dimensions are invalid")
    return data


def validate_images(images: Any) -> list[dict[str, str]]:
    if not isinstance(images, list) or not 1 <= len(images) <= MAX_IMAGES:
        raise ValueError("image count is invalid")
    result = []
    seen = set()
    total = 0
    for item in images:
        if not isinstance(item, dict) or set(item) != {"image_ref", "mime_type", "data_base64"}:
            raise ValueError("image fields are invalid")
        ref = item["image_ref"]
        if not isinstance(ref, str) or not IMAGE_REF_RE.fullmatch(ref) or ref in seen:
            raise ValueError("image reference is invalid")
        seen.add(ref)
        total += len(decode_image(item["mime_type"], item["data_base64"]))
        result.append(dict(item))
    if total > MAX_TOTAL_IMAGE_BYTES:
        raise ValueError("total image size is invalid")
    return result


def image_inputs(images: Any) -> list[dict[str, str]]:
    return [
        {"type": "image", "url": "data:" + item["mime_type"] + ";base64," + item["data_base64"]}
        for item in validate_images(images)
    ]
