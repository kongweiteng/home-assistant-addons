"""Explicit file-session/official-CLI candidates, disabled without a trusted gate.

File sessions are a test/CI storage option in the installed official Skill. This
module does not authorize a production storage downgrade. Trusted composition
must qualify the backend, root-owned official binary and registered private
runtime separately. No model argument chooses a path, command or environment.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import threading
from urllib.parse import urlsplit

from .fnos_file_port import TOKEN
from .fnos_oauth_provider import OAuthAccessSession, OAuthBinding, OAuthProviderError, _remaining


SESSION_LIMIT = 64 * 1024
CLI_LIMIT = 256 * 1024 * 1024
_NATIVE_MAGICS = frozenset({b'\x7fELF', b'\xcf\xfa\xed\xfe', b'\xfe\xed\xfa\xcf',
    b'\xce\xfa\xed\xfe', b'\xfe\xed\xfa\xce', b'\xca\xfe\xba\xbe', b'\xbe\xba\xfe\xca',
    b'\xca\xfe\xba\xbf', b'\xbf\xba\xfe\xca'})
_FIELDS = frozenset({'host', 'port', 'username', 'accessToken', 'scheme', 'tls_insecure',
    'uid', 'admin', 'refreshToken', 'accessTokenExpiresAt', 'oauthClientId', 'oauthDeviceId',
    'oauthScope', 'updatedAt'})


def _fail(code):
    raise OAuthProviderError(code) from None


def _canonical_path(value):
    try:
        raw = os.fspath(value)
        if type(raw) is not str or '\x00' in raw or not raw.startswith('/'):
            raise ValueError()
        path = Path(raw)
        if str(path) != raw or '..' in path.parts or path == Path('/'):
            raise ValueError()
        return path
    except Exception:
        _fail('fnos_oauth_runtime_invalid')


def _root_owned(info):
    # A single test seam for unprivileged subprocess fixtures, never a runtime
    # option. Production requires root ownership of every executable ancestor.
    return info.st_uid == 0


def _private(info):
    return (info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o700
            and stat.S_ISDIR(info.st_mode))


def _decode(raw):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError()
            value[key] = item
        return value
    return json.loads(raw.decode('utf-8'), object_pairs_hook=pairs,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))


class FileSessionLoader:
    """Read only the registered private runtime's exact fixed profile file.

    No file is created, imported or repaired, and no platform secure store is
    consulted. The returned typed object deliberately excludes refreshToken.
    Root/profiles inode identities remain pinned while the CLI may atomically
    replace a valid 0600 profile file when renewing its session.
    """
    def __init__(self, binding, runtime_root, *, allow_file_backend=False):
        if type(binding) is not OAuthBinding or type(allow_file_backend) is not bool:
            _fail('fnos_oauth_runtime_invalid')
        self._binding = binding
        self._root = _canonical_path(runtime_root)
        self._enabled = allow_file_backend
        self._pins = {}
        self._pin_lock = threading.Lock()

    @property
    def binding(self):
        return self._binding

    def _pin(self, name, info):
        identity = info.st_dev, info.st_ino
        with self._pin_lock:
            if name in self._pins and self._pins[name] != identity:
                _fail('fnos_oauth_runtime_drift')
            self._pins[name] = identity

    def _audit_tree(self, directory, *, deadline, budget, depth=0):
        """Reject unsafe pre-existing CLI lock/temp entries without guessing names.

        The registered root is dedicated to this runtime. Its bounded existing
        tree contains only private current-UID data and directories. This is not
        an adversarial same-UID sandbox; trusted qualification excludes unrelated
        writers while the official CLI runs.
        """
        if depth > 4:
            _fail('fnos_oauth_runtime_invalid')
        with os.scandir(directory) as entries:
            for entry in entries:
                name = entry.name
                _remaining(deadline)
                budget[0] += 1
                if budget[0] > 256:
                    _fail('fnos_oauth_runtime_invalid')
                info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    if not _private(info):
                        _fail('fnos_oauth_runtime_invalid')
                    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                    try:
                        opened = os.fstat(child)
                        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                            _fail('fnos_oauth_runtime_drift')
                        self._audit_tree(child, deadline=deadline, budget=budget, depth=depth+1)
                    finally:
                        os.close(child)
                elif (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                        or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                        or info.st_size > SESSION_LIMIT):
                    _fail('fnos_oauth_runtime_invalid')

    @contextmanager
    def _directory(self, *, deadline):
        _remaining(deadline)
        if not self._enabled:
            _fail('fnos_oauth_file_backend_disabled')
        root = profiles = None
        prepared = False
        try:
            root = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            for part in self._root.parts[1:]:
                _remaining(deadline)
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
                os.close(root)
                root = child
            info = os.fstat(root)
            if not _private(info):
                _fail('fnos_oauth_runtime_invalid')
            self._pin('root', info)
            self._audit_tree(root, deadline=deadline, budget=[0])
            profiles = os.open('profiles', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
            info = os.fstat(profiles)
            if not _private(info):
                _fail('fnos_oauth_runtime_invalid')
            self._pin('profiles', info)
            _remaining(deadline)
            prepared = True
            yield profiles
        except OAuthProviderError:
            raise
        except Exception:
            if prepared:
                raise
            _fail('fnos_oauth_session_unavailable')
        finally:
            if profiles is not None: os.close(profiles)
            if root is not None: os.close(root)

    def load(self, *, deadline):
        handle = None
        try:
            with self._directory(deadline=deadline) as profiles:
                handle = os.open(self._binding.profile + '.json',
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=profiles)
                before = os.fstat(handle)
                if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
                        or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1
                        or not 1 <= before.st_size <= SESSION_LIMIT):
                    _fail('fnos_oauth_session_invalid')
                raw = bytearray()
                while len(raw) <= SESSION_LIMIT:
                    _remaining(deadline)
                    block = os.read(handle, min(8192, SESSION_LIMIT + 1 - len(raw)))
                    if not block: break
                    raw.extend(block)
                after = os.fstat(handle)
                current = os.stat(self._binding.profile + '.json', dir_fd=profiles, follow_symlinks=False)
                if (len(raw) != before.st_size or len(raw) > SESSION_LIMIT
                        or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                        != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                        or (after.st_dev, after.st_ino) != (current.st_dev, current.st_ino)):
                    _fail('fnos_oauth_session_invalid')
                value = _decode(bytes(raw))
                session = self._validate(value)
                # Validate the current runtime path again, not only the opened
                # old descriptor, before releasing any access token to caller.
                with self._directory(deadline=deadline):
                    pass
                _remaining(deadline)
                return session
        except OAuthProviderError:
            raise
        except BaseException:
            _fail('fnos_oauth_session_invalid')
        finally:
            if handle is not None: os.close(handle)

    def _validate(self, value):
        binding = self._binding
        expected = urlsplit(binding.endpoint)
        try:
            if (type(value) is not dict or set(value) != _FIELDS
                    or any(type(value[field]) is not str for field in _FIELDS - {
                        'port', 'tls_insecure', 'uid', 'admin', 'accessTokenExpiresAt'})
                    or value['host'] != expected.hostname
                    or type(value['port']) is not int or value['port'] != (expected.port or 443)
                    or value['scheme'] != 'https' or value['tls_insecure'] is not False
                    or type(value['uid']) is not int or value['uid'] != binding.uid
                    or type(value['admin']) is not bool
                    or value['oauthClientId'] != binding.client_id
                    or value['oauthDeviceId'] != binding.device_id
                    or not 1 <= len(value['username']) <= 256
                    or any(ord(c) < 32 or ord(c) == 127 for c in value['username'])
                    or not 1 <= len(value['updatedAt']) <= 128
                    or any(ord(c) < 32 or ord(c) == 127 for c in value['updatedAt'])
                    or type(value['accessTokenExpiresAt']) is not int or value['accessTokenExpiresAt'] <= 0
                    or not 16 <= len(value['accessToken']) <= 4096
                    or TOKEN.fullmatch(value['accessToken']) is None
                    or not 16 <= len(value['refreshToken']) <= 8192
                    or TOKEN.fullmatch(value['refreshToken']) is None):
                raise ValueError()
            scopes = tuple(value['oauthScope'].split(' '))
            if len(scopes) != len(binding.scopes) or set(scopes) != set(binding.scopes):
                raise ValueError()
            return OAuthAccessSession(binding.endpoint, binding.uid, binding.client_id,
                binding.device_id, scopes, value['accessToken'], value['accessTokenExpiresAt'])
        except BaseException:
            _fail('fnos_oauth_session_invalid')


class FixedCLIRefreshPort:
    """One root-owned SHA-pinned native CLI, fixed refresh argv and minimal env.

    This adapter is for FnosOAuthProvider's persistent refresh claim, never direct
    callers or model tools. It cannot login, upload, alter profile, replay or
    choose arguments at call time. A failed invocation is unknown; the enclosing
    provider must permanently stop retries. Root/session filesystems and local
    subprocess creation must remain responsive; FileReplica bounds caller wait.
    """
    def __init__(self, session_loader, executable, executable_sha256, *, allow_file_backend=False):
        if (type(session_loader) is not FileSessionLoader or type(allow_file_backend) is not bool
                or type(executable_sha256) is not str
                or re.fullmatch(r'[0-9a-f]{64}', executable_sha256) is None):
            _fail('fnos_oauth_runtime_invalid')
        self._loader = session_loader
        self._executable = _canonical_path(executable)
        self._sha = executable_sha256
        self._enabled = allow_file_backend
        self._identity = None
        self._lock = threading.Lock()
        self._spent = False

    def _check_cli(self, *, deadline):
        directory = handle = None
        try:
            _remaining(deadline)
            directory = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            for part in self._executable.parent.parts[1:]:
                info = os.fstat(directory)
                if not _root_owned(info) or stat.S_IMODE(info.st_mode) & 0o022:
                    _fail('fnos_oauth_cli_pin_invalid')
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                os.close(directory)
                directory = child
                _remaining(deadline)
            info = os.fstat(directory)
            if not _root_owned(info) or stat.S_IMODE(info.st_mode) & 0o022:
                _fail('fnos_oauth_cli_pin_invalid')
            handle = os.open(self._executable.name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            before = os.fstat(handle)
            identity = before.st_dev, before.st_ino
            if (not stat.S_ISREG(before.st_mode) or not _root_owned(before)
                    or stat.S_IMODE(before.st_mode) & 0o7022 or not before.st_mode & 0o111
                    or before.st_nlink != 1 or not 1 <= before.st_size <= CLI_LIMIT
                    or self._identity is not None and identity != self._identity):
                _fail('fnos_oauth_cli_pin_invalid')
            sha, size, magic = hashlib.sha256(), 0, None
            while size <= CLI_LIMIT:
                _remaining(deadline)
                block = os.read(handle, 1024*1024)
                if not block: break
                if magic is None: magic = block[:4]
                sha.update(block)
                size += len(block)
            after = os.fstat(handle)
            if (magic not in _NATIVE_MAGICS or sha.hexdigest() != self._sha or size != before.st_size
                    or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                    != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
                _fail('fnos_oauth_cli_pin_invalid')
            self._identity = identity
            _remaining(deadline)
        except OAuthProviderError:
            raise
        except BaseException:
            _fail('fnos_oauth_cli_pin_invalid')
        finally:
            if handle is not None: os.close(handle)
            if directory is not None: os.close(directory)

    @staticmethod
    def _abort(process):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            try: process.kill()
            except OSError: pass
        # Cleanup must not consume a fresh timeout after the caller deadline.
        # DEVNULL avoids pipe-draining waits; daemon reaper cannot block return.
        def reap():
            try: process.wait()
            except BaseException: pass
        threading.Thread(target=reap, daemon=True).start()

    def validate_runtime(self, *, deadline):
        """Read-only startup check; never invokes CLI or refreshes a session."""
        _remaining(deadline)
        if not self._enabled or not self._loader._enabled:
            _fail('fnos_oauth_file_backend_disabled')
        if not self._lock.acquire(blocking=False):
            _fail('fnos_oauth_busy')
        try:
            if self._spent:
                _fail('fnos_oauth_refresh_unknown')
            self._loader.load(deadline=deadline)
            self._check_cli(deadline=deadline)
            self._loader.load(deadline=deadline)
        finally:
            self._lock.release()

    def refresh_once(self, *, deadline):
        _remaining(deadline)
        if not self._enabled or not self._loader._enabled:
            _fail('fnos_oauth_file_backend_disabled')
        if not self._lock.acquire(blocking=False):
            _fail('fnos_oauth_busy')
        process = None
        claimed = False
        try:
            if self._spent:
                _fail('fnos_oauth_refresh_unknown')
            self._loader.load(deadline=deadline)
            self._check_cli(deadline=deadline)
            self._loader.load(deadline=deadline)
            environment = {'PATH':'/usr/bin:/bin', 'LANG':'C.UTF-8', 'LC_ALL':'C.UTF-8',
                'TZ':'Asia/Shanghai', 'TRIM_CLI_SESSION_STORAGE':'file',
                'TRIM_CLI_CONFIG_DIR':str(self._loader._root)}
            _remaining(deadline)
            claimed = True
            self._spent = True
            process = subprocess.Popen([str(self._executable), '--profile',
                self._loader.binding.profile, 'login', '--refresh'],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env=environment, cwd=str(self._loader._root), close_fds=True,
                start_new_session=True)
            remaining = _remaining(deadline)
            status = process.wait(timeout=remaining)
            _remaining(deadline)
            if status != 0:
                _fail('fnos_oauth_refresh_unknown')
            self._check_cli(deadline=deadline)
            self._loader.load(deadline=deadline)
            _remaining(deadline)
            # Official CLI refreshes may be needed again in the future; only a
            # fully observed successful invocation releases this local fuse.
            self._spent = False
        except BaseException:
            if process is not None:
                self._abort(process)
            if claimed:
                _fail('fnos_oauth_refresh_unknown')
            raise
        finally:
            self._lock.release()
