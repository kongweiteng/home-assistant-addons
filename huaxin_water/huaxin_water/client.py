"""Fixed-allowlist GET client for the undocumented upstream H5 API."""

from __future__ import annotations

from dataclasses import dataclass
import http.client
import json
import socket
import time
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request


MAX_RESPONSE_BYTES = 2 * 1024 * 1024
ENDPOINT_PATHS = {
    "customer_info": "/bh/customer/info",
    "water_records": "/bh/customer/queryWaterRecords",
    "payment_records": "/bh/customer/queryPaymentRecords",
    "steps": "/bh/customer/queryStep",
    "payment_summary": "/bh/pay/getUserPaymentInfo",
}


class _ConnectionResponse:
    """Close both the response body and its owning HTTP connection."""

    def __init__(
        self,
        connection: http.client.HTTPConnection,
        response: http.client.HTTPResponse,
    ) -> None:
        self._connection = connection
        self._response = response
        self.headers = response.headers
        self.status = response.status

    def read(self, amount: int) -> bytes:
        return self._response.read(amount)

    def __enter__(self) -> "_ConnectionResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        try:
            self._response.close()
        finally:
            self._connection.close()


def _open_with_http_client(request: Request, timeout: int) -> _ConnectionResponse:
    """Issue one non-redirecting request using predictable HTTP headers.

    The upstream intermittently delays Python urllib's default opener before
    sending the status line, while the same request made through http.client
    responds normally. Keeping this transport local also prevents redirects
    from escaping the configured API origin.
    """

    parsed = urlsplit(request.full_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("unsupported upstream URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("credentialed upstream URLs are not allowed")
    if request.get_method() != "GET":
        raise ValueError("only GET requests are allowed")

    connection_type = (
        http.client.HTTPSConnection
        if parsed.scheme == "https"
        else http.client.HTTPConnection
    )
    connection = connection_type(parsed.hostname, parsed.port, timeout=timeout)
    target = parsed.path or "/"
    if parsed.query:
        target = f"{target}?{parsed.query}"
    default_port = 443 if parsed.scheme == "https" else 80
    host = parsed.hostname
    if parsed.port is not None and parsed.port != default_port:
        host = f"{host}:{parsed.port}"

    try:
        connection.putrequest(
            "GET", target, skip_host=True, skip_accept_encoding=True
        )
        connection.putheader("Host", host)
        for name, value in request.header_items():
            canonical_name = "User-Agent" if name.lower() == "user-agent" else name
            connection.putheader(canonical_name, value)
        connection.endheaders()
        response = connection.getresponse()
        return _ConnectionResponse(connection, response)
    except Exception:
        connection.close()
        raise


@dataclass(frozen=True)
class UpstreamError(Exception):
    kind: str
    endpoint: str
    http_status: int | None = None

    def __str__(self) -> str:
        status = "" if self.http_status is None else f" status={self.http_status}"
        return f"upstream {self.kind} endpoint={self.endpoint}{status}"


class HuaxinClient:
    def __init__(
        self,
        base_url: str,
        timeout_seconds: int,
        *,
        opener: Callable = _open_with_http_client,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._opener = opener
        self._sleep = sleep

    def fetch(self, customer_no: str, endpoint: str) -> dict:
        if endpoint not in ENDPOINT_PATHS:
            raise ValueError("endpoint is not allowlisted")
        last_error: UpstreamError | None = None
        for attempt in range(2):
            try:
                return self._fetch_once(customer_no, endpoint)
            except UpstreamError as error:
                last_error = error
                if attempt == 1 or error.kind not in {
                    "timeout",
                    "connection",
                    "rate_limited",
                    "server_error",
                }:
                    raise
                self._sleep(0.25)
        assert last_error is not None
        raise last_error

    def _fetch_once(self, customer_no: str, endpoint: str) -> dict:
        query = urlencode({"customerNo": customer_no})
        url = f"{self.base_url}{ENDPOINT_PATHS[endpoint]}?{query}"
        request = Request(
            url,
            method="GET",
            headers={
                "Accept": "application/json",
                "User-Agent": "HomeAssistant-HuaxinWater/0.1",
            },
        )
        try:
            with self._opener(request, timeout=self.timeout_seconds) as response:
                status = getattr(response, "status", 200)
                if status in {401, 403}:
                    raise UpstreamError("auth_required", endpoint, status)
                if status == 429:
                    raise UpstreamError("rate_limited", endpoint, status)
                if status >= 500:
                    raise UpstreamError("server_error", endpoint, status)
                if status >= 300:
                    raise UpstreamError("http_error", endpoint, status)
                content_type = response.headers.get_content_type()
                if content_type not in {"application/json", "text/json", "text/plain"}:
                    raise UpstreamError("unexpected_content_type", endpoint)
                payload = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as error:
            if error.code in {401, 403}:
                kind = "auth_required"
            elif error.code == 429:
                kind = "rate_limited"
            elif error.code >= 500:
                kind = "server_error"
            else:
                kind = "http_error"
            raise UpstreamError(kind, endpoint, error.code) from None
        except (TimeoutError, socket.timeout):
            raise UpstreamError("timeout", endpoint) from None
        except URLError as error:
            if isinstance(error.reason, (TimeoutError, socket.timeout)):
                raise UpstreamError("timeout", endpoint) from None
            raise UpstreamError("connection", endpoint) from None
        except OSError:
            raise UpstreamError("connection", endpoint) from None

        if len(payload) > MAX_RESPONSE_BYTES:
            raise UpstreamError("response_too_large", endpoint)
        try:
            decoded = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise UpstreamError("invalid_json", endpoint) from None
        if not isinstance(decoded, dict):
            raise UpstreamError("invalid_envelope", endpoint)
        return decoded
