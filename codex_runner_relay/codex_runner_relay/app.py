"""aiohttp WSS data plane and authenticated internal publish API."""

from __future__ import annotations

import asyncio
from collections import deque
import hmac
import json
import time
from typing import Any

from aiohttp import WSMsgType, web

from . import __version__
from .controller import ControllerClient, ControllerRelayError
from .protocol import (
    RelayProtocolError,
    json_size,
    validate_event_message,
    validate_first_message,
    validate_publish,
)


class ConnectionRate:
    def __init__(self, maximum: int) -> None:
        self.maximum = maximum
        self.events: deque[float] = deque()

    def allow(self, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        while self.events and current - self.events[0] >= 60:
            self.events.popleft()
        if len(self.events) >= self.maximum:
            return False
        self.events.append(current)
        return True


class RelayHub:
    def __init__(
        self,
        controller: ControllerClient,
        *,
        api_token: str,
        max_connections: int,
        max_message_bytes: int,
        first_frame_timeout_seconds: int,
        messages_per_minute: int,
    ) -> None:
        if len(api_token) < 32:
            raise ValueError("Relay API token 无效")
        self.controller = controller
        self.api_token = api_token
        self.max_connections = max_connections
        self.max_message_bytes = max_message_bytes
        self.first_frame_timeout_seconds = first_frame_timeout_seconds
        self.messages_per_minute = messages_per_minute
        self.connections: dict[str, web.WebSocketResponse] = {}
        self._pending_connections = 0
        self._pending_runners: set[str] = set()
        self._lock = asyncio.Lock()

    async def websocket(self, request: web.Request) -> web.StreamResponse:
        await self._reserve_connection()
        connection_reserved = True
        runner_reserved = False
        active = False
        ws = web.WebSocketResponse(
            autoping=True,
            heartbeat=30,
            max_msg_size=self.max_message_bytes,
            compress=False,
        )
        runner_id: str | None = None
        credential: str | None = None
        rate = ConnectionRate(self.messages_per_minute)
        try:
            await ws.prepare(request)
            first = await ws.receive(timeout=self.first_frame_timeout_seconds)
            if first.type != WSMsgType.TEXT:
                await self._close_error(ws, "first_message_required")
                return ws
            message = self._json_message(first.data)
            validated = validate_first_message(message)
            runner_id = validated["runner_id"]
            await self._reserve_runner(runner_id)
            runner_reserved = True
            if validated["type"] == "enroll":
                result = await self.controller.enroll({**validated["payload"], "token": validated["token"]})
                credential_document = result.get("credential")
                if not isinstance(credential_document, dict) or not isinstance(credential_document.get("secret"), str):
                    raise ControllerRelayError("credential_missing", "Controller 未返回 Runner credential")
                credential = credential_document["secret"]
                await self._activate(runner_id, ws)
                connection_reserved = False
                runner_reserved = False
                active = True
                await ws.send_json(
                    {
                        "type": "enrolled",
                        "runner_id": runner_id,
                        "credential": credential,
                        "runner": result.get("runner"),
                    }
                )
            else:
                credential = validated["credential"]
                await self.controller.authenticate(runner_id, credential)
                await self._activate(runner_id, ws)
                connection_reserved = False
                runner_reserved = False
                active = True
                await ws.send_json({"type": "authenticated", "runner_id": runner_id})

            async for incoming in ws:
                if incoming.type == WSMsgType.TEXT:
                    if not rate.allow():
                        await self._close_error(ws, "rate_limited")
                        break
                    event_type, document = validate_event_message(
                        self._json_message(incoming.data), runner_id=runner_id
                    )
                    await self.controller.event(event_type, document, credential=credential)
                    await ws.send_json(
                        {
                            "type": "ack",
                            "event_type": event_type,
                            "body_digest": document.get("body_digest"),
                        }
                    )
                elif incoming.type in {WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR}:
                    break
                else:
                    await self._close_error(ws, "text_message_required")
                    break
        except asyncio.TimeoutError:
            await self._close_error(ws, "first_frame_timeout")
        except RelayProtocolError as exc:
            await self._close_error(ws, exc.code)
        except ControllerRelayError as exc:
            await self._close_error(ws, exc.code)
        finally:
            if active and runner_id is not None:
                await self._unregister(runner_id, ws)
            elif connection_reserved or runner_reserved:
                await self._release_reservations(
                    runner_id if runner_reserved else None,
                    release_connection=connection_reserved,
                )
        return ws

    async def publish(self, request: web.Request) -> web.Response:
        self._authorize(request)
        try:
            payload = await request.json(loads=json.loads)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise web.HTTPBadRequest(text="invalid_json")
        runner_id = request.match_info["runner_id"]
        kind = request.match_info["kind"]
        try:
            document = validate_publish(kind, runner_id, payload)
        except RelayProtocolError as exc:
            raise web.HTTPBadRequest(text=exc.code) from exc
        if json_size(document) > self.max_message_bytes:
            raise web.HTTPRequestEntityTooLarge(
                max_size=self.max_message_bytes,
                actual_size=json_size(document),
            )
        async with self._lock:
            ws = self.connections.get(runner_id)
        if ws is None or ws.closed:
            raise web.HTTPServiceUnavailable(text="runner_offline")
        await ws.send_json({"type": kind, "document": document})
        return web.json_response({"status": "accepted", "runner_id": runner_id, "kind": kind}, status=202)

    async def health(self, _request: web.Request) -> web.Response:
        async with self._lock:
            count = len(self.connections)
        return web.json_response(
            {
                "status": "ok",
                "version": __version__,
                "connected_runners": count,
                "max_connections": self.max_connections,
            }
        )

    async def _reserve_connection(self) -> None:
        async with self._lock:
            if len(self.connections) + self._pending_connections >= self.max_connections:
                raise web.HTTPServiceUnavailable(text="Runner connection capacity reached")
            self._pending_connections += 1

    async def _reserve_runner(self, runner_id: str) -> None:
        async with self._lock:
            existing = self.connections.get(runner_id)
            if (existing is not None and not existing.closed) or runner_id in self._pending_runners:
                raise RelayProtocolError("runner_already_connected", "Runner 已有活动连接")
            self._pending_runners.add(runner_id)

    async def _activate(self, runner_id: str, ws: web.WebSocketResponse) -> None:
        async with self._lock:
            if runner_id not in self._pending_runners or self._pending_connections < 1:
                raise RuntimeError("Runner Relay reservation invariant violated")
            self._pending_runners.remove(runner_id)
            self._pending_connections -= 1
            self.connections[runner_id] = ws

    async def _release_reservations(
        self,
        runner_id: str | None,
        *,
        release_connection: bool,
    ) -> None:
        async with self._lock:
            if runner_id is not None:
                self._pending_runners.discard(runner_id)
            if release_connection and self._pending_connections > 0:
                self._pending_connections -= 1

    async def _unregister(self, runner_id: str, ws: web.WebSocketResponse) -> None:
        async with self._lock:
            if self.connections.get(runner_id) is ws:
                self.connections.pop(runner_id, None)

    def _authorize(self, request: web.Request) -> None:
        expected = f"Bearer {self.api_token}"
        if not hmac.compare_digest(request.headers.get("Authorization", ""), expected):
            raise web.HTTPUnauthorized(text="not_authorized")

    def _json_message(self, raw: str) -> dict[str, Any]:
        if len(raw.encode("utf-8")) > self.max_message_bytes:
            raise RelayProtocolError("message_too_large", "Runner message 过大")
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RelayProtocolError("invalid_json", "Runner message JSON 无效") from exc
        if not isinstance(value, dict):
            raise RelayProtocolError("message_invalid", "Runner message 必须是 object")
        return value

    @staticmethod
    async def _close_error(ws: web.WebSocketResponse, code: str) -> None:
        if not ws.closed:
            try:
                await ws.send_json({"type": "error", "code": code})
            finally:
                await ws.close(code=1008, message=code.encode("ascii", errors="ignore")[:120])


def create_app(hub: RelayHub) -> web.Application:
    app = web.Application(client_max_size=hub.max_message_bytes)
    app.router.add_get("/healthz", hub.health)
    app.router.add_get("/v1/runner", hub.websocket)
    app.router.add_post("/internal/v1/runners/{runner_id}/{kind}", hub.publish)

    async def controller_context(_app: web.Application):
        await hub.controller.start()
        yield
        await hub.controller.close()

    app.cleanup_ctx.append(controller_context)
    return app


__all__ = ["ConnectionRate", "RelayHub", "create_app"]
