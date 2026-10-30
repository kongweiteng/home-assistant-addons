"""Lease-bound work execution; this is an executor journal, never a task database.

Delivery remains the authority for authorization, task state and lease issuance.
The optional Docker backend is for a qualified external Runner (including a local
Linux VM).  It must not be used to give a HA add-on a Docker socket.  Only the
last fsynced checkpoint survives a host/container restart; an unsettled execution
requires reconciliation and is never replayed automatically.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import subprocess
import tarfile
import threading
import time
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from .errors import WorkRuntimeError


SHANGHAI = ZoneInfo("Asia/Shanghai")
REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
IMAGE_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
MIB = 1024 * 1024
MAX_PROGRAM_BYTES = 512 * 1024
CHECKPOINT_MANIFEST = ".codex-checkpoint-manifest.json"


def _fail(code: str, *, uncertain: bool = False) -> WorkRuntimeError:
    return WorkRuntimeError(code, "通用执行未通过固定策略；详情仅保留稳定错误码。", uncertain=uncertain)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _ref(value: Any) -> str:
    if not isinstance(value, str) or REF_RE.fullmatch(value) is None:
        raise _fail("work_reference_invalid")
    return value


def _name(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value.encode()) > 255 or "\\" in value or "\x00" in value:
        raise _fail("workspace_path_invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in value.split("/")) or len(path.parts) > 16:
        raise _fail("workspace_path_invalid")
    return value


def _private_dir(path: Path) -> None:
    if path.is_symlink():
        raise _fail("workspace_symlink_rejected")
    if not path.parent.exists():
        _private_dir(path.parent)
    if path.parent.is_symlink():
        raise _fail("workspace_symlink_rejected")
    path.mkdir(mode=0o700, exist_ok=True)
    if not path.is_dir() or stat.S_IMODE(path.stat().st_mode) != 0o700:
        raise _fail("workspace_permissions_invalid")


def _sync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_new(path: Path, data: bytes) -> None:
    _private_dir(path.parent)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        raise
    _sync_dir(path.parent)


@dataclass(frozen=True)
class WorkLimits:
    timeout_seconds: int = 900
    cpu_millis: int = 1000
    memory_bytes: int = 512 * MIB
    workspace_bytes: int = 128 * MIB
    pids: int = 64
    max_files: int = 128
    tmp_bytes: int = 16 * MIB

    def validate(self, profile: str) -> None:
        if profile not in {'document', 'data', 'code', 'browser', 'preview'}:
            raise _fail('work_profile_invalid')
        maxima = {
            "timeout_seconds": {'code': 3600, 'browser': 120, 'preview': 1800}.get(profile, 900),
            "cpu_millis": 2000,
            "memory_bytes": (4096 if profile == "code" else 2048) * MIB,
            "workspace_bytes": (10240 if profile == "code" else 2048) * MIB,
            "pids": 256,
            "max_files": 256,
            "tmp_bytes": 64 * MIB,
        }
        for key, maximum in maxima.items():
            value = getattr(self, key)
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
                raise _fail("work_budget_invalid")
        # tmpfs is charged to the same cgroup memory limit.  Reserve room for
        # the interpreter and metadata instead of promising impossible limits.
        if self.workspace_bytes + self.tmp_bytes + 64 * MIB > self.memory_bytes:
            raise _fail("work_memory_reservation_invalid")


@dataclass(frozen=True)
class WorkRequest:
    owner_ref: str
    task_ref: str
    workspace_ref: str
    input_revision: int
    profile: str
    runtime_ref: str
    lease_id: str
    assignment_epoch: int
    expires_at: dt.datetime
    limits: WorkLimits = WorkLimits()
    version: int = 1

    def validate(self, now: dt.datetime) -> None:
        for value in (self.owner_ref, self.task_ref, self.workspace_ref, self.runtime_ref, self.lease_id):
            _ref(value)
        if type(self.version) is not int or self.version != 1 or not isinstance(self.profile, str) or self.profile not in {"document", "data", "code", "browser", "preview"}:
            raise _fail("work_profile_invalid")
        for value in (self.input_revision, self.assignment_epoch):
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 2**53 - 1:
                raise _fail("work_revision_invalid")
        if not isinstance(self.expires_at, dt.datetime) or self.expires_at.utcoffset() is None or now.utcoffset() is None:
            raise _fail("work_lease_invalid")
        if self.expires_at <= now:
            raise _fail("work_lease_expired")
        if not isinstance(self.limits, WorkLimits):
            raise _fail("work_budget_invalid")
        self.limits.validate(self.profile)

    def document(self) -> dict[str, Any]:
        value = asdict(self)
        value["expires_at"] = self.expires_at.astimezone(SHANGHAI).isoformat()
        return value


@dataclass(frozen=True)
class WorkRuntime:
    """Administrator configuration, not a model-visible image or executable."""

    runtime_ref: str
    image: str
    profiles: tuple[str, ...] = ("document", "data", "code")
    interpreter: str = "python3"
    uid: int = 65532
    gid: int = 65532
    python_binary: str = "/usr/local/bin/python3"
    node_binary: str = "/usr/local/bin/node"
    exporter_uid: int = 65531
    kind: str = 'work-qemu-v1'

    def validate(self) -> None:
        _ref(self.runtime_ref)
        interpreters = {'work-qemu-v1': {'python3', 'node'},
                        'browser-qemu-v1': {'browser-intent-v1'},
                        'preview-qemu-v1': {'preview-app-v1'}}
        profiles = {'work-qemu-v1': {'document', 'data', 'code'},
                    'browser-qemu-v1': {'browser'}, 'preview-qemu-v1': {'preview'}}
        if (not isinstance(self.image, str) or not IMAGE_RE.fullmatch(self.image)
                or not isinstance(self.kind, str) or self.kind not in interpreters
                or self.interpreter not in interpreters[self.kind]):
            raise _fail("work_runtime_invalid")
        if not self.profiles or any(p not in profiles[self.kind] for p in self.profiles):
            raise _fail("work_runtime_invalid")
        if self.python_binary not in {"/usr/local/bin/python3", "/usr/bin/python3"} or self.node_binary not in {"/usr/local/bin/node", "/usr/bin/node"}:
            raise _fail("work_runtime_invalid")
        for value in (self.uid, self.gid, self.exporter_uid):
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 2**31 - 1:
                raise _fail("work_runtime_identity_invalid")
        if self.exporter_uid == self.uid:
            raise _fail("work_runtime_identity_invalid")


class _CheckpointTarInfo(tarfile.TarInfo):
    def _proc_member(self, archive: tarfile.TarFile):
        # tarfile otherwise allocates arbitrary PAX/long-name metadata before
        # exposing the member to our entry checks. Docker cp emits plain tar.
        if self.type in {tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.GNUTYPE_LONGNAME, tarfile.GNUTYPE_LONGLINK} and self.size > 16 * 1024:
            raise _fail("checkpoint_metadata_limit")
        if self.type == tarfile.GNUTYPE_SPARSE:
            raise _fail("checkpoint_entry_invalid")
        return super()._proc_member(archive)


class _BoundedArchive:
    def __init__(self, stream: Any, maximum: int) -> None:
        self.stream = stream
        self.remaining = maximum

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            raise _fail("checkpoint_archive_invalid")
        value = self.stream.read(min(size, self.remaining + 1))
        self.remaining -= len(value)
        if self.remaining < 0:
            raise _fail("checkpoint_archive_limit")
        return value


def _checkpoint_tree(root: Path, limits: WorkLimits) -> dict[str, Any]:
    """Rehash durable contents before reuse, including empty directories."""
    _private_dir(root)
    files: list[dict[str, Any]] = []
    directories: list[str] = []
    total = 0
    for current, dirs, names in os.walk(root, followlinks=False):
        for name in sorted(dirs + names):
            path = Path(current) / name
            relative = path.relative_to(root).as_posix()
            _name(relative)
            info = path.lstat()
            if stat.S_ISDIR(info.st_mode):
                if stat.S_IMODE(info.st_mode) != 0o700:
                    raise _fail("workspace_permissions_invalid")
                directories.append(relative)
            elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                if stat.S_IMODE(info.st_mode) != 0o600:
                    raise _fail("workspace_permissions_invalid")
                total += info.st_size
                if total > limits.workspace_bytes or len(files) >= limits.max_files:
                    raise _fail("checkpoint_size_limit")
                digest = hashlib.sha256()
                with path.open("rb") as source:
                    for block in iter(lambda: source.read(64 * 1024), b""):
                        digest.update(block)
                files.append({"name": relative, "size_bytes": info.st_size, "sha256": "sha256:" + digest.hexdigest()})
            else:
                raise _fail("checkpoint_entry_invalid")
            if len(directories) + len(files) > limits.max_files * 4 + 16:
                raise _fail("checkpoint_entry_limit")
    return {"files": sorted(files, key=lambda value: value["name"]), "directories": sorted(directories)}


def _checkpoint_ref(tree: dict[str, Any]) -> str:
    return "CP-" + hashlib.sha256(_json(tree).encode()).hexdigest()


def extract_checkpoint(stream: Any, destination: Path, limits: WorkLimits, *, require_manifest: bool = False) -> list[dict[str, Any]]:
    """Extract one bounded Docker archive without trusting tar metadata."""
    _private_dir(destination)
    if any(destination.iterdir()):
        raise _fail("checkpoint_destination_not_empty")
    seen: set[str] = set()
    files: list[dict[str, Any]] = []
    total = 0
    count = 0
    manifest = None
    try:
        bounded = _BoundedArchive(stream, limits.workspace_bytes + (limits.max_files * 4 + 16) * 2048 + 32768)
        with tarfile.open(fileobj=bounded, mode="r|", tarinfo=_CheckpointTarInfo) as archive:
            for member in archive:
                count += 1
                if count > limits.max_files * 4 + 16:
                    raise _fail("checkpoint_entry_limit")
                raw = member.name
                while raw.startswith("./"):
                    raw = raw[2:]
                if raw in {"", "."} and member.isdir():
                    continue
                name = _name(raw.rstrip("/") if member.isdir() else raw)
                if name in seen or not (member.isfile() or member.isdir()):
                    raise _fail("checkpoint_entry_invalid")
                seen.add(name)
                target = destination.joinpath(*PurePosixPath(name).parts)
                if name == CHECKPOINT_MANIFEST:
                    if not require_manifest or not member.isfile() or member.size > 256 * 1024:
                        raise _fail("checkpoint_manifest_invalid")
                    handle = archive.extractfile(member)
                    if handle is None:
                        raise _fail("checkpoint_manifest_invalid")
                    manifest = json.loads(handle.read(256 * 1024 + 1))
                    continue
                if member.isdir():
                    _private_dir(target)
                    continue
                if len(files) >= limits.max_files or member.size < 0 or total + member.size > limits.workspace_bytes:
                    raise _fail("checkpoint_size_limit")
                _private_dir(target.parent)
                handle = archive.extractfile(member)
                if handle is None:
                    raise _fail("checkpoint_entry_invalid")
                digest = hashlib.sha256()
                remaining = member.size
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
                with os.fdopen(fd, "wb") as output:
                    while remaining:
                        block = handle.read(min(64 * 1024, remaining))
                        if not block:
                            raise _fail("checkpoint_truncated")
                        output.write(block)
                        digest.update(block)
                        remaining -= len(block)
                    output.flush()
                    os.fsync(output.fileno())
                total += member.size
                files.append({"name": name, "size_bytes": member.size, "sha256": "sha256:" + digest.hexdigest()})
        for path in sorted((p for p in destination.rglob("*") if p.is_dir()), reverse=True):
            _sync_dir(path)
        _sync_dir(destination)
        if require_manifest and manifest != _checkpoint_tree(destination, limits):
            raise _fail("checkpoint_manifest_invalid")
    except (tarfile.TarError, OSError, ValueError) as exc:
        raise _fail("checkpoint_archive_invalid") from exc
    return sorted(files, key=lambda value: value["name"])


_PROBE = r'''import fcntl,json,os,pathlib,socket,struct
def text(p):return pathlib.Path(p).read_text().strip()
def isolated_network():
 with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sock:
  for _,name in socket.if_nameindex():
   flags=struct.unpack('16sH14x',fcntl.ioctl(sock.fileno(),0x8913,struct.pack('256s',name.encode()))[:32])[1]
   if name!='lo' and flags&1:return False
  try:sock.connect(('192.0.2.1',9));return False
  except OSError:return True
checks={"nonroot":os.getuid()!=0,"network":isolated_network(),"socket_absent":not pathlib.Path('/var/run/docker.sock').exists()}
for label,path in [('root_ro','/must-not-write'),('inputs_ro','/inputs/must-not-write')]:
 try:
  pathlib.Path(path).write_text('x');checks[label]=False
 except OSError:checks[label]=True
status=dict(line.split(':',1) for line in text('/proc/self/status').splitlines() if ':' in line)
checks['caps']=int(status.get('CapEff','1').strip(),16)==0
checks['no_new_privs']=status.get('NoNewPrivs','0').strip()=='1'
memory=int(text('/sys/fs/cgroup/memory.max'));pids=int(text('/sys/fs/cgroup/pids.max'))
quota,period=text('/sys/fs/cgroup/cpu.max').split()
size=os.statvfs('/workspace');available=next(int(line.split()[1])*1024 for line in text('/proc/meminfo').splitlines() if line.startswith('MemAvailable:'))
print(json.dumps({'checks':checks,'memory':memory,'pids':pids,'cpu_millis':int(quota)*1000/int(period),'workspace_bytes':size.f_blocks*size.f_frsize,'memory_available':available}))
'''


# Both fixed sources come from the Runner, never from the task/workspace. The
# freezer has the payload UID and produces no data. The exporter has a separate
# UID, so payloads cannot signal/ptrace it or open its stdout through /proc.
_FREEZE = r'''import ctypes,os
if ctypes.CDLL(None).prctl(4,0,0,0,0)!=0:raise RuntimeError('dumpable')
import pathlib,signal,stat,time
def process_states():
 result={}
 for item in pathlib.Path('/proc').iterdir():
  if not item.name.isdigit() or int(item.name) in {1,os.getpid()}:continue
  try:
   state=next(line.split()[1] for line in (item/'status').read_text().splitlines() if line.startswith('State:'))
   result[int(item.name)]=state
  except (FileNotFoundError,ProcessLookupError):pass
 return result
previous=None
for attempt in range(100):
 states=process_states()
 for pid,state in states.items():
  if state not in {'T','t','Z','X'}:
   try:os.kill(pid,signal.SIGSTOP)
   except ProcessLookupError:pass
 time.sleep(.01)
 after=process_states()
 if set(after)==previous and all(s in {'T','t','Z','X'} for s in after.values()):break
 previous=set(after)
else:raise RuntimeError('freeze_incomplete')
root=pathlib.Path('/workspace')
for current,dirs,names in os.walk(root,followlinks=False):
 for name in dirs+names:
  p=pathlib.Path(current)/name;s=p.lstat()
  if s.st_uid!=os.getuid() or not (stat.S_ISREG(s.st_mode) or stat.S_ISDIR(s.st_mode)):raise RuntimeError('entry')
  os.chmod(p,0o750 if stat.S_ISDIR(s.st_mode) else 0o640,follow_symlinks=False)
'''

_EXPORT = r'''import ctypes,os
if ctypes.CDLL(None).prctl(4,0,0,0,0)!=0:raise RuntimeError('dumpable')
import hashlib,json,pathlib,stat,sys,tarfile
limits=json.loads(sys.argv[1]);root=pathlib.Path('/workspace');payload_uid=int(sys.argv[2])
pipe=os.fstat(1)
if pipe.st_uid==payload_uid or pipe.st_mode&0o022:raise RuntimeError('unsafe_export_pipe')
def assert_stopped():
 for item in pathlib.Path('/proc').iterdir():
  if not item.name.isdigit() or int(item.name) in {1,os.getpid()}:continue
  try:state=next(line.split()[1] for line in (item/'status').read_text().splitlines() if line.startswith('State:'))
  except (FileNotFoundError,ProcessLookupError):continue
  if state not in {'T','t','Z','X'}:raise RuntimeError('payload_running')
assert_stopped()
def stable(s):return (s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns)
def tree():
 files=[];directories=[];total=0
 for current,dirs,names in os.walk(root,followlinks=False):
  for name in sorted(dirs+names):
   p=pathlib.Path(current)/name;relative=p.relative_to(root).as_posix();parts=relative.split('/')
   if relative=='.codex-checkpoint-manifest.json' or len(relative.encode())>255 or len(parts)>16 or any(v in {'','.','..'} for v in parts) or '\\' in relative:raise RuntimeError('path')
   s=p.lstat()
   if stat.S_ISDIR(s.st_mode):directories.append(relative)
   elif stat.S_ISREG(s.st_mode) and s.st_nlink==1:
    total+=s.st_size
    if total>limits['workspace_bytes'] or len(files)>=limits['max_files']:raise RuntimeError('size')
    digest=hashlib.sha256()
    with os.fdopen(os.open(p,os.O_RDONLY|os.O_NOFOLLOW),'rb') as f:
     if stable(os.fstat(f.fileno()))!=stable(s):raise RuntimeError('changed')
     for block in iter(lambda:f.read(65536),b''):digest.update(block)
     if stable(os.fstat(f.fileno()))!=stable(s):raise RuntimeError('changed')
    files.append({'name':relative,'size_bytes':s.st_size,'sha256':'sha256:'+digest.hexdigest()})
   else:raise RuntimeError('entry')
   if len(files)+len(directories)>limits['max_files']*4+16:raise RuntimeError('count')
 return {'files':sorted(files,key=lambda f:f['name']),'directories':sorted(directories)}
before=tree()
with tarfile.open(fileobj=sys.stdout.buffer,mode='w|',format=tarfile.PAX_FORMAT) as out:
 for name in before['directories']:
  info=tarfile.TarInfo(name);info.type=tarfile.DIRTYPE;info.mode=0o700;out.addfile(info)
 for item in before['files']:
  p=root/item['name'];s=p.lstat();info=tarfile.TarInfo(item['name']);info.size=item['size_bytes'];info.mode=0o600
  with os.fdopen(os.open(p,os.O_RDONLY|os.O_NOFOLLOW),'rb') as f:
   if stable(os.fstat(f.fileno()))!=stable(s):raise RuntimeError('changed')
   out.addfile(info,f)
   if stable(os.fstat(f.fileno()))!=stable(s):raise RuntimeError('changed')
 if tree()!=before:raise RuntimeError('changed')
 assert_stopped()
 raw=json.dumps(before,sort_keys=True,separators=(',',':')).encode()
 if len(raw)>262144:raise RuntimeError('manifest')
 import io
 info=tarfile.TarInfo('.codex-checkpoint-manifest.json');info.size=len(raw);info.mode=0o600
 out.addfile(info,io.BytesIO(raw))
'''


class DockerWorkBackend:
    """Qualified Linux-container executor; no host commands or sandbox fallback.

    The host CLI is trusted infrastructure. The container never receives the
    daemon socket, host environment, production mounts or deployment credentials.
    Workspace storage is a hard-limited tmpfs; durable checkpoints are owned by
    WorkExecutor, outside the container. A Docker/host restart loses unconfirmed
    working changes and must be reconciled rather than rerun.
    """

    def __init__(self, runtimes: Mapping[str, WorkRuntime], *, docker_binary: str = "docker", reserve_bytes: int = 512 * MIB) -> None:
        if not runtimes or reserve_bytes < 128 * MIB:
            raise _fail("work_backend_invalid")
        self.runtimes = dict(runtimes)
        for key, value in self.runtimes.items():
            value.validate()
            if key != value.runtime_ref or value.kind != 'work-qemu-v1':
                raise _fail("work_runtime_invalid")
        self.docker_binary = docker_binary
        self.reserve_bytes = reserve_bytes

    def _docker(self, *args: str, timeout: int = 20, capture: bool = True) -> str:
        try:
            value = subprocess.run([self.docker_binary, *args], stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE if capture else subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=timeout, check=False, text=True)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise _fail("work_sandbox_unavailable", uncertain=isinstance(exc, subprocess.TimeoutExpired)) from exc
        if value.returncode != 0:
            raise _fail("work_sandbox_rejected")
        return (value.stdout or "").strip()

    def validate_request(self, request: WorkRequest) -> None:
        runtime = self.runtimes.get(request.runtime_ref)
        if runtime is None or request.profile not in runtime.profiles:
            raise _fail("work_runtime_not_registered")

    def _verify_policy(self, container: str, request: WorkRequest, runtime: WorkRuntime) -> None:
        try:
            document = json.loads(self._docker("inspect", container))[0]
            config = document["HostConfig"]
            if (document["Image"] != runtime.image or config["Privileged"] or not config["ReadonlyRootfs"]
                or config["NetworkMode"] != "none" or config["PidMode"] or config["IpcMode"] != "none"
                or "ALL" not in config.get("CapDrop", []) or "no-new-privileges" not in config.get("SecurityOpt", [])
                or config["Memory"] != request.limits.memory_bytes or config["MemorySwap"] != request.limits.memory_bytes
                or config["PidsLimit"] != request.limits.pids or config["NanoCpus"] != request.limits.cpu_millis * 1_000_000
                or config["LogConfig"]["Type"] != "none"):
                raise _fail("work_sandbox_policy_mismatch")
            if any(m.get("RW") or m.get("Destination") not in {"/inputs", "/task/program", "/restore"} for m in document["Mounts"] if m.get("Type") == "bind"):
                raise _fail("work_sandbox_policy_mismatch")
            if any(m.get("Type") not in {"bind", "tmpfs"} for m in document["Mounts"]):
                raise _fail("work_sandbox_policy_mismatch")
            probe = json.loads(self._docker("exec", container, runtime.python_binary, "-I", "-c", _PROBE))
            if (not all(probe["checks"].values()) or probe["memory"] != request.limits.memory_bytes
                or probe["pids"] != request.limits.pids or probe["cpu_millis"] > request.limits.cpu_millis
                or probe["workspace_bytes"] > request.limits.workspace_bytes + 4096
                or probe["memory_available"] < request.limits.memory_bytes + self.reserve_bytes):
                raise _fail("work_sandbox_qualification_failed")
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            raise _fail("work_sandbox_qualification_failed") from exc

    def run(self, request: WorkRequest, *, claim_ref: str, program: Path, inputs: Path,
            restore: Path, destination: Path, created: Callable[[str], None],
            cancelled: Callable[[], bool], now: Callable[[], dt.datetime]) -> dict[str, Any]:
        self.validate_request(request)
        runtime = self.runtimes[request.runtime_ref]
        limits = request.limits
        name = "codex-work-" + hashlib.sha256(claim_ref.encode()).hexdigest()[:24]
        args = ["create", "--pull=never", "--name", name, "--label", "codex.work.execution=" + claim_ref,
            "--network", "none", "--ipc", "none", "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--user", f"{runtime.uid}:{runtime.gid}",
            "--memory", str(limits.memory_bytes), "--memory-swap", str(limits.memory_bytes),
            "--cpus", str(limits.cpu_millis / 1000), "--pids-limit", str(limits.pids),
            "--log-driver", "none", "--init", "--no-healthcheck", "--env", "TZ=Asia/Shanghai", "--env", "HOME=/tmp",
            "--env", "PYTHONPATH=", "--env", "PYTHONHOME=",
            "--tmpfs", f"/workspace:rw,nosuid,nodev,size={limits.workspace_bytes},nr_inodes={limits.max_files * 4 + 64},mode=1777",
            "--tmpfs", f"/tmp:rw,noexec,nosuid,nodev,size={limits.tmp_bytes},mode=1777"]
        for source, target in ((inputs, "/inputs"), (program, "/task/program"), (restore, "/restore")):
            if "," in str(source):
                raise _fail("workspace_path_invalid")
            args += ["--mount", f"type=bind,source={source},target={target},readonly"]
        args += ["--entrypoint", runtime.python_binary, runtime.image, "-I", "-c", "import time; time.sleep(86400)"]
        container: str | None = None
        process: subprocess.Popen[bytes] | None = None
        try:
            container = self._docker(*args)
            if not re.fullmatch(r"[a-f0-9]{64}", container):
                raise _fail("work_container_identity_invalid", uncertain=True)
            created(container)
            self._docker("start", container)
            self._verify_policy(container, request, runtime)
            self._docker("exec", container, runtime.python_binary, "-I", "-c",
                "import pathlib,shutil\nfor p in pathlib.Path('/restore').iterdir():\n"
                " (shutil.copytree if p.is_dir() else shutil.copy2)(p,pathlib.Path('/workspace')/p.name)")
            if cancelled() or request.expires_at <= now():
                reason = "cancelled" if cancelled() else "lease_expired"
                return {"exit_code": None, "stop_reason": reason, "files": [], "checkpoint_written": False}
            command = [self.docker_binary, "exec", "--workdir", "/workspace", container,
                runtime.python_binary if runtime.interpreter == "python3" else runtime.node_binary,
                *(["-I"] if runtime.interpreter == "python3" else []), "/task/program"]
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True)
            start = time.monotonic()
            reason = "finished"
            while process.poll() is None:
                if cancelled():
                    reason = "cancelled"
                    break
                if request.expires_at <= now():
                    reason = "lease_expired"
                    break
                if time.monotonic() - start >= limits.timeout_seconds:
                    reason = "timeout"
                    break
                time.sleep(0.05)
            code = process.poll()
            # Docker cp can silently ignore tmpfs mounts. Export through a
            # fixed interpreter after freezing all payload processes instead.
            self._docker("exec", container, runtime.python_binary, "-I", "-c", _FREEZE, capture=False)
            with subprocess.Popen([self.docker_binary, "exec", "--user", f"{runtime.exporter_uid}:{runtime.gid}",
                    container, runtime.python_binary, "-I", "-c", _EXPORT, _json(asdict(limits)), str(runtime.uid)],
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as copy:
                assert copy.stdout is not None
                watchdog = threading.Timer(20, copy.kill)
                watchdog.daemon = True
                watchdog.start()
                try:
                    files = extract_checkpoint(copy.stdout, destination, limits, require_manifest=True)
                    if copy.wait(timeout=20) != 0:
                        raise _fail("checkpoint_copy_failed", uncertain=True)
                except BaseException:
                    copy.kill()
                    copy.wait()
                    raise
                finally:
                    watchdog.cancel()
            return {"exit_code": code, "stop_reason": reason, "files": files, "checkpoint_written": True}
        finally:
            try:
                if container is not None:
                    # Never remove by a supplied name, wildcard or selector.
                    try:
                        self._docker("rm", "--force", container, capture=False)
                    except WorkRuntimeError as exc:
                        raise _fail("work_container_cleanup_uncertain", uncertain=True) from exc
            finally:
                if process is not None and process.poll() is None:
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)


class WorkExecutor:
    """Single-concurrency adapter for trusted Delivery-issued requests.

    Journal stages describe local execution evidence only.  No project catalog,
    queue, user authorization or canonical task state is owned by this class.
    """

    def __init__(self, state_root: str | Path, backend: DockerWorkBackend, *, now: Callable[[], dt.datetime] | None = None) -> None:
        root = Path(state_root).absolute()
        _private_dir(root)
        self.root = root.resolve()
        self.backend = backend
        self.now = now or (lambda: dt.datetime.now(SHANGHAI))
        self._thread_lock = threading.Lock()
        self.path = self.root / "work-executions.sqlite3"
        if self.path.is_symlink():
            raise _fail("workspace_symlink_rejected")
        with self._connection() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS workspaces(
                    workspace_ref TEXT PRIMARY KEY,owner_ref TEXT NOT NULL,task_ref TEXT NOT NULL,
                    highest_epoch INTEGER NOT NULL,input_revision INTEGER NOT NULL,checkpoint_ref TEXT);
                CREATE TABLE IF NOT EXISTS claims(
                    claim_ref TEXT PRIMARY KEY,workspace_ref TEXT NOT NULL,lease_id TEXT NOT NULL,
                    epoch INTEGER NOT NULL,spec_digest TEXT NOT NULL,stage TEXT NOT NULL,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,container_id TEXT,
                    receipt_json TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
                    UNIQUE(workspace_ref,epoch));
            """)
        self.path.chmod(0o600)
        _sync_dir(self.root)

    def _connection(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=5)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA synchronous=FULL")
        c.execute("PRAGMA foreign_keys=ON")
        return c

    @contextmanager
    def _dispatch_lock(self):
        if not self._thread_lock.acquire(blocking=False):
            raise _fail("work_runner_busy")
        handle = None
        try:
            fd = os.open(self.root / "work-dispatch.lock", os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
            handle = os.fdopen(fd, "r+")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise _fail("work_runner_busy") from exc
            yield
        finally:
            if handle is not None:
                handle.close()
            self._thread_lock.release()

    def _time(self) -> str:
        now = self.now()
        if now.utcoffset() is None:
            raise _fail("work_clock_invalid")
        return now.astimezone(SHANGHAI).isoformat()

    def _workspace_root(self, request: WorkRequest) -> Path:
        key = hashlib.sha256((request.owner_ref + "\n" + request.workspace_ref).encode()).hexdigest()
        path = self.root / "workspaces" / key
        _private_dir(path)
        return path

    def recover(self) -> list[dict[str, Any]]:
        """Fence unsettled executions; do not rerun, kill or adopt containers."""
        with self._dispatch_lock(), self._connection() as c:
            c.execute("BEGIN IMMEDIATE")
            c.execute("UPDATE claims SET stage='recovery_required',updated_at=? WHERE stage IN ('claimed','running')", (self._time(),))
            return [dict(r) for r in c.execute("SELECT claim_ref,workspace_ref,epoch,container_id,stage FROM claims WHERE stage='recovery_required'")]

    def cancel(self, request: WorkRequest) -> bool:
        """Persist an exact-lease request; True is acceptance, not termination."""
        with self._connection() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT c.claim_ref,c.stage FROM claims c JOIN workspaces w USING(workspace_ref) WHERE w.owner_ref=? AND w.task_ref=? AND w.workspace_ref=? AND c.epoch=? AND c.lease_id=?",
                (request.owner_ref, request.task_ref, request.workspace_ref, request.assignment_epoch, request.lease_id)).fetchone()
            if row is None:
                raise _fail("work_lease_mismatch")
            if row["stage"] == "settled":
                return False
            c.execute("UPDATE claims SET cancel_requested=1,updated_at=? WHERE claim_ref=?", (self._time(), row["claim_ref"]))
            return True

    def execute(self, request: WorkRequest, *, program: str, inputs: Mapping[str, bytes] | None = None) -> dict[str, Any]:
        request.validate(self.now())
        self.backend.validate_request(request)
        if not isinstance(program, str) or not 1 <= len(program.encode()) <= MAX_PROGRAM_BYTES:
            raise _fail("work_program_invalid")
        if inputs is not None and not isinstance(inputs, Mapping):
            raise _fail("work_input_invalid")
        input_files = dict(inputs or {})
        total = 0
        for name, content in input_files.items():
            _name(name)
            if not isinstance(content, bytes):
                raise _fail("work_input_invalid")
            if any(str(parent) in input_files for parent in PurePosixPath(name).parents if str(parent) != "."):
                raise _fail("work_input_path_conflict")
            total += len(content)
        if len(input_files) > request.limits.max_files or total > request.limits.workspace_bytes:
            raise _fail("work_input_limit")
        spec = {"request": request.document(), "program_sha256": hashlib.sha256(program.encode()).hexdigest(),
            "inputs": {k: hashlib.sha256(v).hexdigest() for k, v in sorted(input_files.items())}}
        digest = hashlib.sha256(_json(spec).encode()).hexdigest()
        claim_ref = "EX-" + hashlib.sha256((request.workspace_ref + "\n" + request.lease_id + "\n" + str(request.assignment_epoch)).encode()).hexdigest()[:40]
        with self._dispatch_lock():
            with self._connection() as c:
                c.execute("BEGIN IMMEDIATE")
                previous = c.execute("SELECT * FROM claims WHERE claim_ref=?", (claim_ref,)).fetchone()
                if previous:
                    if previous["spec_digest"] != digest:
                        raise _fail("work_request_conflict")
                    if previous["stage"] == "settled":
                        return json.loads(previous["receipt_json"])
                    raise _fail("work_recovery_required", uncertain=True)
                if c.execute("SELECT 1 FROM claims WHERE stage!='settled' LIMIT 1").fetchone():
                    raise _fail("work_recovery_required", uncertain=True)
                workspace = c.execute("SELECT * FROM workspaces WHERE workspace_ref=?", (request.workspace_ref,)).fetchone()
                if workspace:
                    if workspace["owner_ref"] != request.owner_ref or workspace["task_ref"] != request.task_ref:
                        raise _fail("work_owner_mismatch")
                    if request.assignment_epoch <= workspace["highest_epoch"] or request.input_revision < workspace["input_revision"]:
                        raise _fail("work_epoch_stale")
                checkpoint = None if workspace is None else workspace["checkpoint_ref"]
                needed = 3 * request.limits.workspace_bytes + self.backend.reserve_bytes
                if shutil.disk_usage(self.root).free < needed:
                    raise _fail("work_disk_reservation_failed")
                c.execute("INSERT INTO workspaces(workspace_ref,owner_ref,task_ref,highest_epoch,input_revision) VALUES(?,?,?,?,?) ON CONFLICT(workspace_ref) DO UPDATE SET highest_epoch=excluded.highest_epoch,input_revision=excluded.input_revision",
                    (request.workspace_ref, request.owner_ref, request.task_ref, request.assignment_epoch, request.input_revision))
                c.execute("INSERT INTO claims(claim_ref,workspace_ref,lease_id,epoch,spec_digest,stage,created_at,updated_at) VALUES(?,?,?,?,?,'claimed',?,?)",
                    (claim_ref, request.workspace_ref, request.lease_id, request.assignment_epoch, digest, self._time(), self._time()))
            def created(container: str) -> None:
                with self._connection() as c:
                    c.execute("UPDATE claims SET stage='running',container_id=?,updated_at=? WHERE claim_ref=?", (container, self._time(), claim_ref))

            def cancelled() -> bool:
                with self._connection() as c:
                    return bool(c.execute("SELECT cancel_requested FROM claims WHERE claim_ref=?", (claim_ref,)).fetchone()[0])

            try:
                root = self._workspace_root(request)
                run = root / "attempts" / claim_ref
                _private_dir(run)
                program_path = run / "program"
                _write_new(program_path, program.encode())
                inputs_path = run / "inputs"
                _private_dir(inputs_path)
                for name, content in input_files.items():
                    _write_new(inputs_path.joinpath(*PurePosixPath(name).parts), content)
                restore = root / "checkpoints" / (checkpoint or "empty")
                _private_dir(restore)
                restore_tree = _checkpoint_tree(restore, request.limits)
                if (checkpoint is not None and _checkpoint_ref(restore_tree) != checkpoint) or (
                        checkpoint is None and any(restore_tree.values())):
                    raise _fail("checkpoint_integrity_failed")
                destination = run / "checkpoint-candidate"
                prepare_restore = getattr(self.backend, 'prepare_restore', None)
                if callable(prepare_restore):
                    with self._connection() as c:
                        prior = c.execute("SELECT container_id,receipt_json FROM claims WHERE workspace_ref=? "
                            "AND stage='settled' ORDER BY epoch DESC LIMIT 1", (request.workspace_ref,)).fetchone()
                    prepared = prepare_restore(request, source=restore, destination=run/'prepared-restore',
                        prior_execution=None if prior is None else dict(prior))
                    # Preparing a guest view may never modify the durable tree.
                    if _checkpoint_tree(restore, request.limits) != restore_tree:
                        raise _fail('checkpoint_integrity_failed')
                    restore = prepared
                result = self.backend.run(request, claim_ref=claim_ref, program=program_path,
                    inputs=inputs_path, restore=restore, destination=destination, created=created,
                    cancelled=cancelled, now=self.now)
                new_checkpoint = checkpoint
                if result["checkpoint_written"]:
                    tree = _checkpoint_tree(destination, request.limits)
                    if tree["files"] != result["files"]:
                        raise _fail("checkpoint_integrity_failed")
                    new_checkpoint = _checkpoint_ref(tree)
                    target = root / "checkpoints" / new_checkpoint
                    if target.exists() or target.is_symlink():
                        if _checkpoint_ref(_checkpoint_tree(target, request.limits)) != new_checkpoint:
                            raise _fail("checkpoint_integrity_failed")
                        shutil.rmtree(destination)
                    else:
                        os.replace(destination, target)
                        _sync_dir(target.parent)
                receipt = {"version": 1, "execution_ref": claim_ref, "workspace_ref": request.workspace_ref,
                    "input_revision": request.input_revision, "assignment_epoch": request.assignment_epoch,
                    "evidence": "exited" if result["stop_reason"] == "finished" else "terminated",
                    "exit_code": result["exit_code"], "stop_reason": result["stop_reason"],
                    "checkpoint_ref": new_checkpoint, "checkpoint_confirmed": bool(result["checkpoint_written"]),
                    "files": result["files"], "recorded_at": self._time()}
                with self._connection() as c:
                    c.execute("BEGIN IMMEDIATE")
                    c.execute("UPDATE workspaces SET checkpoint_ref=? WHERE workspace_ref=?", (new_checkpoint, request.workspace_ref))
                    c.execute("UPDATE claims SET stage='settled',receipt_json=?,updated_at=? WHERE claim_ref=?", (_json(receipt), self._time(), claim_ref))
                _sync_dir(self.root)
                return receipt
            except BaseException:
                with self._connection() as c:
                    c.execute("UPDATE claims SET stage='recovery_required',updated_at=? WHERE claim_ref=?", (self._time(), claim_ref))
                raise
