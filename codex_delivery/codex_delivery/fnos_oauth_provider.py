"""Default-off trusted OAuth provider candidate; never performs login itself.

There is deliberately no guessed OAuth endpoint, implicit session-file discovery,
Keychain access or file-storage downgrade. A trusted loader returns access-only
facts; a trusted refresh Port runs the fixed official CLI/profile against an
explicitly qualified private runtime session. Neither Port is model supplied.
The enclosing FileReplica bounds caller wait if these trusted calls stall.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
import fcntl
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import threading
import time
from typing import Protocol

from .contracts import DeliveryError, fixed_endpoint, reference
from .fnos_file_port import TOKEN


REQUIRED_SCOPES = frozenset({'trim.file.read', 'trim.file.write'})
CLIENT_ID = 'YJNMPJUGA9'
_POISONED = set()
_POISON_LOCK = threading.Lock()


class OAuthProviderError(DeliveryError):
    """Stable codes only; session contents and underlying errors never escape."""


def _fail(code):
    raise OAuthProviderError(code) from None


def _remaining(deadline):
    if type(deadline) not in {float, int} or not math.isfinite(deadline):
        _fail('fnos_oauth_deadline')
    value = deadline - time.monotonic()
    if value <= 0:
        _fail('fnos_oauth_deadline')
    return value


@dataclass(frozen=True, repr=False)
class OAuthBinding:
    credential_ref: str
    profile: str
    endpoint: str
    uid: int
    device_id: str
    scopes: tuple[str, ...]
    client_id: str = CLIENT_ID

    def __post_init__(self):
        try:
            reference(self.credential_ref)
            fixed_endpoint(self.endpoint)
            if (type(self.profile) is not str or not 1 <= len(self.profile) <= 128
                    or re.fullmatch(r'[A-Za-z0-9_.-]+', self.profile) is None
                    or self.profile in {'.', '..'} or type(self.uid) is not int or self.uid < 0
                    or type(self.device_id) is not str or not 1 <= len(self.device_id) <= 256
                    or any(ord(c) < 33 or ord(c) > 126 for c in self.device_id)
                    or self.client_id != CLIENT_ID or type(self.scopes) is not tuple
                    or any(type(scope) is not str or re.fullmatch(r'[a-z][a-z0-9_.:-]{0,127}', scope) is None
                           for scope in self.scopes)
                    or len(set(self.scopes)) != len(self.scopes)
                    or not REQUIRED_SCOPES <= set(self.scopes) or len(self.scopes) > 32):
                raise ValueError()
        except Exception:
            _fail('fnos_oauth_config_invalid')


@dataclass(frozen=True, repr=False)
class OAuthAccessSession:
    """Only the fixed trusted loader sees the original refreshToken/session."""
    endpoint: str
    uid: int
    client_id: str
    device_id: str
    scopes: tuple[str, ...]
    access_token: str
    expires_at: int


class SessionLoader(Protocol):
    def load(self, *, deadline: float) -> OAuthAccessSession: ...


class OfficialRefreshPort(Protocol):
    """One fixed official CLI `login --refresh`, never login/upload/retry.

    Trusted boot pins the executable, profile, private runtime session, endpoint,
    environment and UID. Output is captured/suppressed and never propagated.
    Success means the CLI returned successfully and persisted the new session.
    Timeout, unknown exit or persistence failure must raise, never retry. The
    provider persists inflight before this call and verifies a new session after.
    """
    def refresh_once(self, *, deadline: float) -> None: ...


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':'),
                      allow_nan=False).encode('ascii')


def _decode(raw):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError()
            value[key] = item
        return value
    value = json.loads(raw, object_pairs_hook=pairs,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    if type(value) is not dict or _json(value) != raw:
        raise ValueError()
    return value


class _AuthJournal:
    """Private responsive local filesystem; directory flock survives workers.

    This dedicated directory must never be removed/reprovisioned to bypass an
    unknown refresh. Recovery requires explicit trusted operator investigation.
    The journal contains expected binding and state, never either OAuth token.
    """
    def __init__(self, path, binding):
        try:
            self.path = Path(path)
            if (not self.path.is_absolute() or '..' in self.path.parts
                    or self.path.parent.resolve(strict=True) != self.path.parent
                    or self.path.name in {'', '.', '..'}):
                raise ValueError()
        except Exception:
            _fail('fnos_oauth_journal_invalid')
        self.binding = asdict(binding)
        self.binding['scopes'] = list(binding.scopes)
        self._identity = None
        self._enrolled = False
        with self.locked() as directory:
            if self.read(directory, initial=True) is None:
                self.write(directory, 'ready')
        self._enrolled = True

    def poisoned(self):
        with _POISON_LOCK:
            return str(self.path) in _POISONED

    def poison(self):
        with _POISON_LOCK:
            _POISONED.add(str(self.path))

    @contextmanager
    def locked(self):
        directory = None
        try:
            directory = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            for part in self.path.parent.parts[1:]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                os.close(directory)
                directory = child
            info = os.fstat(directory)
            identity = info.st_dev, info.st_ino
            if (info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700
                    or self._identity is not None and identity != self._identity):
                _fail('fnos_oauth_journal_invalid')
            self._identity = identity
            try:
                fcntl.flock(directory, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                _fail('fnos_oauth_busy')
            if self.poisoned():
                _fail('fnos_oauth_refresh_unknown')
            yield directory
        except OAuthProviderError:
            raise
        except Exception:
            _fail('fnos_oauth_journal_unavailable')
        finally:
            if directory is not None:
                os.close(directory)

    def read(self, directory, *, initial=False):
        handle = None
        try:
            try:
                handle = os.open(self.path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                 dir_fd=directory)
            except FileNotFoundError:
                if initial and not self._enrolled:
                    return None
                _fail('fnos_oauth_journal_invalid')
            info = os.fstat(handle)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                    or not 1 <= info.st_size <= 8192):
                _fail('fnos_oauth_journal_invalid')
            value = _decode(os.read(handle, 8193))
            if (set(value) != {'version', 'binding', 'state'} or type(value['version']) is not int
                    or value['version'] != 1 or value['binding'] != self.binding
                    or value['state'] not in {'ready', 'inflight', 'fused'}):
                _fail('fnos_oauth_journal_invalid')
            return value['state']
        except OAuthProviderError:
            raise
        except Exception:
            _fail('fnos_oauth_journal_invalid')
        finally:
            if handle is not None:
                os.close(handle)

    def write(self, directory, state):
        handle = None
        name = self.path.name + '.tmp-' + secrets.token_hex(8)
        try:
            raw = _json(dict(version=1, binding=self.binding, state=state))
            handle = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=directory)
            offset = 0
            while offset < len(raw):
                written = os.write(handle, raw[offset:])
                if written <= 0:
                    raise OSError()
                offset += written
            os.fsync(handle)
            os.close(handle)
            handle = None
            os.replace(name, self.path.name, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        except BaseException:
            self.poison()
            _fail('fnos_oauth_journal_unavailable')
        finally:
            if handle is not None:
                os.close(handle)
            try:
                os.unlink(name, dir_fd=directory)
            except FileNotFoundError:
                pass


class FnosOAuthProvider:
    """Access-only callable for FilePort; unknown refresh persistently stops it.

    No automatic 401 recovery exists here. Refresh happens only before returning
    an expiring token, under one persistent claim. A FilePort upload is never
    replayed. Production qualification and actual CLI/session adapters belong to
    trusted boot, outside this candidate.
    """
    def __init__(self, binding, session_loader=None, refresh_port=None, *, journal_path=None):
        if (type(binding) is not OAuthBinding
                or session_loader is not None and not callable(getattr(session_loader, 'load', None))
                or refresh_port is not None and not callable(getattr(refresh_port, 'refresh_once', None))):
            _fail('fnos_oauth_config_invalid')
        self._binding = binding
        self._loader, self._refresh = session_loader, refresh_port
        self._lock = threading.Lock()
        self._journal = None
        if session_loader is not None:
            if journal_path is None:
                _fail('fnos_oauth_config_invalid')
            self._journal = _AuthJournal(journal_path, binding)

    @property
    def binding(self):
        return self._binding

    def _load(self, deadline):
        _remaining(deadline)
        try:
            session = self._loader.load(deadline=deadline)
        except BaseException:
            _fail('fnos_oauth_session_unavailable')
        _remaining(deadline)
        binding = self._binding
        if (type(session) is not OAuthAccessSession or session.endpoint != binding.endpoint
                or type(session.uid) is not int or session.uid != binding.uid
                or session.client_id != binding.client_id or session.device_id != binding.device_id
                or type(session.scopes) is not tuple or any(type(item) is not str for item in session.scopes)
                or len(session.scopes) != len(binding.scopes) or set(session.scopes) != set(binding.scopes)
                or type(session.expires_at) is not int or session.expires_at <= 0
                or type(session.access_token) is not str or not 16 <= len(session.access_token) <= 4096
                or TOKEN.fullmatch(session.access_token) is None):
            _fail('fnos_oauth_session_invalid')
        return session

    def __call__(self, *, deadline):
        _remaining(deadline)
        if self._loader is None:
            _fail('fnos_oauth_disabled')
        if not self._lock.acquire(blocking=False):
            _fail('fnos_oauth_busy')
        try:
            with self._journal.locked() as directory:
                if self._journal.read(directory) != 'ready':
                    _fail('fnos_oauth_refresh_unknown')
                session = self._load(deadline)
                if session.expires_at > time.time() + 60:
                    _remaining(deadline)
                    return session.access_token
                if self._refresh is None:
                    _fail('fnos_oauth_refresh_unavailable')
                _remaining(deadline)
                self._journal.write(directory, 'inflight')
                try:
                    _remaining(deadline)
                    result = self._refresh.refresh_once(deadline=deadline)
                    _remaining(deadline)
                    if result is not None:
                        _fail('fnos_oauth_refresh_unknown')
                    renewed = self._load(deadline)
                    if renewed.expires_at <= time.time() + 60 or renewed.access_token == session.access_token:
                        _fail('fnos_oauth_refresh_unknown')
                    _remaining(deadline)
                    self._journal.write(directory, 'ready')
                    _remaining(deadline)
                    return renewed.access_token
                except BaseException:
                    # Persist unknown even if refresh raised after writing the
                    # official session. Never return a new token or retry here.
                    try:
                        self._journal.write(directory, 'fused')
                    except BaseException:
                        pass
                    self._journal.poison()
                    _fail('fnos_oauth_refresh_unknown')
        finally:
            self._lock.release()
