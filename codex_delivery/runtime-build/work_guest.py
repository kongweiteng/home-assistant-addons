"""Fixed guest root supervisor. Task code has no access to the export port."""
import ctypes
import hashlib
import io
import json
import math
import os
from pathlib import Path
import resource
import signal
import stat
import struct
import subprocess
import tarfile
import time

UID = GID = 65532
MANIFEST = '.codex-checkpoint-manifest.json'
ROOT = Path('/workspace')

def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
        allow_nan=False).encode()

def name(value):
    parts = value.split('/')
    if (not value or len(value.encode()) > 255 or len(parts) > 16 or '\\' in value
            or any(part in {'', '.', '..'} for part in parts) or value.startswith('/')
            or value == MANIFEST):
        raise RuntimeError('entry_name')
    return value

def request():
    data = Path('/bundle/request.json').read_bytes()
    if len(data) > 65536:
        raise RuntimeError('request_size')
    value = json.loads(data)
    if set(value) != {'version', 'nonce', 'limits', 'interpreter'} or type(value['version']) is not int or value['version'] != 1:
        raise RuntimeError('request')
    if (not isinstance(value['nonce'], str) or len(value['nonce']) != 32
            or any(c not in '0123456789abcdef' for c in value['nonce'])
            or value['interpreter'] not in {'python3', 'node'}):
        raise RuntimeError('request')
    limits = value['limits']
    maxima = {'timeout_seconds': 3600, 'cpu_millis': 2000, 'memory_bytes': 4096*1024*1024,
        'workspace_bytes': 4096*1024*1024, 'pids': 256, 'max_files': 256, 'tmp_bytes': 64*1024*1024}
    if set(limits) != set(maxima) or any(type(limits[k]) is not int or not 1 <= limits[k] <= maximum for k, maximum in maxima.items()):
        raise RuntimeError('limits')
    return value

def entries(limits):
    files, directories, total = [], [], 0
    for current, dirs, names in os.walk(ROOT, followlinks=False):
        # ext4's root-owned empty administrative directory is never an artifact.
        if Path(current) == ROOT and 'lost+found' in dirs:
            p = ROOT / 'lost+found'
            if not p.is_dir() or any(p.iterdir()):
                raise RuntimeError('lost_found')
            dirs.remove('lost+found')
        for entry in sorted(dirs + names):
            path = Path(current) / entry
            relative = name(path.relative_to(ROOT).as_posix())
            info = path.lstat()
            if stat.S_ISDIR(info.st_mode):
                directories.append(relative)
            elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                total += info.st_size
                if total > limits['workspace_bytes'] or len(files) >= limits['max_files']:
                    raise RuntimeError('size')
                digest = hashlib.sha256()
                with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as source:
                    for block in iter(lambda: source.read(65536), b''):
                        digest.update(block)
                files.append({'name': relative, 'size_bytes': info.st_size, 'sha256': 'sha256:' + digest.hexdigest()})
            else:
                raise RuntimeError('entry_type')
            if len(files) + len(directories) > limits['max_files'] * 4 + 16:
                raise RuntimeError('count')
    return {'files': sorted(files, key=lambda v: v['name']), 'directories': sorted(directories)}

def prepare(limits):
    tree = entries(limits)
    os.chown(ROOT, UID, GID)
    os.chmod(ROOT, 0o700)
    for directory in tree['directories']:
        path = ROOT / directory
        os.chown(path, UID, GID); os.chmod(path, 0o700)
    for item in tree['files']:
        path = ROOT / item['name']
        os.chown(path, UID, GID); os.chmod(path, 0o600)
    subprocess.run(['/bin/mount', '-t', 'tmpfs', '-o',
        'nosuid,nodev,noexec,mode=1777,size=' + str(limits['tmp_bytes']), 'tmpfs', '/tmp'], check=True)

def demote(limits):
    os.setgroups([]); os.setgid(GID); os.setuid(UID)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(38, 1, 0, 0, 0) or libc.prctl(4, 0, 0, 0, 0):
        raise RuntimeError('demote')
    status = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)
    if int(status['CapEff'].strip(), 16) or status['NoNewPrivs'].strip() != '1':
        raise RuntimeError('caps')
    for kind, bound in ((resource.RLIMIT_CORE, 0), (resource.RLIMIT_NOFILE, 64),
            (resource.RLIMIT_NPROC, limits['pids']), (resource.RLIMIT_AS, limits['memory_bytes']),
            (resource.RLIMIT_FSIZE, limits['workspace_bytes']),
            (resource.RLIMIT_CPU, math.ceil(limits['timeout_seconds'] * limits['cpu_millis'] / 1000) + 1)):
        resource.setrlimit(kind, (bound, bound))

