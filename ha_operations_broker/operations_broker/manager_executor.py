"""Fixed private client for the read-only HA Manager Executor shadow."""

from __future__ import annotations

import ipaddress
import json
import re
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


MAX_RESPONSE_BYTES = 1024 * 1024
INTERNAL_HOST_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


class ManagerExecutorError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class ManagerExecutorClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout_seconds: int = 5,
        opener: Callable[..., Any] = urlopen,
    ) -> None:
        parsed = urlsplit(base_url)
        if (
            parsed.scheme != "http"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ManagerExecutorError("manager_url_invalid", "Manager Executor URL is invalid")
        try:
            ipaddress.ip_address(parsed.hostname)
        except ValueError:
            if not INTERNAL_HOST_RE.fullmatch(parsed.hostname):
                raise ManagerExecutorError("manager_url_invalid", "Manager Executor host is invalid")
        else:
            raise ManagerExecutorError("manager_url_invalid", "Manager Executor must use an internal service name")
        if len(token) < 32:
            raise ManagerExecutorError("manager_token_invalid", "Manager Executor token is invalid")
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout_seconds = int(timeout_seconds)
        self._opener = opener

    def shadow_restart(self, proposal: dict[str, Any]) -> dict[str, Any]:
        payload = {
            "version": 1,
            "action_id": proposal["action_id"],
            "proposal_hash": proposal["proposal_hash"],
            "action_type": "restart_addon",
            "target": proposal["target"],
            "adapter_version": proposal["adapter_version"],
            "adapter_schema_version": proposal["adapter_schema_version"],
            "baseline_etag": proposal["baseline_etag"],
        }
        request = Request(
            self._base_url + "/internal/v1/shadow/restart-addon",
            data=json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "ha-operations-broker/0.5.1",
            },
            method="POST",
        )
        try:
            with self._opener(request, timeout=self._timeout_seconds) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            raise ManagerExecutorError("manager_shadow_rejected", f"Manager Executor returned HTTP {exc.code}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise ManagerExecutorError("manager_shadow_unavailable", "Manager Executor shadow is unavailable") from exc
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ManagerExecutorError("manager_shadow_response_too_large", "Manager Executor response is too large")
        try:
            result = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ManagerExecutorError("manager_shadow_invalid", "Manager Executor response is invalid") from exc
        if not isinstance(result, dict):
            raise ManagerExecutorError("manager_shadow_invalid", "Manager Executor response is invalid")
        expected = {
            "mode": "shadow",
            "action_id": proposal["action_id"],
            "proposal_hash": proposal["proposal_hash"],
            "action_type": "restart_addon",
            "target": proposal["target"],
            "adapter_version": proposal["adapter_version"],
            "adapter_schema_version": proposal["adapter_schema_version"],
            "baseline_etag": proposal["baseline_etag"],
            "execution_allowed": False,
        }
        if any(result.get(field) != value for field, value in expected.items()) or not isinstance(result.get("observation"), dict):
            raise ManagerExecutorError("manager_shadow_mismatch", "Manager Executor shadow did not match the proposal")
        return result
