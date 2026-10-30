"""Fixed Manager GET projections; bounded DNS/connect/HTTP without ambient proxies."""
from __future__ import annotations

import http.client
import ipaddress
import json
import math
import re
import socket
import ssl
import threading
import time
from urllib.parse import urlsplit

from .release_authority import (AuthorityError, TARGET_FIELDS, MANAGER_FIELDS, BACKUP_FIELDS,
                                REF, _closed)

ROLES = {'broker': TARGET_FIELDS, 'manager': MANAGER_FIELDS, 'backup': BACKUP_FIELDS}
MAX_RESPONSE = 32768


def fail():
    raise AuthorityError() from None


def decode(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                fail()
            result[key] = value
        return result
    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda _: fail())
    except Exception:
        fail()


def origin_binding(origin):
    try:
        if type(origin) is not str or not 1 <= len(origin) <= 512 or any(ord(c) <= 32 or ord(c) > 126 for c in origin):
            fail()
        parsed = urlsplit(origin)
        host, port = parsed.hostname, parsed.port
        if (parsed.scheme not in {'http', 'https'} or not host or parsed.username is not None
                or parsed.password is not None or parsed.path or parsed.query or parsed.fragment):
            fail()
        try:
            address = ipaddress.ip_address(host)
            if str(address) != host or address.is_unspecified or address.is_multicast:
                fail()
            rendered = '[' + host + ']' if address.version == 6 else host
        except ValueError:
            if (len(host) > 253 or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9_-]{0,61}[a-z0-9])?', part)
                    for part in host.split('.'))):
                fail()
            rendered = host
        if origin != parsed.scheme + '://' + rendered + (':' + str(port) if port is not None else ''):
            fail()
        if port is not None and not 1 <= port <= 65535:
            fail()
        return parsed.scheme, host, port or (443 if parsed.scheme == 'https' else 80)
    except Exception:
        fail()


class _HeaderBudget:
    def __init__(self, stream):
        self._stream, self._used = stream, 0

    def readline(self, limit=-1):
        available = MAX_RESPONSE + 1 - self._used
        raw = self._stream.readline(available if limit < 0 else min(limit, available))
        self._used += len(raw)
        if self._used > MAX_RESPONSE:
            fail()
        return raw

    def __getattr__(self, name):
        return getattr(self._stream, name)


class _BoundedResponse(http.client.HTTPResponse):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fp = _HeaderBudget(self.fp)


class ManagerTargetObservationClient:
    def __init__(self, origin, token, *, target_allowlists, timeout_seconds=5, monotonic=time.monotonic):
        self._binding = origin_binding(origin)
        if (type(token) is not str or not 32 <= len(token) <= 4096
                or any(not 33 <= ord(c) <= 126 for c in token)
                or type(target_allowlists) is not dict or set(target_allowlists) != set(ROLES)
                or type(timeout_seconds) not in {int, float} or not math.isfinite(timeout_seconds)
                or not .05 <= timeout_seconds <= 5):
            fail()
        self._targets = {}
        for role, refs in target_allowlists.items():
            if (type(refs) is not frozenset or not 1 <= len(refs) <= 128
                    or any(type(ref) is not str or re.fullmatch(REF, ref) is None for ref in refs)):
                fail()
            self._targets[role] = refs
        self._token, self._timeout, self._now = token, timeout_seconds, monotonic
        self._slots = threading.BoundedSemaphore(2)

    def __call__(self, target_ref, role, *, deadline):
        if (type(target_ref) is not str or re.fullmatch(REF, target_ref) is None
                or type(role) is not str or role not in ROLES or target_ref not in self._targets[role]
                or type(deadline) not in {int, float} or not math.isfinite(deadline)):
            fail()
        limit = min(deadline, self._now() + self._timeout)
        if self._now() >= limit or not self._slots.acquire(blocking=False):
            fail()
        completed, outcome = threading.Event(), []
        def work():
            try:
                outcome.append((True, self._exchange(target_ref, role, limit)))
            except Exception:
                outcome.append((False, None))
            finally:
                self._slots.release()
                completed.set()
        try:
            threading.Thread(target=work, name='release-target-observation', daemon=True).start()
        except Exception:
            self._slots.release()
            fail()
        if not completed.wait(max(0, limit - self._now())) or self._now() >= limit:
            fail()
        if not outcome[0][0]:
            fail()
        return outcome[0][1]

    def _exchange(self, target_ref, role, deadline):
        scheme, host, port = self._binding
        lock, expired = threading.Lock(), threading.Event()
        watched = [None]
        connection = response = None
        def remaining():
            value = deadline - self._now()
            if value <= 0 or expired.is_set():
                fail()
            return value
        def expire():
            expired.set()
            with lock:
                selected = watched[0]
                if selected is not None:
                    try:
                        selected.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    selected.close()
        timer = threading.Timer(max(0, deadline - self._now()), expire)
        timer.daemon = True
        timer.start()
        try:
            remaining()
            # Exactly one resolution and one selected address connect. If DNS
            # outlives the deadline this worker retains its slot until return.
            addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
            remaining()
            if not addresses:
                fail()
            family, kind, protocol, _, address = addresses[0]
            if family not in {socket.AF_INET, socket.AF_INET6} or kind != socket.SOCK_STREAM:
                fail()
            with lock:
                remaining()
                sock = socket.socket(family, kind, protocol)
                watched[0] = sock
            sock.settimeout(remaining())
            sock.connect(address)
            remaining()
            if scheme == 'https':
                context = ssl.create_default_context()
                with lock:
                    remaining()
                    sock = context.wrap_socket(sock, server_hostname=host, do_handshake_on_connect=False)
                    watched[0] = sock
                sock.settimeout(remaining())
                sock.do_handshake()
            connection = http.client.HTTPConnection(host, port, timeout=remaining())
            connection.sock = sock
            connection.response_class = _BoundedResponse
            path = '/internal/v2/release-target-observations/' + target_ref + '/' + role
            sock.settimeout(remaining())
            connection.request('GET', path, headers={'Authorization': 'Bearer ' + self._token,
                'Accept': 'application/json', 'Connection': 'close'})
            sock.settimeout(remaining())
            response = connection.getresponse()
            remaining()
            headers = response.getheaders()
            names = [name.lower() for name, _ in headers]
            if (response.status != 200 or len(names) != len(set(names))
                    or names.count('content-length') != 1 or names.count('content-type') != 1
                    or 'transfer-encoding' in names or 'content-encoding' in names
                    or response.getheader('Content-Type') not in {'application/json', 'application/json; charset=utf-8'}):
                fail()
            length = response.getheader('Content-Length')
            if not re.fullmatch(r'[1-9][0-9]{0,4}', length) or int(length) > MAX_RESPONSE:
                fail()
            size, raw = int(length), bytearray()
            while len(raw) < size:
                sock.settimeout(remaining())
                chunk = response.read1(min(8192, size - len(raw)))
                if not chunk:
                    fail()
                raw.extend(chunk)
            remaining()
            return _closed(decode(bytes(raw)), ROLES[role])
        finally:
            timer.cancel()
            try:
                if response is not None:
                    response.close()
            finally:
                try:
                    if connection is not None:
                        connection.close()
                finally:
                    with lock:
                        if watched[0] is not None:
                            watched[0].close()
