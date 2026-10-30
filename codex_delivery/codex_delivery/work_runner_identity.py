"""Fixed HA Runner identity client and short-lived, read-only execution gate.

No registration, automatic credential rotation or lease renewal runs here. The
trusted launcher explicitly opens/reports its session; this monitor reads only.
Canonical assignments come from Delivery, never the Controller presence pointer.
"""
from __future__ import annotations

import copy
import http.client
import hashlib
import json
import math
import re
import threading
import time
from urllib.parse import urlsplit

from .store import DeliveryError

RUNNER = re.compile(r'RN-[A-Z2-7]{20,32}\Z')
DIGEST = re.compile(r'sha256:[a-f0-9]{64}\Z')


def fail():
    raise DeliveryError('work_runner_identity_unavailable')


class WorkRunnerClient:
    def __init__(self, base_url, runner_ref, bearer, *, timeout_seconds=2, max_response_bytes=32768):
        try:
            parsed = urlsplit(base_url); port = parsed.port
        except (ValueError, TypeError):
            fail()
        if (parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.path not in {'', '/'}
                or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment
                or not isinstance(runner_ref, str) or not RUNNER.fullmatch(runner_ref)
                or not isinstance(bearer, str) or not 32 <= len(bearer) <= 512
                or any(not 33 <= ord(c) <= 126 for c in bearer)
                or type(timeout_seconds) not in {int, float} or not math.isfinite(timeout_seconds)
                or not 0 < timeout_seconds <= 2 or type(max_response_bytes) is not int
                or not 1024 <= max_response_bytes <= 32768):
            fail()
        self._parsed, self._port = parsed, port
        self.runner_ref, self._bearer = runner_ref, bearer
        self.timeout_seconds, self.max_response_bytes = timeout_seconds, max_response_bytes
        self.session_epoch = None
        self._inflight = threading.Lock()

    def _request(self, method, action, payload=None, *, epoch=None):
        # DNS/kernel stalls must not block identity checks or spawn repeated
        # workers. A timed-out write is unknown; this client never retries it.
        deadline = time.monotonic() + self.timeout_seconds
        if not self._inflight.acquire(timeout=self.timeout_seconds):
            fail()
        if time.monotonic() >= deadline:
            self._inflight.release(); fail()
        payload = copy.deepcopy(payload)
        done = threading.Event(); outcome = {}
        def run():
            try:
                outcome['result'] = self._wire(method, action, payload, epoch=epoch, deadline=deadline)
            except Exception:
                outcome['failed'] = True
            finally:
                self._inflight.release(); done.set()
        worker = threading.Thread(target=run, daemon=True, name='work-runner-http')
        worker.start()
        if not done.wait(max(0, deadline - time.monotonic())) or outcome.get('failed'):
            fail()
        return outcome['result']

    def _wire(self, method, action, payload=None, *, epoch=None, deadline=None):
        if action not in {'sessions', 'heartbeat', 'identity'} or (method, action) not in {('POST', 'sessions'), ('POST', 'heartbeat'), ('GET', 'identity')}:
            fail()
        body = None if payload is None else json.dumps(payload, separators=(',', ':'), allow_nan=False).encode()
        if body is not None and len(body) > 32768:
            fail()
        headers = {'Authorization': 'Bearer ' + self._bearer, 'Connection': 'close'}
        if body is not None:
            headers['Content-Type'] = 'application/json'
        if epoch is not None:
            headers['X-Runner-Session-Epoch'] = str(epoch)
        connection_type = http.client.HTTPSConnection if self._parsed.scheme == 'https' else http.client.HTTPConnection
        deadline = deadline if deadline is not None else time.monotonic() + self.timeout_seconds
        connection = connection_type(self._parsed.hostname, self._port, timeout=max(.001, deadline - time.monotonic()))
        finished = threading.Event(); expired = threading.Event()
        def stop():
            if not finished.wait(max(0, deadline - time.monotonic())):
                expired.set()
                sock = connection.sock
                if sock is not None:
                    try:
                        sock.shutdown(2)
                    except OSError:
                        pass
                    sock.close()
        timer = threading.Thread(target=stop, daemon=True, name='work-identity-deadline')
        timer.start()
        try:
            connection.connect()
            if expired.is_set() or time.monotonic() >= deadline:
                fail()
            connection.request(method, '/internal/v1/work-runners/' + self.runner_ref + '/' + action,
                               body=body, headers=headers)
            response = connection.getresponse()
            if response.status != 200 or response.getheader('Content-Encoding') or response.getheader('Transfer-Encoding'):
                fail()
            raw = response.read(self.max_response_bytes + 1)
            if len(raw) > self.max_response_bytes or expired.is_set() or time.monotonic() >= deadline:
                fail()
            def unique(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        fail()
                    result[key] = value
                return result
            document = json.loads(raw, object_pairs_hook=unique, parse_constant=lambda _: fail())
            if not isinstance(document, dict) or document.get('version') != 1 or not isinstance(document.get('result'), dict):
                fail()
            return document['result']
        except Exception:
            fail()
        finally:
            finished.set(); connection.close(); timer.join(.1)

    def session(self, payload):
        result = self._request('POST', 'sessions', payload)
        if (result.get('runner_id') != self.runner_ref or type(result.get('session_epoch')) is not int
                or result['session_epoch'] < 1):
            fail()
        self.session_epoch = result['session_epoch']
        return result

    def heartbeat(self, payload):
        if self.session_epoch is None or payload.get('session_epoch') != self.session_epoch:
            fail()
        return self._request('POST', 'heartbeat', payload)

    def identity(self):
        if self.session_epoch is None:
            fail()
        return self._request('GET', 'identity', epoch=self.session_epoch)


class CachedWorkRunnerIdentity:
    def __init__(self, reader, *, runner_ref, session_epoch, policy_digest, assignment_gate,
                 refresh_seconds=1, max_age_seconds=3, clock=time.monotonic):
        if (not callable(reader) or not callable(assignment_gate) or not callable(clock)
                or not isinstance(runner_ref, str) or not RUNNER.fullmatch(runner_ref)
                or type(session_epoch) is not int or session_epoch < 1
                or not isinstance(policy_digest, str) or not DIGEST.fullmatch(policy_digest)
                or type(refresh_seconds) not in {int, float} or not math.isfinite(refresh_seconds)
                or type(max_age_seconds) not in {int, float} or not math.isfinite(max_age_seconds)
                or not 0 < refresh_seconds <= 1 or not refresh_seconds < max_age_seconds <= 3):
            fail()
        self.reader, self.runner_ref, self.session_epoch = reader, runner_ref, session_epoch
        self.policy_digest, self.assignment_gate = policy_digest, assignment_gate
        self.refresh_seconds, self.max_age_seconds, self.clock = refresh_seconds, max_age_seconds, clock
        self._lock = threading.Lock(); self._stop = threading.Event(); self._thread = None
        self._snapshot = None; self._expires = 0

    def refresh(self):
        # Expiry is measured from request start, not delayed response arrival.
        began = self.clock()
        try:
            value = self.reader()
            if (not isinstance(value, dict) or value.get('runner_ref') != self.runner_ref
                    or value.get('runner_kind') != 'owner_work_v1'
                    or type(value.get('session_epoch')) is not int or value.get('session_epoch') != self.session_epoch
                    or value.get('policy_digest') != self.policy_digest
                    or 'sha256:' + hashlib.sha256(json.dumps(value.get('policy'), ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest() != self.policy_digest
                    or value.get('registered') is not True
                    or type(value.get('valid_for_ms')) is not int or not 0 <= value['valid_for_ms'] <= 3000):
                fail()
            expires = began + min(self.max_age_seconds, value['valid_for_ms'] / 1000)
            if expires <= self.clock():
                fail()
            result = copy.deepcopy(value)
        except Exception:
            result = None; expires = 0
        with self._lock:
            if self._stop.is_set():
                result = None; expires = 0
            self._snapshot, self._expires = result, expires
        return result is not None

    def __call__(self, runner_ref):
        if runner_ref != self.runner_ref:
            fail()
        with self._lock:
            if self._snapshot is None or self.clock() >= self._expires:
                fail()
            result = copy.deepcopy(self._snapshot)
        if result.get('enabled') is not True or result.get('state') not in {'idle', 'busy'}:
            fail()
        try:
            assignment = self.assignment_gate()
            if assignment is not None:
                if (not isinstance(assignment, dict) or assignment.get('runner_ref') != self.runner_ref
                        or not isinstance(assignment.get('task_ref'), str)
                        or not isinstance(assignment.get('lease_ref'), str)
                        or type(assignment.get('lease_epoch')) is not int or assignment['lease_epoch'] < 1
                        or assignment.get('live') is not True):
                    fail()
                if result['state'] == 'busy' and result.get('active_task_ref') != assignment['task_ref']:
                    fail()
                result.update(state='busy', task_ref=assignment['task_ref'], lease_ref=assignment['lease_ref'])
            elif result['state'] != 'idle' or result.get('accept_new') is not True:
                fail()
        except Exception:
            fail()
        return result

    def retiring(self, runner_ref, task_ref):
        """Fresh node identity for stopping an existing assignment only.

        This never admits work. The caller must independently verify Delivery's
        original owner, revisions, lease and epoch, including pending controls.
        """
        if runner_ref != self.runner_ref:
            fail()
        with self._lock:
            if self._snapshot is None or self.clock() >= self._expires:
                fail()
            result = copy.deepcopy(self._snapshot)
        if (result.get('enabled') is not True or result.get('state') not in {'idle', 'busy'}
                or (result['state'] == 'busy' and result.get('active_task_ref') != task_ref)):
            fail()
        return result

    def start(self):
        if self._thread is not None:
            return
        self.refresh()
        def run():
            while not self._stop.wait(self.refresh_seconds):
                self.refresh()
        self._thread = threading.Thread(target=run, daemon=True, name='work-runner-identity')
        self._thread.start()

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(2.2)
            self._thread = None
        with self._lock:
            self._snapshot = None; self._expires = 0
