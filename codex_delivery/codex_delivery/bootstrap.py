"""Fixed one-way privilege drop after loading private administrator settings."""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
import resource
import stat
import sys

from .store import DeliveryError

STATE_ROOT = Path('/data/state')


def prepare_resources(policy) -> None:
    """Bound the native-profile trusted parent before it creates any threads.

    Address limits are per process, NPROC counts the real UID/ucount, and CPU
    affinity limits simultaneous logical CPUs. None is a cgroup RSS/quota claim.
    Do not set cumulative RLIMIT_CPU on this long-lived service.
    """
    try:
        policy.validate()
        if policy.resource_profile == 'cgroup-v1':
            return
        if sys.platform != 'linux' or policy.resource_profile != 'native-resource-v1' or os.getuid() != policy.uid:
            raise ValueError()
        if len(list(Path('/proc/self/task').iterdir())) != 1:
            raise ValueError()
        cpus = sorted(os.sched_getaffinity(0))
        if len(cpus) < policy.native_cpu_count:
            raise ValueError()
        for kind, maximum in ((resource.RLIMIT_CORE, 0), (resource.RLIMIT_NOFILE, 128),
                (resource.RLIMIT_AS, policy.native_max_address_bytes),
                (resource.RLIMIT_NPROC, policy.native_max_threads)):
            soft, hard = resource.getrlimit(kind)
            if hard != resource.RLIM_INFINITY and maximum > hard:
                raise ValueError()
            resource.setrlimit(kind, (maximum, maximum))
            if resource.getrlimit(kind) != (maximum, maximum):
                raise ValueError()
        selected = set(cpus[:policy.native_cpu_count])
        os.sched_setaffinity(0, selected)
        if os.sched_getaffinity(0) != selected:
            raise ValueError()
    except Exception:
        raise DeliveryError('execution_resource_bootstrap_invalid') from None


def prepare_and_drop(uid: int = 65534) -> Path:
    """Prepare only the dedicated state dir; never chown existing user data.

    No shell, subprocess, environment copy, arbitrary path or restore operation
    is accepted here. The already parsed credentials stay in trusted memory.
    """
    try:
        if (sys.platform != 'linux' or type(uid) is not int or not 1 <= uid <= 2**31-1
                or os.getuid() != os.geteuid() or os.getuid() not in {0, uid}):
            raise ValueError()
        os.umask(0o077)
        if STATE_ROOT.resolve() != STATE_ROOT:
            raise ValueError()
        for parent in STATE_ROOT.parents:
            info = parent.lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0
                    or info.st_mode & 0o022 or not info.st_mode & 0o001):
                raise ValueError()
        if not STATE_ROOT.exists():
            if os.getuid() != 0:
                raise ValueError()
            STATE_ROOT.mkdir(mode=0o700)
            os.chown(STATE_ROOT, uid, uid, follow_symlinks=False)
            descriptor = os.open(STATE_ROOT.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        info = STATE_ROOT.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != uid or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError()
        libc = ctypes.CDLL(None, use_errno=True)
        def prctl(option, a=0):
            if libc.prctl(ctypes.c_int(option), ctypes.c_ulong(a), ctypes.c_ulong(0),
                          ctypes.c_ulong(0), ctypes.c_ulong(0)) != 0:
                raise ValueError()
        prctl(38, 1)  # PR_SET_NO_NEW_PRIVS, irreversible.
        if os.getuid() == 0:
            last = int(Path('/proc/sys/kernel/cap_last_cap').read_text())
            if not 0 <= last <= 63:
                raise ValueError()
            prctl(47, 4)  # PR_CAP_AMBIENT_CLEAR_ALL.
            for capability in range(last + 1):
                prctl(24, capability)  # PR_CAPBSET_DROP.
            os.setgroups([])
            os.setresgid(uid, uid, uid)
            os.setresuid(uid, uid, uid)
        prctl(4, 0)  # PR_SET_DUMPABLE: credentials in parent memory stay private.
        status = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)
        if (os.getresuid() != (uid, uid, uid) or os.getresgid() != (uid, uid, uid)
                or os.getgroups() or status['NoNewPrivs'].strip() != '1'
                or any(int(status[name].strip(), 16) for name in ('CapEff', 'CapPrm', 'CapBnd', 'CapAmb'))):
            raise ValueError()
        return STATE_ROOT
    except Exception:
        raise DeliveryError('execution_bootstrap_invalid') from None
