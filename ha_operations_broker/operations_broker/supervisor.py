"""Narrow Supervisor client with fixed reads and one exact restart action."""

from __future__ import annotations

import json
import re
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ADDON_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_]{0,63}$")
MAX_RESPONSE_BYTES = 1024 * 1024


class SupervisorError(RuntimeError):
    """Raised when the fixed Supervisor observation cannot be completed."""

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

    def _get(self, path: str) -> dict[str, Any]:
        request = Request(
            self._base_url + path,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/json",
                "User-Agent": "ha-operations-broker/0.5.2",
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
        if not isinstance(payload, dict) or payload.get("result") != "ok" or not isinstance(
            payload.get("data"), dict
        ):
            raise SupervisorError("supervisor_rejected", "Supervisor did not return an information result")
        return payload["data"]

    def supervisor_info(self) -> dict[str, Any]:
        data = self._get("/supervisor/info")
        return _select(
            data,
            "version",
            "version_latest",
            "update_available",
            "healthy",
            "supported",
            "arch",
        )

    def core_info(self) -> dict[str, Any]:
        data = self._get("/core/info")
        return _select(
            data,
            "version",
            "version_latest",
            "update_available",
            "machine",
            "arch",
            "state",
            "healthy",
            "supported",
        )

    def addon_info(self, slug: str) -> dict[str, Any]:
        if not ADDON_SLUG_RE.fullmatch(slug):
            raise SupervisorError("invalid_addon_slug", "Add-on target must be an exact slug")
        data = self._get(f"/addons/{slug}/info")
        result = _select(
            data,
            "slug",
            "name",
            "state",
            "version",
            "version_latest",
            "update_available",
            "available",
            "installed",
            "protected",
            "rating",
            "hassio_role",
            "hassio_api",
            "homeassistant_api",
            "host_network",
            "full_access",
        )
        if result.get("installed") is None:
            # A successful /addons/<slug>/info response already proves the
            # Add-on is installed. Current HAOS releases omit this field.
            result["installed"] = True
        return result

    def restart_addon(self, slug: str) -> None:
        if not ADDON_SLUG_RE.fullmatch(slug):
            raise SupervisorError("invalid_addon_slug", "Add-on target must be an exact slug")
        request = Request(
            f"{self._base_url}/addons/{slug}/restart",
            data=b"{}",
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "ha-operations-broker/0.5.2",
            },
            method="POST",
        )
        try:
            with self._opener(request, timeout=self._timeout_seconds) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            raise SupervisorError(
                "supervisor_restart_rejected", f"Supervisor returned HTTP {exc.code}"
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise SupervisorError(
                "supervisor_restart_uncertain", "Supervisor restart result is unavailable"
            ) from exc
        if len(raw) > MAX_RESPONSE_BYTES:
            raise SupervisorError(
                "supervisor_response_too_large", "Supervisor response is too large"
            )
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SupervisorError(
                "supervisor_restart_uncertain", "Supervisor restart response is invalid"
            ) from exc
        if not isinstance(payload, dict) or payload.get("result") != "ok":
            raise SupervisorError(
                "supervisor_restart_rejected", "Supervisor rejected the add-on restart"
            )


def _select(data: dict[str, Any], *keys: str) -> dict[str, Any]:
    return {key: data.get(key) for key in keys if key in data}
