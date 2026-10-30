"""Trusted bare Git management and one-attempt Publisher candidates.

No task database, checkout, code execution, credential retrieval, deployment or
network enrollment. The installer uses the existing DeliveryStore connection.
Production transports are fixed HTTPS; local transports exist only in tests.
Authorization and validation are mandatory trusted, revocation-aware gates.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass
import fcntl
from functools import wraps
import hashlib
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import selectors
import signal
import shutil
import sqlite3
import stat
import subprocess
import tarfile
import tempfile
import threading
import time
from typing import Any, Callable, Iterator
from urllib.parse import urlsplit

from .durability import digest
from .store import DeliveryError, DeliveryStore, canonical, identifier, now, reference
from .object_archive import ArchiveError, ArchiveReceipt, ObjectArchive, MAX_BYTES, _name, _open_at


OID = re.compile(r'(?:[a-f0-9]{40}|[a-f0-9]{64})\Z')
SHA = re.compile(r'sha256:[a-f0-9]{64}\Z')
IDENTITY = re.compile(r'[a-z][a-z0-9-]{0,31}:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z')
REF = re.compile(r'refs/heads/[A-Za-z0-9][A-Za-z0-9_./-]{0,180}\Z')

_SCHEMA = (
    '''CREATE TABLE IF NOT EXISTS repository_mirrors (
        repo_id TEXT PRIMARY KEY REFERENCES repositories(repo_id), owner_ref TEXT NOT NULL,
        policy_digest TEXT NOT NULL, baseline_oid TEXT NOT NULL, retained_ref TEXT NOT NULL,
        manifest_digest TEXT NOT NULL, updated_at TEXT NOT NULL,
        FOREIGN KEY(owner_ref,repo_id) REFERENCES repositories(owner_ref,repo_id))''',
    '''CREATE TABLE IF NOT EXISTS repository_artifacts (
        artifact_ref TEXT PRIMARY KEY, owner_ref TEXT NOT NULL, task_id TEXT NOT NULL,
        task_revision INTEGER NOT NULL CHECK(task_revision>0), repo_id TEXT NOT NULL,
        kind TEXT NOT NULL CHECK(kind IN ('source','candidate_input')),
        sha256 TEXT NOT NULL, size INTEGER NOT NULL CHECK(size>0), metadata_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY(owner_ref,task_id) REFERENCES tasks(owner_ref,task_id),
        FOREIGN KEY(owner_ref,repo_id) REFERENCES repositories(owner_ref,repo_id))''',
    '''CREATE TABLE IF NOT EXISTS repository_candidates (
        candidate_ref TEXT PRIMARY KEY, owner_ref TEXT NOT NULL, task_id TEXT NOT NULL,
        task_revision INTEGER NOT NULL, repo_id TEXT NOT NULL, baseline_oid TEXT NOT NULL,
        candidate_oid TEXT NOT NULL, input_bundle_ref TEXT NOT NULL REFERENCES repository_artifacts(artifact_ref),
        input_digest TEXT NOT NULL, retained_ref TEXT NOT NULL, policy_digest TEXT NOT NULL,
        validation_json TEXT NOT NULL, created_at TEXT NOT NULL,
        FOREIGN KEY(owner_ref,task_id) REFERENCES tasks(owner_ref,task_id),
        FOREIGN KEY(owner_ref,repo_id) REFERENCES repositories(owner_ref,repo_id))''',
    '''CREATE TABLE IF NOT EXISTS repository_publications (
        publication_ref TEXT PRIMARY KEY, owner_ref TEXT NOT NULL, task_id TEXT NOT NULL,
        task_revision INTEGER NOT NULL, repo_id TEXT NOT NULL,
        candidate_ref TEXT NOT NULL REFERENCES repository_candidates(candidate_ref),
        destination_ref TEXT NOT NULL, expected_remote_oid TEXT,
        candidate_oid TEXT NOT NULL, policy_digest TEXT NOT NULL, validation_digest TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('prepared','sending','unknown','confirmed','conflict')),
        remote_oid TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        FOREIGN KEY(owner_ref,task_id) REFERENCES tasks(owner_ref,task_id),
        FOREIGN KEY(owner_ref,repo_id) REFERENCES repositories(owner_ref,repo_id))''',
    '''CREATE UNIQUE INDEX IF NOT EXISTS repository_one_unknown_publication ON repository_publications(repo_id)
       WHERE state IN ('sending','unknown')''',
    '''CREATE UNIQUE INDEX IF NOT EXISTS repository_one_source ON repository_artifacts(owner_ref,task_id,task_revision,repo_id)
       WHERE kind='source' ''',
    '''CREATE TRIGGER IF NOT EXISTS repository_artifact_no_update BEFORE UPDATE ON repository_artifacts
       BEGIN SELECT RAISE(ABORT,'append_only_artifact'); END''',
    '''CREATE TRIGGER IF NOT EXISTS repository_artifact_no_delete BEFORE DELETE ON repository_artifacts
       BEGIN SELECT RAISE(ABORT,'append_only_artifact'); END''',
    '''CREATE TRIGGER IF NOT EXISTS repository_candidate_no_update BEFORE UPDATE ON repository_candidates
       BEGIN SELECT RAISE(ABORT,'append_only_candidate'); END''',
    '''CREATE TRIGGER IF NOT EXISTS repository_candidate_no_delete BEFORE DELETE ON repository_candidates
       BEGIN SELECT RAISE(ABORT,'append_only_candidate'); END''',
    '''CREATE TRIGGER IF NOT EXISTS repository_publication_no_rewrite BEFORE UPDATE OF
       publication_ref,owner_ref,task_id,task_revision,repo_id,candidate_ref,destination_ref,
       expected_remote_oid,candidate_oid,policy_digest,validation_digest,created_at ON repository_publications
       BEGIN SELECT RAISE(ABORT,'immutable_publication'); END''',
    '''CREATE TRIGGER IF NOT EXISTS repository_publication_no_delete BEFORE DELETE ON repository_publications
       BEGIN SELECT RAISE(ABORT,'retained_publication'); END''',
    '''CREATE TRIGGER IF NOT EXISTS repository_publication_transition BEFORE UPDATE OF state ON repository_publications
       WHEN NOT (NEW.state=OLD.state OR (OLD.state='prepared' AND NEW.state='sending')
         OR (OLD.state IN ('sending','unknown') AND NEW.state IN ('unknown','confirmed','conflict')))
       BEGIN SELECT RAISE(ABORT,'publication_transition_invalid'); END''',
)


def install_repository_schema(connection: sqlite3.Connection) -> None:
    """Caller owns the DeliveryStore migration transaction; no implicit commit."""
    if not connection.in_transaction:
        raise DeliveryError('repository_migration_transaction_required')
    for statement in _SCHEMA:
        connection.execute(statement)


def _oid(value: Any) -> str:
    if not isinstance(value, str) or OID.fullmatch(value) is None:
        raise DeliveryError('repository_oid_invalid')
    return value


def _ref(value: Any) -> str:
    if (not isinstance(value, str) or REF.fullmatch(value) is None or '..' in value
            or '@{' in value or value.endswith(('/', '.', '.lock'))
            or any(part.startswith('.') or part.endswith('.lock') for part in value.split('/'))):
        raise DeliveryError('repository_ref_invalid')
    return value


def _sha(value: Any) -> str:
    if not isinstance(value, str) or SHA.fullmatch(value) is None:
        raise DeliveryError('repository_digest_invalid')
    return value


def _safe_io(method):
    @wraps(method)
    def call(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except DeliveryError:
            raise
        except (OSError, ValueError, UnicodeError, sqlite3.Error):
            raise DeliveryError('repository_storage_unavailable') from None
    return call


@dataclass(frozen=True, repr=False)
class RepositoryPolicy:
    remote_identity: str
    https_url: str
    baseline_ref: str = 'refs/heads/main'
    publish_targets: tuple[tuple[str, str], ...] = (('integration', 'refs/heads/main'),)
    revision: int = 1

    def __post_init__(self):
        if (not isinstance(self.remote_identity, str) or IDENTITY.fullmatch(self.remote_identity) is None
                or any(part in {'.', '..'} for part in self.remote_identity.split(':')[-1].split('/'))
                or not isinstance(self.https_url, str) or not 1 <= len(self.https_url) <= 2048
                or any(ord(char) < 33 for char in self.https_url)
                or any(char in self.https_url for char in ('\\', '%', '?', '#'))
                or type(self.revision) is not int or self.revision < 1):
            raise DeliveryError('repository_policy_invalid')
        try:
            parsed = urlsplit(self.https_url)
            valid = (parsed.scheme == 'https' and parsed.hostname and not parsed.username
                     and not parsed.password and not parsed.query and not parsed.fragment
                     and parsed.port in (None, 443) and parsed.path.startswith('/')
                     and parsed.path != '/' and '..' not in parsed.path.split('/')
                     and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]*', parsed.hostname))
        except ValueError:
            valid = False
        if not valid:
            raise DeliveryError('repository_policy_invalid')
        _ref(self.baseline_ref)
        if not isinstance(self.publish_targets, tuple) or not 1 <= len(self.publish_targets) <= 16:
            raise DeliveryError('repository_policy_invalid')
        names = set()
        for item in self.publish_targets:
            if (not isinstance(item, tuple) or len(item) != 2 or not isinstance(item[0], str)
                    or re.fullmatch(r'[a-z][a-z0-9-]{0,31}', item[0]) is None or item[0] in names):
                raise DeliveryError('repository_policy_invalid')
            names.add(item[0])
            _ref(item[1])

    @property
    def sha256(self) -> str:
        return digest(canonical(asdict(self)).encode())


@dataclass(frozen=True)
class AuthorizationReceipt:
    context_digest: str
    grant_ref: str


@dataclass(frozen=True)
class ValidationReceipt:
    context_digest: str
    bundle_sha256: str
    baseline_oid: str
    candidate_oid: str
    scan_digest: str
    tests_digest: str
    scan_passed: bool
    tests_passed: bool


class _Budget:
    def __init__(self, seconds: float, output: int):
        self.deadline = time.monotonic() + seconds
        self.remaining_output = output

    def remaining(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise DeliveryError('repository_git_timeout')
        return remaining

    def consume(self, size: int) -> None:
        self.remaining_output -= size
        if self.remaining_output < 0:
            raise DeliveryError('repository_output_limit')


class GitRuntime:
    """Fixed commands, clean environment, bounded aggregate output and deadline.

    No secrets are inherited or acquired. Authenticated production transports
    require separate qualification; this candidate supplies none.
    """
    def __init__(self, git_binary: str = '/usr/bin/git', *, timeout_seconds: float = 30,
                 max_output: int = 8 * 1024 * 1024, max_bundle: int = 64 * 1024 * 1024):
        if (not isinstance(git_binary, str) or not Path(git_binary).is_absolute()
                or type(timeout_seconds) not in {int, float} or not math.isfinite(timeout_seconds)
                or not 0.1 <= timeout_seconds <= 300
                or type(max_output) is not int or not 1024 <= max_output <= 128 * 1024 * 1024
                or type(max_bundle) is not int or not 1024 <= max_bundle <= 512 * 1024 * 1024):
            raise DeliveryError('repository_runtime_invalid')
        self.git_binary, self.timeout_seconds = git_binary, float(timeout_seconds)
        self.max_output, self.max_bundle = max_output, max_bundle

    def budget(self) -> _Budget:
        return _Budget(self.timeout_seconds, self.max_output)

    def _configuration(self, home: Path) -> list[str]:
        values = {'core.hooksPath': str(home / 'no-hooks'), 'core.fsmonitor': 'false',
                  'core.attributesFile': '/dev/null', 'core.protectHFS': 'true', 'core.protectNTFS': 'true',
                  'core.fsync': 'all', 'core.fsyncMethod': 'fsync',
                  'credential.helper': '', 'credential.interactive': 'never', 'http.followRedirects': 'false',
                  'http.sslVerify': 'true', 'protocol.allow': 'never', 'protocol.https.allow': 'always',
                  'protocol.file.allow': 'never', 'protocol.ext.allow': 'never', 'protocol.ssh.allow': 'never',
                  'submodule.recurse': 'false', 'fetch.recurseSubmodules': 'false', 'gc.auto': '0',
                  'maintenance.auto': 'false', 'transfer.fsckObjects': 'true', 'fetch.fsckObjects': 'true',
                  'receive.fsckObjects': 'true', 'filter.lfs.required': 'false', 'filter.lfs.clean': '',
                  'filter.lfs.smudge': '', 'filter.lfs.process': '', 'diff.external': '',
                  'user.name': 'Delivery Publisher', 'user.email': 'delivery@example.invalid'}
        return [part for key, value in values.items() for part in ('-c', key + '=' + value)]

    def run(self, home: Path, repository: Path | None, arguments: list[str], budget: _Budget,
            *, lockfd: int, allowed: tuple[int, ...] = (0,)) -> tuple[int, bytes]:
        budget.remaining()
        if (any(path.name != 'no-hooks' for path in home.iterdir())
                or (home / 'no-hooks').is_symlink() or any((home / 'no-hooks').iterdir())):
            # An empty dedicated HOME also excludes implicit .netrc/cookie and
            # helper material, rather than discovering old personal credentials.
            raise DeliveryError('repository_runtime_drift')
        command = [self.git_binary, *self._configuration(home)]
        if repository is not None:
            command += ['--git-dir=' + str(repository)]
        command += arguments
        environment = self._environment(home, arguments)
        process = None
        try:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, env=environment, cwd=home,
                                       close_fds=True, pass_fds=(lockfd,), start_new_session=True, bufsize=0)
            output = bytearray()
            with selectors.DefaultSelector() as selector:
                for stream in (process.stdout, process.stderr):
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, selectors.EVENT_READ)
                while selector.get_map():
                    events = selector.select(budget.remaining())
                    if not events:
                        raise DeliveryError('repository_git_timeout')
                    for key, _ in events:
                        try:
                            block = os.read(key.fileobj.fileno(), 65536)
                        except BlockingIOError:
                            continue
                        if not block:
                            selector.unregister(key.fileobj)
                            continue
                        budget.consume(len(block))
                        if key.fileobj is process.stdout:
                            output.extend(block)
            try:
                status = process.wait(timeout=budget.remaining())
            except subprocess.TimeoutExpired:
                raise DeliveryError('repository_git_timeout') from None
            budget.remaining()
            if status not in allowed:
                raise DeliveryError('repository_git_failed')
            return status, bytes(output)
        except OSError:
            raise DeliveryError('repository_git_unavailable') from None
        finally:
            if process is not None:
                if process.poll() is None:
                    # Requests only, never an assertion that all syscalls stopped.
                    for sig in (signal.SIGTERM, signal.SIGKILL):
                        try:
                            os.killpg(process.pid, sig)
                        except OSError:
                            pass
                    threading.Thread(target=process.wait, daemon=True, name='repository-git-reaper').start()
                for stream in (process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()

    def _environment(self, home, arguments):
        """Trusted subclass seam; the base runtime never acquires credentials."""
        return {'PATH': '/usr/bin:/bin', 'LANG': 'C', 'LC_ALL': 'C', 'TZ': 'Asia/Shanghai', 'HOME': str(home),
                       'XDG_CONFIG_HOME': str(home), 'GIT_CONFIG_NOSYSTEM': '1',
                       'GIT_CONFIG_SYSTEM': '/dev/null', 'GIT_CONFIG_GLOBAL': '/dev/null',
                       'GIT_TERMINAL_PROMPT': '0', 'GIT_ASKPASS': '/usr/bin/false',
                       'GIT_LFS_SKIP_SMUDGE': '1', 'GIT_NO_REPLACE_OBJECTS': '1',
                       'GIT_PROTOCOL_FROM_USER': '0', 'GIT_CONFIG_COUNT': '0'}

    def _remote(self, policy: RepositoryPolicy) -> str:
        return policy.https_url

    def ls_remote(self, home, repository, policy, ref, budget, lockfd):
        _, output = self.run(home, repository, ['ls-remote', '--refs', self._remote(policy), ref], budget, lockfd=lockfd)
        if not output:
            return None
        try:
            lines = output.decode('ascii').splitlines()
            oid, name = lines[0].split('\t')
            if len(lines) != 1 or name != ref:
                raise ValueError('ref')
            return _oid(oid)
        except (ValueError, UnicodeError):
            raise DeliveryError('repository_remote_invalid') from None

    def fetch(self, home, repository, policy, destination, budget, lockfd):
        self.run(home, repository, ['fetch', '--no-tags', '--no-recurse-submodules', '--no-auto-maintenance',
                                   '--no-write-fetch-head', self._remote(policy),
                                   policy.baseline_ref + ':' + destination], budget, lockfd=lockfd)

    def push(self, home, repository, policy, oid, destination, budget, lockfd):
        self.run(home, repository, ['push', '--porcelain', '--no-verify', '--recurse-submodules=no',
                                   self._remote(policy), oid + ':' + destination], budget, lockfd=lockfd)


def _private(path: Path) -> None:
    if not path.is_absolute() or '..' in path.parts:
        raise DeliveryError('repository_storage_invalid')
    current = Path('/')
    for component in path.parts[1:]:
        current /= component
        if current.is_symlink():
            raise DeliveryError('repository_storage_invalid')
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.is_dir() or path.stat().st_mode & 0o077:
        raise DeliveryError('repository_storage_permissions_invalid')


def _read(path: Path, limit: int) -> bytes:
    handle = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(handle)
        if not stat.S_ISREG(before.st_mode) or before.st_mode & 0o077 or not 0 <= before.st_size <= limit:
            raise DeliveryError('repository_artifact_invalid')
        chunks = []
        size = 0
        while size <= limit:
            block = os.read(handle, min(65536, limit + 1 - size))
            if not block:
                break
            size += len(block)
            chunks.append(block)
        after = os.fstat(handle)
        identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        if size != before.st_size or identity(before) != identity(after):
            raise DeliveryError('repository_artifact_changed')
        return b''.join(chunks)
    finally:
        os.close(handle)


def _immutable(path: Path, content: bytes) -> None:
    temporary = path.parent / ('.pending-' + identifier('IO'))
    handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(handle, 'wb') as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError:
            if _read(path, len(content)) != content:
                raise DeliveryError('repository_artifact_conflict')
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


class RepoManager:
    @_safe_io
    def __init__(self, store: DeliveryStore, root: str | Path, *,
                 policies: tuple[RepositoryPolicy, ...], git_runtime: GitRuntime,
                 authorization_gate: Callable[[dict], AuthorizationReceipt],
                 validation_gate: Callable[[dict], ValidationReceipt]):
        if (not isinstance(store, DeliveryStore) or not isinstance(git_runtime, GitRuntime)
                or not callable(authorization_gate) or not callable(validation_gate)
                or not isinstance(policies, tuple) or not policies
                or any(not isinstance(policy, RepositoryPolicy) for policy in policies)
                or len({policy.remote_identity for policy in policies}) != len(policies)):
            raise DeliveryError('repository_manager_invalid')
        self.store, self.root, self.git = store, Path(root), git_runtime
        _private(self.root)
        self.home = self.root / 'runtime'
        _private(self.home)
        _private(self.home / 'no-hooks')
        if any((self.home / 'no-hooks').iterdir()):
            raise DeliveryError('repository_runtime_drift')
        self.policies = {policy.remote_identity: policy for policy in policies}
        self.authorization_gate, self.validation_gate = authorization_gate, validation_gate
        with closing(store._connect()) as connection:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {'repository_mirrors', 'repository_artifacts', 'repository_candidates', 'repository_publications'} <= tables:
                raise DeliveryError('repository_migration_required')

    def _policy(self, row) -> RepositoryPolicy:
        policy = self.policies.get(row['remote_identity'])
        if policy is None or row['status'] != 'enrolled':
            raise DeliveryError('repository_policy_unavailable')
        return policy

    def _context(self, owner, task_ref, revision, repo_ref, *, write=False, terminal=False) -> tuple[dict, RepositoryPolicy]:
        if not terminal and self.store.recovery_mode:
            raise DeliveryError('recovery_read_only')
        with closing(self.store._connect()) as connection:
            repo = self.store._owned(connection, 'repositories', 'repo_id', owner, repo_ref)
            task = self.store._owned(connection, 'tasks', 'task_id', owner, task_ref)
            if type(revision) is not int or task['revision'] != revision:
                raise DeliveryError('revision_conflict')
            if not terminal and (task['state'] not in {'queued', 'running'} or task['control_pending']):
                raise DeliveryError('repository_task_inactive')
            project = self.store._project(connection, owner, task['project_id']) if task['project_id'] else None
            if project is None or not terminal and project['state'] != 'active':
                raise DeliveryError('project_inactive')
            binding = connection.execute('SELECT * FROM task_repositories WHERE owner_ref=? AND task_id=? AND repo_id=?',
                                         (owner, task_ref, repo_ref)).fetchone()
            if binding is None or write and binding['access'] != 'write':
                raise DeliveryError('repository_scope_missing')
            if json.loads(binding['paths_json']) != []:
                raise DeliveryError('path_isolation_unavailable')
            if write and task['mode'] not in {'develop', 'deliver', 'deploy'}:
                raise DeliveryError('repository_development_mode_required')
            policy = self._policy(repo)
            context = dict(owner_ref=owner, task_id=task_ref, task_revision=revision,
                           project_id=task['project_id'], repo_id=repo_ref,
                           task_mode=task['mode'],
                           remote_identity=repo['remote_identity'], policy_digest=policy.sha256)
        return context, policy

    def _authorize(self, action: str, context: dict) -> AuthorizationReceipt:
        request = dict(context, action=action)
        try:
            receipt = self.authorization_gate(request)
            if not isinstance(receipt, AuthorizationReceipt) or receipt.context_digest != digest(canonical(request).encode()):
                raise ValueError('binding')
            reference(receipt.grant_ref)
            return receipt
        except Exception:
            raise DeliveryError('repository_authorization_denied') from None

    def _validate(self, context: dict) -> ValidationReceipt:
        try:
            receipt = self.validation_gate(dict(context))
            if (not isinstance(receipt, ValidationReceipt) or receipt.context_digest != digest(canonical(context).encode())
                    or receipt.bundle_sha256 != context['bundle_sha256']
                    or receipt.baseline_oid != context['baseline_oid'] or receipt.candidate_oid != context['candidate_oid']
                    or type(receipt.scan_passed) is not bool or receipt.scan_passed is not True
                    or type(receipt.tests_passed) is not bool or receipt.tests_passed is not True):
                raise ValueError('binding')
            _sha(receipt.scan_digest)
            _sha(receipt.tests_digest)
            return receipt
        except Exception:
            raise DeliveryError('repository_validation_failed') from None

    def _existing(self, owner, key, kind, arguments):
        reference(owner)
        reference(key)
        with closing(self.store._connect()) as connection:
            row = connection.execute('SELECT * FROM commands WHERE owner_ref=? AND idempotency_key=?', (owner, key)).fetchone()
            if row is None:
                return None
            if row['argument_digest'] != digest(canonical(dict(kind=kind, arguments=arguments)).encode()):
                raise DeliveryError('idempotency_conflict')
            result = json.loads(row['result_json'])
        return {**result, **self.store.reconcile(row['event_id'])}

    @contextmanager
    def _locked(self, repo_ref: str) -> Iterator[tuple[Path, int]]:
        reference(repo_ref)
        directory = self.root / 'repositories' / repo_ref
        _private(directory)
        handle = os.open(directory / 'manager.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            info = os.fstat(handle)
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_nlink != 1:
                raise DeliveryError('repository_storage_permissions_invalid')
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise DeliveryError('repository_busy') from None
            yield directory, handle
        finally:
            # Do not LOCK_UN: a surviving exec child can retain this description.
            os.close(handle)

    def _run(self, repository, args, budget, lockfd, *, allowed=(0,)):
        return self.git.run(self.home, repository, args, budget, lockfd=lockfd, allowed=allowed)

    def _retain(self, mirror, retained, oid, budget, lockfd):
        status, output = self._run(mirror, ['rev-parse', '--verify', retained], budget, lockfd, allowed=(0, 128))
        if status == 0:
            if output.decode('ascii').strip() != oid:
                raise DeliveryError('repository_retained_ref_conflict')
            return
        self._run(mirror, ['update-ref', retained, oid, '0' * len(oid)], budget, lockfd)

    def _mirror(self, directory: Path, budget: _Budget, lockfd: int, policy: RepositoryPolicy) -> Path:
        mirror = directory / 'mirror.git'
        marker = directory / 'mirror.json'
        if mirror.is_symlink() or marker.is_symlink():
            raise DeliveryError('repository_mirror_invalid')
        if not mirror.exists():
            if marker.exists():
                raise DeliveryError('repository_mirror_missing')
            stage = directory / ('.initializing-' + identifier('MI'))
            self._run(None, ['init', '--bare', '--template=', str(stage)], budget, lockfd)
            stage.chmod(0o700)
            (stage / 'config').chmod(0o600)
            self._run(stage, ['fsck', '--strict', '--no-reflogs'], budget, lockfd)
            config_digest = digest(_read(stage / 'config', 65536))
            _immutable(marker, canonical(dict(version=1, remote_identity=policy.remote_identity,
                                               config_digest=config_digest)).encode())
            os.rename(stage, mirror)
            parent = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
        if mirror.is_symlink() or not mirror.is_dir() or not marker.exists():
            raise DeliveryError('repository_mirror_invalid')
        try:
            raw = _read(marker, 65536)
            record = json.loads(raw)
        except (ValueError, OSError):
            raise DeliveryError('repository_mirror_invalid') from None
        if (set(record) != {'version', 'remote_identity', 'config_digest'} or record['version'] != 1
                or type(record['version']) is not int or raw != canonical(record).encode()
                or record['remote_identity'] != policy.remote_identity
                or digest(_read(mirror / 'config', 65536)) != record['config_digest']):
            raise DeliveryError('repository_mirror_config_drift')
        return mirror

    def _artifact_path(self, sha256: str) -> Path:
        _sha(sha256)
        directory = self.root / 'bundles'
        _private(directory)
        return directory / (sha256[7:] + '.bundle')

    def _artifact(self, connection, owner, artifact_ref, context):
        row = connection.execute('SELECT * FROM repository_artifacts WHERE owner_ref=? AND artifact_ref=?',
                                 (owner, reference(artifact_ref))).fetchone()
        if (row is None or any(row[key] != context[key] for key in ('task_id', 'task_revision', 'repo_id'))):
            raise DeliveryError('repository_artifact_not_found')
        return dict(row)

    def _source(self, connection, context):
        row = connection.execute("SELECT * FROM repository_artifacts WHERE owner_ref=? AND task_id=? AND task_revision=? AND repo_id=? AND kind='source' ORDER BY created_at DESC LIMIT 1",
                                 (context['owner_ref'], context['task_id'], context['task_revision'], context['repo_id'])).fetchone()
        if row is None:
            raise DeliveryError('repository_baseline_missing')
        return dict(row), json.loads(row['metadata_json'])

    def _inspect(self, repository: Path, candidate: str, budget, lockfd, blobs):
        # Read Git objects, never filters/checkout. This candidate conservatively
        # rejects all symlinks rather than guessing downstream platform behavior.
        _, tree = self._run(repository, ['ls-tree', '-r', '-z', '--full-tree', candidate], budget, lockfd)
        for entry in tree.split(b'\0'):
            if not entry:
                continue
            try:
                header, path = entry.split(b'\t', 1)
                mode, kind, oid = header.decode('ascii').split(' ')
                name = path.decode('utf-8')
                _oid(oid)
            except (ValueError, UnicodeError):
                raise DeliveryError('repository_tree_invalid') from None
            if (mode not in {'100644', '100755'} or kind != 'blob'
                    or any(part.lower() in {'.git', '.gitmodules'} for part in PurePosixPath(name).parts)):
                raise DeliveryError('repository_recursive_content_unsupported')
            if oid not in blobs:
                _, blob = self._run(repository, ['cat-file', 'blob', oid], budget, lockfd)
                blobs[oid] = blob
            blob = blobs[oid]
            if blob.startswith(b'version https://git-lfs.github.com/spec/v1\n'):
                raise DeliveryError('repository_lfs_unsupported')
            if PurePosixPath(name).name == '.gitattributes' and re.search(rb'(?i)filter', blob):
                raise DeliveryError('repository_filters_unsupported')

    def _inspect_history(self, repository, tip, budget, lockfd):
        _, commits = self._run(repository, ['rev-list', tip], budget, lockfd)
        blobs = {}
        for commit in commits.decode('ascii').splitlines():
            self._inspect(repository, _oid(commit), budget, lockfd, blobs)

    @_safe_io
    def sync(self, owner: str, key: str, *, repo_ref: str, expected_revision: int) -> dict:
        arguments = dict(repo_ref=repo_ref, expected_revision=expected_revision)
        existing = self._existing(owner, key, 'repository.sync', arguments)
        if existing:
            return existing
        if self.store.recovery_mode:
            raise DeliveryError('recovery_read_only')
        with closing(self.store._connect()) as connection:
            row = self.store._owned(connection, 'repositories', 'repo_id', owner, repo_ref)
        if type(expected_revision) is not int or row['revision'] != expected_revision:
            raise DeliveryError('revision_conflict')
        policy = self._policy(row)
        context = dict(owner_ref=owner, repo_id=repo_ref, remote_identity=policy.remote_identity,
                       policy_digest=policy.sha256, repository_revision=expected_revision)
        self._authorize('sync', context)
        budget = self.git.budget()
        with self._locked(repo_ref) as (directory, lockfd):
            mirror = self._mirror(directory, budget, lockfd, policy)
            retained = 'refs/codex/fetch/' + identifier('SYNC')
            self.git.fetch(self.home, mirror, policy, retained, budget, lockfd)
            _, output = self._run(mirror, ['rev-parse', '--verify', retained + '^{commit}'], budget, lockfd)
            baseline = _oid(output.decode('ascii').strip())
            remote = self.git.ls_remote(self.home, mirror, policy, policy.baseline_ref, budget, lockfd)
            if remote != baseline:
                raise DeliveryError('repository_baseline_drift')
            self._inspect_history(mirror, baseline, budget, lockfd)
            manifest = digest(canonical(dict(repo_id=repo_ref, policy_digest=policy.sha256,
                                               baseline_oid=baseline, retained_ref=retained)).encode())
            def apply(connection, timestamp):
                current = self.store._owned(connection, 'repositories', 'repo_id', owner, repo_ref)
                if current['revision'] != expected_revision or self._policy(current).sha256 != policy.sha256:
                    raise DeliveryError('revision_conflict')
                connection.execute('INSERT INTO repository_mirrors VALUES (?,?,?,?,?,?,?) ON CONFLICT(repo_id) DO UPDATE SET policy_digest=excluded.policy_digest,baseline_oid=excluded.baseline_oid,retained_ref=excluded.retained_ref,manifest_digest=excluded.manifest_digest,updated_at=excluded.updated_at',
                                   (repo_ref, owner, policy.sha256, baseline, retained, manifest, timestamp))
                return dict(repo_id=repo_ref, baseline_oid=baseline, manifest_digest=manifest)
            return self.store._mutation(owner, key, 'repository.sync', arguments, apply)

    @_safe_io
    def export_bundle(self, owner: str, key: str, *, task_ref: str, expected_revision: int, repo_ref: str) -> dict:
        arguments = dict(task_ref=task_ref, expected_revision=expected_revision, repo_ref=repo_ref)
        existing = self._existing(owner, key, 'repository.export', arguments)
        if existing:
            return existing
        context, policy = self._context(owner, task_ref, expected_revision, repo_ref)
        self._authorize('export', context)
        with closing(self.store._connect()) as connection:
            row = connection.execute('SELECT * FROM repository_mirrors WHERE repo_id=? AND owner_ref=?', (repo_ref, owner)).fetchone()
            if row is None or row['policy_digest'] != policy.sha256:
                raise DeliveryError('repository_baseline_missing')
            baseline = row['baseline_oid']
            old = connection.execute("SELECT artifact_ref FROM repository_artifacts WHERE owner_ref=? AND task_id=? AND task_revision=? AND repo_id=? AND kind='source'", (owner, task_ref, expected_revision, repo_ref)).fetchone()
            if old:
                raise DeliveryError('repository_baseline_already_frozen')
        budget = self.git.budget()
        artifact = identifier('RA')
        retained = 'refs/codex/baselines/' + task_ref + '/r' + str(expected_revision)
        with self._locked(repo_ref) as (directory, lockfd):
            mirror = self._mirror(directory, budget, lockfd, policy)
            self._run(mirror, ['fsck', '--strict', '--no-reflogs'], budget, lockfd)
            self._retain(mirror, retained, baseline, budget, lockfd)
            bundle = directory / (artifact + '.bundle')
            self._run(mirror, ['bundle', 'create', str(bundle), retained], budget, lockfd)
            bundle.chmod(0o600)
            content = _read(bundle, self.git.max_bundle)
            sha256 = digest(content)
            _immutable(self._artifact_path(sha256), content)
            metadata = dict(baseline_oid=baseline, retained_ref=retained, policy_digest=policy.sha256,
                            candidate_branch='refs/heads/codex/' + task_ref)
            def apply(connection, timestamp):
                self._context(owner, task_ref, expected_revision, repo_ref)
                connection.execute('INSERT INTO repository_artifacts VALUES (?,?,?,?,?,?,?,?,?,?)',
                    (artifact, owner, task_ref, expected_revision, repo_ref, 'source', sha256, len(content), canonical(metadata), timestamp))
                return dict(task_id=task_ref, repo_id=repo_ref, artifact_ref=artifact, sha256=sha256,
                            size=len(content), baseline_oid=baseline, candidate_branch=metadata['candidate_branch'])
            return self.store._mutation(owner, key, 'repository.export', arguments, apply)

    @_safe_io
    def stage_bundle(self, owner: str, key: str, *, task_ref: str, expected_revision: int,
                     repo_ref: str, content: bytes, expected_digest: str) -> dict:
        """Trusted upload adapter only; model calls use the returned artifact ref."""
        if not isinstance(content, bytes) or not 1 <= len(content) <= self.git.max_bundle or digest(content) != _sha(expected_digest):
            raise DeliveryError('repository_artifact_invalid')
        arguments = dict(task_ref=task_ref, expected_revision=expected_revision, repo_ref=repo_ref,
                         sha256=expected_digest, size=len(content))
        existing = self._existing(owner, key, 'repository.stage', arguments)
        if existing:
            return existing
        context, _ = self._context(owner, task_ref, expected_revision, repo_ref, write=True)
        self._authorize('stage', dict(context, bundle_sha256=expected_digest, size=len(content)))
        _immutable(self._artifact_path(expected_digest), content)
        artifact = identifier('RA')
        def apply(connection, timestamp):
            self._context(owner, task_ref, expected_revision, repo_ref, write=True)
            connection.execute('INSERT INTO repository_artifacts VALUES (?,?,?,?,?,?,?,?,?,?)',
                (artifact, owner, task_ref, expected_revision, repo_ref, 'candidate_input', expected_digest,
                 len(content), '{}', timestamp))
            return dict(task_id=task_ref, repo_id=repo_ref, artifact_ref=artifact, sha256=expected_digest)
        return self.store._mutation(owner, key, 'repository.stage', arguments, apply)

    @_safe_io
    def assemble_candidate(self,owner,key,*,task_ref,expected_revision,repo_ref,content,expected_digest):
        """Trusted handoff adapter: data-only guest changes, never host project execution."""
        from .repository_changes import assemble_candidate
        return assemble_candidate(self,owner,key,task_ref=task_ref,expected_revision=expected_revision,
            repo_ref=repo_ref,content=content,expected_digest=expected_digest)

    @_safe_io
    def import_candidate(self, owner: str, key: str, *, task_ref: str, expected_revision: int,
                         repo_ref: str, bundle_ref: str, expected_digest: str, candidate_oid: str) -> dict:
        _oid(candidate_oid)
        _sha(expected_digest)
        arguments = dict(task_ref=task_ref, expected_revision=expected_revision, repo_ref=repo_ref,
                         bundle_ref=bundle_ref, expected_digest=expected_digest, candidate_oid=candidate_oid)
        existing = self._existing(owner, key, 'repository.import', arguments)
        if existing:
            return existing
        context, policy = self._context(owner, task_ref, expected_revision, repo_ref, write=True)
        self._authorize('import', dict(context, bundle_sha256=expected_digest, candidate_oid=candidate_oid))
        with closing(self.store._connect()) as connection:
            incoming = self._artifact(connection, owner, bundle_ref, context)
            source, metadata = self._source(connection, context)
        if (incoming['kind'] != 'candidate_input' or incoming['sha256'] != expected_digest
                or metadata['policy_digest'] != policy.sha256):
            raise DeliveryError('repository_artifact_invalid')
        for artifact in (incoming, source):
            content = _read(self._artifact_path(artifact['sha256']), self.git.max_bundle)
            if digest(content) != artifact['sha256'] or len(content) != artifact['size']:
                raise DeliveryError('repository_artifact_changed')
        baseline = metadata['baseline_oid']
        validation_context = dict(context, bundle_sha256=expected_digest, baseline_oid=baseline,
                                  candidate_oid=candidate_oid)
        # Deferred same-Store tests must settle before allocating a quarantine
        # identity. A pending/unknown test never creates another import attempt.
        validation = self._validate(validation_context)
        candidate_ref = identifier('RC')
        retained = 'refs/codex/candidates/' + task_ref + '/r' + str(expected_revision) + '/' + candidate_ref
        budget = self.git.budget()
        with self._locked(repo_ref) as (directory, lockfd):
            mirror = self._mirror(directory, budget, lockfd, policy)
            quarantine = directory / ('quarantine-' + candidate_ref + '.git')
            self._run(None, ['init', '--bare', '--template=', str(quarantine)], budget, lockfd)
            quarantine.chmod(0o700)
            source_path = self._artifact_path(source['sha256'])
            incoming_path = self._artifact_path(incoming['sha256'])
            self._run(quarantine, ['bundle', 'unbundle', str(source_path)], budget, lockfd)
            self._run(quarantine, ['update-ref', 'refs/codex/baseline', baseline], budget, lockfd)
            self._run(quarantine, ['bundle', 'verify', str(incoming_path)], budget, lockfd)
            _, heads = self._run(quarantine, ['bundle', 'list-heads', str(incoming_path)], budget, lockfd)
            if heads != (candidate_oid + ' ' + metadata['candidate_branch'] + '\n').encode():
                raise DeliveryError('repository_bundle_refs_invalid')
            self._run(quarantine, ['bundle', 'unbundle', str(incoming_path)], budget, lockfd)
            self._run(quarantine, ['update-ref', retained, candidate_oid], budget, lockfd)
            self._run(quarantine, ['fsck', '--strict', '--no-reflogs'], budget, lockfd)
            status, _ = self._run(quarantine, ['merge-base', '--is-ancestor', baseline, candidate_oid], budget, lockfd, allowed=(0, 1))
            if status != 0:
                raise DeliveryError('repository_candidate_not_descendant')
            self._inspect_history(quarantine, candidate_oid, budget, lockfd)
            self._context(owner, task_ref, expected_revision, repo_ref, write=True)
            if self._validate(validation_context) != validation:
                raise DeliveryError('repository_validation_changed')
            full_bundle = directory / (candidate_ref + '.bundle')
            self._run(quarantine, ['bundle', 'create', str(full_bundle), retained], budget, lockfd)
            full_bundle.chmod(0o600)
            _read(full_bundle, self.git.max_bundle)
            self._run(mirror, ['bundle', 'unbundle', str(full_bundle)], budget, lockfd)
            self._run(mirror, ['update-ref', retained, candidate_oid, '0' * len(candidate_oid)], budget, lockfd)
            def apply(connection, timestamp):
                self._context(owner, task_ref, expected_revision, repo_ref, write=True)
                connection.execute('INSERT INTO repository_candidates VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (candidate_ref, owner, task_ref, expected_revision, repo_ref, baseline, candidate_oid,
                     bundle_ref, expected_digest, retained, policy.sha256, canonical(asdict(validation)), timestamp))
                return dict(task_id=task_ref, repo_id=repo_ref, candidate_ref=candidate_ref,
                            baseline_oid=baseline, candidate_oid=candidate_oid,
                            validation_digest=digest(canonical(asdict(validation)).encode()))
            return self.store._mutation(owner, key, 'repository.import', arguments, apply)

    def _candidate(self, connection, owner, candidate_ref, context):
        row = connection.execute('SELECT * FROM repository_candidates WHERE owner_ref=? AND candidate_ref=?',
                                 (owner, reference(candidate_ref))).fetchone()
        if row is None or any(row[key] != context[key] for key in ('task_id', 'task_revision', 'repo_id', 'policy_digest')):
            raise DeliveryError('repository_candidate_not_found')
        return dict(row)

    def _validate_candidate(self, context, candidate):
        validation = self._validate(dict(context, bundle_sha256=candidate['input_digest'],
                                         baseline_oid=candidate['baseline_oid'], candidate_oid=candidate['candidate_oid']))
        if canonical(asdict(validation)) != candidate['validation_json']:
            raise DeliveryError('repository_validation_changed')
        return digest(canonical(asdict(validation)).encode())

    @_safe_io
    def prepare_publish(self, owner: str, key: str, *, task_ref: str, expected_revision: int,
                        repo_ref: str, candidate_ref: str, target_alias: str) -> dict:
        arguments = dict(task_ref=task_ref, expected_revision=expected_revision, repo_ref=repo_ref,
                         candidate_ref=candidate_ref, target_alias=target_alias)
        existing = self._existing(owner, key, 'repository.publish.prepare', arguments)
        if existing:
            return existing
        context, policy = self._context(owner, task_ref, expected_revision, repo_ref, write=True)
        if context['task_mode'] not in {'deliver', 'deploy'}:
            raise DeliveryError('repository_publish_mode_required')
        destination = dict(policy.publish_targets).get(target_alias)
        if destination is None:
            raise DeliveryError('repository_publish_target_invalid')
        with closing(self.store._connect()) as connection:
            candidate = self._candidate(connection, owner, candidate_ref, context)
            if connection.execute("SELECT 1 FROM repository_publications WHERE repo_id=? AND state IN ('sending','unknown')", (repo_ref,)).fetchone():
                raise DeliveryError('repository_publish_unknown')
        validation_digest = self._validate_candidate(context, candidate)
        self._authorize('prepare_publish', dict(context, candidate_oid=candidate['candidate_oid'], destination_ref=destination))
        budget = self.git.budget()
        with self._locked(repo_ref) as (directory, lockfd):
            mirror = self._mirror(directory, budget, lockfd, policy)
            remote = self.git.ls_remote(self.home, mirror, policy, destination, budget, lockfd)
            if (destination == policy.baseline_ref and remote != candidate['baseline_oid']
                    or remote is not None and remote != candidate['baseline_oid']):
                raise DeliveryError('repository_baseline_drift')
            publication = identifier('PUB')
            def apply(connection, timestamp):
                self._context(owner, task_ref, expected_revision, repo_ref, write=True)
                connection.execute('INSERT INTO repository_publications VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (publication, owner, task_ref, expected_revision, repo_ref, candidate_ref, destination,
                     remote, candidate['candidate_oid'], policy.sha256, validation_digest, 'prepared', None, timestamp, timestamp))
                return dict(task_id=task_ref, repo_id=repo_ref, publication_ref=publication, state='prepared',
                            candidate_oid=candidate['candidate_oid'])
            return self.store._mutation(owner, key, 'repository.publish.prepare', arguments, apply)

    def _publication(self, owner, publication_ref):
        with closing(self.store._connect()) as connection:
            row = connection.execute('SELECT * FROM repository_publications WHERE owner_ref=? AND publication_ref=?',
                                     (owner, reference(publication_ref))).fetchone()
        if row is None:
            raise DeliveryError('not_found')
        return dict(row)

    @_safe_io
    def publish(self, owner: str, key: str, *, publication_ref: str) -> dict:
        """Only the invocation that FIRST durably claims can call push once."""
        reference(key)
        publication = self._publication(owner, publication_ref)
        if publication['state'] != 'prepared':
            return self.publication_status(owner, publication_ref)
        context, policy = self._context(owner, publication['task_id'], publication['task_revision'], publication['repo_id'], write=True)
        if context['task_mode'] not in {'deliver', 'deploy'}:
            raise DeliveryError('repository_publish_mode_required')
        with closing(self.store._connect()) as connection:
            candidate = self._candidate(connection, owner, publication['candidate_ref'], context)
        if self._validate_candidate(context, candidate) != publication['validation_digest']:
            raise DeliveryError('repository_validation_changed')
        authorization = self._authorize('publish', dict(context, publication_ref=publication_ref,
            candidate_oid=publication['candidate_oid'], baseline_oid=candidate['baseline_oid'],
            destination_ref=publication['destination_ref'], expected_remote_oid=publication['expected_remote_oid']))
        budget = self.git.budget()
        with self._locked(publication['repo_id']) as (directory, lockfd):
            mirror = self._mirror(directory, budget, lockfd, policy)
            remote = self.git.ls_remote(self.home, mirror, policy, publication['destination_ref'], budget, lockfd)
            if remote != publication['expected_remote_oid']:
                raise DeliveryError('repository_baseline_drift')
            fresh = False
            arguments = dict(publication_ref=publication_ref, authorization=asdict(authorization))
            def claim(connection, timestamp):
                nonlocal fresh
                refreshed, _ = self._context(owner, publication['task_id'], publication['task_revision'], publication['repo_id'], write=True)
                if refreshed != context:
                    raise DeliveryError('repository_policy_changed')
                changed = connection.execute("UPDATE repository_publications SET state='sending',updated_at=? WHERE publication_ref=? AND owner_ref=? AND state='prepared'", (timestamp, publication_ref, owner)).rowcount
                if changed != 1:
                    raise DeliveryError('repository_publish_unknown')
                fresh = True
                return dict(task_id=publication['task_id'], repo_id=publication['repo_id'],
                            publication_ref=publication_ref, state='sending')
            claimed = self.store._mutation(owner, key, 'repository.publish.claim', arguments, claim)
            if not fresh or claimed['status'] != 'accepted':
                return dict(claimed, state='unknown', code='repository_publish_unknown')
            # Recheck revocable gates after claim replication and immediately
            # before the only external write. Failure leaves an unknown claim.
            try:
                refreshed, _ = self._context(owner, publication['task_id'], publication['task_revision'], publication['repo_id'], write=True)
                if refreshed != context:
                    raise DeliveryError('repository_policy_changed')
                self._validate_candidate(context, candidate)
                current_authorization = self._authorize('publish', dict(context, publication_ref=publication_ref,
                    candidate_oid=publication['candidate_oid'], baseline_oid=candidate['baseline_oid'],
                    destination_ref=publication['destination_ref'], expected_remote_oid=publication['expected_remote_oid']))
                if current_authorization != authorization:
                    raise DeliveryError('repository_authorization_changed')
                if self.git.ls_remote(self.home, mirror, policy, publication['destination_ref'], budget, lockfd) != publication['expected_remote_oid']:
                    raise DeliveryError('repository_baseline_drift')
                current_authorization = self._authorize('publish', dict(context, publication_ref=publication_ref,
                    candidate_oid=publication['candidate_oid'], baseline_oid=candidate['baseline_oid'],
                    destination_ref=publication['destination_ref'], expected_remote_oid=publication['expected_remote_oid']))
                if current_authorization != authorization:
                    raise DeliveryError('repository_authorization_changed')
                budget.remaining()
                self.git.push(self.home, mirror, policy, publication['candidate_oid'], publication['destination_ref'], budget, lockfd)
                remote = self.git.ls_remote(self.home, mirror, policy, publication['destination_ref'], budget, lockfd)
            except Exception:
                remote = None
            state = 'confirmed' if remote == publication['candidate_oid'] else 'unknown'
            return self._record_publication(owner, publication_ref, state, remote)

    def _record_publication(self, owner, publication_ref, state, remote, *, key=None,
                            kind='repository.publish.result', arguments=None):
        publication = self._publication(owner, publication_ref)
        arguments = arguments or dict(publication_ref=publication_ref, state=state, remote_oid=remote)
        def apply(connection, timestamp):
            current = self._publication(owner, publication_ref)
            if current['state'] not in {'sending', 'unknown', state}:
                raise DeliveryError('repository_publish_conflict')
            connection.execute('UPDATE repository_publications SET state=?,remote_oid=?,updated_at=? WHERE publication_ref=? AND owner_ref=?',
                               (state, remote, timestamp, publication_ref, owner))
            return dict(task_id=publication['task_id'], repo_id=publication['repo_id'],
                        publication_ref=publication_ref, state=state, remote_oid=remote)
        return self.store._mutation(owner, key or identifier('PR'), kind, arguments, apply)

    @_safe_io
    def reconcile_publish(self, owner: str, key: str, *, publication_ref: str) -> dict:
        """Read only remote reconciliation. Even baseline/absence NEVER retries."""
        reference(key)
        arguments = dict(publication_ref=publication_ref)
        existing = self._existing(owner, key, 'repository.publish.reconcile', arguments)
        if existing:
            return existing
        publication = self._publication(owner, publication_ref)
        if publication['state'] in {'prepared', 'confirmed', 'conflict'}:
            return self.publication_status(owner, publication_ref)
        context, policy = self._context(owner, publication['task_id'], publication['task_revision'], publication['repo_id'], terminal=True)
        if policy.sha256 != publication['policy_digest']:
            raise DeliveryError('repository_policy_changed')
        self._authorize('reconcile_publish', dict(context, publication_ref=publication_ref,
                                                destination_ref=publication['destination_ref']))
        budget = self.git.budget()
        with self._locked(publication['repo_id']) as (directory, lockfd):
            mirror = self._mirror(directory, budget, lockfd, policy)
            remote = self.git.ls_remote(self.home, mirror, policy, publication['destination_ref'], budget, lockfd)
            state = ('confirmed' if remote == publication['candidate_oid'] else 'unknown'
                     if remote == publication['expected_remote_oid'] else 'conflict')
            return self._record_publication(owner, publication_ref, state, remote, key=key,
                                            kind='repository.publish.reconcile', arguments=arguments)

    @_safe_io
    def publication_status(self, owner: str, publication_ref: str) -> dict:
        """No implicit Git/network read. 'sending' is always publicly unknown."""
        publication = self._publication(owner, publication_ref)
        with closing(self.store._connect()) as connection:
            evidence = connection.execute("SELECT d.state FROM commands c JOIN events e ON e.event_id=c.event_id JOIN durability d ON d.event_id=e.event_id WHERE c.owner_ref=? AND json_extract(c.result_json,'$.publication_ref')=? ORDER BY e.sequence DESC LIMIT 1", (owner, publication_ref)).fetchone()
        replicated = evidence is not None and evidence['state'] == 'confirmed'
        return dict(publication_ref=publication_ref, state='unknown' if publication['state'] == 'sending'
                    else publication['state'], candidate_oid=publication['candidate_oid'],
                    remote_oid=publication['remote_oid'], durability='replicated' if replicated else 'local_only',
                    status='accepted' if replicated else 'durability_pending')

    @_safe_io
    def observe_publication(self, owner: str, publication_ref: str, *, deadline: float) -> dict:
        """Fresh read of the exact remote OID; never write/reconcile or push.

        Release qualification must not treat a historical confirmed push as a
        current remote observation. The caller's original deadline is retained.
        """
        if type(deadline) not in {int,float} or not math.isfinite(deadline) or time.monotonic() >= deadline:
            raise DeliveryError('repository_git_timeout')
        status=self.publication_status(owner,publication_ref)
        if status['state']!='confirmed' or status['durability']!='replicated':
            raise DeliveryError('repository_publish_unknown')
        publication=self._publication(owner,publication_ref)
        context,policy=self._context(owner,publication['task_id'],publication['task_revision'],
                                     publication['repo_id'],write=True)
        with closing(self.store._connect()) as connection:
            candidate=self._candidate(connection,owner,publication['candidate_ref'],context)
        if self._validate_candidate(context,candidate)!=publication['validation_digest']:
            raise DeliveryError('repository_validation_changed')
        self._authorize('observe_publication',dict(context,publication_ref=publication_ref,
            candidate_oid=candidate['candidate_oid'],destination_ref=publication['destination_ref']))
        budget=self.git.budget();budget.deadline=min(budget.deadline,deadline)
        with self._locked(publication['repo_id']) as (directory,lockfd):
            mirror=self._mirror(directory,budget,lockfd,policy)
            remote=self.git.ls_remote(self.home,mirror,policy,publication['destination_ref'],budget,lockfd)
            budget.remaining()
        if remote!=candidate['candidate_oid']:
            raise DeliveryError('repository_baseline_drift')
        return dict(repo_id=publication['repo_id'],candidate_ref=publication['candidate_ref'],
            candidate_oid=candidate['candidate_oid'],remote_oid=remote,
            validation_digest=publication['validation_digest'],policy_digest=policy.sha256)


@dataclass(frozen=True, repr=False)
class RepositoryWorkspace:
    """Administrator/Runner binding; never constructed from model arguments."""
    owner_ref: str
    task_ref: str
    revision: int
    repo_ref: str
    remote_identity: str
    workspace_ref: str
    root: Path

    def __post_init__(self):
        for value in (self.owner_ref,self.task_ref,self.repo_ref,self.workspace_ref):
            reference(value)
        if (type(self.revision) is not int or self.revision < 1
                or not isinstance(self.remote_identity,str) or not IDENTITY.fullmatch(self.remote_identity)
                or not isinstance(self.root,Path) or not self.root.is_absolute()
                or '..' in self.root.parts or self.root==Path('/')):
            raise DeliveryError('repository_workspace_binding_invalid')


class RepositoryArchive:
    """Whole local Git/workspace bytes through the store's ONE archive replica.

    No Git transport, project execution, journal, task table or automatic resume.
    The caller must own the bound stopped workspace, not an arbitrary host path.
    """
    MAX_ENTRIES=65536
    MAX_MANIFEST=16*1024**2

    def __init__(self,manager: RepoManager,archive: ObjectArchive,*,
                 workspaces: tuple[RepositoryWorkspace,...],quiescence_gate: Callable[[dict],bool]):
        if (not isinstance(manager,RepoManager) or not isinstance(archive,ObjectArchive)
                or archive.replica is not manager.store.replica
                or archive.local_domain!=manager.store.local_domain
                or archive.replica_domain!=manager.store.replica_domain):
            raise DeliveryError('archive_replica_invalid')
        if (not isinstance(workspaces,tuple) or not workspaces
                or any(not isinstance(item,RepositoryWorkspace) for item in workspaces)
                or len({item.workspace_ref for item in workspaces})!=len(workspaces)
                or not callable(quiescence_gate)):
            raise DeliveryError('repository_workspace_binding_invalid')
        self.manager,self.store,self.archive=manager,manager.store,archive
        self.bindings={item.workspace_ref:item for item in workspaces}
        self.quiescence_gate=quiescence_gate

    def _binding(self,owner,task,revision,repo,workspace):
        binding=self.bindings.get(reference(workspace))
        context,policy=self.manager._context(owner,task,revision,repo,terminal=True)
        if (binding is None or (binding.owner_ref,binding.task_ref,binding.revision,binding.repo_ref,
                binding.remote_identity)!=(owner,task,revision,repo,policy.remote_identity)):
            raise DeliveryError('repository_workspace_scope_invalid')
        return binding,dict(context,workspace_ref=workspace),policy

    def _stopped(self,context):
        try:allowed=self.quiescence_gate(dict(context)) is True
        except Exception:allowed=False
        if not allowed:raise DeliveryError('repository_workspace_not_quiescent')

    def _discard_confirmed_capture(self,result):
        # Plaintext upload staging is temporary. Keep unknown/pending captures
        # byte-for-byte, but remove only our exact confirmed command's staging.
        if result.get('status')!='accepted':return
        backup_ref=result.get('backup_ref')
        if not isinstance(backup_ref,str) or not re.fullmatch(r'RB-[a-f0-9]{64}',backup_ref):
            raise DeliveryError('repository_archive_evidence_invalid')
        path=self.manager.root/'repository-archives'/backup_ref
        if path.is_symlink():raise DeliveryError('repository_archive_source_invalid')
        if path.exists():
            shutil.rmtree(path)
            parent=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
            try:os.fsync(parent)
            finally:os.close(parent)

    @staticmethod
    def _identity(info):
        return (info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns,info.st_mode)

    def _tree(self,root,prefix,budget=None):
        if (root.is_symlink() or root.resolve()!=root or not root.is_dir()
                or root.stat().st_mode & 0o077):
            raise DeliveryError('repository_archive_source_invalid')
        directories={prefix:self._identity(root.stat())};files={};names={prefix.casefold()};total=0
        rootfd=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        pending=['']
        try:
            while pending:
                if budget is not None:budget.remaining()
                relative=pending.pop()
                fd=_open_at(rootfd,relative,directory=True) if relative else os.dup(rootfd)
                try:
                    with os.scandir(fd) as entries:
                        for entry in entries:
                            name=_name(relative+'/'+entry.name if relative else entry.name)
                            if prefix=='workspace' and entry.name=='.git' and relative:
                                raise DeliveryError('repository_archive_git_layout_unsupported')
                            packed=_name(prefix+'/'+name);key=packed.casefold()
                            if key in names:raise DeliveryError('repository_archive_path_collision')
                            names.add(key);info=entry.stat(follow_symlinks=False)
                            if info.st_mode & 0o6000:raise DeliveryError('repository_archive_entry_unsupported')
                            if stat.S_ISDIR(info.st_mode):
                                directories[packed]=self._identity(info);pending.append(name)
                            elif stat.S_ISREG(info.st_mode) and info.st_nlink==1 and info.st_mode & 0o400:
                                files[packed]=(name,self._identity(info));total+=info.st_size
                            else:raise DeliveryError('repository_archive_entry_unsupported')
                            if len(names)>self.MAX_ENTRIES or total>MAX_BYTES:
                                raise DeliveryError('repository_archive_budget_exceeded')
                finally:os.close(fd)
        finally:os.close(rootfd)
        return directories,files

    def _git_state(self,repository,policy,budget,lockfd,*,workspace=None):
        for name in ('commondir','gitdir','worktrees','modules','shallow','config.worktree','lfs',
                     'objects/info/alternates','info/grafts','index.lock','packed-refs.lock'):
            path=repository/name
            if path.exists() or path.is_symlink():raise DeliveryError('repository_archive_git_layout_unsupported')
        if any(repository.glob('objects/pack/*.promisor')):
            raise DeliveryError('repository_archive_git_layout_unsupported')
        run=lambda arguments,allowed=(0,):self.manager._run(repository,arguments,budget,lockfd,allowed=allowed)
        _,keys=run(['config','--local','--no-includes','--name-only','--list'])
        for key in keys.decode('utf-8').lower().splitlines():
            if (key.startswith(('include.','includeif.','filter.')) or key in {'core.worktree','extensions.partialclone','extensions.worktreeconfig'}
                    or key.endswith(('.promisor','.partialclonefilter'))):
                raise DeliveryError('repository_archive_git_layout_unsupported')
        if workspace is not None:
            status,origin=run(['config','--local','--no-includes','--get','remote.origin.url'],(0,1))
            if status or origin.decode('utf-8').strip()!=policy.https_url:
                raise DeliveryError('repository_workspace_identity_mismatch')
        run(['fsck','--full','--strict','--no-reflogs'])
        _,raw=run(['for-each-ref','--format=%(refname) %(objectname)'])
        refs={}
        for line in raw.decode('utf-8').splitlines():
            ref,oid=line.rsplit(' ',1);_name(ref);_oid(oid)
            if not ref.startswith('refs/') or ref.startswith('refs/replace/') or ref in refs:
                raise DeliveryError('repository_archive_refs_invalid')
            refs[ref]=oid
        status,head=run(['rev-parse','--verify','HEAD'],(0,128))
        head_oid=_oid(head.decode('ascii').strip()) if not status else None
        if workspace is not None and head_oid is None:raise DeliveryError('repository_archive_unborn_unsupported')
        status,symbolic=run(['symbolic-ref','-q','HEAD'],(0,1))
        symbolic_ref=symbolic.decode('utf-8').strip() if not status else None
        if symbolic_ref is not None:_name(symbolic_ref)
        _,commits=run(['rev-list','--all',*(['HEAD'] if head_oid else [])])
        blobs={}
        for commit in commits.decode('ascii').splitlines():
            self.manager._inspect(repository,_oid(commit),budget,lockfd,blobs)
        result={'refs':refs,'head_oid':head_oid,'symbolic_head':symbolic_ref}
        if workspace is not None:
            _,dirty=run(['--no-optional-locks','--work-tree='+str(workspace),'status','--porcelain=v1','-z',
                         '--untracked-files=all','--ignored=matching'])
            result['status_sha256']=digest(dirty)
        return result

    def _capture(self,stage,binding,context,policy,directory,lockfd,backup_ref):
        workspace=binding.root
        if not (workspace/'.git').is_dir() or (workspace/'.git').is_symlink():
            raise DeliveryError('repository_archive_git_layout_unsupported')
        mirror=directory/'mirror.git'
        if not mirror.is_dir() or not (directory/'mirror.json').is_file():
            raise DeliveryError('repository_mirror_missing')
        budget=self.manager.git.budget()
        self.manager._mirror(directory,budget,lockfd,policy)
        roots=[(workspace,'workspace'),(directory,'manager/repositories/'+binding.repo_ref)]
        scans=[self._tree(root,prefix,budget) for root,prefix in roots]
        with closing(self.store._connect()) as connection:
            artifacts=[dict(row) for row in connection.execute('SELECT sha256,size FROM repository_artifacts WHERE owner_ref=? AND repo_id=?',
                                                               (binding.owner_ref,binding.repo_ref))]
        # Both roots and out-of-tree bundles share ONE byte budget. Reject
        # known oversize captures before reading a single payload into memory.
        planned=sum(identity[2] for _,entries in scans for _,identity in entries.values())
        unique={}
        for artifact in artifacts:
            sha=_sha(artifact['sha256']);size=artifact['size']
            if type(size) is not int or size<1:raise DeliveryError('repository_artifact_changed')
            if sha in unique:
                if unique[sha]['size']!=size:raise DeliveryError('repository_artifact_changed')
                continue
            unique[sha]=artifact;planned+=size
        if planned>MAX_BYTES:raise DeliveryError('repository_archive_budget_exceeded')
        artifacts=list(unique.values())
        states={'workspace':self._git_state(workspace/'.git',policy,budget,lockfd,workspace=workspace),
                'manager':self._git_state(mirror,policy,budget,lockfd)}
        directories={'manager','manager/repositories','manager/bundles'}
        files=[];total=0
        fd=os.open(stage/'repository-state.tar',os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600);os.close(fd)
        with tarfile.open(stage/'repository-state.tar','w',format=tarfile.USTAR_FORMAT) as packed:
            for _,scan in zip(roots,scans):directories.update(scan[0])
            for name in sorted(directories):
                member=tarfile.TarInfo(name);member.type=tarfile.DIRTYPE;member.mode=0o700;packed.addfile(member)
            for (root,prefix),(_,entries) in zip(roots,scans):
                rootfd=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
                try:
                    for name,(relative,identity) in sorted(entries.items()):
                        budget.remaining()
                        if identity[2]>MAX_BYTES-total:
                            raise DeliveryError('repository_archive_budget_exceeded')
                        if prefix=='workspace' and '.git' in PurePosixPath(relative).parts and not relative.startswith('.git/'):
                            raise DeliveryError('repository_archive_git_layout_unsupported')
                        if prefix=='workspace' and '.git' not in PurePosixPath(relative).parts and PurePosixPath(relative).name=='.gitmodules':
                            raise DeliveryError('repository_recursive_content_unsupported')
                        fd=_open_at(rootfd,relative)
                        with os.fdopen(fd,'rb') as source:
                            if self._identity(os.fstat(source.fileno()))!=identity:
                                raise DeliveryError('repository_archive_source_changed')
                            content=source.read(identity[2]+1)
                            if len(content)!=identity[2] or self._identity(os.fstat(source.fileno()))!=identity:
                                raise DeliveryError('repository_archive_source_changed')
                        if prefix=='workspace' and ('.git' not in PurePosixPath(relative).parts or relative=='.git/info/attributes'):
                            if content.startswith(b'version https://git-lfs.github.com/spec/v1\n'):
                                raise DeliveryError('repository_lfs_unsupported')
                            if (PurePosixPath(relative).name=='.gitattributes' or relative=='.git/info/attributes') and re.search(rb'(?i)filter',content):
                                raise DeliveryError('repository_filters_unsupported')
                        mode=identity[5]&0o700;total+=len(content)
                        if total>MAX_BYTES:raise DeliveryError('repository_archive_budget_exceeded')
                        info={'name':name,'size_bytes':len(content),'sha256':digest(content),'mode':mode}
                        member=tarfile.TarInfo(name);member.mode=mode;member.size=len(content)
                        packed.addfile(member,io.BytesIO(content));files.append(info)
                finally:os.close(rootfd)
            for artifact in artifacts:
                sha=_sha(artifact['sha256'])
                if artifact['size']>MAX_BYTES-total:
                    raise DeliveryError('repository_archive_budget_exceeded')
                content=_read(self.manager._artifact_path(sha),min(self.manager.git.max_bundle,MAX_BYTES-total))
                if digest(content)!=sha or len(content)!=artifact['size']:
                    raise DeliveryError('repository_artifact_changed')
                name='manager/bundles/'+sha[7:]+'.bundle';total+=len(content)
                if total>MAX_BYTES:raise DeliveryError('repository_archive_budget_exceeded')
                member=tarfile.TarInfo(name);member.mode=0o600;member.size=len(content)
                packed.addfile(member,io.BytesIO(content))
                files.append({'name':name,'size_bytes':len(content),'sha256':sha,'mode':0o600})
        if any(self._tree(root,prefix,budget)!=scan for (root,prefix),scan in zip(roots,scans)):
            raise DeliveryError('repository_archive_source_changed')
        after={'workspace':self._git_state(workspace/'.git',policy,budget,lockfd,workspace=workspace),
               'manager':self._git_state(mirror,policy,budget,lockfd)}
        if states!=after:raise DeliveryError('repository_archive_source_changed')
        self._stopped(context)
        document={'version':1,'backup_ref':backup_ref,'context':context,'directories':sorted(directories),
                  'files':sorted(files,key=lambda item:item['name']),'git':states,
                  'pack_sha256':digest(_read(stage/'repository-state.tar',MAX_BYTES))}
        raw=canonical(document).encode()
        if (len(raw)>self.MAX_MANIFEST or len(files)+len(directories)>self.MAX_ENTRIES
                or (stage/'repository-state.tar').stat().st_size+len(raw)>MAX_BYTES):
            raise DeliveryError('repository_archive_budget_exceeded')
        (stage/'repository-state.tar').chmod(0o600)
        _immutable(stage/'repository-state.json',raw)
        with (stage/'repository-state.tar').open('rb') as source:os.fsync(source.fileno())
        descriptor=os.open(stage,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:os.fsync(descriptor)
        finally:os.close(descriptor)

    @_safe_io
    def save(self,owner,key,*,task_ref,expected_revision,repo_ref,workspace_ref):
        if self.store.recovery_mode:raise DeliveryError('recovery_read_only')
        binding,context,policy=self._binding(owner,task_ref,expected_revision,repo_ref,workspace_ref)
        arguments=dict(task_ref=task_ref,expected_revision=expected_revision,repo_ref=repo_ref,workspace_ref=workspace_ref)
        existing=self.manager._existing(owner,key,'repository.archive',arguments)
        if existing:
            self._discard_confirmed_capture(existing)
            return existing
        self.manager._authorize('archive',context);self._stopped(context)
        backup_ref='RB-'+hashlib.sha256(canonical(dict(owner_ref=owner,key=key,**arguments)).encode()).hexdigest()
        captures=self.manager.root/'repository-archives';_private(captures);stage=captures/backup_ref
        try:
            with self.manager._locked(repo_ref) as (directory,lockfd),self.store._writer_guard():
                with closing(self.store._connect()) as connection:
                    if connection.execute("SELECT 1 FROM durability WHERE state='pending' LIMIT 1").fetchone():
                        raise DeliveryError('durability_pending')
                if not stage.exists():
                    pending=Path(tempfile.mkdtemp(prefix='.capture-',dir=captures))
                    try:
                        self._capture(pending,binding,context,policy,directory,lockfd,backup_ref)
                        os.rename(pending,stage)
                        parent=os.open(captures,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
                        try:os.fsync(parent)
                        finally:os.close(parent)
                    finally:
                        if pending.exists():shutil.rmtree(pending)
                raw=_read(stage/'repository-state.json',self.MAX_MANIFEST);document=json.loads(raw)
                if document.get('context')!=context or document.get('backup_ref')!=backup_ref or canonical(document).encode()!=raw:
                    raise DeliveryError('repository_archive_scope_invalid')
                if digest(_read(stage/'repository-state.tar',MAX_BYTES))!=document.get('pack_sha256'):
                    raise DeliveryError('repository_archive_source_changed')
                expected=[{'name':name,'size_bytes':(stage/name).stat().st_size,'sha256':digest(_read(stage/name,MAX_BYTES))}
                          for name in ('repository-state.json','repository-state.tar')]
                scope=dict(owner_ref=owner,task_ref=task_ref,revision=expected_revision,checkpoint_ref=backup_ref)
                self._stopped(context)
                receipt=self.archive.save(stage,scope=scope,expected_files=expected)
                self.manager._authorize('archive',context);self._stopped(context)
                def apply(connection,timestamp):
                    _,current,_=self._binding(owner,task_ref,expected_revision,repo_ref,workspace_ref)
                    if current!=context:raise DeliveryError('repository_archive_scope_invalid')
                    return dict(task_id=task_ref,project_id=context['project_id'],repo_id=repo_ref,workspace_ref=workspace_ref,
                                backup_ref=backup_ref,context_digest=digest(canonical(context).encode()),
                                state_sha256=digest(raw),archive=receipt.document())
                result=self.store._mutation(owner,key,'repository.archive',arguments,apply)
                self._discard_confirmed_capture(result)
                return result
        except ArchiveError:
            raise DeliveryError('repository_archive_unavailable') from None

    def _record(self,owner,key):
        reference(owner);reference(key)
        self.store.verify_event_chain()
        with closing(self.store._connect()) as connection:
            row=connection.execute('SELECT c.*,e.kind,e.body_json FROM commands c JOIN events e USING(event_id) '
                                   'WHERE c.owner_ref=? AND c.idempotency_key=?',(owner,key)).fetchone()
        if row is None or row['kind']!='repository.archive':raise DeliveryError('repository_archive_not_found')
        event=json.loads(row['body_json']);result=json.loads(row['result_json']);arguments=event['arguments']
        if (event['owner_ref']!=owner or event['result']!=result
                or row['argument_digest']!=digest(canonical(dict(kind=event['kind'],arguments=arguments)).encode())):
            raise DeliveryError('repository_archive_evidence_invalid')
        binding,context,policy=self._binding(owner,arguments['task_ref'],arguments['expected_revision'],arguments['repo_ref'],arguments['workspace_ref'])
        if result['context_digest']!=digest(canonical(context).encode()):raise DeliveryError('repository_archive_scope_invalid')
        return result,context,policy,binding

    @_safe_io
    def restore(self,owner,key,*,destination: Path):
        """Offline recovery only, from an authenticated frozen DeliveryStore."""
        if not self.store.recovery_mode:raise DeliveryError('repository_restore_requires_recovery')
        return self._restore_verified_tree(owner,key,destination=destination,action='restore_archive')

    @_safe_io
    def _restore_verified_tree(self,owner,key,*,destination: Path,action: str):
        """Private trusted staging only; callers retain their own admission gate.

        Execution input export uses a private temporary tree, verifies the exact
        same encrypted capture, and releases an independently checked source view.
        It never grants a guest this full Git/config/ignored-file recovery tree.
        """
        destination=Path(destination).absolute()
        if (destination.exists() or destination.is_symlink() or destination.parent.resolve()!=destination.parent
                or not destination.parent.is_dir() or destination.parent.stat().st_mode & 0o077):
            raise DeliveryError('repository_archive_destination_invalid')
        result,context,policy,binding=self._record(owner,key)
        self.manager._authorize(action,context)
        scope=dict(owner_ref=owner,task_ref=context['task_id'],revision=context['task_revision'],checkpoint_ref=result['backup_ref'])
        try:
            with tempfile.TemporaryDirectory(prefix='.repository-recovery-',dir=destination.parent) as folder:
                root=Path(folder);staging=root/'bytes'
                self.archive.restore(staging,scope=scope,receipt=ArchiveReceipt.from_document(result['archive']))
                raw=_read(staging/'repository-state.json',self.MAX_MANIFEST);document=json.loads(raw)
                if (digest(raw)!=result['state_sha256'] or canonical(document).encode()!=raw
                        or set(document)!={'version','backup_ref','context','directories','files','git','pack_sha256'}
                        or type(document['version']) is not int or document['version']!=1
                        or document['backup_ref']!=result['backup_ref'] or document['context']!=context):
                    raise DeliveryError('repository_archive_evidence_invalid')
                if digest(_read(staging/'repository-state.tar',MAX_BYTES))!=document['pack_sha256']:
                    raise DeliveryError('repository_archive_pack_invalid')
                directories=document['directories'];files=document['files']
                if (not isinstance(directories,list) or not isinstance(files,list)
                        or len(directories)+len(files)>self.MAX_ENTRIES):
                    raise DeliveryError('repository_archive_budget_exceeded')
                names=set();entries={}
                def name_valid(name):
                    _name(name)
                    return (name=='workspace' or name.startswith('workspace/')
                        or name in {'manager','manager/repositories','manager/bundles','manager/repositories/'+binding.repo_ref}
                        or name.startswith('manager/repositories/'+binding.repo_ref+'/')
                        or re.fullmatch(r'manager/bundles/[a-f0-9]{64}\.bundle',name) is not None)
                for name in directories:
                    _name(name)
                    if not name_valid(name) or name.casefold() in names:
                        raise DeliveryError('repository_archive_manifest_invalid')
                    names.add(name.casefold());entries[name]=None
                total=0
                for item in files:
                    if (not isinstance(item,dict) or set(item)!={'name','size_bytes','sha256','mode'}
                            or type(item['size_bytes']) is not int or item['size_bytes']<0
                            or type(item['mode']) is not int or item['mode'] not in {0o400,0o500,0o600,0o700}):
                        raise DeliveryError('repository_archive_manifest_invalid')
                    name=_name(item['name']);_sha(item['sha256']);total+=item['size_bytes']
                    if total>MAX_BYTES or name.casefold() in names or not name_valid(name):
                        raise DeliveryError('repository_archive_manifest_invalid')
                    if name.startswith('manager/bundles/') and name.rsplit('/',1)[1][:-7]!=item['sha256'][7:]:
                        raise DeliveryError('repository_archive_manifest_invalid')
                    names.add(name.casefold());entries[name]=item
                directory_set=set(directories)
                for name in entries:
                    if any(str(parent) not in directory_set for parent in list(PurePosixPath(name).parents)[:-1]):
                        raise DeliveryError('repository_archive_manifest_invalid')
                with tarfile.open(staging/'repository-state.tar','r:') as packed:
                    members=packed.getmembers()
                    if len(members)!=len(entries) or len({item.name for item in members})!=len(members):
                        raise DeliveryError('repository_archive_pack_invalid')
                    for member in members:
                        if (member.name not in entries or member.linkname or member.pax_headers
                                or member.uid or member.gid or member.mtime or member.uname or member.gname
                                or member.type not in {tarfile.REGTYPE,tarfile.DIRTYPE}):
                            raise DeliveryError('repository_archive_pack_invalid')
                        expected=entries[member.name]
                        if expected is None:
                            if not member.isdir() or member.mode!=0o700 or member.size!=0:
                                raise DeliveryError('repository_archive_pack_invalid')
                        elif (not member.isfile() or member.mode!=expected['mode'] or member.size!=expected['size_bytes']
                              or digest(packed.extractfile(member).read())!=expected['sha256']):
                            raise DeliveryError('repository_archive_pack_invalid')
                    tree=root/'tree';tree.mkdir(mode=0o700)
                    for name in sorted(directories,key=lambda item:(item.count('/'),item)):(tree/name).mkdir(mode=0o700)
                    for member in members:
                        if entries[member.name] is None:continue
                        fd=os.open(tree/member.name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
                        with os.fdopen(fd,'wb') as output:
                            shutil.copyfileobj(packed.extractfile(member),output);output.flush();os.fsync(output.fileno())
                        (tree/member.name).chmod(entries[member.name]['mode'])
                lockfd=os.open(root/'verification.lock',os.O_RDWR|os.O_CREAT|os.O_EXCL,0o600)
                try:
                    budget=self.manager.git.budget();repository=tree/'manager/repositories'/binding.repo_ref
                    # Verify the original immutable mirror config marker too.
                    if not (repository/'mirror.git').is_dir() or not (repository/'mirror.json').is_file():
                        raise DeliveryError('repository_mirror_missing')
                    self.manager._mirror(repository,budget,lockfd,policy)
                    actual={'workspace':self._git_state(tree/'workspace/.git',policy,budget,lockfd,workspace=tree/'workspace'),
                            'manager':self._git_state(repository/'mirror.git',policy,budget,lockfd)}
                    if actual!=document['git']:raise DeliveryError('repository_archive_git_state_invalid')
                finally:os.close(lockfd)
                self.manager._authorize(action,context)
                for name in sorted(directories,key=lambda item:(-item.count('/'),item)):
                    descriptor=os.open(tree/name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
                    try:os.fsync(descriptor)
                    finally:os.close(descriptor)
                descriptor=os.open(tree,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
                try:os.fsync(descriptor)
                finally:os.close(descriptor)
                os.rename(tree,destination)
                parent=os.open(destination.parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
                try:os.fsync(parent)
                finally:os.close(parent)
            return dict(recovery_read_only=True,backup_ref=result['backup_ref'],repo_id=binding.repo_ref,
                        workspace_ref=binding.workspace_ref,state_sha256=result['state_sha256'],files=len(files))
        except (ArchiveError,tarfile.TarError):
            raise DeliveryError('repository_archive_unavailable') from None
