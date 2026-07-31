"""Minimal bounded Home Assistant Core service client."""

from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


class HomeAssistantApiError(RuntimeError):
    pass


class HomeAssistantClient:
    def __init__(self, base_url: str, token: str, timeout_s: float = 15.0) -> None:
        if not token:
            raise ValueError("Home Assistant token is required")
        self.base_url = base_url.rstrip("/")
        self._token = token
        self.timeout_s = timeout_s

    def call_service(self, domain: str, service: str, data: dict) -> None:
        if not domain or not service or not isinstance(data, dict):
            raise ValueError("domain, service and object data are required")
        request = Request(
            f"{self.base_url}/services/{quote(domain, safe='')}/{quote(service, safe='')}",
            data=json.dumps(data, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_s) as response:
                response.read()
        except HTTPError as error:
            raise HomeAssistantApiError(f"status_{error.code}") from error
        except (URLError, TimeoutError) as error:
            raise HomeAssistantApiError("connection_failed") from error
