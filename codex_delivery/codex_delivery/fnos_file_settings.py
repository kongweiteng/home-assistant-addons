"""Independent, default-off fnOS policy settings; no credential/file/network I/O.

The trusted composition layer must select exactly one replica backend and apply
its independent live qualification. This module never falls back to a mount,
imports credentials, migrates journals, or changes the mounted replica contract.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import unicodedata

from .contracts import DeliveryError, fixed_endpoint, reference
from .fnos_file_replica import FileBinding, POLICY


@dataclass(frozen=True, repr=False)
class FnosFileSettings:
    policy: str
    endpoint: str
    directory: str
    store_ref: str
    namespace_ref: str
    qualification_ref: str
    credential_ref: str
    key_ref: str
    timeout_seconds: int = 10
    maxbytes: int = 128 * 1024 * 1024

    def __post_init__(self):
        try:
            if self.policy != POLICY:
                raise ValueError()
            fixed_endpoint(self.endpoint)
            if (type(self.directory) is not str or len(self.directory.encode('utf-8')) > 1024
                    or re.fullmatch(r'/vol[1-9][0-9]*/[^/]+(?:/[^/]+)*', self.directory) is None
                    or any(part in {'.', '..'} for part in self.directory.split('/')[1:])
                    or unicodedata.normalize('NFC', self.directory) != self.directory
                    or any(ord(c) < 32 or ord(c) == 127 or c in '\\%?#' for c in self.directory)
                    or type(self.timeout_seconds) is not int or not 1 <= self.timeout_seconds <= 30
                    or type(self.maxbytes) is not int or not 1 <= self.maxbytes <= 512*1024*1024):
                raise ValueError()
            for value in (self.store_ref, self.namespace_ref, self.qualification_ref,
                          self.credential_ref, self.key_ref):
                reference(value)
            self.binding
        except Exception:
            raise DeliveryError('fnos_file_config_invalid') from None

    @property
    def binding(self):
        return FileBinding(self.store_ref, self.namespace_ref, self.qualification_ref)

    @classmethod
    def from_dict(cls, value):
        if value is None:
            return None
        if not isinstance(value, dict) or type(value.get('enabled')) is not bool:
            raise DeliveryError('fnos_file_config_invalid')
        required = {'policy', 'endpoint', 'directory', 'store_ref', 'namespace_ref',
                    'qualification_ref', 'credential_ref', 'key_ref'}
        optional = {'timeout_seconds', 'maxbytes'}
        if set(value) - required - optional - {'enabled'}:
            raise DeliveryError('fnos_file_config_invalid')
        # Disabled minimal settings do no filesystem or provider work. Complete
        # supplied settings are still validated, so malformed policy cannot hide.
        if value['enabled'] is False and set(value) == {'enabled'}:
            return None
        if not required <= set(value):
            raise DeliveryError('fnos_file_config_invalid')
        try:
            settings = cls(**{key: item for key, item in value.items() if key != 'enabled'})
        except Exception:
            raise DeliveryError('fnos_file_config_invalid') from None
        return settings if value['enabled'] else None

    def create(self, state_root: Path, *, encryption_key, credential_provider,
               failure_domain, ssl_context=None):
        """Explicit trusted candidate construction, never live qualification."""
        from .encrypted_replica import OVERHEAD
        from .fnos_file_port import FnosHTTPFilePort
        from .fnos_file_replica import FnosFileReplica
        port = FnosHTTPFilePort(self.binding, self.endpoint, self.directory,
            credential_provider=credential_provider, maxbytes=self.maxbytes + OVERHEAD,
            ssl_context=ssl_context)
        return FnosFileReplica(port, encryption_key, binding=self.binding,
            key_ref=self.key_ref, failure_domain=failure_domain,
            journal_path=Path(state_root).absolute() / 'fnos-file-replica-journal.json',
            timeout_seconds=self.timeout_seconds, maxbytes=self.maxbytes)
