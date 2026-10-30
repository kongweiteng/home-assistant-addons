"""Independent signed read-only measurement of this trusted service process.

No expected facts, runtime selection, guest command or authority mutation enters
this Port. Its image/source and container epoch are verified by the observer.
"""
from datetime import datetime
from dataclasses import dataclass, field
import hmac
import json
import os
from pathlib import Path
import re
import resource
import stat
import threading
import time
from zoneinfo import ZoneInfo

from .store import DeliveryError, canonical

PATH = '/internal/v1/work-preview/runtime-observation'
REQUEST_DOMAIN = b'codex-preview-runtime-observation-request-v1\0'
RESPONSE_DOMAIN = b'codex-preview-runtime-observation-response-v1\0'
TZ = ZoneInfo('Asia/Shanghai')
REGISTRATION_ROOT = Path('/etc/codex-delivery/preview-observation')


def _fail():
    raise DeliveryError('preview_runtime_observation_unavailable')


def mac(key, domain, raw):
    return hmac.digest(key.encode('ascii'), domain + raw, 'sha256').hex()


def measure_process():
    # Procfs is not available through Docker's filesystem archive API. This
    # reviewed in-process collector reads only its own fixed kernel metadata.
    raw = Path('/proc/self/status').read_bytes()
    if len(raw) > 16384:
        _fail()
    rows = dict(line.split(':', 1) for line in raw.decode('ascii').splitlines() if ':' in line)
    security = {name: rows[name].strip() for name in ('CapEff', 'CapPrm', 'CapInh', 'CapAmb', 'CapBnd', 'NoNewPrivs')}
    security['uids'] = [int(item) for item in rows['Uid'].split()]
    security['actual_threads'] = int(rows['Threads'])
    return {'process_id': os.getpid(), 'profile': {'uid': os.geteuid(),
        'address_bytes': resource.getrlimit(resource.RLIMIT_AS)[1],
        'threads': resource.getrlimit(resource.RLIMIT_NPROC)[1],
        'open_files': resource.getrlimit(resource.RLIMIT_NOFILE)[1],
        'core_bytes': resource.getrlimit(resource.RLIMIT_CORE)[1], 'cpus': len(os.sched_getaffinity(0))},
        'security': security}


@dataclass(frozen=True, repr=False)
class RuntimeObservationPlan:
    """Read before native UID drop; holds one private runtime-only key in RAM."""
    _key: str = field(repr=False)
    _forbidden: tuple = field(repr=False)

    @classmethod
    def load(cls, *, forbidden_credentials=()):
        parent = directory = descriptor = None
        try:
            try:
                REGISTRATION_ROOT.lstat()
            except FileNotFoundError:
                return None
            parent = os.open(REGISTRATION_ROOT.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            metadata = os.fstat(parent)
            if metadata.st_uid != 0 or metadata.st_mode & 0o022:
                _fail()
            directory = os.open(REGISTRATION_ROOT.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            metadata = os.fstat(directory)
            if metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) != 0o700:
                _fail()
            descriptor = os.open('registration.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            metadata = os.fstat(descriptor)
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0 or metadata.st_nlink != 1
                    or stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_size > 16384):
                _fail()
            raw = os.read(descriptor, 16385)
            if len(raw) > 16384:
                _fail()
            def pairs(items):
                result = {}
                for key, value in items:
                    if key in result:
                        _fail()
                    result[key] = value
                return result
            value = json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda _: _fail())
            if value == {'version': 1, 'enabled': False} and type(value['version']) is int:
                return None
            if (not isinstance(value, dict) or set(value) != {'version', 'enabled', 'runtime_key'}
                    or type(value['version']) is not int or value['version'] != 1 or value['enabled'] is not True):
                _fail()
            # Validate key independence now, without taking any runtime fact.
            RuntimeObservationRPC(value['runtime_key'], forbidden_credentials=forbidden_credentials)
            return cls(value['runtime_key'], tuple(forbidden_credentials))
        except Exception:
            _fail()
        finally:
            for opened in (descriptor, directory, parent):
                if opened is not None:
                    os.close(opened)

    def create(self):
        return RuntimeObservationRPC(self._key, forbidden_credentials=self._forbidden)


class RuntimeObservationRPC:
    def __init__(self, key, *, forbidden_credentials=(), measure=None, now=None):
        if (not isinstance(key, str) or not 32 <= len(key) <= 512 or any(not 33 <= ord(char) <= 126 for char in key)
                or any(not isinstance(item, str) or hmac.compare_digest(key.encode('utf-8'), item.encode('utf-8'))
                       for item in forbidden_credentials)):
            _fail()
        self._key = key
        self._measure = measure or measure_process
        self._now = now or (lambda: datetime.now(TZ))
        self._lock = threading.Lock()

    def dispatch(self, raw, authorization, *, deadline):
        if (not isinstance(raw, bytes) or not 1 <= len(raw) <= 1024 or not isinstance(authorization, str)
                or not hmac.compare_digest(authorization, 'PreviewRuntime-HMAC-SHA256 ' + mac(self._key, REQUEST_DOMAIN, raw))):
            _fail()
        def pairs(items):
            value = {}
            for key, item in items:
                if key in value:
                    _fail()
                value[key] = item
            return value
        try:
            value = json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda _: _fail())
            if (not isinstance(value, dict) or set(value) != {'version', 'nonce'} or type(value['version']) is not int
                    or value['version'] != 1 or not isinstance(value['nonce'], str)
                    or re.fullmatch(r'[a-f0-9]{64}', value['nonce']) is None or time.monotonic() >= deadline):
                _fail()
            if not self._lock.acquire(blocking=False):
                _fail()
            try:
                observed = self._now().astimezone(TZ).isoformat()
                actual = self._measure()
                if not isinstance(actual, dict) or set(actual) != {'process_id', 'profile', 'security'} or time.monotonic() >= deadline:
                    _fail()
                body = canonical(value | actual | {'observed_at': observed}).encode()
                if len(body) > 16384:
                    _fail()
                return body, mac(self._key, RESPONSE_DOMAIN, body)
            finally:
                self._lock.release()
        except Exception:
            _fail()