def task_pids():
    values = []
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            info = dict(line.split(':', 1) for line in (entry / 'status').read_text().splitlines() if ':' in line)
            if int(info['Uid'].split()[0]) == UID and info['State'].strip()[0] not in 'ZX':
                values.append(int(entry.name))
        except (FileNotFoundError, ProcessLookupError):
            pass
    return values

def stop_payload():
    for attempt in range(100):
        values = task_pids()
        if not values:
            return
        for pid in values:
            try: os.kill(pid, signal.SIGKILL)
            except ProcessLookupError: pass
        time.sleep(.01)
    raise RuntimeError('payload_alive')

class Sink:
    def __init__(self, maximum, target=None):
        self.maximum, self.target, self.size = maximum, target, 0
        self.digest = hashlib.sha256()
    def write(self, value):
        self.size += len(value)
        if self.size > self.maximum:
            raise RuntimeError('archive_size')
        self.digest.update(value)
        if self.target is not None:
            view = memoryview(value)
            while view:
                count = self.target.write(view)
                if not count:
                    raise RuntimeError('archive_write')
                view = view[count:]
        return len(value)
    def flush(self): pass

def archive(tree, sink):
    with tarfile.open(fileobj=sink, mode='w|', format=tarfile.PAX_FORMAT) as target:
        for directory in tree['directories']:
            item = tarfile.TarInfo(directory); item.type = tarfile.DIRTYPE; item.mode = 0o700
            target.addfile(item)
        for item in tree['files']:
            info = tarfile.TarInfo(item['name']); info.size = item['size_bytes']; info.mode = 0o600
            with os.fdopen(os.open(ROOT / item['name'], os.O_RDONLY | os.O_NOFOLLOW), 'rb') as source:
                target.addfile(info, source)
        value = canonical(tree)
        info = tarfile.TarInfo(MANIFEST); info.size = len(value); info.mode = 0o600
        target.addfile(info, io.BytesIO(value))

def export_port():
    for entry in Path('/sys/class/virtio-ports').iterdir():
        if (entry / 'name').read_text().strip() == 'codex-work':
            path = Path('/dev') / entry.name
            info = path.stat()
            if not stat.S_ISCHR(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o077:
                raise RuntimeError('port_identity')
            return path
    raise RuntimeError('port_missing')

def main():
    value = request(); limits = value['limits']; prepare(limits)
    binary = '/usr/bin/python3' if value['interpreter'] == 'python3' else '/usr/bin/node'
    command = [binary, *(['-I'] if value['interpreter'] == 'python3' else []), '/bundle/program']
    # Port FD is never inherited by payloads. stdout is unrelated to checkpoint protocol.
    port = export_port()
    environment = {'PATH': '/usr/bin:/bin', 'HOME': '/workspace', 'TMPDIR': '/tmp',
        'LANG': 'C.UTF-8', 'PYTHONDONTWRITEBYTECODE': '1'}
    process = subprocess.Popen(command, cwd=ROOT, env=environment, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
        start_new_session=True, preexec_fn=lambda: demote(limits))
    try:
        code = process.wait(timeout=limits['timeout_seconds'])
    except subprocess.TimeoutExpired:
        code = -signal.SIGKILL
    finally:
        stop_payload()
        process.wait(timeout=2)
    tree = entries(limits)
    maximum = limits['workspace_bytes'] + (limits['max_files'] * 4 + 16) * 2048 + 32768
    first = Sink(maximum); archive(tree, first)
    if entries(limits) != tree:
        raise RuntimeError('checkpoint_changed')
    header = canonical({'version': 1, 'nonce': value['nonce'], 'exit_code': code,
        'archive_bytes': first.size, 'archive_sha256': 'sha256:' + first.digest.hexdigest()})
    if len(header) > 16384:
        raise RuntimeError('header_size')
    with os.fdopen(os.open(port, os.O_WRONLY | os.O_NOFOLLOW), 'wb', buffering=0) as output:
        framed = b'CWQ1\n' + struct.pack('!I', len(header)) + header
        position = 0
        while position < len(framed):
            count = output.write(framed[position:])
            if not count: raise RuntimeError('header_write')
            position += count
        second = Sink(maximum, output); archive(tree, second)
        if second.size != first.size or second.digest.digest() != first.digest.digest():
            raise RuntimeError('checkpoint_changed')

if __name__ == '__main__':
    main()
