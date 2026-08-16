"""Minimal iLink HTTP, long-poll and AES media protocol implementation."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import mimetypes
from pathlib import Path
import re
import secrets
import struct
from typing import Any
from urllib.parse import quote, urlsplit

import aiohttp
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"
WEIXIN_CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"
ILINK_APP_ID = "bot"
CHANNEL_VERSION = "2.2.0"
ILINK_APP_CLIENT_VERSION = (2 << 16) | (2 << 8)

EP_GET_UPDATES = "ilink/bot/getupdates"
EP_SEND_MESSAGE = "ilink/bot/sendmessage"
EP_GET_CONFIG = "ilink/bot/getconfig"
EP_SEND_TYPING = "ilink/bot/sendtyping"
EP_GET_UPLOAD_URL = "ilink/bot/getuploadurl"
EP_GET_BOT_QR = "ilink/bot/get_bot_qrcode"
EP_GET_QR_STATUS = "ilink/bot/get_qrcode_status"

ITEM_TEXT = 1
ITEM_IMAGE = 2
ITEM_VOICE = 3
ITEM_FILE = 4
ITEM_VIDEO = 5
MSG_TYPE_BOT = 2
MSG_STATE_FINISH = 2
TYPING_STATUS_START = 1
TYPING_STATUS_STOP = 2

MEDIA_IMAGE = 1
MEDIA_VIDEO = 2
MEDIA_FILE = 3
MEDIA_VOICE = 4

SESSION_EXPIRED_ERRCODE = -14
RATE_LIMIT_ERRCODE = -2

WEIXIN_CDN_ALLOWLIST = {
    "novac2c.cdn.weixin.qq.com",
    "ilinkai.weixin.qq.com",
    "wx.qlogo.cn",
    "thirdwx.qlogo.cn",
    "res.wx.qq.com",
    "mmbiz.qpic.cn",
    "mmbiz.qlogo.cn",
}


class ProtocolError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        delivery_unknown: bool = False,
    ):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.delivery_unknown = delivery_unknown


def is_stale_context_response(response: dict[str, Any]) -> bool:
    """Distinguish iLink's stale-context ``-2`` from a genuine rate limit."""
    ret = response.get("ret")
    errcode = response.get("errcode")
    if ret != RATE_LIMIT_ERRCODE and errcode != RATE_LIMIT_ERRCODE:
        return False
    message = str(response.get("errmsg") or response.get("msg") or "").strip().casefold()
    return message == "unknown error"


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def validate_ilink_base_url(value: str) -> str:
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or not host
        or not (host == "ilinkai.weixin.qq.com" or host.endswith(".weixin.qq.com"))
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ProtocolError("invalid_ilink_url", "iLink 地址不在固定微信 HTTPS 边界")
    port = f":{parsed.port}" if parsed.port else ""
    return f"https://{host}{port}"


def validate_cdn_base_url(value: str) -> str:
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or host not in WEIXIN_CDN_ALLOWLIST or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ProtocolError("invalid_cdn_url", "CDN 地址不在固定微信 HTTPS 边界")
    path = parsed.path.rstrip("/")
    port = f":{parsed.port}" if parsed.port else ""
    return f"https://{host}{port}{path}"


def assert_cdn_url(value: str) -> str:
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or host not in WEIXIN_CDN_ALLOWLIST or parsed.username or parsed.password:
        raise ProtocolError("media_url_rejected", "媒体 URL 不在微信 HTTPS allowlist")
    return value


def random_wechat_uin() -> str:
    value = struct.unpack(">I", secrets.token_bytes(4))[0]
    return base64.b64encode(str(value).encode("utf-8")).decode("ascii")


def request_headers(token: str | None, body: str) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "Content-Length": str(len(body.encode("utf-8"))),
        "X-WECHAT-UIN": random_wechat_uin(),
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def pkcs7_pad(data: bytes) -> bytes:
    pad_length = 16 - len(data) % 16
    return data + bytes([pad_length] * pad_length)


def aes128_ecb_encrypt(plaintext: bytes, key: bytes) -> bytes:
    if len(key) != 16:
        raise ProtocolError("media_key_invalid", "AES-128 key 长度无效")
    encryptor = Cipher(algorithms.AES(key), modes.ECB(), backend=default_backend()).encryptor()
    return encryptor.update(pkcs7_pad(plaintext)) + encryptor.finalize()


