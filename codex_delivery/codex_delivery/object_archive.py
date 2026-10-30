"""Immutable encrypted file archives; a DB receipt is not a file backup.

The caller supplies a stopped, trusted checkpoint and its exact file manifest.
One verified manifest commits all chunks. No filesystem paths or keys leave the
service. Recovery reads only, validates every byte before creating a new tree,
and never changes a live workspace or resumes execution.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from functools import wraps
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
import time
import unicodedata
from typing import Any

from .durability import ReplicaReceipt, Snapshot, digest, verify_receipt
from .encrypted_replica import EncryptedReplica

CHUNK = 4 * 1024**2
MAX_BYTES = 256 * 1024**2
MAX_FILES = 256
MAX_SECONDS = 120
MAX_ADMIN_RESTORE_SECONDS = 300
SHA = re.compile(r'sha256:[a-f0-9]{64}\Z')
REF = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z')


class ArchiveError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _safe(method):
    @wraps(method)
    def call(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except ArchiveError:
            raise
        except Exception:
            # Neither a mount failure nor a local filesystem error may reveal
            # source paths, raw protocol data or credential-bearing exceptions.
            raise ArchiveError('archive_unavailable') from None
    return call


def _json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':'), allow_nan=False).encode()


def _pairs(items):
    value = {}
    for key, item in items:
        if key in value:
            raise ValueError('duplicate')
        value[key] = item
    return value


def _name(value: Any) -> str:
    if (not isinstance(value, str) or not value or len(value.encode()) > 255
            or any(ord(c) < 32 or ord(c) == 127 for c in value) or '\\' in value
            or unicodedata.normalize('NFC', value) != value
            or PurePosixPath(value).is_absolute() or len(value.split('/')) > 16
            or any(part in {'', '.', '..'} for part in value.split('/'))):
        raise ArchiveError('archive_path_invalid')
    return value


def _scope(value: Any) -> dict:
    if (not isinstance(value, dict) or set(value) != {'owner_ref', 'task_ref', 'revision', 'checkpoint_ref'}
            or type(value['revision']) is not int or not 1 <= value['revision'] <= 2**53-1
            or any(not isinstance(value[key], str) or REF.fullmatch(value[key]) is None
                   for key in ('owner_ref', 'task_ref', 'checkpoint_ref'))):
        raise ArchiveError('archive_scope_invalid')
    return dict(value)


def _snapshot(kind: str, scope: dict, content: bytes) -> Snapshot:
    sha = digest(content)
    event = 'OA-' + hashlib.sha256(_json({'kind': kind, 'scope': scope, 'sha256': sha})).hexdigest()
    return Snapshot(event, content, sha)


def _identity(info) -> tuple:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _open_at(root_fd: int, name: str, *, directory=False) -> int:
    parent = os.dup(root_fd)
    try:
        parts = name.split('/')
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent)
            parent = child
        return os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            | (os.O_DIRECTORY if directory else 0), dir_fd=parent)
    finally:
        os.close(parent)


def _budget(deadline):
    if time.monotonic() >= deadline:
        raise ArchiveError('archive_budget_exceeded')


def _tree(root: Path) -> tuple[list[str], dict[str, tuple]]:
    if root.is_symlink() or root.resolve() != root or not root.is_dir() or root.stat().st_mode & 0o077:
        raise ArchiveError('archive_source_invalid')
    directories, files, names = [], {}, set()
    total = 0
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    pending = ['']
    try:
        while pending:
            prefix = pending.pop()
            fd = _open_at(root_fd, prefix, directory=True) if prefix else os.dup(root_fd)
            try:
                with os.scandir(fd) as entries:
                    for entry in entries:
                        name = _name(prefix + '/' + entry.name if prefix else entry.name)
                        key = name.casefold()
                        if key in names or len(names) >= MAX_FILES * 17:
                            raise ArchiveError('archive_entries_invalid')
                        names.add(key)
                        info = entry.stat(follow_symlinks=False)
                        if stat.S_ISDIR(info.st_mode) and not info.st_mode & 0o6000:
                            directories.append(name)
                            pending.append(name)
                        elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and not info.st_mode & 0o6000:
                            files[name] = _identity(info)
                            total += info.st_size
                        else:
                            raise ArchiveError('archive_entries_invalid')
                        if len(files) > MAX_FILES or len(directories) > MAX_FILES * 16 or total > MAX_BYTES:
                            raise ArchiveError('archive_size_exceeded')
            finally:
                os.close(fd)
    finally:
        os.close(root_fd)
    return sorted(directories), files


@dataclass(frozen=True)
class ArchiveReceipt:
    scope_digest: str
    manifest: ReplicaReceipt

    def document(self) -> dict:
        return {'version': 1, **asdict(self)}

    @classmethod
    def from_document(cls, value: Any) -> 'ArchiveReceipt':
        try:
            if (not isinstance(value, dict) or set(value) != {'version', 'scope_digest', 'manifest'}
                    or type(value['version']) is not int or value['version'] != 1
                    or not isinstance(value['scope_digest'], str) or SHA.fullmatch(value['scope_digest']) is None
                    or not isinstance(value['manifest'], dict)
                    or set(value['manifest']) != {'event_id', 'sha256', 'failure_domain', 'object_ref'}):
                raise ValueError('receipt')
            manifest = ReplicaReceipt(**value['manifest'])
            if (not isinstance(manifest.event_id, str) or re.fullmatch(r'OA-[a-f0-9]{64}', manifest.event_id) is None
                    or not isinstance(manifest.sha256, str) or SHA.fullmatch(manifest.sha256) is None
                    or not isinstance(manifest.failure_domain, str) or REF.fullmatch(manifest.failure_domain) is None
                    or manifest.object_ref != EncryptedReplica.object_ref(manifest.event_id, manifest.sha256)):
                raise ValueError('receipt')
            return cls(value['scope_digest'], manifest)
        except Exception:
            raise ArchiveError('archive_receipt_invalid') from None


class ObjectArchive:
    def __init__(self, replica, *, local_domain: str, replica_domain: str):
        if (not isinstance(local_domain, str) or REF.fullmatch(local_domain) is None
                or not isinstance(replica_domain, str) or REF.fullmatch(replica_domain) is None or local_domain == replica_domain
                or not callable(getattr(replica, 'save', None)) or not callable(getattr(replica, 'get', None))):
            raise ArchiveError('archive_replica_invalid')
        self.replica, self.local_domain, self.replica_domain = replica, local_domain, replica_domain

    def _receipt(self, snapshot: Snapshot) -> ReplicaReceipt:
        return ReplicaReceipt(snapshot.event_id, snapshot.sha256, self.replica_domain,
                              EncryptedReplica.object_ref(snapshot.event_id, snapshot.sha256))

    def _read(self, receipt: ReplicaReceipt) -> bytes:
        expected = self._receipt(Snapshot(receipt.event_id, b'', receipt.sha256))
        if receipt != expected:
            raise ArchiveError('archive_receipt_invalid')
        snapshot = self.replica.get(receipt)
        if (not isinstance(snapshot, Snapshot) or not isinstance(snapshot.content, bytes)
                or len(snapshot.content) > max(CHUNK, 1024**2) or digest(snapshot.content) != snapshot.sha256):
            raise ArchiveError('archive_integrity_failed')
        verify_receipt(snapshot, receipt, local_domain=self.local_domain, replica_domain=self.replica_domain)
        return snapshot.content

    @_safe
    def save(self, root: Path, *, scope: dict, expected_files: list[dict]) -> ArchiveReceipt:
        deadline = time.monotonic() + MAX_SECONDS
        scope = _scope(scope)
        root = Path(root).absolute()
        directories, files = _tree(root)
        if (not isinstance(expected_files, list) or len(expected_files) != len(files)
                or any(not isinstance(item, dict) or set(item) != {'name', 'size_bytes', 'sha256'} for item in expected_files)):
            raise ArchiveError('archive_manifest_invalid')
        expected = {}
        for item in expected_files:
            name = _name(item['name'])
            if (name in expected or type(item['size_bytes']) is not int or item['size_bytes'] < 0
                    or not isinstance(item['sha256'], str) or SHA.fullmatch(item['sha256']) is None):
                raise ArchiveError('archive_manifest_invalid')
            expected[name] = item
        if set(files) != set(expected):
            raise ArchiveError('archive_source_changed')
        objects, entries = {}, []
        for name, identity in sorted(files.items()):
            _budget(deadline)
            chunks, hasher, size = [], hashlib.sha256(), 0
            root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                fd = _open_at(root_fd, name)
            finally:
                os.close(root_fd)
            with os.fdopen(fd, 'rb') as source:
                if _identity(os.fstat(source.fileno())) != identity:
                    raise ArchiveError('archive_source_changed')
                for block in iter(lambda: source.read(CHUNK), b''):
                    _budget(deadline)
                    size += len(block)
                    if size > identity[2]:
                        raise ArchiveError('archive_source_changed')
                    hasher.update(block)
                    snapshot = _snapshot('chunk', scope, block)
                    objects[snapshot.event_id] = snapshot
                    chunks.append(asdict(self._receipt(snapshot)))
                if _identity(os.fstat(source.fileno())) != identity:
                    raise ArchiveError('archive_source_changed')
            if size != expected[name]['size_bytes'] or 'sha256:' + hasher.hexdigest() != expected[name]['sha256']:
                raise ArchiveError('archive_source_changed')
            entries.append({**expected[name], 'chunks': chunks})
        if _tree(root) != (directories, files):
            raise ArchiveError('archive_source_changed')
        manifest = _snapshot('manifest', scope, _json({'version': 1, 'scope': scope, 'directories': directories, 'files': entries}))
        if len(manifest.content) > 1024**2:
            raise ArchiveError('archive_manifest_invalid')
        snapshots = [*objects.values(), manifest]
        # A restart might encounter a fused later chunk. Reconcile that exact
        # object first; never PUT a missing unknown object or reset its journal.
        if callable(getattr(self.replica, 'status', None)):
            state = self.replica.status()
            if state.get('state') == 'fused':
                selected = [s for s in snapshots if self._receipt(s).object_ref == state.get('object_ref')]
                if len(selected) != 1 or not callable(getattr(self.replica, 'reconcile', None)):
                    raise ArchiveError('archive_replica_unsettled')
                receipt = self.replica.reconcile(selected[0])
                if receipt != self._receipt(selected[0]):
                    raise ArchiveError('archive_receipt_invalid')
        for snapshot in snapshots:
            _budget(deadline)
            if snapshot is manifest and _tree(root) != (directories, files):
                raise ArchiveError('archive_source_changed')
            receipt = self.replica.save(snapshot)
            if receipt != self._receipt(snapshot):
                raise ArchiveError('archive_receipt_invalid')
            verify_receipt(snapshot, receipt, local_domain=self.local_domain, replica_domain=self.replica_domain)
            _budget(deadline)
        return ArchiveReceipt(digest(_json(scope)), self._receipt(manifest))

    @_safe
    def restore(self, destination: Path, *, scope: dict, receipt: ArchiveReceipt,
                deadline: float | None = None) -> dict:
        start = time.monotonic()
        if deadline is None:
            deadline = start + MAX_SECONDS
        elif (type(deadline) not in {int, float} or not math.isfinite(deadline)
                or not start < deadline <= start + MAX_ADMIN_RESTORE_SECONDS):
            raise ArchiveError('archive_budget_invalid')
        scope = _scope(scope)
        if not isinstance(receipt, ArchiveReceipt) or receipt.scope_digest != digest(_json(scope)):
            raise ArchiveError('archive_scope_invalid')
        try:
            raw = self._read(receipt.manifest)
            if len(raw) > 1024**2:
                raise ValueError('manifest')
            manifest = json.loads(raw, object_pairs_hook=_pairs)
            if (not isinstance(manifest, dict) or set(manifest) != {'version', 'scope', 'directories', 'files'}
                    or type(manifest['version']) is not int or manifest['version'] != 1
                    or _scope(manifest['scope']) != scope or _json(manifest) != raw
                    or _snapshot('manifest', scope, raw).event_id != receipt.manifest.event_id
                    or not isinstance(manifest['files'], list) or len(manifest['files']) > MAX_FILES
                    or not isinstance(manifest['directories'], list) or len(manifest['directories']) > MAX_FILES * 16):
                raise ValueError('manifest')
            names, contents, total = set(), {}, 0
            directories = manifest['directories']
            for name in [*directories, *(entry['name'] for entry in manifest['files'])]:
                key = _name(name).casefold()
                if key in names:
                    raise ValueError('duplicate')
                names.add(key)
            for entry in manifest['files']:
                if (not isinstance(entry, dict) or set(entry) != {'name', 'size_bytes', 'sha256', 'chunks'}
                        or type(entry['size_bytes']) is not int or not 0 <= entry['size_bytes'] <= MAX_BYTES
                        or not isinstance(entry['chunks'], list)
                        or len(entry['chunks']) != (entry['size_bytes'] + CHUNK - 1) // CHUNK
                        or not isinstance(entry['sha256'], str) or SHA.fullmatch(entry['sha256']) is None):
                    raise ValueError('file')
                total += entry['size_bytes']
                if total > MAX_BYTES:
                    raise ValueError('limit')
                content = bytearray()
                for chunk in entry['chunks']:
                    _budget(deadline)
                    if not isinstance(chunk, dict) or set(chunk) != {'event_id', 'sha256', 'failure_domain', 'object_ref'}:
                        raise ValueError('chunk')
                    chunk_receipt = ReplicaReceipt(**chunk)
                    block = self._read(chunk_receipt)
                    if (len(block) != min(CHUNK, entry['size_bytes'] - len(content))
                            or _snapshot('chunk', scope, block).event_id != chunk_receipt.event_id):
                        raise ValueError('chunk')
                    content.extend(block)
                if len(content) != entry['size_bytes'] or digest(content) != entry['sha256']:
                    raise ValueError('digest')
                contents[entry['name']] = bytes(content)
            # Validate the entire hierarchy before writing any recovered bytes.
            file_names = set(contents)
            directory_names = set(directories)
            for name in [*directories, *file_names]:
                parents = list(PurePosixPath(name).parents)[:-1]
                if any(str(parent) not in directory_names or str(parent) in file_names for parent in parents):
                    raise ValueError('hierarchy')
        except (ValueError, KeyError, TypeError, UnicodeError, RecursionError):
            raise ArchiveError('archive_manifest_invalid') from None
        destination = Path(destination).absolute()
        _budget(deadline)
        if (destination.parent.is_symlink() or destination.parent.resolve() != destination.parent
                or not destination.parent.is_dir() or destination.parent.stat().st_mode & 0o077
                or destination.exists() or destination.is_symlink()):
            raise ArchiveError('archive_destination_invalid')
        destination.mkdir(mode=0o700)
        for name in sorted(directories, key=lambda value: (len(value.split('/')), value)):
            (destination / name).mkdir(mode=0o700)
        for name, content in contents.items():
            fd = os.open(destination / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'wb') as target:
                target.write(content)
                target.flush()
                os.fsync(target.fileno())
        for path in [*(destination / name for name in reversed(directories)), destination, destination.parent]:
            fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        _budget(deadline)
        return {'version': 1, 'recovery_read_only': True, 'files': len(contents), 'size_bytes': total,
                'scope_digest': receipt.scope_digest, 'manifest_sha256': receipt.manifest.sha256}
