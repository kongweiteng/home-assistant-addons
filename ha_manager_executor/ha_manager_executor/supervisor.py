"""Read-only Supervisor client for one fixed Add-on information endpoint."""

from __future__ import annotations

import json
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .contract import ADDON_SLUG_RE, normalize_addon_info


MAX_RESPONSE_BYTES = 1024 * 1024


class SupervisorError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class SupervisorClient:
    def __init__(
        self,
        token: str,
        *,
        base_url: str = "http://supervisor",
        timeout_seconds: int = 5,
        opener: Callable[..., Any] = urlopen,
    ) -> None:
        if not token:
            raise SupervisorError("missing_supervisor_token", "Supervisor credential is unavailable")
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = int(timeout_seconds)
        self._opener = opener

    def addon_info(self, slug: str) -> dict[str, Any]:
        if not ADDON_SLUG_RE.fullmatch(slug):
            raise SupervisorError("invalid_addon_slug", "Add-on target must be an exact slug")
        request = Request(
            f"{self._base_url}/addons/{slug}/info",
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/json",
                "User-Agent": "ha-manager-executor/0.1.1",
            },
            method="GET",
        )
        try:
            with self._opener(request, timeout=self._timeout_seconds) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            raise SupervisorError("supervisor_http_error", f"Supervisor returned HTTP {exc.code}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise SupervisorError("supervisor_unavailable", "Supervisor information API is unavailable") from exc
        if len(raw) > MAX_RESPONSE_BYTES:
            raise SupervisorError("supervisor_response_too_large", "Supervisor response is too large")
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SupervisorError("supervisor_invalid_json", "Supervisor response is invalid") from exc
        if not isinstance(payload, dict) or payload.get("result") != "ok" or not isinstance(payload.get("data"), dict):
            raise SupervisorError("supervisor_rejected", "Supervisor did not return Add-on information")
        result = normalize_addon_info(payload["data"])
        if result.get("installed") is None:
            # A successful /addons/<slug>/info response already proves the
            # Add-on is installed. Current HAOS releases omit this field.
            result["installed"] = True
        return result
