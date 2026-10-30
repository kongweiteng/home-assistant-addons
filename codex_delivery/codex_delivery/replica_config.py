"""Administrator-only replica configuration; no network I/O during validation."""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .store import DeliveryError, reference


@dataclass(frozen=True, repr=False)
class ReplicaSettings:
    root: str
    fstype: str
    source: str
    mountpoint: str
    key_ref: str
    key: bytes = field(repr=False)
    timeout_seconds: int = 10

    @classmethod
    def from_dict(cls, value: Any, *, domain: str,
                  reserved_secrets: tuple[str, ...]) -> ReplicaSettings | None:
        if value is None:
            return None
        fields = {'enabled', 'root', 'fstype', 'source', 'mountpoint', 'key_ref', 'key', 'timeout_seconds'}
        if (not isinstance(value, dict) or set(value) != fields or type(value['enabled']) is not bool
                or any(not isinstance(value[key], str) for key in fields - {'enabled', 'timeout_seconds'})
                or type(value['timeout_seconds']) is not int or not 1 <= value['timeout_seconds'] <= 30):
            raise DeliveryError('replica_config_invalid')
        if value['enabled'] is False:
            return None
        try:
            from .bounded_replica import MountSpec
            if not domain or not value['key_ref'] or value['key'] in reserved_secrets:
                raise ValueError('binding')
            reference(value['key_ref'])
            raw_key = base64.b64decode(value['key'], validate=True)
            if (len(raw_key) != 32 or base64.b64encode(raw_key).decode('ascii') != value['key']
                    or raw_key in tuple(secret.encode() for secret in reserved_secrets)):
                raise ValueError('key')
            MountSpec(value['root'], value['fstype'], value['source'], value['mountpoint'])
            return cls(value['root'], value['fstype'], value['source'], value['mountpoint'],
                       value['key_ref'], raw_key, value['timeout_seconds'])
        except Exception:
            raise DeliveryError('replica_config_invalid') from None

    def create(self, state_root: Path, *, failure_domain: str):
        from .bounded_replica import DeadlineReplica, MountSpec
        # Store has checked/created this private LOCAL state directory.
        # Network storage is opened only inside the adapter's separate worker.
        return DeadlineReplica(MountSpec(self.root, self.fstype, self.source, self.mountpoint),
            self.key, key_ref=self.key_ref, failure_domain=failure_domain,
            journal_path=state_root.absolute() / 'replica-journal.json',
            timeout_seconds=self.timeout_seconds)
