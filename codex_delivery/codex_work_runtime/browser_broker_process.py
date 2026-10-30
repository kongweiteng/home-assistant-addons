"""One bounded trusted subprocess for synchronous DNS/HTTP, no worker threads."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import struct
import subprocess
import sys
import time
from typing import Callable

from .errors import WorkRuntimeError
from .public_browser import PublicBrowserGrant, PublicResponse, MAX_FRAME, MAX_RESOURCE


def _fail(code='browser_broker_failed', *, uncertain=False):
    return WorkRuntimeError(code, 'Public browser broker did not complete.', uncertain=uncertain)


def _canonical(value):
    return json.dumps(value, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode()


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError('duplicate')
        result[key] = value
    return result


class BrokerProcess:
    """Private owner API; models cannot choose the worker, grant or transport."""
    def __init__(self, grant: PublicBrowserGrant, *, cpu_count=2, cleanup_seconds=3):
        self.grant = grant
        self.cpu_count = cpu_count
        self.cleanup_seconds = cleanup_seconds
        self.process = None
        self.pidfd = None
        self.sequence = 0
        self.output_bytes = 0
        self.stderr_bytes = 0
        self.buffer = bytearray()
        self.guard: Callable[[], None] = lambda: self.grant.remaining()
        self.inflight = False
        self.closed = False
        self.fetch_deadline = None

    def _active(self):
        # Cancellation and the original grant have priority over this shorter
        # per-fetch deadline. No resolver, header or body progress extends it.
        self.guard()
        if self.fetch_deadline is not None and time.monotonic() >= self.fetch_deadline:
            self.close()
            raise _fail('browser_timeout')

    @staticmethod
    def _command(cpu_count):
        path = Path(__file__).with_name('browser_broker_worker.py').absolute()
        if path.resolve() != path:
            raise _fail('browser_broker_code_invalid')
        for item in (path, *path.parents):
            info = item.lstat()
            if info.st_uid != 0 or info.st_mode & 0o022 or item.is_symlink():
                raise _fail('browser_broker_code_invalid')
        if not stat.S_ISREG(path.stat().st_mode):
            raise _fail('browser_broker_code_invalid')
        return [sys.executable, '-I', '-B', str(path), str(os.getpid()), str(cpu_count)]

    def _send(self, value):
        raw = _canonical(value)
        if len(raw) > MAX_FRAME:
            raise _fail('browser_broker_frame_limit')
        pending = memoryview(struct.pack('!I', len(raw))+raw)
        with selectors.DefaultSelector() as selector:
            selector.register(self.process.stdin, selectors.EVENT_WRITE)
            while pending:
                self._active()
                if not selector.select(.02):
                    if self.process.poll() is not None:
                        raise _fail()
                    continue
                try:
                    count = os.write(self.process.stdin.fileno(), pending)
                except BlockingIOError:
                    continue
                except OSError:
                    raise _fail() from None
                if count <= 0:
                    raise _fail()
                pending = pending[count:]

    def _start(self):
        if self.process is not None:
            return
        self._active()
        self.process = subprocess.Popen(self._command(self.cpu_count), stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True, env={'LANG': 'C'},
            start_new_session=True)
        try:
            self.pidfd = os.pidfd_open(self.process.pid)
        except OSError:
            self.process.kill()
            try:
                self.process.wait(timeout=self.cleanup_seconds)
            except subprocess.TimeoutExpired:
                raise _fail('browser_broker_cleanup_uncertain', uncertain=True) from None
            raise _fail('browser_broker_pidfd_unavailable') from None
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            os.set_blocking(stream.fileno(), False)
        self._send({'version': 1, 'grant': {'urls': list(self.grant.urls),
            'expires_at': self.grant.expires_at.isoformat(), 'monotonic_deadline': self.grant.monotonic_deadline,
            'grant_ref': self.grant.grant_ref, 'input_digest': self.grant.input_digest}})

    def fetch(self, url, method='GET') -> PublicResponse:
        if self.closed:
            raise _fail('browser_broker_closed')
        if self.inflight:
            raise _fail('browser_broker_busy')
        self.inflight = True
        try:
            self._active()
            self.fetch_deadline = time.monotonic()+min(8, self.grant.remaining())
            self._active(); self.grant.require(url)
            self._start()
            self.sequence += 1
            if self.sequence > 64 or method not in {'GET', 'HEAD'}:
                raise _fail('browser_broker_request_invalid')
            self._send({'id': self.sequence, 'url': url, 'method': method})
            with selectors.DefaultSelector() as selector:
                selector.register(self.process.stdout, selectors.EVENT_READ)
                selector.register(self.process.stderr, selectors.EVENT_READ)
                while True:
                    self._active()
                    if len(self.buffer) >= 4:
                        size = struct.unpack('!I', self.buffer[:4])[0]
                        if not 1 <= size <= MAX_FRAME:
                            raise _fail('browser_broker_frame_limit')
                        if len(self.buffer) >= size+4:
                            raw = bytes(self.buffer[4:size+4]); del self.buffer[:size+4]
                            if self.buffer:
                                raise _fail('browser_broker_protocol_invalid')
                            self._active()
                            response = self._response(raw, url)
                            self._active()
                            return response
                    for key, _ in selector.select(.02):
                        try:
                            data = os.read(key.fd, 65536)
                        except BlockingIOError:
                            continue
                        if key.fd == self.process.stderr.fileno():
                            self.stderr_bytes += len(data)
                            if self.stderr_bytes > 65536:
                                raise _fail('browser_broker_output_limit')
                            if not data:
                                selector.unregister(key.fd)
                            continue
                        if not data:
                            raise _fail()
                        self.output_bytes += len(data)
                        if self.output_bytes > 32*1024**2 or len(self.buffer)+len(data) > MAX_FRAME+4:
                            raise _fail('browser_broker_output_limit')
                        self.buffer.extend(data)
        finally:
            self.inflight = False
            self.fetch_deadline = None

    def _response(self, raw, url):
        try:
            value = json.loads(raw, object_pairs_hook=_pairs,
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError('constant')))
            if not isinstance(value, dict) or type(value.get('id')) is not int or value['id'] != self.sequence:
                raise ValueError('id')
            if set(value) == {'id', 'error'}:
                if not isinstance(value['error'], str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,79}', value['error']):
                    raise ValueError('error')
                raise _fail(value['error'])
            if (set(value) != {'id', 'url', 'status', 'content_type', 'location', 'body'} or value['url'] != url
                    or type(value['status']) is not int or not 100 <= value['status'] <= 599
                    or not isinstance(value['content_type'], str) or not re.fullmatch(r'[\x20-\x7e]{0,128}', value['content_type'])
                    or value['location'] is not None and (not isinstance(value['location'], str) or len(value['location']) > 4096)
                    or not isinstance(value['body'], str) or len(value['body']) > 4*((MAX_RESOURCE+2)//3)):
                raise ValueError('response')
            body = base64.b64decode(value['body'], validate=True)
            if len(body) > MAX_RESOURCE:
                raise ValueError('body')
            self.grant.require(url)
            if value['location'] is not None:
                self.grant.require(value['location'])
            return PublicResponse(url, value['status'], value['content_type'], body, value['location'])
        except (ValueError, TypeError, KeyError, UnicodeError):
            raise _fail('browser_broker_protocol_invalid') from None

    def close(self):
        # Closing dispatch is irreversible, even when confirmed cleanup fails.
        self.closed = True
        process = self.process
        if process is None:
            return
        try:
            if process.poll() is None:
                if self.pidfd is None:
                    raise _fail('browser_broker_cleanup_uncertain', uncertain=True)
                try:
                    signal.pidfd_send_signal(self.pidfd, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=self.cleanup_seconds)
            if process.poll() is None:
                raise _fail('browser_broker_cleanup_uncertain', uncertain=True)
        except (OSError, subprocess.TimeoutExpired):
            raise _fail('browser_broker_cleanup_uncertain', uncertain=True) from None
        finally:
            if process.poll() is not None:
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()
                if self.pidfd is not None:
                    os.close(self.pidfd); self.pidfd = None
