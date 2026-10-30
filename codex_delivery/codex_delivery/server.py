"""Bounded internal HTTP adapter; no default production listener or auto-worker."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import hmac
import json
import re
import threading
import time
from typing import Any

from .provider import DeliveryProvider, MAX_REQUEST, MAX_RESPONSE
from .store import canonical
from .preview_rpc import PREFIX, MAX_PREVIEW_RESPONSE
from .release_authority import PREFIX as AUTHORITY_PREFIX, AuthorityError
from .preview_observation import PATH as PREVIEW_OBSERVATION_PATH


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
        self.preview_rpc = None
        self.release_authority = None
        self.preview_observation = None
        self.build_evidence = None
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
        self._request_deadline = time.monotonic() + self.server.read_deadline_seconds
        self.rfile = io.BufferedReader(DeadlineReader(self.connection, self._request_deadline))
        self.connection.settimeout(self.server.read_deadline_seconds)

    def version_string(self) -> str:
        return 'CodexDelivery'

    def log_message(self, format: str, *args: Any) -> None:
        pass  # Request paths, headers and raw bodies are never ordinary logs.

    def _reply(self, status: int, value: dict[str, Any], *, preview=False) -> None:
        body = canonical(value).encode()
        if len(body) > (MAX_PREVIEW_RESPONSE if preview else MAX_RESPONSE):
            status = 500
            body = b'{"status":"rejected","code":"response_too_large","execution_enabled":false}'
        self.close_connection = True
        if preview:
            self.connection.settimeout(max(.001,self._request_deadline-time.monotonic()))
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
        if self._build_evidence_request():
            return
        if self._authority_request():
            return
        if self._preview_observation_request():
            return
        preview = self.path in {PREFIX+'scope', PREFIX+'frame', PREFIX+'action'}
        if self.path != '/internal/v1/tools/call' and not preview:
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
        if (len(self.headers.get_all('Content-Type', [])) != 1
                or len(self.headers.get_all('Content-Encoding', [])) > 1
                or self.headers.get('Content-Type', '').split(';')[0].strip().lower() != 'application/json'
                or self.headers.get('Content-Encoding', 'identity') != 'identity'):
            self._reject(415, 'content_type_invalid'); return
        try:
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError('incomplete_body')
            payload = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs, parse_constant=_constant)
        except (ValueError, OSError, RecursionError):
            self._reject(400, 'json_invalid'); return
        if preview:
            if self.server.preview_rpc is None:
                self._reject(503, 'preview_disabled'); return
            try:
                result = self.server.preview_rpc.dispatch(self.path[len(PREFIX):],payload,
                    deadline=self._request_deadline)
            except Exception as error:
                code = getattr(error, 'code', 'preview_unavailable')
                if not isinstance(code,str) or re.fullmatch(r'[a-z][a-z0-9_]{0,79}',code) is None:
                    code = 'preview_unavailable'
                self._reject(503, code); return
            self._reply(200, result, preview=True)
        else:
            self._reply(200, self.server.provider.call(payload))

    def do_GET(self) -> None:
        if self._build_evidence_request():
            return
        if self._authority_request():
            return
        self._reject(405, 'method_unavailable')

    def _build_evidence_request(self) -> bool:
        if not self.path.startswith('/internal/v1/build-evidence'):
            return False
        if re.fullmatch(r'/internal/v1/build-evidence/[a-f0-9]{64}/(?:input|artifact)', self.path) is None:
            self._reject(404, 'not_found')
            return True
        if self.command != 'GET':
            self._reject(405, 'method_unavailable')
            return True
        routes = self.server.build_evidence
        if routes is None:
            self._reject(503, 'registry_build_unavailable')
            return True
        auth = self.headers.get_all('Authorization', [])
        try:
            authenticated = len(auth) == 1 and hmac.compare_digest(
                auth[0].encode('utf-8'), ('Bearer ' + routes._token).encode('ascii'))
        except Exception:
            authenticated = False
        if not authenticated:
            self._reject(401, 'service_unauthorized')
            return True
        lengths = self.headers.get_all('Content-Length', [])
        if (self.headers.get_all('Transfer-Encoding', []) or self.headers.get_all('Content-Encoding', [])
                or len(lengths) > 1 or lengths and lengths != ['0']):
            self._reject(400, 'framing_invalid')
            return True
        try:
            result = routes.dispatch('GET', self.path, auth[0], deadline=self._request_deadline)
            if time.monotonic() >= self._request_deadline:
                raise TimeoutError()
            self.connection.settimeout(max(.001, self._request_deadline - time.monotonic()))
            self._reply(200, result)
        except Exception:
            self._reject(503, 'registry_build_unavailable')
        return True

    def _preview_observation_request(self) -> bool:
        if self.path != PREVIEW_OBSERVATION_PATH:
            return False
        if self.server.preview_observation is None:
            self._reject(503, 'preview_runtime_observation_unavailable')
            return True
        auth, lengths = self.headers.get_all('Authorization', []), self.headers.get_all('Content-Length', [])
        if (len(auth) != 1 or len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit()
                or len(lengths[0]) > 4 or not 1 <= int(lengths[0]) <= 1024
                or self.headers.get_all('Transfer-Encoding', []) or self.headers.get_all('Content-Encoding', [])
                or len(self.headers.get_all('Content-Type', [])) != 1
                or self.headers.get_content_type() != 'application/json'):
            self._reject(400, 'preview_runtime_observation_request_invalid')
            return True
        try:
            self._request_deadline = min(self._request_deadline, time.monotonic() + 1)
            self.rfile.raw.deadline = self._request_deadline
            raw = self.rfile.read(int(lengths[0]))
            if len(raw) != int(lengths[0]):
                raise ValueError()
            body, mac = self.server.preview_observation.dispatch(raw, auth[0],
                deadline=min(self._request_deadline, time.monotonic() + 1))
            self.close_connection = True
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Connection', 'close')
            self.send_header('X-Qualification-MAC', mac)
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            self._reject(503, 'preview_runtime_observation_unavailable')
        return True

    def _authority_request(self) -> bool:
        if not self.path.startswith(AUTHORITY_PREFIX):
            return False
        authority = self.server.release_authority
        if authority is None:
            self._reject(503, 'release_authority_unavailable')
            return True
        try:
            authorizations = self.headers.get_all('Authorization', [])
            authority.authenticate(authorizations)
            lengths = self.headers.get_all('Content-Length', [])
            if (self.headers.get_all('Transfer-Encoding', []) or len(lengths) > 1
                    or any(not length.isascii() or not length.isdigit() or len(length) > 5 for length in lengths)
                    or self.headers.get_all('Content-Encoding', [])):
                raise AuthorityError('release_authority_request_invalid', 400)
            payload = None
            if self.command == 'GET':
                if lengths and int(lengths[0]) != 0:
                    raise AuthorityError('release_authority_request_invalid', 400)
            else:
                if (len(lengths) != 1 or not 1 <= int(lengths[0]) <= 4096
                        or len(self.headers.get_all('Content-Type', [])) != 1
                        or self.headers.get('Content-Type', '').split(';')[0].strip().lower() != 'application/json'):
                    raise AuthorityError('release_authority_request_invalid', 400)
                raw = self.rfile.read(int(lengths[0]))
                if len(raw) != int(lengths[0]):
                    raise AuthorityError('release_authority_request_invalid', 400)
                try:
                    payload = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_constant)
                except (ValueError, UnicodeError, RecursionError):
                    raise AuthorityError('release_authority_request_invalid', 400) from None
            result = authority.dispatch(self.command, self.path, authorizations, payload,
                                        deadline=self._request_deadline)
            self._reply(200, result, preview=True)
        except AuthorityError as error:
            self._reject(error.status, error.code)
        except Exception:
            self._reject(503, 'release_authority_unavailable')
        return True
