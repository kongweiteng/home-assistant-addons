"""Bounded internal HTTP client for the Controller Runner Manager."""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any
from urllib.parse import urlsplit

import aiohttp


CONTROLLER_HOST = "local-codex-controller"
CONTROLLER_PORT = 8102


class ControllerRelayError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def validate_controller_base_url(value: str) -> str:
    if not isinstance(value, str) or value.strip() != value:
        raise ValueError("Controller base URL 无效")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "http"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or parsed.hostname != CONTROLLER_HOST
        or parsed.port != CONTROLLER_PORT
    ):
        raise ValueError("Controller base URL 必须是精确 Add-on 内部 HTTP 地址")
    return value.rstrip("/")


class ControllerClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout_seconds: int = 10,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self.base_url = validate_controller_base_url(base_url)
        if not isinstance(token, str) or len(token) < 32:
            raise ValueError("Controller API token 无效")
        self._token = token
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._session = session
        self._owns_session = session is None

    async def start(self) -> None:
        if self._session is None:
            self._session = aiohttp.ClientSession(trust_env=False)

    async def close(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
        self._session = None

    async def enroll(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._post("/internal/v2/runner-relay/enroll", payload)

    async def authenticate(self, runner_id: str, credential: str) -> dict[str, Any]:
        return await self._post(
            "/internal/v2/runner-relay/authenticate",
            {"runner_id": runner_id, "credential": credential},
        )

    async def event(self, event_type: str, document: dict[str, Any], *, credential: str) -> dict[str, Any]:
        return await self._post(
            f"/internal/v2/runner-relay/events/{event_type}",
            document,
            runner_credential=credential,
        )

    async def _post(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        runner_credential: str | None = None,
    ) -> dict[str, Any]:
        if self._session is None:
            raise ControllerRelayError("controller_client_not_started", "Controller client 未启动")
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if runner_credential is not None:
            headers["X-Runner-Credential"] = runner_credential
        try:
            async with self._session.post(
                self.base_url + path,
                json=payload,
                headers=headers,
                timeout=self._timeout,
            ) as response:
                raw = await response.content.read(64 * 1024 + 1)
                if len(raw) > 64 * 1024:
                    raise ControllerRelayError("controller_response_too_large", "Controller 响应过大")
                try:
                    document = json.loads(raw)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ControllerRelayError("controller_invalid_json", "Controller 响应无效") from exc
                if response.status >= 400:
                    code = document.get("error", {}).get("code") if isinstance(document, dict) else None
                    raise ControllerRelayError(
                        code if isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,127}", code) else "controller_rejected",
                        "Controller 拒绝 Runner Relay 请求",
                    )
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise ControllerRelayError("controller_unavailable", "Controller 当前不可用") from exc
        if not isinstance(document, dict):
            raise ControllerRelayError("controller_invalid_response", "Controller 响应结构无效")
        result = document.get("result") if document.get("version") == 1 else document
        if not isinstance(result, dict):
            raise ControllerRelayError("controller_invalid_response", "Controller 响应缺少 result")
        return result


__all__ = ["ControllerClient", "ControllerRelayError", "validate_controller_base_url"]
