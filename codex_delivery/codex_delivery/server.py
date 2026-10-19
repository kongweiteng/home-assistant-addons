"""Bounded internal HTTP adapter; no default production listener or auto-worker."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import threading
import time
from typing import Any

from .provider import DeliveryProvider, MAX_REQUEST, MAX_RESPONSE
from .store import canonical


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise ValueError('duplicate_key')
        result[key] = value
    return result


def _constant(_: str) -> None:
    raise ValueError('nonfinite_json')


class DeadlineReader(io.RawIOBase):
    """A total read deadline; incremental bytes cannot reset the timeout."""
    def __init__(self, connection: Any, deadline: float):
        self.connection = connection
        self.deadline = deadline

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('request_deadline')
        self.connection.settimeout(remaining)
        return self.connection.recv_into(buffer)


class DeliveryHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False
    request_queue_size = 8

    def __init__(self, address: tuple[str, int], provider: DeliveryProvider, *, read_deadline_seconds: float = 5):
        if not .05 <= read_deadline_seconds <= 10:
            raise ValueError('read_deadline_invalid')
        self.provider = provider
        self.read_deadline_seconds = read_deadline_seconds
        self._slots = threading.BoundedSemaphore(8)
        super().__init__(address, DeliveryHandler)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def handle_error(self, request: Any, client_address: Any) -> None:
        # Base implementation prints traceback/payload-related diagnostics.
        pass


class DeliveryHandler(BaseHTTPRequestHandler):
    server: DeliveryHTTPServer
    protocol_version = 'HTTP/1.1'

    def setup(self) -> None:
        super().setup()
        self.rfile.close()
        self.rfile = io.BufferedReader(DeadlineReader(self.connection, time.monotonic() + self.server.read_deadline_seconds))
        self.connection.settimeout(self.server.read_deadline_seconds)

    def version_string(self) -> str:
        return 'CodexDelivery'

    def log_message(self, format: str, *args: Any) -> None:
        pass  # Request paths, headers and raw bodies are never ordinary logs.

    def _reply(self, status: int, value: dict[str, Any]) -> None:
        body = canonical(value).encode()
        if len(body) > MAX_RESPONSE:
            status = 500
            body = b'{"status":"rejected","code":"response_too_large","execution_enabled":false}'
        self.close_connection = True
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(body)

    def _reject(self, status: int, code: str) -> None:
        self._reply(status, {'status': 'rejected', 'code': code, 'execution_enabled': False})

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        self._reject(code, 'http_request_invalid')

    def do_POST(self) -> None:
        if self.path != '/internal/v1/tools/call':
            self._reject(404, 'not_found'); return
        auth = self.headers.get_all('Authorization', [])
        if len(auth) != 1 or not self.server.provider.authenticated(auth[0]):
            self._reject(401, 'service_unauthorized'); return
        lengths = self.headers.get_all('Content-Length', [])
        if (self.headers.get('Transfer-Encoding') is not None or len(lengths) != 1
                or not lengths[0].isascii() or not lengths[0].isdigit()
                or len(lengths[0]) > 8):
            self._reject(400, 'framing_invalid'); return
        length = int(lengths[0])
        if not 1 <= length <= MAX_REQUEST:
            self._reject(413, 'request_too_large'); return
        if (self.headers.get('Content-Type', '').split(';')[0].strip().lower() != 'application/json'
                or self.headers.get('Content-Encoding', 'identity') != 'identity'):
            self._reject(415, 'content_type_invalid'); return
        try:
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError('incomplete_body')
            payload = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs, parse_constant=_constant)
        except (ValueError, OSError, RecursionError):
            self._reject(400, 'json_invalid'); return
        self._reply(200, self.server.provider.call(payload))

    def do_GET(self) -> None:
        self._reject(405, 'method_unavailable')
