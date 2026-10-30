"""Image-owned execution policy, independent of options and model requests.

The launch code, not a configuration option, chooses this module and its fixed
policy path. A policy describes data only; it cannot import code or commands.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Any

if __package__ == 'codex_delivery':  # Installed single-layer Add-on package.
    from codex_work_runtime.qemu_work import QemuHostPolicy
    from codex_work_runtime.work_execution import WorkLimits, WorkRuntime
else:  # Repository namespace used by the existing public test suite.
    from ..codex_work_runtime.qemu_work import QemuHostPolicy
    from ..codex_work_runtime.work_execution import WorkLimits, WorkRuntime

from .store import DeliveryError, canonical, reference


POLICY_PATH = Path('/opt/codex-delivery/execution-policy.json')
ASSET_ROOT = Path('/opt/codex-delivery/runtimes')
RUNTIME_ID = re.compile(r'[a-z][a-z0-9-]{0,63}\Z')
DIGEST = re.compile(r'sha256:[a-f0-9]{64}\Z')
PROFILES = frozenset({'document', 'data', 'code', 'browser', 'preview'})


def _fail():
    raise DeliveryError('execution_policy_invalid')


def digest(value: Any) -> str:
    return 'sha256:' + hashlib.sha256(canonical(value).encode()).hexdigest()


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            _fail()
        result[key] = value
    return result


def _root_file(path: Path) -> bytes:
    """No symlinks, writable ancestors or service-owned policy substitution."""
    try:
        if not path.is_absolute() or path.resolve() != path:
            _fail()
        for parent in path.parents:
            info = parent.lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0
                    or stat.S_IMODE(info.st_mode) & 0o022):
                _fail()
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as source:
            before = os.fstat(source.fileno())
            if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                    or before.st_uid != 0 or before.st_mode & 0o022
                    or not 1 <= before.st_size <= 65536):
                _fail()
            raw = source.read(65537)
            after = os.fstat(source.fileno())
        if (len(raw) != before.st_size or (before.st_ino, before.st_size,
                before.st_mtime_ns, before.st_ctime_ns) != (after.st_ino,
                after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
            _fail()
        return raw
    except (OSError, ValueError):
        _fail()


@dataclass(frozen=True)
class ExecutionPolicy:
    delivery_instance_ref: str
    extension_id: str
    host: QemuHostPolicy
    runtimes: tuple[WorkRuntime, ...]
    limits: tuple[tuple[str, WorkLimits], ...]
    runtime_pins: tuple[tuple[str, str, str], ...]

    @classmethod
    def load(cls) -> 'ExecutionPolicy':
        return cls.parse(_root_file(POLICY_PATH))

    @classmethod
    def parse(cls, raw: bytes) -> 'ExecutionPolicy':
        """Parser only. Untrusted bytes never obtain root-file provenance here."""
        try:
            if not isinstance(raw, bytes) or not 1 <= len(raw) <= 65536:
                _fail()
            value = json.loads(raw, object_pairs_hook=_pairs,
                               parse_constant=lambda _: _fail())
            fields = {'version', 'engine', 'delivery_instance_ref', 'extension_id',
                      'host_policy', 'runtimes', 'limits'}
            if (not isinstance(value, dict) or set(value) != fields
                    or type(value['version']) is not int or value['version'] != 1
                    or value['engine'] != 'qemu-work-v1'):
                _fail()
            reference(value['delivery_instance_ref']); reference(value['extension_id'])
            host = value['host_policy']
            if not isinstance(host, dict) or set(host) != set(asdict(QemuHostPolicy())):
                _fail()
            host = QemuHostPolicy(**host); host.validate()
            budgets = value['limits']
            if not isinstance(budgets, dict) or not budgets or not set(budgets) <= PROFILES:
                _fail()
            limits = {}
            for profile, item in budgets.items():
                if not isinstance(item, dict) or set(item) != set(asdict(WorkLimits())):
                    _fail()
                budget = WorkLimits(**item); budget.validate(profile)
                if budget.workspace_bytes > host.max_workspace_bytes:
                    _fail()
                limits[profile] = budget
            items = value['runtimes']
            if not isinstance(items, list) or not 1 <= len(items) <= 8:
                _fail()
            runtimes, pins, names, used = [], [], set(), set()
            for item in items:
                if (not isinstance(item, dict) or set(item) - {'runtime_id',
                        'profiles', 'interpreter', 'manifest_digest', 'launcher_digest',
                        'isolation_profile', 'kind'} or not {'runtime_id', 'profiles', 'interpreter',
                        'manifest_digest', 'launcher_digest', 'isolation_profile'} <= set(item)
                        or not isinstance(item['runtime_id'], str)
                        or not RUNTIME_ID.fullmatch(item['runtime_id'])
                        or item['runtime_id'] in names
                        or not isinstance(item['profiles'], list) or not item['profiles']
                        or any(not isinstance(p, str) or p not in limits for p in item['profiles'])
                        or len(set(item['profiles'])) != len(item['profiles'])
                        or any(not isinstance(item[k], str) or not DIGEST.fullmatch(item[k])
                               for k in ('manifest_digest', 'launcher_digest'))
                        or item['isolation_profile'] not in {'landlock-v1', 'readonly-jail-v1'}):
                    _fail()
                runtime = WorkRuntime(item['runtime_id'], item['manifest_digest'],
                    tuple(sorted(item['profiles'])), item['interpreter'],
                    python_binary='/usr/bin/python3', node_binary='/usr/bin/node',
                    kind=item.get('kind', 'work-qemu-v1'))
                runtime.validate()
                runtimes.append(runtime); names.add(runtime.runtime_ref)
                used.update(runtime.profiles)
                pins.append((runtime.runtime_ref, item['launcher_digest'], item['isolation_profile']))
            if used != set(limits):
                _fail()
            return cls(value['delivery_instance_ref'], value['extension_id'], host,
                       tuple(sorted(runtimes, key=lambda r: r.runtime_ref)),
                       tuple(sorted(limits.items())), tuple(sorted(pins)))
        except DeliveryError:
            raise DeliveryError('execution_policy_invalid') from None
        except Exception:
            raise DeliveryError('execution_policy_invalid') from None

    def registry_policy(self) -> dict[str, Any]:
        budgets = dict(self.limits)
        pins = {name: (launcher, isolation) for name, launcher, isolation in self.runtime_pins}
        runtimes = []
        for runtime in self.runtimes:
            launcher, isolation = pins[runtime.runtime_ref]
            runtimes.append({'runtime_id': runtime.runtime_ref,
                'profiles': list(runtime.profiles), 'manifest_digest': runtime.image,
                'launcher_digest': launcher, 'isolation_profile': isolation,
                'limits_digest': digest({p: asdict(budgets[p]) for p in runtime.profiles})})
        qualification = digest({'version': 1, 'engine': 'qemu-work-v1',
                                'host_policy': asdict(self.host), 'runtimes': runtimes,
                                'runtime_contracts': [{'runtime_id': runtime.runtime_ref,
                                    'kind': runtime.kind, 'interpreter': runtime.interpreter}
                                    for runtime in self.runtimes]})
        return {'delivery_instance_ref': self.delivery_instance_ref, 'extension_id': self.extension_id,
                'qualification_digest': qualification, 'runtimes': runtimes}

    def verify_backend(self, backend) -> None:
        """Verify measured fixed assets before any identity or self-check report."""
        try:
            if backend.policy != self.host or backend.runtimes != {r.runtime_ref: r for r in self.runtimes}:
                _fail()
            for name, launcher, isolation in self.runtime_pins:
                asset = backend.assets[name]
                if (asset.root != ASSET_ROOT / name or asset.manifest['isolation_profile'] != isolation
                        or asset.manifest['kind'] != backend.runtimes[name].kind
                        or asset.digest != backend.runtimes[name].image):
                    _fail()
                measured = [item for item in asset.manifest['files'] if item['name'] == 'bin/isolate']
                if len(measured) != 1 or measured[0]['sha256'] != launcher:
                    _fail()
        except DeliveryError:
            raise
        except Exception:
            _fail()
