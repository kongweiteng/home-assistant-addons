"""Single-poller iLink service and durable Controller delivery loop."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import threading
from typing import Any
from urllib.parse import urlsplit

import aiohttp

from .protocol import (
    EP_GET_BOT_QR,
    EP_GET_QR_STATUS,
    IlinkClient,
    ProtocolError,
    RATE_LIMIT_ERRCODE,
    SESSION_EXPIRED_ERRCODE,
    extract_message,
)
from .store import GatewayStore, IdentityStore, StoreError, TokenLock, utc_now


def validate_controller_url(value: str) -> str:
    if not value:
        return ""
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "http"
        or not host
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or all(character.isdigit() or character == "." for character in host)
        or ":" in host
    ):
        raise StoreError("controller_url_invalid", "Controller 地址必须使用固定 http 主机名")
    port = f":{parsed.port}" if parsed.port else ""
    return f"http://{host}{port}"


class ControllerClient:
    def __init__(self, base_url: str, token: str, *, session: aiohttp.ClientSession | None = None):
        self.base_url = validate_controller_url(base_url)
        self.token = token
        self.session = session
        self._owns_session = session is None

    @property
    def configured(self) -> bool:
        return bool(self.base_url and len(self.token) >= 32)

    async def start(self) -> None:
        if self.session is None:
            self.session = aiohttp.ClientSession(trust_env=True)

    async def close(self) -> None:
        if self._owns_session and self.session is not None and not self.session.closed:
            await self.session.close()

    async def submit(self, message: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "/internal/v1/jobs", message)

    async def job(self, job_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/internal/v1/jobs/{job_id}", None)

    async def _request(self, method: str, path: str, payload: dict[str, Any] | None) -> dict[str, Any]:
        if not self.configured:
            raise StoreError("controller_unavailable", "Codex Controller 未配置", status=503)
        if self.session is None:
            await self.start()
        body = None if payload is None else json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        assert self.session is not None
        try:
            async with self.session.request(
                method,
                f"{self.base_url}{path}",
                data=body,
                headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json", "Accept": "application/json"},
                timeout=aiohttp.ClientTimeout(total=20),
            ) as response:
                raw = await response.content.read(2 * 1024 * 1024 + 1)
                if len(raw) > 2 * 1024 * 1024:
                    raise StoreError("controller_invalid_response", "Controller 响应过大", status=502)
                try:
                    document = json.loads(raw)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise StoreError("controller_invalid_response", "Controller 响应 JSON 无效", status=502) from exc
                if response.status < 200 or response.status >= 300:
                    error = document.get("error") if isinstance(document, dict) else {}
                    code = error.get("code") if isinstance(error, dict) else "controller_rejected"
                    raise StoreError(str(code or "controller_rejected"), "Controller 暂未接受作业", status=response.status)
                result = document.get("result") if isinstance(document, dict) else None
                if not isinstance(result, dict):
                    raise StoreError("controller_invalid_response", "Controller 响应缺少 result", status=502)
                return result
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise StoreError("controller_unavailable", "Codex Controller 不可用", status=503) from exc


class GatewayService:
    def __init__(
        self,
        *,
        identity_store: IdentityStore,
        store: GatewayStore,
        controller: ControllerClient,
        bootstrap_identity: dict[str, Any],
        poller_enabled: bool,
        activation_confirmation: str,
        max_media_bytes: int,
    ):
        self.identity_store = identity_store
        self.store = store
        self.controller = controller
        self.identity = identity_store.bootstrap(bootstrap_identity)
        self.poller_enabled = poller_enabled
        self.activation_confirmation = activation_confirmation
        self.max_media_bytes = max_media_bytes
        self.client: IlinkClient | None = None
        self.token_lock: TokenLock | None = None
        self.poller_state = "disabled"
        self.last_error: str | None = None
        self.last_poll_at: str | None = None
        self.last_message_at: str | None = None
        self.qr_state: dict[str, Any] | None = None
        self.qr_image_path = identity_store.data_dir / "qr" / "current.png"
        self._tasks: list[asyncio.Task[Any]] = []
        self._status_lock = threading.Lock()
        self._stop = asyncio.Event()

    async def start(self) -> None:
        await self.controller.start()
        self._refresh_client()
        if self.poller_enabled:
            if self.activation_confirmation != "HERMES_POLLER_STOPPED":
                raise StoreError("activation_confirmation_required", "未确认 Hermes poller 已停止", status=409)
            if self.identity is None or self.client is None:
                raise StoreError("credential_missing", "缺少完整 iLink 身份", status=409)
            self.token_lock = self.identity_store.acquire_token_lock(self.identity["token"])
            self.token_lock.acquire()
            self.poller_state = "polling"
            self._tasks.append(asyncio.create_task(self._poll_loop(), name="weixin-poller"))
        self._tasks.append(asyncio.create_task(self._delivery_loop(), name="weixin-controller-delivery"))
        self._tasks.append(asyncio.create_task(self._cleanup_loop(), name="weixin-spool-cleanup"))

    async def stop(self) -> None:
        self._stop.set()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self.client is not None:
            await self.client.close()
        await self.controller.close()
        if self.token_lock is not None:
            self.token_lock.release()
        self.poller_state = "stopped"

    def _refresh_client(self) -> None:
        if self.identity is None:
            self.client = None
            return
        self.client = IlinkClient(
            base_url=self.identity["base_url"],
            cdn_base_url=self.identity["cdn_base_url"],
            token=self.identity["token"],
            max_media_bytes=self.max_media_bytes,
        )

    async def _poll_loop(self) -> None:
        assert self.identity is not None and self.client is not None
        cursor = self.identity_store.cursor(self.identity)
        timeout_ms = 35000
        failures = 0
        while not self._stop.is_set():
            try:
                response = await self.client.get_updates(cursor, timeout_ms=timeout_ms)
                suggested = response.get("longpolling_timeout_ms")
                if isinstance(suggested, int) and suggested > 0:
                    timeout_ms = min(suggested, 120000)
                ret = response.get("ret", 0)
                errcode = response.get("errcode", 0)
                if ret not in {0, None} or errcode not in {0, None}:
                    if ret == SESSION_EXPIRED_ERRCODE or errcode == SESSION_EXPIRED_ERRCODE or (
                        (ret == RATE_LIMIT_ERRCODE or errcode == RATE_LIMIT_ERRCODE)
                        and str(response.get("errmsg") or "").lower() == "unknown error"
                    ):
                        self.poller_state = "session_expired"
                        self.last_error = "session_expired"
                        return
                    failures += 1
                    self.last_error = "poll_failed"
                    await asyncio.sleep(30 if failures >= 3 else 2)
                    if failures >= 3:
                        failures = 0
                    continue
                failures = 0
                self.last_poll_at = utc_now()
                for raw_message in response.get("msgs") or []:
                    if isinstance(raw_message, dict):
                        await self._ingest(raw_message)
                new_cursor = str(response.get("get_updates_buf") or "")
                if new_cursor and new_cursor != cursor:
                    self.identity_store.set_cursor(self.identity, new_cursor)
                    cursor = new_cursor
            except asyncio.CancelledError:
                return
            except ProtocolError as exc:
                failures += 1
                self.last_error = exc.code
                await asyncio.sleep(30 if failures >= 3 else 2)
            except Exception:
                failures += 1
                self.last_error = "poll_failed"
                await asyncio.sleep(30 if failures >= 3 else 2)

    async def _ingest(self, raw_message: dict[str, Any]) -> None:
        assert self.identity is not None and self.client is not None
        message = extract_message(raw_message, self.identity["account_id"])
        if message is None or message["is_group"]:
            return
        sender_id = message["sender_id"]
        if sender_id not in set(self.identity.get("allowed_user_ids", [])):
            return
        if self.store.message_exists(message["message_id"]):
            return
        if message["context_token"]:
            self.identity_store.set_context(self.identity, sender_id, message["context_token"])
        media: list[tuple[dict[str, Any], bytes]] = []
        for spec in message["media"]:
            try:
                media.append((spec, await self.client.download_media(spec)))
            except ProtocolError as exc:
                self.last_error = exc.code
        if not message["text"] and not media:
            return
        conversation_key = "sha256:" + hashlib.sha256(f"weixin:{sender_id}".encode("utf-8")).hexdigest()
        self.store.store_message(
            message_id=message["message_id"],
            sender_id=sender_id,
            conversation_key=conversation_key,
            text=message["text"],
            media=media,
        )
        self.last_message_at = utc_now()

    async def _delivery_loop(self) -> None:
        while not self._stop.is_set():
            try:
                if self.controller.configured:
                    for message in self.store.pending_controller():
                        payload = {
                            "version": 1,
                            "message_id": message["message_id"],
                            "conversation_key": message["conversation_key"],
                            "received_at": message["received_at"],
                            "text": message["text"],
                            "attachments": message["attachments"],
                            "reply_capabilities": ["text", "image", "file"],
                        }
                        try:
                            job = await self.controller.submit(payload)
                        except StoreError as exc:
                            self.last_error = exc.code
                            break
                        self.store.mark_submitted(message["message_id"], job["job_id"])
                    for message in self.store.submitted():
                        try:
                            job = await self.controller.job(message["controller_job_id"])
                        except StoreError as exc:
                            self.last_error = exc.code
                            break
                        if job["state"] == "completed":
                            await self._send_result(message, str(job.get("result") or "任务已完成。"))
                            self.store.mark_finished(message["message_id"], success=True)
                        elif job["state"] in {"failed", "cancelled", "recovery_required"}:
                            text = "任务状态需要人工核对，请在 Codex Controller 页面查看。" if job["state"] == "recovery_required" else "任务未完成，请在 Codex Controller 页面查看错误状态。"
                            await self._send_result(message, text)
                            self.store.mark_finished(message["message_id"], success=False, error_code=job.get("error_code") or job["state"])
                await asyncio.sleep(2)
            except asyncio.CancelledError:
                return
            except Exception:
                self.last_error = "delivery_failed"
                await asyncio.sleep(5)

    async def _send_result(self, message: dict[str, Any], text: str) -> None:
        if self.identity is None or self.client is None:
            raise StoreError("credential_missing", "无法回传微信消息", status=503)
        chunks = split_text(text, 4000)
        for index, chunk in enumerate(chunks):
            client_id, already_sent = self.store.prepare_chunk(message["controller_job_id"], index)
            if already_sent:
                continue
            context = self.identity_store.context(self.identity, message["sender_id"])
            response = await self.client.send_text(message["sender_id"], chunk, context, client_id)
            ret = response.get("ret", 0)
            errcode = response.get("errcode", 0)
            if (ret == SESSION_EXPIRED_ERRCODE or errcode == SESSION_EXPIRED_ERRCODE) and context:
                self.identity_store.clear_context(self.identity, message["sender_id"])
                response = await self.client.send_text(message["sender_id"], chunk, None, client_id)
                ret = response.get("ret", 0)
                errcode = response.get("errcode", 0)
            if ret not in {0, None} or errcode not in {0, None}:
                code = "send_rate_limited" if ret == RATE_LIMIT_ERRCODE or errcode == RATE_LIMIT_ERRCODE else "send_failed"
                self.store.mark_chunk(message["controller_job_id"], index, success=False, error_code=code)
                raise ProtocolError(code, "微信发送失败", retryable=code == "send_rate_limited")
            self.store.mark_chunk(message["controller_job_id"], index, success=True)
            if index + 1 < len(chunks):
                await asyncio.sleep(0.5)

    async def _cleanup_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.store.cleanup_spool()
                await asyncio.sleep(300)
            except asyncio.CancelledError:
                return
            except Exception:
                await asyncio.sleep(60)

    async def start_qr_login(self) -> dict[str, Any]:
        if self.qr_state and self.qr_state.get("state") in {"waiting", "scanned"}:
            return self.public_qr_state()
        client = IlinkClient(base_url="https://ilinkai.weixin.qq.com", cdn_base_url="https://novac2c.cdn.weixin.qq.com/c2c", token="", max_media_bytes=self.max_media_bytes)
        await client.start()
        response = await client.api_get(f"{EP_GET_BOT_QR}?bot_type=3")
        qrcode_value = str(response.get("qrcode") or "")
        qrcode_content = str(response.get("qrcode_img_content") or qrcode_value)
        if not qrcode_value:
            await client.close()
            raise ProtocolError("qr_invalid", "iLink 未返回二维码")
        self.qr_image_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        try:
            import qrcode

            image = qrcode.make(qrcode_content)
            image.save(self.qr_image_path)
            self.qr_image_path.chmod(0o600)
        except Exception as exc:
            await client.close()
            raise ProtocolError("qr_render_failed", "二维码生成失败") from exc
        self.qr_state = {"state": "waiting", "qrcode": qrcode_value, "has_image": True, "base_url": "https://ilinkai.weixin.qq.com"}
        self._tasks.append(asyncio.create_task(self._poll_qr(client), name="weixin-qr-login"))
        return self.public_qr_state()

    async def _poll_qr(self, client: IlinkClient) -> None:
        try:
            for _ in range(480):
                if self.qr_state is None:
                    return
                response = await client.api_get(
                    f"{EP_GET_QR_STATUS}?qrcode={self.qr_state['qrcode']}", base_url=self.qr_state["base_url"]
                )
                state = str(response.get("status") or "wait")
                if state == "scaned":
                    self.qr_state["state"] = "scanned"
                elif state == "scaned_but_redirect" and response.get("redirect_host"):
                    self.qr_state["base_url"] = f"https://{response['redirect_host']}"
                elif state == "expired":
                    self.qr_state["state"] = "expired"
                    return
                elif state == "confirmed":
                    identity = {
                        "account_id": str(response.get("ilink_bot_id") or ""),
                        "token": str(response.get("bot_token") or ""),
                        "base_url": str(response.get("baseurl") or "https://ilinkai.weixin.qq.com"),
                        "cdn_base_url": "https://novac2c.cdn.weixin.qq.com/c2c",
                        "user_id": str(response.get("ilink_user_id") or ""),
                        "allowed_user_ids": [],
                        "get_updates_buf": "",
                        "context_tokens": {},
                    }
                    self.identity_store.save_identity(identity)
                    self.identity = self.identity_store.load_identity()
                    self._refresh_client()
                    self.qr_state["state"] = "credential_ready"
                    return
                await asyncio.sleep(1)
        except (ProtocolError, asyncio.CancelledError):
            if self.qr_state is not None and self.qr_state.get("state") not in {"credential_ready", "expired"}:
                self.qr_state["state"] = "failed"
        finally:
            await client.close()

    def public_qr_state(self) -> dict[str, Any]:
        if self.qr_state is None:
            return {"state": "idle", "has_image": False}
        return {"state": self.qr_state["state"], "has_image": bool(self.qr_state.get("has_image"))}

    def inspect_migration(self, reference: str) -> dict[str, Any]:
        return self.identity_store.inspect_migration(reference)

    def import_migration(self, reference: str, key: str) -> dict[str, Any]:
        result = self.identity_store.import_migration(reference, key)
        self.identity = self.identity_store.load_identity()
        self._refresh_client()
        return result

    def status(self) -> dict[str, Any]:
        identity = None if self.identity is None else self.identity_store.public_summary(self.identity)
        return {
            "version": "0.1.1",
            "poller_enabled": self.poller_enabled,
            "poller_state": self.poller_state,
            "identity": identity,
            "controller_configured": self.controller.configured,
            "last_poll_at": self.last_poll_at,
            "last_message_at": self.last_message_at,
            "last_error": self.last_error,
            "qr": self.public_qr_state(),
            "queue": self.store.status(),
        }


def split_text(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        boundary = max(remaining.rfind("\n", 0, limit + 1), remaining.rfind("。", 0, limit + 1))
        if boundary < limit // 2:
            boundary = limit
        else:
            boundary += 1
        chunks.append(remaining[:boundary])
        remaining = remaining[boundary:]
    return chunks