def aes128_ecb_decrypt(ciphertext: bytes, key: bytes) -> bytes:
    if len(key) != 16 or len(ciphertext) % 16:
        raise ProtocolError("media_decrypt_failed", "媒体密钥或密文长度无效")
    decryptor = Cipher(algorithms.AES(key), modes.ECB(), backend=default_backend()).decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    if not padded:
        return padded
    pad_length = padded[-1]
    if not 1 <= pad_length <= 16 or not padded.endswith(bytes([pad_length]) * pad_length):
        raise ProtocolError("media_decrypt_failed", "媒体 PKCS7 padding 无效")
    return padded[:-pad_length]


def parse_aes_key(value: str) -> bytes:
    try:
        decoded = base64.b64decode(value, validate=True)
    except Exception as exc:
        raise ProtocolError("media_key_invalid", "媒体 AES key 不是有效 base64") from exc
    if len(decoded) == 16:
        return decoded
    if len(decoded) == 32:
        try:
            return bytes.fromhex(decoded.decode("ascii"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ProtocolError("media_key_invalid", "媒体 AES key hex 无效") from exc
    raise ProtocolError("media_key_invalid", "媒体 AES key 长度无效")


def aes_padded_size(size: int) -> int:
    return ((size + 16) // 16) * 16


def _media_spec(item: dict[str, Any]) -> dict[str, Any] | None:
    item_type = item.get("type")
    mapping = {
        ITEM_IMAGE: ("image", "image_item", "image/jpeg"),
        ITEM_VIDEO: ("video", "video_item", "video/mp4"),
        ITEM_FILE: ("file", "file_item", "application/octet-stream"),
        ITEM_VOICE: ("audio", "voice_item", "audio/silk"),
    }
    if item_type not in mapping:
        return None
    media_type, field, default_mime = mapping[item_type]
    value = item.get(field) if isinstance(item.get(field), dict) else {}
    media = value.get("media") if isinstance(value.get("media"), dict) else {}
    aes_value = media.get("aes_key")
    if item_type == ITEM_IMAGE and value.get("aeskey"):
        try:
            aes_value = base64.b64encode(bytes.fromhex(str(value["aeskey"]))).decode("ascii")
        except ValueError:
            raise ProtocolError("media_key_invalid", "图片 AES key hex 无效")
    filename = str(value.get("file_name") or {ITEM_IMAGE: "image.jpg", ITEM_VIDEO: "video.mp4", ITEM_VOICE: "voice.silk"}.get(item_type, "file.bin"))
    mime = mimetypes.guess_type(filename)[0] or default_mime
    return {
        "media_type": media_type,
        "filename": Path(filename).name[:255],
        "mime_type": mime,
        "encrypt_query_param": media.get("encrypt_query_param"),
        "full_url": media.get("full_url"),
        "aes_key": aes_value,
    }


def extract_message(message: dict[str, Any], account_id: str) -> dict[str, Any] | None:
    sender_id = str(message.get("from_user_id") or "").strip()
    if not sender_id or sender_id == account_id:
        return None
    room_id = str(message.get("room_id") or message.get("chat_room_id") or "").strip()
    to_user_id = str(message.get("to_user_id") or "").strip()
    is_group = bool(room_id) or bool(to_user_id and account_id and to_user_id != account_id and message.get("msg_type") == 1)
    items = message.get("item_list") if isinstance(message.get("item_list"), list) else []
    text_parts: list[str] = []
    media: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("type") == ITEM_TEXT:
            text_item = item.get("text_item") if isinstance(item.get("text_item"), dict) else {}
            if isinstance(text_item.get("text"), str) and text_item["text"]:
                text_parts.append(text_item["text"])
        spec = _media_spec(item)
        if spec:
            media.append(spec)
    message_id = str(message.get("message_id") or "").strip()
    if not message_id or (not text_parts and not media):
        return None
    return {
        "message_id": message_id,
        "sender_id": sender_id,
        "is_group": is_group,
        "text": "\n".join(text_parts),
        "context_token": str(message.get("context_token") or "").strip(),
        "media": media,
    }


class IlinkClient:
    def __init__(
        self,
        *,
        base_url: str,
        cdn_base_url: str,
        token: str,
        max_media_bytes: int,
        session: aiohttp.ClientSession | None = None,
    ):
        self.base_url = validate_ilink_base_url(base_url)
        self.cdn_base_url = validate_cdn_base_url(cdn_base_url)
        self.token = token
        self.max_media_bytes = max_media_bytes
        self.session = session
        self._owns_session = session is None

    async def start(self) -> None:
        if self.session is None:
            self.session = aiohttp.ClientSession(trust_env=True)

    async def close(self) -> None:
        if self._owns_session and self.session is not None and not self.session.closed:
            await self.session.close()

    async def api_post(
        self,
        endpoint: str,
        payload: dict[str, Any],
        *,
        timeout: float = 15.0,
        include_base_info: bool = True,
    ) -> dict[str, Any]:
        if self.session is None:
            await self.start()
        document = {**payload, "base_info": {"channel_version": CHANNEL_VERSION}} if include_base_info else payload
        body = canonical_json(document)
        url = f"{self.base_url}/{endpoint}"

        async def execute() -> dict[str, Any]:
            assert self.session is not None
            async with self.session.post(url, data=body, headers=request_headers(self.token, body)) as response:
                raw = await response.content.read(2 * 1024 * 1024 + 1)
                if response.status < 200 or response.status >= 300:
                    raise ProtocolError("ilink_http_error", f"iLink HTTP {response.status}", retryable=response.status >= 500)
                if len(raw) > 2 * 1024 * 1024:
                    raise ProtocolError("ilink_response_too_large", "iLink 响应过大")
                try:
                    result = json.loads(raw)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ProtocolError("ilink_invalid_json", "iLink 响应 JSON 无效") from exc
                if not isinstance(result, dict):
                    raise ProtocolError("ilink_invalid_json", "iLink 响应不是对象")
                return result

        try:
            return await asyncio.wait_for(execute(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise ProtocolError("ilink_timeout", "iLink 请求超时", retryable=True) from exc

    async def api_get(self, endpoint: str, *, timeout: float = 35.0, base_url: str | None = None) -> dict[str, Any]:
        if self.session is None:
            await self.start()
        target_base = validate_ilink_base_url(base_url or self.base_url)
        url = f"{target_base}/{endpoint}"
        headers = {"iLink-App-Id": ILINK_APP_ID, "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION)}

        async def execute() -> dict[str, Any]:
            assert self.session is not None
            async with self.session.get(url, headers=headers) as response:
                raw = await response.content.read(2 * 1024 * 1024 + 1)
                if response.status < 200 or response.status >= 300:
                    raise ProtocolError("ilink_http_error", f"iLink HTTP {response.status}", retryable=response.status >= 500)
                if len(raw) > 2 * 1024 * 1024:
                    raise ProtocolError("ilink_response_too_large", "iLink 响应过大")
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise ProtocolError("ilink_invalid_json", "iLink 响应不是对象")
                return result

        try:
            return await asyncio.wait_for(execute(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise ProtocolError("ilink_timeout", "iLink 请求超时", retryable=True) from exc

    async def get_updates(self, sync_buf: str, *, timeout_ms: int = 35000) -> dict[str, Any]:
        try:
            return await self.api_post(EP_GET_UPDATES, {"get_updates_buf": sync_buf}, timeout=timeout_ms / 1000)
        except ProtocolError as exc:
            if exc.code == "ilink_timeout":
                return {"ret": 0, "msgs": [], "get_updates_buf": sync_buf}
            raise

    async def create_bot_qr(self, local_tokens: list[str]) -> dict[str, Any]:
        clean_tokens: list[str] = []
        for token in local_tokens:
            if not isinstance(token, str):
                continue
            value = token.strip()
            if not value or value in clean_tokens:
                continue
            clean_tokens.append(value)
            if len(clean_tokens) >= 10:
                break
        return await self.api_post(
            f"{EP_GET_BOT_QR}?bot_type=3",
            {"local_token_list": clean_tokens},
            include_base_info=False,
        )

    async def get_bot_qr_status(
        self,
        qrcode_value: str,
        *,
        base_url: str | None = None,
        verify_code: str | None = None,
    ) -> dict[str, Any]:
        endpoint = f"{EP_GET_QR_STATUS}?qrcode={quote(qrcode_value, safe='')}"
        if verify_code:
            endpoint += f"&verify_code={quote(verify_code, safe='')}"
        try:
            return await self.api_get(endpoint, timeout=35.0, base_url=base_url)
        except ProtocolError as exc:
            if exc.code == "ilink_timeout":
                return {"status": "wait"}
            raise

    async def send_text(self, to_user_id: str, text: str, context_token: str | None, client_id: str) -> dict[str, Any]:
        if not text.strip():
            raise ProtocolError("empty_message", "不能发送空消息")
        message: dict[str, Any] = {
            "from_user_id": "",
            "to_user_id": to_user_id,
            "client_id": client_id,
            "message_type": MSG_TYPE_BOT,
            "message_state": MSG_STATE_FINISH,
            "item_list": [{"type": ITEM_TEXT, "text_item": {"text": text}}],
        }
        if context_token:
            message["context_token"] = context_token
        return await self.api_post(EP_SEND_MESSAGE, {"msg": message})

    async def get_config(self, ilink_user_id: str, context_token: str | None = None) -> dict[str, Any]:
        if not ilink_user_id.strip():
            raise ProtocolError("typing_user_invalid", "输入状态用户 ID 不能为空")
        payload: dict[str, Any] = {"ilink_user_id": ilink_user_id}
        if context_token:
            payload["context_token"] = context_token
        return await self.api_post(EP_GET_CONFIG, payload)

    async def send_typing(
        self,
        ilink_user_id: str,
        typing_ticket: str,
        status: int,
    ) -> dict[str, Any]:
        if not ilink_user_id.strip():
            raise ProtocolError("typing_user_invalid", "输入状态用户 ID 不能为空")
        if not typing_ticket.strip():
            raise ProtocolError("typing_ticket_missing", "输入状态 ticket 不能为空")
        if status not in {TYPING_STATUS_START, TYPING_STATUS_STOP}:
            raise ProtocolError("typing_status_invalid", "输入状态 status 无效")
        return await self.api_post(
            EP_SEND_TYPING,
            {
                "ilink_user_id": ilink_user_id,
                "typing_ticket": typing_ticket,
                "status": status,
            },
        )

    async def download_media(self, spec: dict[str, Any]) -> bytes:
        encrypted_query = spec.get("encrypt_query_param")
        full_url = spec.get("full_url")
        if encrypted_query:
            url = f"{self.cdn_base_url}/download?encrypted_query_param={quote(str(encrypted_query), safe='')}"
        elif isinstance(full_url, str) and full_url:
            url = assert_cdn_url(full_url)
        else:
            raise ProtocolError("media_reference_missing", "媒体缺少下载引用")
        if self.session is None:
            await self.start()

        async def execute() -> bytes:
            assert self.session is not None
            async with self.session.get(url) as response:
                if response.status < 200 or response.status >= 300:
                    raise ProtocolError("media_download_failed", f"媒体 HTTP {response.status}", retryable=response.status >= 500)
                length = response.headers.get("Content-Length")
                if length and int(length) > self.max_media_bytes + 16:
                    raise ProtocolError("media_too_large", "媒体超过大小上限")
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.content.iter_chunked(65536):
                    size += len(chunk)
                    if size > self.max_media_bytes + 16:
                        raise ProtocolError("media_too_large", "媒体超过大小上限")
                    chunks.append(chunk)
                return b"".join(chunks)

        try:
            raw = await asyncio.wait_for(execute(), timeout=120)
        except asyncio.TimeoutError as exc:
            raise ProtocolError("media_download_failed", "媒体下载超时", retryable=True) from exc
        key_value = spec.get("aes_key")
        if key_value:
            raw = aes128_ecb_decrypt(raw, parse_aes_key(str(key_value)))
        if len(raw) > self.max_media_bytes:
            raise ProtocolError("media_too_large", "解密媒体超过大小上限")
        return raw

    async def send_media(
        self,
        to_user_id: str,
        path: Path,
        context_token: str | None,
        client_id: str,
    ) -> str:
        if not isinstance(client_id, str) or not re.fullmatch(r"codex-weixin-[a-f0-9]{32}", client_id):
            raise ProtocolError("client_id_invalid", "媒体 client_id 无效")
        if self.session is None:
            await self.start()
        plaintext = path.read_bytes()
        if not plaintext or len(plaintext) > self.max_media_bytes:
            raise ProtocolError("media_too_large", "出站媒体大小无效")
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        media_type = MEDIA_IMAGE if mime.startswith("image/") else MEDIA_VIDEO if mime.startswith("video/") else MEDIA_FILE
        item_type = ITEM_IMAGE if media_type == MEDIA_IMAGE else ITEM_VIDEO if media_type == MEDIA_VIDEO else ITEM_FILE
        filekey = secrets.token_hex(16)
        key = secrets.token_bytes(16)
        ciphertext = aes128_ecb_encrypt(plaintext, key)
        response = await self.api_post(
            EP_GET_UPLOAD_URL,
            {
                "filekey": filekey,
                "media_type": media_type,
                "to_user_id": to_user_id,
                "rawsize": len(plaintext),
                "rawfilemd5": hashlib.md5(plaintext).hexdigest(),
                "filesize": aes_padded_size(len(plaintext)),
                "no_need_thumb": True,
                "aeskey": key.hex(),
            },
        )
        upload_url = response.get("upload_full_url")
        if not upload_url and response.get("upload_param"):
            upload_url = f"{self.cdn_base_url}/upload?encrypted_query_param={quote(str(response['upload_param']), safe='')}&filekey={quote(filekey, safe='')}"
        if not isinstance(upload_url, str) or not upload_url:
            raise ProtocolError("media_upload_failed", "iLink 未返回上传 URL")
        assert_cdn_url(upload_url)
        assert self.session is not None
        async with self.session.post(upload_url, data=ciphertext, headers={"Content-Type": "application/octet-stream"}) as upload:
            if upload.status != 200:
                raise ProtocolError("media_upload_failed", f"CDN 上传 HTTP {upload.status}", retryable=upload.status >= 500)
            encrypted_param = upload.headers.get("x-encrypted-param")
            await upload.read()
        if not encrypted_param:
            raise ProtocolError("media_upload_failed", "CDN 缺少加密参数")
        aes_key_for_api = base64.b64encode(key.hex().encode("ascii")).decode("ascii")
        media = {"encrypt_query_param": encrypted_param, "aes_key": aes_key_for_api, "encrypt_type": 1}
        if item_type == ITEM_IMAGE:
            item = {"type": ITEM_IMAGE, "image_item": {"media": media, "mid_size": len(ciphertext)}}
        elif item_type == ITEM_VIDEO:
            item = {"type": ITEM_VIDEO, "video_item": {"media": media, "video_size": len(ciphertext), "video_md5": hashlib.md5(plaintext).hexdigest(), "play_length": 0}}
        else:
            item = {"type": ITEM_FILE, "file_item": {"media": media, "file_name": path.name[:255], "len": str(len(plaintext))}}
        message: dict[str, Any] = {
            "from_user_id": "",
            "to_user_id": to_user_id,
            "client_id": client_id,
            "message_type": MSG_TYPE_BOT,
            "message_state": MSG_STATE_FINISH,
            "item_list": [item],
        }
        if context_token:
            message["context_token"] = context_token
        try:
            result = await self.api_post(EP_SEND_MESSAGE, {"msg": message})
        except ProtocolError as exc:
            raise ProtocolError(
                exc.code,
                str(exc),
                retryable=exc.retryable,
                delivery_unknown=True,
            ) from exc
        if result.get("ret") == SESSION_EXPIRED_ERRCODE or result.get("errcode") == SESSION_EXPIRED_ERRCODE:
            raise ProtocolError("session_expired", "iLink 会话已过期")
        if result.get("ret") not in {0, None} or result.get("errcode") not in {0, None}:
            raise ProtocolError("media_send_failed", "iLink 媒体发送失败")
        return client_id
