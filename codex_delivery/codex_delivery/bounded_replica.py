"""Deadline-bounded encrypted replicas, with a persistent fail-closed circuit.

Only the exec worker touches the configured network mount. The parent retains
the AES key and plaintext, and assumes its separately provisioned *local*
journal filesystem and trusted Python/code installation remain responsive.
This bounds the parent's wait for network I/O, not a kernel D-state syscall.
TERM/KILL are requests: an orphan retains a local flock until it actually exits.
No real mount qualification or NAS power-loss acceptance is implied here.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
import fcntl
import importlib
import json
import math
import os
from pathlib import Path
import re
import resource
import selectors
import stat
import struct
import subprocess
import sys
import threading
import time
import types
from typing import Any, Iterator

if __package__ in {None, ''}:  # Fixed exec entry; -I ignores PYTHONPATH/user site.
    # Import only fixed sibling sources, independent of repository vs add-on
    # package layout. Do not execute a caller-controlled import root/__init__.
    _package = types.ModuleType('_codex_replica_worker')
    _package.__path__ = [str(Path(__file__).resolve().parent)]
    sys.modules[_package.__name__] = _package
    _durability = importlib.import_module(_package.__name__ + '.durability')
    _crypto = importlib.import_module(_package.__name__ + '.encrypted_replica')
    ReplicaReceipt, Snapshot, digest = _durability.ReplicaReceipt, _durability.Snapshot, _durability.digest
    EncryptedReplica, MAGIC = _crypto.EncryptedReplica, _crypto.MAGIC
    NetworkMountObjectStore, OBJECT, OVERHEAD = _crypto.NetworkMountObjectStore, _crypto.OBJECT, _crypto.OVERHEAD
    ObjectStoreUnsupported, ReplicaError = _crypto.ObjectStoreUnsupported, _crypto.ReplicaError
else:
    from .durability import ReplicaReceipt, Snapshot, digest
    from .encrypted_replica import (
        EncryptedReplica, MAGIC, NetworkMountObjectStore, OBJECT, OVERHEAD,
        ObjectStoreUnsupported, ReplicaError,
    )


FRAME_LIMIT = 16384
JOURNAL_LIMIT = 32768
_CODES = frozenset({
    'replica_mount_timeout', 'replica_write_unknown', 'replica_parent_interrupted',
    'replica_worker_protocol_invalid', 'replica_worker_unavailable',
    'replica_mount_identity_mismatch', 'replica_mount_unavailable',
    'replica_mountinfo_invalid', 'replica_mount_config_invalid',
    'replica_storage_permissions_invalid', 'replica_store_semantics_unsupported',
    'replica_object_invalid', 'replica_object_changed', 'replica_object_reference_invalid',
    'replica_read_after_write_failed', 'replica_object_missing',
    'replica_envelope_invalid', 'replica_snapshot_conflict', 'replica_store_unavailable',
    'replica_worker_unknown',
})


class BoundedReplicaError(ReplicaError):
    def __init__(self, code: str, *, reason: str | None = None):
        super().__init__(code)
        self.reason = reason


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=True, allow_nan=False).encode('ascii')


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError('duplicate')
        result[key] = value
    return result


def _constant(_):
    raise ValueError('nonfinite')


def _decode(content: bytes) -> dict:
    value = json.loads(content, object_pairs_hook=_pairs, parse_constant=_constant)
    if not isinstance(value, dict) or _canonical(value) != content:
        raise ValueError('canonical')
    return value


def _absolute(value: Any) -> str:
    if (not isinstance(value, str) or not 1 <= len(value) <= 4096
            or any(ord(char) < 32 for char in value)):
        raise BoundedReplicaError('replica_config_invalid')
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts or str(path) != value:
        raise BoundedReplicaError('replica_config_invalid')
    return value


@dataclass(frozen=True, repr=False)
class MountSpec:
    root: str
    expected_fstype: str
    expected_source: str
    expected_mountpoint: str
    maxbytes: int = 256 * 1024 * 1024

    def __post_init__(self):
        root = Path(_absolute(self.root))
        mount = Path(_absolute(self.expected_mountpoint))
        if not isinstance(self.expected_fstype, str):
            raise BoundedReplicaError('replica_config_invalid')
        source = self.expected_source
        # Exact server/export identity only. Never a URL, options, or userinfo.
        host = r'(?:[A-Za-z0-9][A-Za-z0-9._-]*|\[[a-fA-F0-9:]+\])'
        expression = (host + r':/[^\s@?#%\\]*' if self.expected_fstype in {'nfs', 'nfs4'}
                      else r'//' + host + r'/[^/\s@?#%:;=\\]+')
        if (mount == Path('/') or self.expected_fstype not in {'nfs', 'nfs4', 'cifs'}
                or not isinstance(source, str) or not 1 <= len(source) <= 2048
                or '://' in source
                or re.fullmatch(expression, source) is None
                or re.search(r'(?i)(?:password|passwd|token|secret|credential|username)\s*=', source)
                or type(self.maxbytes) is not int or not 1 <= self.maxbytes <= 512 * 1024 * 1024 + OVERHEAD):
            raise BoundedReplicaError('replica_config_invalid')
        try:
            root.relative_to(mount)
        except ValueError:
            raise BoundedReplicaError('replica_config_invalid') from None

    @classmethod
    def from_wire(cls, value: Any) -> MountSpec:
        if not isinstance(value, dict) or set(value) != set(cls.__dataclass_fields__):
            raise BoundedReplicaError('replica_config_invalid')
        return cls(**value)


def _pin(value: Any, mount: MountSpec) -> Any:
    if value is None:
        return None
    if (not isinstance(value, list) or len(value) != 2
            or not isinstance(value[0], list) or len(value[0]) != 10
            or not isinstance(value[1], list) or len(value[1]) != 2):
        raise ValueError('pin')
    identity, inode = value
    if (any(type(item) is not int or item < 0 for item in identity[:4] + inode)
            or any(not isinstance(item, str) or len(item) > 4096
                   or any(ord(char) < 32 for char in item) for item in identity[4:])
            or identity[4:8] != ['/', mount.expected_mountpoint, mount.expected_fstype, mount.expected_source]
            or 'rw' not in identity[8].split(',') or 'rw' not in identity[9].split(',')):
        raise ValueError('pin')
    return value


def _wire_pin(value: tuple) -> list:
    return [list(value[0]), list(value[1])]


def _ref(value: Any) -> str:
    if not isinstance(value, str) or OBJECT.fullmatch(value) is None:
        raise BoundedReplicaError('replica_object_reference_invalid')
    return value


class _Journal:
    """Private, administrator-provisioned LOCAL directory; never a mount path."""
    def __init__(self, path: str | Path, mount: MountSpec, binding: dict):
        self.path = Path(_absolute(str(path)))
        try:
            self.path.relative_to(Path(mount.expected_mountpoint))
        except ValueError:
            pass
        else:
            raise BoundedReplicaError('replica_journal_not_local')
        self.mount, self.binding = mount, binding
        self._directory_pin = None
        with self.locked() as directory:
            state = self.read(directory)
            if state is None:
                if self._locks_existed:
                    raise BoundedReplicaError('replica_journal_missing')
                self.write(directory, self.initial())
            elif state['state'] == 'inflight':
                state.update(state='fused', code='replica_write_unknown' if state['may_write']
                             else 'replica_worker_unknown', reason='replica_parent_interrupted')
                self.write(directory, state)

    def initial(self) -> dict:
        return dict(version=1, mount=asdict(self.mount), binding=self.binding, pin=None,
                    state='ready', object_ref=None, may_write=False, code=None, reason=None)

    def _directory(self) -> int:
        handle = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in self.path.parent.parts[1:]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=handle)
                os.close(handle)
                handle = child
            info = os.fstat(handle)
            if info.st_mode & 0o077:
                raise BoundedReplicaError('replica_journal_permissions_invalid')
            identity = (info.st_dev, info.st_ino)
            if self._directory_pin is not None and self._directory_pin != identity:
                raise BoundedReplicaError('replica_journal_changed')
            self._directory_pin = identity
            return handle
        except BaseException:
            os.close(handle)
            raise

    def _file(self, directory: int, suffix: str) -> tuple[int, bool]:
        name = self.path.name + suffix
        try:
            handle = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            existed = False
        except FileExistsError:
            handle = os.open(name, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            existed = True
        info = os.fstat(handle)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_nlink != 1:
            os.close(handle)
            raise BoundedReplicaError('replica_journal_permissions_invalid')
        return handle, existed

    @contextmanager
    def locked(self) -> Iterator[int]:
        directory = lock = None
        try:
            directory = self._directory()
            lock, existed = self._file(directory, '.lock')
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise BoundedReplicaError('replica_busy') from None
            worker, worker_existed = self._file(directory, '.worker-lock')
            os.close(worker)
            self._locks_existed = existed or worker_existed
            yield directory
        except OSError:
            raise BoundedReplicaError('replica_journal_unavailable') from None
        finally:
            if lock is not None:
                os.close(lock)
            if directory is not None:
                os.close(directory)

    def worker_lock(self, directory: int) -> int:
        handle, _ = self._file(directory, '.worker-lock')
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(handle)
            raise BoundedReplicaError('replica_worker_unsettled') from None
        return handle

    def read(self, directory: int) -> dict | None:
        try:
            handle = os.open(self.path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        except FileNotFoundError:
            return None
        try:
            info = os.fstat(handle)
            if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_nlink != 1
                    or not 1 <= info.st_size <= JOURNAL_LIMIT):
                raise ValueError('permissions')
            content = bytearray()
            while len(content) <= JOURNAL_LIMIT:
                block = os.read(handle, min(8192, JOURNAL_LIMIT + 1 - len(content)))
                if not block:
                    break
                content.extend(block)
            state = _decode(bytes(content))
            if (set(state) != set(self.initial()) or state['version'] != 1 or type(state['version']) is not int
                    or state['mount'] != asdict(self.mount) or state['binding'] != self.binding
                    or state['state'] not in {'ready', 'inflight', 'fused'}
                    or type(state['may_write']) is not bool
                    or state['code'] is not None and state['code'] not in _CODES
                    or state['reason'] is not None and state['reason'] not in _CODES):
                raise ValueError('schema')
            _pin(state['pin'], self.mount)
            if state['state'] == 'ready':
                if any(state[key] is not None for key in ('object_ref', 'code', 'reason')) or state['may_write']:
                    raise ValueError('ready')
            else:
                _ref(state['object_ref'])
            return state
        except (ValueError, TypeError, KeyError, ReplicaError):
            raise BoundedReplicaError('replica_journal_invalid') from None
        finally:
            os.close(handle)

    def write(self, directory: int, state: dict) -> None:
        content = _canonical(state)
        if len(content) > JOURNAL_LIMIT:
            raise BoundedReplicaError('replica_journal_invalid')
        temporary = '.replica-journal-' + os.urandom(16).hex()
        handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        try:
            with os.fdopen(handle, 'wb') as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path.name, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass


class _Session:
    def __init__(self, replica: DeadlineReplica, directory: int, state: dict, deadline: float):
        self.replica, self.directory, self.state, self.deadline = replica, directory, state, deadline
        self.process = None
        self.fatal: BoundedReplicaError | None = None

    def remaining(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise BoundedReplicaError('replica_mount_timeout')
        return remaining

    def start(self) -> None:
        self.remaining()
        handle = self.replica._journal.worker_lock(self.directory)
        try:
            self.process = self.replica._launch(handle)
        finally:
            # The child alone retains this flock even if TERM/KILL cannot stop it.
            os.close(handle)
        self.remaining()
        for stream in (self.process.stdin, self.process.stdout):
            os.set_blocking(stream.fileno(), False)
        response, payload = self.rpc(dict(op='open', mount=asdict(self.replica.mount),
                                          pin=self.state['pin'], ref=self.state['object_ref']))
        if set(response) != {'ok', 'pin'} or payload or response['ok'] is not True:
            raise BoundedReplicaError('replica_worker_protocol_invalid')
        try:
            qualified = _pin(response['pin'], self.replica.mount)
            if qualified is None or self.state['pin'] is not None and self.state['pin'] != qualified:
                raise ValueError('identity')
        except (ValueError, TypeError):
            raise BoundedReplicaError('replica_mount_identity_mismatch') from None
        self.state['pin'] = qualified
        # Persist qualification before permitting any object access or PUT.
        self.replica._journal.write(self.directory, self.state)
        self.remaining()

    def _transfer(self, handle: int, value: bytes | int, *, writing: bool) -> bytes:
        target = len(value) if writing else value
        position = 0
        chunks = []
        with selectors.DefaultSelector() as selector:
            selector.register(handle, selectors.EVENT_WRITE if writing else selectors.EVENT_READ)
            while position < target:
                if not selector.select(self.remaining()):
                    raise BoundedReplicaError('replica_mount_timeout')
                try:
                    if writing:
                        count = os.write(handle, memoryview(value)[position:position + 65536])
                        if not count:
                            raise OSError('closed')
                    else:
                        chunk = os.read(handle, min(65536, target - position))
                        if not chunk:
                            raise OSError('closed')
                        chunks.append(chunk)
                        count = len(chunk)
                    position += count
                except BlockingIOError:
                    continue
        self.remaining()
        return b''.join(chunks)

    def rpc(self, request: dict, payload: bytes = b'') -> tuple[dict, bytes]:
        request = dict(request, size=len(payload))
        header = _canonical(request)
        if len(header) > FRAME_LIMIT:
            raise BoundedReplicaError('replica_worker_protocol_invalid')
        try:
            self._transfer(self.process.stdin.fileno(), struct.pack('>I', len(header)) + header, writing=True)
            self._transfer(self.process.stdin.fileno(), payload, writing=True)
            length = struct.unpack('>I', self._transfer(self.process.stdout.fileno(), 4, writing=False))[0]
            if not 1 <= length <= FRAME_LIMIT:
                raise ValueError('frame')
            response = _decode(self._transfer(self.process.stdout.fileno(), length, writing=False))
            size = response.pop('size', None)
            if type(size) is not int or not 0 <= size <= self.replica.mount.maxbytes:
                raise ValueError('size')
            body = self._transfer(self.process.stdout.fileno(), size, writing=False)
            if response.get('ok') is False:
                if set(response) != {'ok', 'code'} or response['code'] not in _CODES or body:
                    raise ValueError('error')
                if response['code'] == 'replica_store_semantics_unsupported':
                    raise ObjectStoreUnsupported(response['code'])
                raise BoundedReplicaError(response['code'])
            if response.get('ok') is not True:
                raise ValueError('response')
            return response, body
        except BoundedReplicaError as error:
            self.fatal = error
            raise
        except ObjectStoreUnsupported:
            raise
        except (OSError, ValueError, TypeError, KeyError, struct.error):
            error = BoundedReplicaError('replica_worker_protocol_invalid')
            self.fatal = error
            raise error from None

    def get(self, ref: str) -> bytes | None:
        if self.fatal is not None:
            raise self.fatal
        if ref != self.state['object_ref']:
            raise BoundedReplicaError('replica_worker_protocol_invalid')
        response, content = self.rpc(dict(op='get', ref=ref))
        if set(response) != {'ok', 'present'} or type(response['present']) is not bool:
            self.fatal = BoundedReplicaError('replica_worker_protocol_invalid')
            raise self.fatal
        if not response['present']:
            if content:
                self.fatal = BoundedReplicaError('replica_worker_protocol_invalid')
                raise self.fatal
            return None
        return content

    def put_if_absent(self, ref: str, content: bytes) -> bool:
        if self.fatal is not None:
            raise self.fatal
        if ref != self.state['object_ref'] or not content.startswith(MAGIC):
            raise BoundedReplicaError('replica_worker_protocol_invalid')
        self.state['may_write'] = True
        self.replica._journal.write(self.directory, self.state)
        self.remaining()
        response, payload = self.rpc(dict(op='put', ref=ref), content)
        if set(response) != {'ok', 'created'} or type(response['created']) is not bool or payload:
            self.fatal = BoundedReplicaError('replica_worker_protocol_invalid')
            raise self.fatal
        return response['created']

    def finish(self) -> None:
        response, content = self.rpc(dict(op='close'))
        if response != {'ok': True} or content:
            raise BoundedReplicaError('replica_worker_protocol_invalid')
        try:
            status = self.process.wait(timeout=self.remaining())
        except subprocess.TimeoutExpired:
            raise BoundedReplicaError('replica_mount_timeout') from None
        self.remaining()
        if status != 0:
            raise BoundedReplicaError('replica_worker_protocol_invalid')

    def stop(self) -> None:
        if self.process is None:
            return
        self.replica._request_stop(self.process)
        for stream in (self.process.stdin, self.process.stdout):
            if stream is not None:
                stream.close()
        if self.process.poll() is None:
            self.replica._stranded = self.process
            # Reap eventual exit without adding to the API wait budget. A
            # D-state worker can retain this daemon waiter and its flock.
            threading.Thread(target=self.process.wait, name='replica-worker-reaper', daemon=True).start()


class _CiphertextStore:
    def __init__(self, replica):
        self.replica = replica

    def get(self, ref: str) -> bytes | None:
        if self.replica._session is None:
            raise BoundedReplicaError('replica_operation_required')
        return self.replica._session.get(ref)

    def put_if_absent(self, ref: str, content: bytes) -> bool:
        if self.replica._session is None:
            raise BoundedReplicaError('replica_operation_required')
        return self.replica._session.put_if_absent(ref, content)


class DeadlineReplica:
    """EncryptedReplica facade; unknown outcomes require explicit exact GET.

    The journal's parent directory must already exist, be private and reside on
    responsive local storage. Losing/removing the journal or lock files requires
    operator recovery, never automatic re-enrollment. Use one journal per mount
    and encryption/failure-domain binding across every process using the store.
    """
    def __init__(self, mountspec: MountSpec, encryption_key: bytes, *, key_ref: str,
                 failure_domain: str, journal_path: str | Path,
                 timeout_seconds: float = 30, maxbytes: int = 128 * 1024 * 1024):
        if (not isinstance(mountspec, MountSpec) or type(timeout_seconds) not in {int, float}
                or not math.isfinite(timeout_seconds) or not 0.05 <= timeout_seconds <= 300
                or type(maxbytes) is not int or not 1 <= maxbytes <= 512 * 1024 * 1024
                or mountspec.maxbytes < maxbytes + OVERHEAD):
            raise BoundedReplicaError('replica_config_invalid')
        self.mount, self.timeout_seconds = mountspec, float(timeout_seconds)
        self._session = None
        self._stranded = None
        self._lock = threading.Lock()
        self._encrypted = EncryptedReplica(_CiphertextStore(self), encryption_key, key_ref=key_ref,
                                           failure_domain=failure_domain, maxbytes=maxbytes)
        self._journal = _Journal(journal_path, mountspec,
                                dict(key_ref=key_ref, failure_domain=failure_domain, maxbytes=maxbytes))

    def _launch(self, lockfd: int):
        return subprocess.Popen([sys.executable, '-I', '-B', str(Path(__file__).resolve()),
                                 '--worker', str(lockfd)], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'},
                                close_fds=True, pass_fds=(lockfd,), bufsize=0,
                                start_new_session=True)

    @staticmethod
    def _request_stop(process) -> None:
        # Never wait here: kill is not evidence that a D-state worker has died.
        if process.poll() is None:
            for request in (process.terminate, process.kill):
                try:
                    request()
                except OSError:
                    pass

    def get(self, receipt: ReplicaReceipt) -> Snapshot:
        self._encrypted._validate_receipt(receipt)
        return self._operation(receipt.object_ref, lambda: self._encrypted.get(receipt))

    def ciphertext_digest(self, receipt: ReplicaReceipt) -> str:
        """Exact bounded GET and authenticated hash of the immutable envelope.

        This is not reconciliation: a fused/unknown journal stays fused and no
        PUT or fresh write opportunity is introduced. Decrypt validates the
        receipt, authenticated plaintext hash, format and size before hashing.
        """
        self._encrypted._validate_receipt(receipt)
        def read_only():
            envelope = self._session.get(receipt.object_ref)
            if envelope is None:
                raise ReplicaError('replica_object_missing')
            self._encrypted.decrypt(receipt, envelope)
            return digest(envelope)
        return self._operation(receipt.object_ref, read_only)

    def save(self, snapshot: Snapshot) -> ReplicaReceipt:
        receipt = self._encrypted._receipt(snapshot)
        return self._operation(receipt.object_ref, lambda: self._encrypted.save(snapshot))

    def reconcile(self, snapshot: Snapshot) -> ReplicaReceipt:
        """Exact authenticated GET only; a missing unknown object stays fused."""
        receipt = self._encrypted._receipt(snapshot)
        def read_only():
            if self._encrypted.get(receipt) != snapshot:
                raise BoundedReplicaError('replica_snapshot_conflict')
            return receipt
        return self._operation(receipt.object_ref, read_only, reconcile=True)

    def _operation(self, ref: str, action, *, reconcile: bool = False):
        deadline = time.monotonic() + self.timeout_seconds
        if not self._lock.acquire(blocking=False):
            raise BoundedReplicaError('replica_busy')
        session = None
        try:
            with self._journal.locked() as directory:
                state = self._journal.read(directory)
                if state is None:
                    raise BoundedReplicaError('replica_journal_missing')
                if state['state'] != 'ready' and (not reconcile or state['object_ref'] != ref):
                    raise BoundedReplicaError(state['code'] or 'replica_worker_unknown', reason=state['reason'])
                if reconcile and state['state'] == 'ready':
                    raise BoundedReplicaError('replica_reconcile_not_required')
                # Check orphan lifetime before changing its recovery record.
                worker_lock = self._journal.worker_lock(directory)
                os.close(worker_lock)
                state.update(state='inflight', object_ref=ref, code=None, reason=None)
                self._journal.write(directory, state)
                session = _Session(self, directory, state, deadline)
                self._session = session
                try:
                    session.start()
                    result = action()
                    session.remaining()
                    session.finish()
                    pin = state['pin']
                    state = self._journal.initial()
                    state['pin'] = pin
                    self._journal.write(directory, state)
                    session.remaining()
                    return result
                except BaseException as error:
                    fatal = session.fatal or error
                    reason = getattr(fatal, 'code', 'replica_worker_unknown')
                    if reason not in _CODES:
                        reason = 'replica_worker_unknown'
                    # A read-only reconciliation can NEVER clear a missing or
                    # corrupt unknown result, or turn absence into a new PUT.
                    code = 'replica_write_unknown' if session.state['may_write'] else reason
                    failed = dict(session.state, state='fused', code=code, reason=reason)
                    self._journal.write(directory, failed)
                    if isinstance(error, (KeyboardInterrupt, SystemExit)):
                        raise
                    if isinstance(fatal, ObjectStoreUnsupported) and not session.state['may_write']:
                        raise fatal
                    raise BoundedReplicaError(code, reason=reason) from None
        finally:
            self._session = None
            if session is not None:
                session.stop()
            self._lock.release()

    def status(self) -> dict:
        if not self._lock.acquire(blocking=False):
            return dict(version=1, state='busy', code='replica_busy')
        try:
            with self._journal.locked() as directory:
                state = self._journal.read(directory)
                if state is None:
                    raise BoundedReplicaError('replica_journal_missing')
                active = False
                try:
                    handle = self._journal.worker_lock(directory)
                except BoundedReplicaError as error:
                    if error.code != 'replica_worker_unsettled':
                        raise
                    active = True
                else:
                    os.close(handle)
                return dict(version=1, state=state['state'] if state['pin'] is not None
                            or state['state'] != 'ready' else 'unqualified',
                            code=state['code'], reason=state['reason'],
                            object_ref=state['object_ref'], qualified=state['pin'] is not None,
                            may_write=state['may_write'], worker_active=active)
        finally:
            self._lock.release()


def _blocking_read(handle: int, count: int) -> bytes:
    chunks = []
    position = 0
    while position < count:
        block = os.read(handle, min(65536, count - position))
        if not block:
            raise EOFError()
        chunks.append(block)
        position += len(block)
    return b''.join(chunks)


def _worker_receive(limit: int) -> tuple[dict, bytes]:
    length = struct.unpack('>I', _blocking_read(0, 4))[0]
    if not 1 <= length <= FRAME_LIMIT:
        raise ValueError('frame')
    request = _decode(_blocking_read(0, length))
    size = request.pop('size', None)
    if type(size) is not int or not 0 <= size <= limit:
        raise ValueError('size')
    return request, _blocking_read(0, size)


def _worker_send(response: dict, payload: bytes = b'') -> None:
    header = _canonical(dict(response, size=len(payload)))
    for content in (struct.pack('>I', len(header)), header, payload):
        position = 0
        while position < len(content):
            count = os.write(1, memoryview(content)[position:position + 65536])
            if not count:
                raise EOFError()
            position += count


def _worker_main(lockfd: int) -> int:
    os.umask(0o077)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    try:
        info = os.fstat(lockfd)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_nlink != 1:
            raise ValueError('lock')
        request, payload = _worker_receive(0)
        if set(request) != {'op', 'mount', 'pin', 'ref'} or request['op'] != 'open' or payload:
            raise ValueError('open')
        mount = MountSpec.from_wire(request['mount'])
        expected = _pin(request['pin'], mount)
        ref = _ref(request['ref'])
        store = NetworkMountObjectStore(**asdict(mount))
        qualified = _wire_pin(store._pinned)
        _pin(qualified, mount)
        if expected is not None and expected != qualified:
            raise BoundedReplicaError('replica_mount_identity_mismatch')
        _worker_send(dict(ok=True, pin=qualified))
        while True:
            request, payload = _worker_receive(mount.maxbytes)
            if request == {'op': 'close'} and not payload:
                _worker_send(dict(ok=True))
                return 0
            if set(request) != {'op', 'ref'} or request['ref'] != ref:
                raise ValueError('operation')
            if request['op'] == 'get' and not payload:
                value = store.get(ref)
                if value is not None and (not isinstance(value, bytes) or len(value) > mount.maxbytes):
                    raise ValueError('object')
                _worker_send(dict(ok=True, present=value is not None), value or b'')
            elif request['op'] == 'put' and payload.startswith(MAGIC):
                created = store.put_if_absent(ref, payload)
                if type(created) is not bool:
                    raise ObjectStoreUnsupported('replica_store_semantics_unsupported')
                _worker_send(dict(ok=True, created=created))
            else:
                raise ValueError('operation')
    except (BrokenPipeError, EOFError):
        return 1
    except BaseException as error:
        code = getattr(error, 'code', 'replica_worker_protocol_invalid')
        if code not in _CODES:
            code = 'replica_worker_protocol_invalid'
        try:
            _worker_send(dict(ok=False, code=code))
        except (OSError, EOFError):
            pass
        return 1


if __name__ == '__main__':
    if len(sys.argv) != 3 or sys.argv[1] != '--worker' or not sys.argv[2].isdigit():
        raise SystemExit(2)
    raise SystemExit(_worker_main(int(sys.argv[2])))
