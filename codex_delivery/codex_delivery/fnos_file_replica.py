"""Default-off fnOS file replica core, for NAS-live / primary-loss recovery.

No HTTP, OAuth, credentials, NAS discovery, mount, or production qualification.
FilePort is a trusted injected capability, NOT a stable-write ObjectStore.
Local journal storage and the fixed Python/crypto implementation must remain
responsive. Port calls and crypto work run in bounded daemon threads; a timeout
permanently poisons this journal in this process. The original thread retains a
cross-process file lock until it actually exits. Late results never clear it.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from enum import Enum
import fcntl
import json
import math
import os
from pathlib import Path
import stat
import threading
import time
from typing import Protocol

from .durability import ReplicaReceipt, Snapshot, digest
from .encrypted_replica import EncryptedReplica, OBJECT, OVERHEAD, REF, SHA, ReplicaError


POLICY = 'nas-live-primary-loss-v1'
JOURNAL_LIMIT = 16384
_POISONED = set()
_POISON_LOCK = threading.Lock()
_CODES = frozenset({
    'replica_disabled', 'replica_config_invalid', 'replica_codec_store_forbidden',
    'replica_journal_invalid', 'replica_journal_changed', 'replica_journal_missing',
    'replica_journal_permissions_invalid', 'replica_journal_unavailable',
    'replica_busy', 'replica_worker_unsettled', 'replica_parent_interrupted',
    'replica_port_fused', 'replica_port_unavailable', 'replica_port_timeout',
    'replica_port_protocol_invalid', 'replica_binding_drift', 'replica_deadline',
    'replica_read_failed', 'replica_write_unknown', 'replica_object_missing',
    'replica_snapshot_invalid', 'replica_snapshot_conflict',
    'replica_recovery_read_only', 'replica_reconcile_not_required',
})


class FileReplicaError(ReplicaError):
    """Only stable codes cross the trusted boundary; never raw Port errors."""


def _fail(code):
    raise FileReplicaError(code)


@dataclass(frozen=True, repr=False)
class FileBinding:
    """Non-secret administrator references, not proof of a real NAS or ACL."""
    store_ref: str
    namespace_ref: str
    qualification_ref: str

    def __post_init__(self):
        if any(not isinstance(value, str) or REF.fullmatch(value) is None
               for value in asdict(self).values()):
            _fail('replica_config_invalid')


class UploadResult(Enum):
    CREATED = 'created'
    EXISTING = 'existing'


class FilePort(Protocol):
    """Fixed endpoint/namespace, unique writer, at most one upload attempt.

    None means confirmed absence, never forbidden/unreadable/partial/timeout.
    upload_once may perform one documented checkUpload and one upload, both
    without overwrite, rename, resume or automatic retries. An exception may
    mean unknown. Neither upload result nor readback proves NAS power-loss
    durability. No credential, path, URL or delete operation is exposed here.
    """
    binding: FileBinding

    def get_exact(self, ref: str, *, deadline: float) -> bytes | None: ...
    def upload_once(self, ref: str, envelope: bytes, *, deadline: float) -> UploadResult: ...


class _NoObjectStore:
    """Codec constructor sentinel; FilePort never implements ObjectStore."""
    def get(self, *args):
        _fail('replica_codec_store_forbidden')

    def put_if_absent(self, *args):
        _fail('replica_codec_store_forbidden')


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True,
                      separators=(',', ':'), allow_nan=False).encode('ascii')


def _decode(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError()
            result[key] = value
        return result
    value = json.loads(raw, object_pairs_hook=pairs,
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    if not isinstance(value, dict) or _json(value) != raw:
        raise ValueError()
    return value


class _Journal:
    def __init__(self, path, binding):
        try:
            self.path = Path(path)
            if (not self.path.is_absolute() or self.path.parent.resolve(strict=True) != self.path.parent
                    or self.path.name in {'', '.', '..'} or '..' in self.path.parts):
                raise ValueError()
        except Exception:
            _fail('replica_journal_invalid')
        self.binding = binding
        self._pins = {}
        with self.locked(initial=True) as directory:
            state = self.read(directory)
            if state is None:
                if self._existed:
                    _fail('replica_journal_missing')
                self.write(directory, self.ready())
            elif state['state'] == 'inflight':
                worker = self.worker_lock(directory)
                os.close(worker)
                state.update(state='fused', code='replica_write_unknown' if state['may_write']
                             else 'replica_parent_interrupted')
                self.write(directory, state)

    def ready(self):
        return dict(version=1, binding=self.binding, state='ready', object_ref=None,
                    may_write=False, code=None)

    def _pin(self, label, handle):
        info = os.fstat(handle)
        identity = info.st_dev, info.st_ino
        if label in self._pins and self._pins[label] != identity:
            _fail('replica_journal_changed')
        self._pins[label] = identity
        return info

    def _directory(self):
        handle = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in self.path.parent.parts[1:]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=handle)
                os.close(handle)
                handle = child
            info = self._pin('directory', handle)
            if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
                _fail('replica_journal_permissions_invalid')
            return handle
        except BaseException:
            os.close(handle)
            raise

    def _file(self, directory, suffix, *, create=False):
        name = self.path.name + suffix
        existed = True
        try:
            handle = os.open(name, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        except FileNotFoundError:
            if not create:
                _fail('replica_journal_missing')
            try:
                handle = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=directory)
                existed = False
            except FileExistsError:
                handle = os.open(name, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        try:
            info = self._pin(suffix, handle)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
                _fail('replica_journal_permissions_invalid')
            return handle, existed
        except BaseException:
            os.close(handle)
            raise

    @contextmanager
    def locked(self, *, initial=False):
        directory = lock = None
        try:
            directory = self._directory()
            present = True
            try:
                os.stat(self.path.name, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                present = False
            lock, existed = self._file(directory, '.lock', create=initial and not present)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                _fail('replica_busy')
            worker, worker_existed = self._file(directory, '.worker-lock', create=initial and not present)
            os.close(worker)
            self._existed = existed or worker_existed
            yield directory
        except FileReplicaError:
            raise
        except OSError:
            _fail('replica_journal_unavailable')
        finally:
            if lock is not None:
                os.close(lock)
            if directory is not None:
                os.close(directory)

    def worker_lock(self, directory):
        handle, _ = self._file(directory, '.worker-lock')
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except BlockingIOError:
            os.close(handle)
            _fail('replica_worker_unsettled')

    def read(self, directory):
        try:
            handle = os.open(self.path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        except FileNotFoundError:
            return None
        try:
            with os.fdopen(handle, 'rb') as source:
                info = os.fstat(source.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                        or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                        or not 1 <= info.st_size <= JOURNAL_LIMIT):
                    _fail('replica_journal_invalid')
                raw = source.read(JOURNAL_LIMIT + 1)
            state = _decode(raw)
            if (set(state) != set(self.ready()) or type(state['version']) is not int or state['version'] != 1
                    or state['binding'] != self.binding or state['state'] not in {'ready', 'inflight', 'fused'}
                    or type(state['may_write']) is not bool
                    or state['code'] is not None and state['code'] not in _CODES):
                raise ValueError()
            if state['state'] == 'ready':
                if state != self.ready():
                    raise ValueError()
            elif not isinstance(state['object_ref'], str) or OBJECT.fullmatch(state['object_ref']) is None:
                raise ValueError()
            elif ((state['state'] == 'inflight' and state['code'] is not None)
                  or (state['state'] == 'fused' and state['code'] is None)):
                raise ValueError()
            return state
        except FileReplicaError:
            raise
        except Exception:
            _fail('replica_journal_invalid')

    def write(self, directory, state):
        raw = _json(state)
        if len(raw) > JOURNAL_LIMIT:
            _fail('replica_journal_invalid')
        temporary = '.fnos-journal-' + os.urandom(16).hex()
        handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=directory)
        try:
            with os.fdopen(handle, 'wb') as output:
                output.write(raw)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path.name, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass


class _CompletedPortFailure(Exception):
    pass


class FnosFileReplica:
    """Explicit trusted injection only. No Port => disabled, without writes.

    recovery_only permits exact receipt reads into a NEW local recovery context;
    it never enrolls a writer or clears an unknown operation. A missing journal
    beside existing lock files is always an error, including recovery mode.
    """
    def __init__(self, port=None, encryption_key=None, *, binding=None, key_ref=None,
                 failure_domain=None, journal_path=None, timeout_seconds=10,
                 maxbytes=128 * 1024 * 1024, recovery_only=False):
        self._port = port
        self._disabled = port is None
        self._lock = threading.Lock()
        self._permanent = False
        if self._disabled:
            return
        if (type(binding) is not FileBinding or type(getattr(port, 'binding', None)) is not FileBinding
                or port.binding != binding or not callable(getattr(port, 'get_exact', None))
                or not callable(getattr(port, 'upload_once', None)) or type(recovery_only) is not bool
                or type(timeout_seconds) not in {float, int} or not math.isfinite(timeout_seconds)
                or not 0.02 <= timeout_seconds <= 300):
            _fail('replica_config_invalid')
        self._binding, self._recovery_only = binding, recovery_only
        self._timeout = float(timeout_seconds)
        self._codec = EncryptedReplica(_NoObjectStore(), encryption_key, key_ref=key_ref,
                                       failure_domain=failure_domain, maxbytes=maxbytes)
        config = dict(policy=POLICY, port=asdict(binding), key_ref=key_ref,
                      failure_domain=failure_domain, maxbytes=maxbytes)
        self._journal = _Journal(journal_path, config)
        self._identity = str(self._journal.path)

    def _available(self):
        if self._disabled:
            _fail('replica_disabled')
        with _POISON_LOCK:
            poisoned = self._identity in _POISONED
        if self._permanent or poisoned:
            _fail('replica_port_fused')

    def _poison(self):
        self._permanent = True
        with _POISON_LOCK:
            _POISONED.add(self._identity)

    def _remaining(self, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _fail('replica_deadline')
        return remaining

    def _bounded(self, directory, deadline, callback, *, port=False):
        self._available()
        self._remaining(deadline)
        handle = self._journal.worker_lock(directory)
        done, result = threading.Event(), {}
        def run():
            try:
                if port and (type(getattr(self._port, 'binding', None)) is not FileBinding
                             or self._port.binding != self._binding):
                    result['code'] = 'replica_binding_drift'
                    return
                result['value'] = callback()
                if port and (type(getattr(self._port, 'binding', None)) is not FileBinding
                             or self._port.binding != self._binding):
                    result['code'] = 'replica_binding_drift'
                    return
                result['ok'] = True
            except BaseException:
                result['ok'] = False
            finally:
                os.close(handle)
                done.set()
        try:
            threading.Thread(target=run, daemon=True, name='fnos-file-port').start()
        except BaseException:
            os.close(handle)
            _fail('replica_port_unavailable')
        if not done.wait(max(0, deadline - time.monotonic())) or time.monotonic() >= deadline:
            self._poison()
            _fail('replica_port_timeout')
        if 'code' in result:
            _fail(result['code'])
        if result.get('ok') is not True:
            raise _CompletedPortFailure()
        return result['value']

    def _read(self, directory, deadline, receipt):
        raw = self._bounded(directory, deadline,
            lambda: self._port.get_exact(receipt.object_ref, deadline=deadline), port=True)
        if raw is not None and (type(raw) is not bytes or not 1 <= len(raw) <= self._codec.maxbytes + OVERHEAD):
            _fail('replica_port_protocol_invalid')
        return raw

    def _decrypt(self, directory, deadline, receipt, raw):
        if raw is None:
            _fail('replica_object_missing')
        return self._bounded(directory, deadline, lambda: self._codec.decrypt(receipt, raw))

    def _receipt(self, snapshot):
        if (not isinstance(snapshot, Snapshot) or type(snapshot.content) is not bytes
                or not 1 <= len(snapshot.content) <= self._codec.maxbytes
                or not isinstance(snapshot.sha256, str) or SHA.fullmatch(snapshot.sha256) is None):
            _fail('replica_snapshot_invalid')
        return ReplicaReceipt(snapshot.event_id, snapshot.sha256, self._codec.failure_domain,
                              self._codec.object_ref(snapshot.event_id, snapshot.sha256))

    def _operation(self, receipt, action, *, reconcile=False):
        self._available()
        deadline = time.monotonic() + self._timeout
        if not self._lock.acquire(blocking=False):
            _fail('replica_busy')
        try:
            with self._journal.locked() as directory:
                state = self._journal.read(directory)
                if state is None:
                    _fail('replica_journal_missing')
                if state['state'] != 'ready' and (not reconcile or state['object_ref'] != receipt.object_ref):
                    _fail(state['code'] or 'replica_write_unknown')
                if reconcile and state['state'] == 'ready':
                    _fail('replica_reconcile_not_required')
                handle = self._journal.worker_lock(directory)
                os.close(handle)
                self._remaining(deadline)
                state.update(state='inflight', object_ref=receipt.object_ref, code=None)
                self._journal.write(directory, state)
                try:
                    result = action(directory, deadline, state)
                    self._remaining(deadline)
                    self._journal.write(directory, self._journal.ready())
                    self._remaining(deadline)
                    return result
                except BaseException as error:
                    code = error.code if isinstance(error, FileReplicaError) else 'replica_read_failed'
                    if state['may_write']:
                        code = 'replica_write_unknown'
                    state.update(state='fused', code=code)
                    self._journal.write(directory, state)
                    if isinstance(error, (KeyboardInterrupt, SystemExit)):
                        raise
                    _fail(code)
        finally:
            self._lock.release()

    def save(self, snapshot):
        self._available()
        if self._recovery_only:
            _fail('replica_recovery_read_only')
        receipt = self._receipt(snapshot)
        def save(directory, deadline, state):
            valid = self._bounded(directory, deadline, lambda: digest(snapshot.content) == snapshot.sha256)
            if not valid:
                _fail('replica_snapshot_invalid')
            raw = self._read(directory, deadline, receipt)
            if raw is None:
                sealed_receipt, raw = self._bounded(directory, deadline, lambda: self._codec.seal(snapshot))
                if sealed_receipt != receipt:
                    _fail('replica_snapshot_invalid')
                state['may_write'] = True
                self._journal.write(directory, state)
                try:
                    result = self._bounded(directory, deadline,
                        lambda: self._port.upload_once(receipt.object_ref, raw, deadline=deadline), port=True)
                    if type(result) is not UploadResult:
                        _fail('replica_port_protocol_invalid')
                except _CompletedPortFailure:
                    # The call has really returned. Exact GET may settle its
                    # lost response; a still-running timeout never reaches here.
                    pass
                raw = self._read(directory, deadline, receipt)
            if self._decrypt(directory, deadline, receipt, raw) != snapshot:
                _fail('replica_snapshot_conflict')
            return receipt
        return self._operation(receipt, save)

    def get(self, receipt):
        self._available()
        self._codec._validate_receipt(receipt)
        return self._operation(receipt, lambda directory, deadline, state:
            self._decrypt(directory, deadline, receipt, self._read(directory, deadline, receipt)))

    def reconcile(self, snapshot):
        self._available()
        if self._recovery_only:
            _fail('replica_recovery_read_only')
        receipt = self._receipt(snapshot)
        def read_only(directory, deadline, state):
            if self._decrypt(directory, deadline, receipt, self._read(directory, deadline, receipt)) != snapshot:
                _fail('replica_snapshot_conflict')
            return receipt
        return self._operation(receipt, read_only, reconcile=True)

    def ciphertext_digest(self, receipt):
        self._available()
        self._codec._validate_receipt(receipt)
        def read_only(directory, deadline, state):
            raw = self._read(directory, deadline, receipt)
            self._decrypt(directory, deadline, receipt, raw)
            return self._bounded(directory, deadline, lambda: digest(raw))
        return self._operation(receipt, read_only)

    def status(self):
        base = dict(version=1, policy=POLICY, qualified=False, worker_active=False,
                    object_ref=None, may_write=False)
        if self._disabled:
            return dict(base, state='disabled', code='replica_disabled')
        if not self._lock.acquire(blocking=False):
            return dict(base, state='busy', code='replica_busy')
        try:
            with self._journal.locked() as directory:
                state = self._journal.read(directory)
                if state is None:
                    _fail('replica_journal_missing')
                try:
                    handle = self._journal.worker_lock(directory)
                    os.close(handle)
                except FileReplicaError as error:
                    if error.code != 'replica_worker_unsettled':
                        raise
                    base['worker_active'] = True
                with _POISON_LOCK:
                    poisoned = self._identity in _POISONED
                return dict(base, state='fused' if poisoned or self._permanent else
                            'read_only' if self._recovery_only and state['state'] == 'ready' else state['state'],
                            code=(state['code'] or 'replica_port_fused') if poisoned or self._permanent
                            else state['code'], object_ref=state['object_ref'], may_write=state['may_write'])
        finally:
            self._lock.release()
