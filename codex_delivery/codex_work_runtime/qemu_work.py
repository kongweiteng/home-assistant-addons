"""HA-native TCG backend; no Docker API, host mount or control-plane credentials.

The administrator pins one complete runtime manifest (including static native
launcher and guest kernel/initramfs). The native launcher performs userns/chroot,
its explicitly pinned filesystem profile, and seccomp before exec. A guest is never an authorization authority:
only a stopped VM's bounded, rehashed archive becomes a local checkpoint.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import datetime as dt
import hashlib
import io
import json
import math
import os
from pathlib import Path
import platform
import resource
import secrets
import selectors
import shutil
import signal
import stat
import struct
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Mapping

from .work_execution import (MIB, WorkLimits, WorkRequest, WorkRuntime, _checkpoint_tree,
    _fail, _name, _private_dir, _sync_dir, _write_new, extract_checkpoint)


MAGIC = b'CWQ1\n'
HEADER_LIMIT = 16384
DISK_OVERHEAD = 32 * MIB
ASSET_LIMIT = 1024 * MIB


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
        allow_nan=False).encode()


def _digest(path: Path, *, maximum: int) -> str:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > maximum:
        raise _fail('work_qemu_asset_invalid')
    digest = hashlib.sha256()
    total = 0
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as source:
        start = os.fstat(source.fileno())
        if (start.st_dev, start.st_ino) != (info.st_dev, info.st_ino):
            raise _fail('work_qemu_asset_changed')
        for block in iter(lambda: source.read(65536), b''):
            total += len(block)
            if total > maximum:
                raise _fail('work_qemu_asset_invalid')
            digest.update(block)
        end = os.fstat(source.fileno())
    if (start.st_size, start.st_mtime_ns, start.st_ctime_ns) != (end.st_size, end.st_mtime_ns, end.st_ctime_ns):
        raise _fail('work_qemu_asset_changed')
    return 'sha256:' + digest.hexdigest()


@dataclass(frozen=True)
class QemuHostPolicy:
    """Trusted service configuration. Model arguments cannot change this policy."""
    uid: int = 65534
    max_workspace_bytes: int = 256 * MIB
    overhead_memory_bytes: int = 512 * MIB
    max_host_memory_bytes: int = 4608 * MIB
    max_host_cpu_millis: int = 2000
    max_host_pids: int = 256
    max_stderr_bytes: int = 65536
    cleanup_seconds: float = 3.0
    resource_profile: str = 'cgroup-v1'
    native_address_overhead_bytes: int = 1024 * MIB
    native_max_address_bytes: int = 3072 * MIB
    native_max_threads: int = 32
    native_cpu_count: int = 2

    def validate(self) -> None:
        bounds = {'uid': (1, 2**31-1), 'max_workspace_bytes': (MIB, 1024*MIB),
            'overhead_memory_bytes': (128*MIB, 1024*MIB),
            'max_host_memory_bytes': (256*MIB, 8192*MIB), 'max_host_cpu_millis': (1000, 2000),
            'max_host_pids': (16, 256), 'max_stderr_bytes': (1024, 65536),
            'native_address_overhead_bytes': (128*MIB, 1024*MIB),
            'native_max_address_bytes': (256*MIB, 4096*MIB),
            'native_max_threads': (4, 256), 'native_cpu_count': (1, 2)}
        if any(type(getattr(self, name)) is not int or not low <= getattr(self, name) <= high
                for name, (low, high) in bounds.items()):
            raise _fail('work_qemu_policy_invalid')
        if type(self.cleanup_seconds) not in {int, float} or not 0.1 <= self.cleanup_seconds <= 5:
            raise _fail('work_qemu_policy_invalid')
        if (not isinstance(self.resource_profile, str) or self.resource_profile not in {'cgroup-v1', 'native-resource-v1'}
                or self.native_address_overhead_bytes >= self.native_max_address_bytes):
            raise _fail('work_qemu_policy_invalid')


@dataclass(frozen=True)
class _Assets:
    root: Path
    digest: str
    manifest: dict[str, Any]

    @property
    def launcher(self) -> Path:
        return self.root / 'bin/isolate'


def _assets(root: Path, expected: str, *, expected_kind: str = 'work-qemu-v1') -> _Assets:
    if expected_kind not in {'work-qemu-v1', 'browser-qemu-v1', 'preview-qemu-v1'}:
        raise _fail('work_qemu_configuration_invalid')
    root = root.absolute()
    if root.is_symlink() or root.resolve() != root or not root.is_dir():
        raise _fail('work_qemu_asset_path_invalid')
    if root.stat().st_uid != 0 or root.stat().st_mode & 0o022:
        raise _fail('work_qemu_asset_permissions_invalid')
    try:
        manifest_path = root / 'manifest.json'
        if manifest_path.is_symlink() or manifest_path.lstat().st_size > 2*MIB:
            raise OSError('manifest')
        raw = manifest_path.read_bytes()
    except OSError as error:
        raise _fail('work_qemu_asset_invalid') from error
    if len(raw) > 2*MIB or 'sha256:' + hashlib.sha256(raw).hexdigest() != expected:
        raise _fail('work_qemu_asset_digest_mismatch')
    try:
        manifest = json.loads(raw)
        required = {'version', 'kind', 'guest_protocol', 'isolation_profile', 'host_arch', 'loader', 'qemu',
            'mke2fs', 'guest_kernel', 'guest_initramfs', 'files'}
        if expected_kind in {'browser-qemu-v1', 'preview-qemu-v1'} or 'resource_profile' in manifest:
            required.add('resource_profile')
        if (set(manifest) != required or type(manifest['version']) is not int or manifest['version'] != 1
                or manifest['kind'] != expected_kind or type(manifest['guest_protocol']) is not int
                or manifest['guest_protocol'] != {'work-qemu-v1':1, 'browser-qemu-v1':2, 'preview-qemu-v1':4}[expected_kind]
                or manifest['isolation_profile'] not in {'landlock-v1', 'readonly-jail-v1'}
                or manifest['host_arch'] != platform.machine()):
            raise ValueError('manifest')
        if manifest.get('resource_profile', 'cgroup-v1') not in {'cgroup-v1', 'native-resource-v1'}:
            raise ValueError('resource_profile')
        files = manifest['files']
        if not isinstance(files, list) or not 1 <= len(files) <= 4096:
            raise ValueError('files')
        names, total = set(), 0
        for item in files:
            if not isinstance(item, dict) or set(item) != {'name', 'size_bytes', 'sha256'}:
                raise ValueError('file')
            name = _name(item['name'])
            if name in names or name == 'manifest.json' or type(item['size_bytes']) is not int or item['size_bytes'] < 0:
                raise ValueError('file')
            names.add(name); total += item['size_bytes']
            path = root / name
            info = path.lstat()
            if (total > ASSET_LIMIT or info.st_uid != 0 or info.st_mode & 0o6022
                    or info.st_size != item['size_bytes'] or _digest(path, maximum=ASSET_LIMIT) != item['sha256']):
                raise ValueError('integrity')
        for key in ('loader', 'qemu', 'mke2fs', 'guest_kernel', 'guest_initramfs'):
            if manifest[key] not in names:
                raise ValueError('executable')
        if 'bin/isolate' not in names:
            raise ValueError('launcher')
        actual = set()
        for path in root.rglob('*'):
            info = path.lstat()
            if stat.S_ISDIR(info.st_mode):
                if path.is_symlink() or info.st_uid != 0 or info.st_mode & 0o022:
                    raise ValueError('directory')
            elif stat.S_ISREG(info.st_mode):
                actual.add(path.relative_to(root).as_posix())
            else:
                raise ValueError('type')
        if actual != names | {'manifest.json'}:
            raise ValueError('unregistered')
    except (ValueError, TypeError, KeyError, OSError) as error:
        raise _fail('work_qemu_asset_invalid') from error
    return _Assets(root, expected, manifest)


def _host_boundary(policy: QemuHostPolicy) -> dict[str, Any]:
    try:
        if sys.platform != 'linux' or os.getuid() != policy.uid or not hasattr(os, 'pidfd_open') or not hasattr(signal, 'pidfd_send_signal'):
            raise ValueError('platform')
        status = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)
        if int(status['CapEff'].strip(), 16) or int(status['CapPrm'].strip(), 16) or status['NoNewPrivs'].strip() != '1':
            raise ValueError('identity')
        apparmor = Path('/proc/self/attr/current').read_text().strip()
        if apparmor != 'docker-default (enforce)':
            raise ValueError('apparmor')
        if Path('/run/docker.sock').exists() or Path('/var/run/docker.sock').exists():
            raise ValueError('socket')
        if policy.resource_profile == 'native-resource-v1':
            address = resource.getrlimit(resource.RLIMIT_AS)
            threads = resource.getrlimit(resource.RLIMIT_NPROC)
            cpus = os.sched_getaffinity(0)
            if (not 128*MIB <= address[0] == address[1] <= policy.native_max_address_bytes
                    or threads != (policy.native_max_threads, policy.native_max_threads)
                    or not 1 <= len(cpus) <= policy.native_cpu_count):
                raise ValueError('native_resource')
            return {'resource_profile': 'native-resource-v1', 'address_bytes': address[1],
                'threads': threads[1], 'cpu_count': len(cpus), 'apparmor': apparmor, 'uid': os.getuid()}
        memory = int(Path('/sys/fs/cgroup/memory.max').read_text())
        pids = int(Path('/sys/fs/cgroup/pids.max').read_text())
        quota, period = map(int, Path('/sys/fs/cgroup/cpu.max').read_text().split())
        cpu = quota * 1000 / period
        if (not 1 <= memory <= policy.max_host_memory_bytes or not 1 <= pids <= policy.max_host_pids
                or not 1 <= cpu <= policy.max_host_cpu_millis):
            raise ValueError('cgroup')
        return {'memory_bytes': memory, 'cpu_millis': cpu, 'pids': pids,
            'apparmor': apparmor, 'uid': os.getuid()}
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise _fail('work_qemu_host_not_qualified') from error


def _copy_file(source: Path, destination: Path, expected: str, *, mode: int = 0o600) -> None:
    _private_dir(destination.parent)
    digest = hashlib.sha256()
    total = 0
    before = source.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise _fail('work_qemu_copy_invalid')
    with os.fdopen(os.open(source, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as original:
        observed = os.fstat(original.fileno())
        if (observed.st_dev, observed.st_ino) != (before.st_dev, before.st_ino):
            raise _fail('work_qemu_copy_changed')
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
        with os.fdopen(fd, 'wb') as target:
            for block in iter(lambda: original.read(65536), b''):
                total += len(block)
                if total > before.st_size:
                    raise _fail('work_qemu_copy_changed')
                digest.update(block); target.write(block)
            target.flush(); os.fsync(target.fileno())
        after = os.fstat(original.fileno())
    if ('sha256:' + digest.hexdigest() != expected or
            (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
        raise _fail('work_qemu_copy_changed')
    destination.chmod(mode)


def _copy_checkpoint(source: Path, target: Path, limits: WorkLimits, *, input_mode: bool = False) -> None:
    tree = _checkpoint_tree(source, limits)
    _private_dir(target)
    for directory in tree['directories']:
        _private_dir(target / directory)
    for item in tree['files']:
        _copy_file(source / item['name'], target / item['name'], item['sha256'], mode=0o444 if input_mode else 0o600)
    if input_mode:
        for directory in sorted(target.rglob('*'), reverse=True):
            if directory.is_dir(): directory.chmod(0o555)
        target.chmod(0o555)


def _archive_limit(limits: WorkLimits) -> int:
    return limits.workspace_bytes + (limits.max_files*4+16)*2048 + 32768


def read_qemu_export(path: Path, destination: Path, limits: WorkLimits, nonce: str) -> tuple[int, list[dict[str, Any]]]:
    """Only called after the VM exited; no guest-controlled path or tar metadata."""
    maximum = _archive_limit(limits)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not len(MAGIC)+4 < info.st_size <= maximum+HEADER_LIMIT+9:
        raise _fail('work_qemu_export_invalid')
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as source:
        if source.read(len(MAGIC)) != MAGIC:
            raise _fail('work_qemu_export_protocol_invalid')
        size_raw = source.read(4)
        if len(size_raw) != 4:
            raise _fail('work_qemu_export_protocol_invalid')
        header_size = struct.unpack('!I', size_raw)[0]
        if not 1 <= header_size <= HEADER_LIMIT:
            raise _fail('work_qemu_export_header_limit')
        try:
            header = json.loads(source.read(header_size))
            if (not isinstance(header, dict) or set(header) != {'version', 'nonce', 'exit_code', 'archive_bytes', 'archive_sha256'}
                    or type(header['version']) is not int or header['version'] != 1 or header['nonce'] != nonce
                    or type(header['exit_code']) is not int or not -255 <= header['exit_code'] <= 255
                    or type(header['archive_bytes']) is not int or not 1 <= header['archive_bytes'] <= maximum
                    or info.st_size != len(MAGIC)+4+header_size+header['archive_bytes']):
                raise ValueError('header')
        except (ValueError, TypeError, KeyError) as error:
            raise _fail('work_qemu_export_protocol_invalid') from error
        archive_start = source.tell(); digest = hashlib.sha256()
        remaining = header['archive_bytes']
        while remaining:
            block = source.read(min(65536, remaining))
            if not block: raise _fail('work_qemu_export_truncated')
            digest.update(block); remaining -= len(block)
        if 'sha256:' + digest.hexdigest() != header['archive_sha256']:
            raise _fail('work_qemu_export_digest_mismatch')
        source.seek(archive_start)
        files = extract_checkpoint(source, destination, limits, require_manifest=True)
    _sync_dir(destination)
    return header['exit_code'], files


class QemuWorkBackend:
    """Single-concurrency, credential-free VM subprocess under fixed host policy."""
    def __init__(self, runtimes: Mapping[str, WorkRuntime], assets: Mapping[str, str | Path],
            state_root: str | Path, *, policy: QemuHostPolicy = QemuHostPolicy()) -> None:
        policy.validate(); self.policy = policy
        self.runtimes = dict(runtimes)
        if not self.runtimes or set(self.runtimes) != set(assets):
            raise _fail('work_qemu_configuration_invalid')
        self.assets: dict[str, _Assets] = {}
        for key, runtime in self.runtimes.items():
            runtime.validate()
            if (key != runtime.runtime_ref or runtime.uid != 65532 or runtime.gid != 65532
                    or runtime.kind != 'work-qemu-v1'):
                raise _fail('work_qemu_configuration_invalid')
            self.assets[key] = _assets(Path(assets[key]), runtime.image)
            if self.assets[key].manifest.get('resource_profile', 'cgroup-v1') != policy.resource_profile:
                raise _fail('work_qemu_resource_profile_mismatch')
        self.root = Path(state_root).absolute()
        _private_dir(self.root)
        if self.root.resolve() != self.root:
            raise _fail('work_qemu_state_path_invalid')
        self.reserve_bytes = 2*policy.max_workspace_bytes + max(
            sum(item['size_bytes'] for item in asset.manifest['files']) for asset in self.assets.values()) + 128*MIB
        self._lock = threading.Lock()
        self._qualified: set[str] = set()

    def validate_request(self, request: WorkRequest) -> None:
        runtime = self.runtimes.get(request.runtime_ref)
        if runtime is None or request.profile not in runtime.profiles:
            raise _fail('work_runtime_not_registered')
        if request.limits.workspace_bytes > self.policy.max_workspace_bytes:
            raise _fail('work_qemu_workspace_budget_invalid')
        boundary = _host_boundary(self.policy)
        if self.policy.resource_profile == 'native-resource-v1':
            self._native_address(request.limits.memory_bytes, boundary)
            if request.limits.cpu_millis != self.policy.native_cpu_count*1000:
                raise _fail('work_qemu_host_budget_mismatch')
        elif (boundary['memory_bytes'] > request.limits.memory_bytes + self.policy.overhead_memory_bytes
                or boundary['cpu_millis'] > request.limits.cpu_millis or boundary['pids'] > request.limits.pids):
            raise _fail('work_qemu_host_budget_mismatch')
        _assets(self.assets[request.runtime_ref].root, runtime.image)

    def _qualify(self, asset: _Assets) -> None:
        if asset.manifest.get('resource_profile', 'cgroup-v1') != self.policy.resource_profile:
            raise _fail('work_qemu_resource_profile_mismatch')
        if asset.digest in self._qualified:
            return
        root = self.root / ('qualification-' + secrets.token_hex(12))
        _private_dir(root); _private_dir(root / 'runtime'); _private_dir(root / 'tmp')
        for name in ('runtime/readonly.txt', 'input.img', 'output.img', 'export.bin'):
            _write_new(root / name, b'qualification-fixture\n')
        self._seal_jail(root)
        try:
            command = [str(asset.launcher), str(root), '--self-test', str(os.getpid())]
            if self.policy.resource_profile == 'native-resource-v1':
                command.extend([str(256*MIB), str(self.policy.native_max_threads), str(self.policy.native_cpu_count)])
            result = subprocess.run(command,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env={'LANG': 'C'}, timeout=5, close_fds=True)
            expected = {'version': 1, 'chroot': True,
                'landlock': asset.manifest['isolation_profile'] == 'landlock-v1',
                'isolation_profile': asset.manifest['isolation_profile'], 'seccomp': True, 'caps': 0,
                'network_denied': True, 'parent_signal_denied': True, 'outside_stat_denied': True, 'inputs_readonly': True}
            valid = False
            if not result.returncode and len(result.stdout) <= 8192:
                report = json.loads(result.stdout)
                if self.policy.resource_profile == 'native-resource-v1':
                    valid = (isinstance(report, dict) and set(report) == {'resource_profile', 'expected_parent_bound',
                        'address_bytes', 'thread_limit', 'created_threads', 'logical_cpus', 'qualified'}
                        and report['resource_profile'] == 'native-resource-v1' and report['expected_parent_bound'] is True
                        and report['qualified'] is True and type(report['address_bytes']) is int and report['address_bytes'] == 256*MIB
                        and type(report['thread_limit']) is int and report['thread_limit'] == self.policy.native_max_threads
                        and type(report['created_threads']) is int and 1 <= report['created_threads'] < self.policy.native_max_threads
                        and type(report['logical_cpus']) is int and 1 <= report['logical_cpus'] <= self.policy.native_cpu_count)
                else:
                    valid = report == expected
            if not valid:
                raise _fail('work_qemu_launcher_not_qualified')
            self._qualified.add(asset.digest)
        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
            raise _fail('work_qemu_launcher_not_qualified') from error
        finally:
            self._remove_run(root)

    @staticmethod
    def _seal_jail(jail: Path) -> None:
        (jail / 'input.img').chmod(0o444)
        for path in jail.rglob('*'):
            info = path.lstat()
            if path.is_symlink():
                raise _fail('work_qemu_jail_invalid')
            if stat.S_ISDIR(info.st_mode):
                path.chmod(0o555)
            elif stat.S_ISREG(info.st_mode) and path.is_relative_to(jail / 'runtime'):
                path.chmod(0o555 if info.st_mode & 0o111 else 0o444)
        jail.chmod(0o555)

    @staticmethod
    def _remove_run(run: Path) -> None:
        try:
            for current, directories, _ in os.walk(run, followlinks=False):
                path = Path(current)
                if path.is_symlink():
                    raise OSError('cleanup_link')
                path.chmod(0o700)
                for directory in directories:
                    if (path / directory).is_symlink():
                        raise OSError('cleanup_link')
            shutil.rmtree(run)
        except OSError as error:
            raise _fail('work_qemu_cleanup_uncertain', uncertain=True) from error

    @staticmethod
    def _tool(asset: _Assets, key: str) -> list[str]:
        return [str(asset.root / asset.manifest['loader']), '--inhibit-cache', '--library-path',
            str(asset.root / 'lib'), str(asset.root / asset.manifest[key])]

    def _disk(self, asset: _Assets, path: Path, source: Path, size: int) -> None:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as disk:
            disk.truncate(size); disk.flush(); os.fsync(disk.fileno())
        try:
            result = subprocess.run([*self._tool(asset, 'mke2fs'), '-q', '-t', 'ext4', '-F', '-m', '0',
                '-E', 'root_owner=0:0,lazy_itable_init=0,lazy_journal_init=0', '-d', str(source), str(path)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LANG': 'C',
                    'MKE2FS_CONFIG': str(asset.root / 'etc/mke2fs.conf')}, timeout=20, close_fds=True)
            if result.returncode:
                raise _fail('work_qemu_disk_build_failed')
        except (OSError, subprocess.TimeoutExpired) as error:
            raise _fail('work_qemu_disk_build_failed') from error

    def _native_address(self, guest_bytes: int, boundary: Mapping[str, Any] | None = None) -> int:
        if type(guest_bytes) is not int or guest_bytes < 128*MIB:
            raise _fail('work_qemu_native_address_budget_invalid')
        address = guest_bytes + self.policy.native_address_overhead_bytes
        inherited = resource.getrlimit(resource.RLIMIT_AS)[1] if boundary is None else boundary['address_bytes']
        if address > self.policy.native_max_address_bytes or not 1 <= address <= inherited:
            raise _fail('work_qemu_native_address_budget_invalid')
        return address

    def _launcher_command(self, asset: _Assets, jail: Path, file_limit: int, cpu_seconds: int, guest_bytes: int) -> list[str]:
        command = [str(asset.launcher), str(jail)]
        if self.policy.resource_profile == 'native-resource-v1':
            return [*command, '--exec-resource', str(file_limit), str(cpu_seconds),
                str(self._native_address(guest_bytes)), str(self.policy.native_max_threads),
                str(self.policy.native_cpu_count), str(os.getpid())]
        return [*command, '--exec', str(file_limit), str(cpu_seconds)]

    def _command(self, asset: _Assets, jail: Path, request: WorkRequest, file_limit: int) -> list[str]:
        cpu_seconds = math.ceil(request.limits.timeout_seconds*request.limits.cpu_millis/1000)+2
        accel = 'tcg,tb-size=64' if self.policy.resource_profile == 'native-resource-v1' else 'tcg'
        return [*self._launcher_command(asset, jail, file_limit, cpu_seconds, request.limits.memory_bytes),
            '/runtime/' + asset.manifest['loader'], '--inhibit-cache', '--library-path', '/runtime/lib',
            '/runtime/' + asset.manifest['qemu'], '-L', '/runtime/share/qemu', '-accel', accel,
            '-machine', 'pc', '-smp', str(math.ceil(request.limits.cpu_millis/1000)),
            '-m', str(request.limits.memory_bytes//MIB), '-nodefaults', '-no-user-config',
            '-display', 'none', '-serial', 'none', '-monitor', 'none', '-no-reboot', '-nic', 'none',
            '-kernel', '/runtime/' + asset.manifest['guest_kernel'],
            '-initrd', '/runtime/' + asset.manifest['guest_initramfs'],
            '-append', 'console=ttyS0 rdinit=/init panic=-1 quiet',
            '-drive', 'file=/input.img,format=raw,if=virtio,readonly=on',
            '-drive', 'file=/output.img,format=raw,if=virtio,cache=writeback',
            '-device', 'virtio-serial-pci', '-chardev', 'stdio,id=work,signal=off',
            '-device', 'virtserialport,chardev=work,name=codex-work']

    def _stop(self, process: subprocess.Popen[bytes], pidfd: int) -> None:
        if process.poll() is not None:
            return
        try:
            signal.pidfd_send_signal(pidfd, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError as error:
            raise _fail('work_qemu_cleanup_uncertain', uncertain=True) from error
        try:
            process.wait(timeout=self.policy.cleanup_seconds)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            signal.pidfd_send_signal(pidfd, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError as error:
            raise _fail('work_qemu_cleanup_uncertain', uncertain=True) from error
        try:
            process.wait(timeout=self.policy.cleanup_seconds)
        except subprocess.TimeoutExpired as error:
            raise _fail('work_qemu_cleanup_uncertain', uncertain=True) from error

    def run(self, request: WorkRequest, *, claim_ref: str, program: Path, inputs: Path,
            restore: Path, destination: Path, created: Callable[[str], None],
            cancelled: Callable[[], bool], now: Callable[[], dt.datetime]) -> dict[str, Any]:
        self.validate_request(request)
        if not self._lock.acquire(blocking=False):
            raise _fail('work_runner_busy')
        run: Path | None = None; process = None; pidfd = None; stopped = True
        try:
            asset = self.assets[request.runtime_ref]; runtime = self.runtimes[request.runtime_ref]
            self._qualify(asset)
            if cancelled() or request.expires_at <= now():
                return {'exit_code': None, 'stop_reason': 'cancelled' if cancelled() else 'lease_expired',
                    'files': [], 'checkpoint_written': False}
            ref = 'QVM-' + hashlib.sha256((claim_ref + secrets.token_hex(16)).encode()).hexdigest()[:40]
            run = self.root / ref; _private_dir(run)
            jail = run / 'jail'; _private_dir(jail); _private_dir(jail / 'tmp')
            _private_dir(jail / 'runtime')
            for item in asset.manifest['files']:
                source = asset.root / item['name']
                _copy_file(source, jail / 'runtime' / item['name'], item['sha256'],
                    mode=0o555 if source.stat().st_mode & 0o111 else 0o444)
            _copy_file(asset.root / 'manifest.json', jail / 'runtime/manifest.json', asset.digest, mode=0o444)
            staged_inputs = run / 'staged-inputs'; _private_dir(staged_inputs)
            _copy_checkpoint(inputs, staged_inputs / 'files', request.limits, input_mode=True)
            _copy_file(program, staged_inputs / 'program', _digest(program, maximum=512*1024), mode=0o444)
            nonce = secrets.token_hex(16)
            _write_new(staged_inputs / 'request.json', _canonical({'version': 1, 'nonce': nonce,
                'limits': asdict(request.limits), 'interpreter': runtime.interpreter}))
            (staged_inputs / 'request.json').chmod(0o444)
            staged_inputs.chmod(0o555)
            staged_restore = run / 'staged-restore'
            _copy_checkpoint(restore, staged_restore, request.limits)
            size = request.limits.workspace_bytes + DISK_OVERHEAD
            self._disk(asset, jail / 'input.img', staged_inputs, size+MIB)
            self._disk(asset, jail / 'output.img', staged_restore, size)
            _write_new(jail / 'export.bin', b'')
            self._seal_jail(jail)
            if os.statvfs(jail / 'runtime').f_flag & os.ST_NOEXEC:
                raise _fail('work_qemu_runtime_noexec')
            if cancelled() or request.expires_at <= now():
                return {'exit_code': None, 'stop_reason': 'cancelled' if cancelled() else 'lease_expired',
                    'files': [], 'checkpoint_written': False}
            started = time.monotonic()
            export_fd = os.open(jail / 'export.bin', os.O_WRONLY | os.O_NOFOLLOW)
            try:
                process = subprocess.Popen(self._command(asset, jail, request, size+MIB),
                    # Keep the empty input channel open: an immediate EOF would
                    # disconnect QEMU's virtio stdio backend. No data is sent.
                    stdin=subprocess.PIPE, stdout=export_fd, stderr=subprocess.PIPE,
                    env={'LANG': 'C'}, close_fds=True, start_new_session=True)
            finally:
                os.close(export_fd)
            stopped = False
            try:
                pidfd = os.pidfd_open(process.pid)
            except OSError as error:
                if process.poll() is None:
                    try: process.kill()
                    except ProcessLookupError: pass
                process.wait(timeout=self.policy.cleanup_seconds); stopped = True
                raise _fail('work_qemu_pidfd_unavailable') from error
            created(ref)
            reason, stderr_size = 'finished', 0
            assert process.stderr is not None
            os.set_blocking(process.stderr.fileno(), False)
            with selectors.DefaultSelector() as selector:
                selector.register(process.stderr, selectors.EVENT_READ)
                while process.poll() is None:
                    for key, _ in selector.select(timeout=.05):
                        data = os.read(key.fd, 4096)
                        if not data:
                            selector.unregister(key.fd)
                        stderr_size += len(data)
                    if cancelled(): reason = 'cancelled'
                    elif request.expires_at <= now(): reason = 'lease_expired'
                    elif time.monotonic()-started >= request.limits.timeout_seconds: reason = 'timeout'
                    elif stderr_size > self.policy.max_stderr_bytes: reason = 'output_limit'
                    if reason != 'finished':
                        self._stop(process, pidfd); break
            process.wait(timeout=self.policy.cleanup_seconds); stopped = True
            while True:
                try: data = os.read(process.stderr.fileno(), 4096)
                except BlockingIOError: break
                if not data: break
                stderr_size += len(data)
            if stderr_size > self.policy.max_stderr_bytes:
                reason = 'output_limit'
            if reason != 'finished':
                return {'exit_code': None, 'stop_reason': reason, 'files': [], 'checkpoint_written': False}
            if process.returncode != 0:
                raise _fail('work_qemu_execution_failed')
            code, files = read_qemu_export(jail / 'export.bin', destination, request.limits, nonce)
            return {'exit_code': code, 'stop_reason': 'finished', 'files': files, 'checkpoint_written': True}
        finally:
            try:
                if process is not None and not stopped:
                    if pidfd is None:
                        raise _fail('work_qemu_cleanup_uncertain', uncertain=True)
                    self._stop(process, pidfd); stopped = True
                if process is not None and process.stderr is not None:
                    process.stderr.close()
                if process is not None and process.stdin is not None:
                    process.stdin.close()
                if pidfd is not None:
                    os.close(pidfd)
                if run is not None and stopped:
                    self._remove_run(run)
            finally:
                self._lock.release()
