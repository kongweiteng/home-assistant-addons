"""Fixed administrator-owned NAS boot policy; options cannot qualify storage.

This policy selects NAS-survives/primary-loss recovery explicitly. A successful
calibration GET verifies the pinned key and remote bytes now, not NAS fsync,
power-loss durability, independent key retention or exclusive NAS ACLs.
"""
from __future__ import annotations

import base64
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
import hmac
import json
from pathlib import Path
import re
import stat
import time

from .durability import ReplicaReceipt
from .encrypted_replica import EncryptedReplica
from .execution_policy import _root_file
from .fnos_file_replica import POLICY
from .fnos_file_settings import FnosFileSettings
from .fnos_oauth_provider import OAuthBinding, FnosOAuthProvider
from .fnos_oauth_runtime import FileSessionLoader, FixedCLIRefreshPort
from .store import DeliveryError, SHANGHAI, reference

POLICY_PATH = Path('/opt/codex-delivery/fnos-policy.json')
KEY_PATH = Path('/data/fnos-replica-key.json')
AUTH_ROOT = Path('/data/fnos-auth')
CLI_PATH = Path('/opt/codex-delivery/fnos/bin/trim-cli')
SERVICE_UID = 65534
SHA = re.compile(r'sha256:[a-f0-9]{64}\Z')


def _fail():
    raise DeliveryError('fnos_composition_invalid') from None


def _decode(raw):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError()
            value[key] = item
        return value
    return json.loads(raw, object_pairs_hook=pairs,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))


def _instant(value):
    if not isinstance(value, str):
        _fail()
    result = datetime.fromisoformat(value)
    if result.utcoffset() != timedelta(hours=8):
        _fail()
    return result


@dataclass(frozen=True, repr=False)
class FnosBootPlan:
    settings: FnosFileSettings
    domain: str
    oauth_binding: OAuthBinding
    cli_digest: str
    calibration: ReplicaReceipt
    qualified_at: datetime
    expires_at: datetime
    evidence_digest: str
    encryption_key: bytes = field(repr=False)
    service_uid: int = SERVICE_UID

    @classmethod
    def load(cls, settings, *, failure_domain, reserved_secrets):
        try:
            raw = _root_file(POLICY_PATH)
            # The general root-policy reader permits public policy files; the
            # independently provisioned key must additionally be owner-only.
            key_info = KEY_PATH.lstat()
            if stat.S_IMODE(key_info.st_mode) != 0o600:
                _fail()
            key_raw = _root_file(KEY_PATH)
            return cls.parse(raw, key_raw, settings=settings, failure_domain=failure_domain,
                reserved_secrets=reserved_secrets)
        except Exception:
            _fail()

    @classmethod
    def parse(cls, raw, key_raw, *, settings, failure_domain, reserved_secrets):
        """Data validation only; parse() does not confer root-file provenance."""
        try:
            if (type(settings) is not FnosFileSettings or not isinstance(raw, bytes)
                    or not isinstance(key_raw, bytes) or not 1 <= len(raw) <= 65536
                    or not 1 <= len(key_raw) <= 8192):
                _fail()
            value, secret = _decode(raw), _decode(key_raw)
            fields = {'version', 'policy', 'settings', 'service_uid', 'oauth_binding',
                'cli_digest', 'allow_file_backend', 'file_backend_authorization_ref', 'qualification'}
            if (not isinstance(value, dict) or set(value) != fields
                    or type(value['version']) is not int or value['version'] != 1
                    or value['policy'] != POLICY or json.dumps(value['settings'], sort_keys=True)
                        != json.dumps(asdict(settings), sort_keys=True)
                    or type(value['service_uid']) is not int or value['service_uid'] != SERVICE_UID
                    or value['allow_file_backend'] is not True
                    or not isinstance(value['cli_digest'], str) or not SHA.fullmatch(value['cli_digest'])):
                _fail()
            reference(value['file_backend_authorization_ref'])
            binding = value['oauth_binding']
            if not isinstance(binding, dict) or set(binding) != set(OAuthBinding.__dataclass_fields__):
                _fail()
            binding = OAuthBinding(**{**binding, 'scopes': tuple(binding['scopes'])})
            if binding.endpoint != settings.endpoint or binding.credential_ref != settings.credential_ref:
                _fail()
            reference(failure_domain)
            if (not isinstance(secret, dict) or set(secret) != {'version', 'key_ref', 'failure_domain', 'key'}
                    or type(secret['version']) is not int or secret['version'] != 1
                    or secret['key_ref'] != settings.key_ref or secret['failure_domain'] != failure_domain):
                _fail()
            key = base64.b64decode(secret['key'], validate=True)
            if len(key) != 32 or base64.b64encode(key).decode() != secret['key']:
                _fail()
            for reserved in reserved_secrets:
                if hmac.compare_digest(key, reserved.encode()) or hmac.compare_digest(secret['key'].encode(), reserved.encode()):
                    _fail()
            qualification = value['qualification']
            if (not isinstance(qualification, dict) or set(qualification) != {
                    'receipt', 'qualified_at', 'expires_at', 'recovery_evidence_digest'}
                    or not isinstance(qualification['recovery_evidence_digest'], str)
                    or not SHA.fullmatch(qualification['recovery_evidence_digest'])):
                _fail()
            receipt = ReplicaReceipt(**qualification['receipt'])
            if (not receipt.event_id.startswith('QF-') or receipt.failure_domain != failure_domain
                    or receipt.object_ref != EncryptedReplica.object_ref(receipt.event_id, receipt.sha256)):
                _fail()
            start, end = _instant(qualification['qualified_at']), _instant(qualification['expires_at'])
            now = datetime.now(SHANGHAI)
            if not start <= now < end or end-start > timedelta(days=7):
                _fail()
            return cls(settings, failure_domain, binding, value['cli_digest'], receipt,
                start, end, qualification['recovery_evidence_digest'], key)
        except Exception:
            _fail()

    def create(self, settings, state_root, *, failure_domain):
        if settings != self.settings or failure_domain != self.domain or datetime.now(SHANGHAI) >= self.expires_at:
            _fail()
        loader = FileSessionLoader(self.oauth_binding, AUTH_ROOT, allow_file_backend=True)
        refresh = FixedCLIRefreshPort(loader, CLI_PATH, self.cli_digest[7:], allow_file_backend=True)
        try:
            refresh.validate_runtime(deadline=time.monotonic()+settings.timeout_seconds)
        except Exception:
            _fail()
        provider = FnosOAuthProvider(self.oauth_binding, loader, refresh,
            journal_path=Path(state_root)/'fnos-oauth-journal.json')
        core = settings.create(state_root, encryption_key=self.encryption_key,
            credential_provider=provider, failure_domain=failure_domain)
        # Read the administrator's exact calibration object. Never create one,
        # search for "latest", reset a fused writer or upload during startup.
        try:
            core.get(self.calibration)
        except Exception:
            _fail()
        return QualifiedFileReplica(core, self)


class QualifiedFileReplica:
    def __init__(self, core, plan):
        self._core, self._plan = core, plan

    def _current(self):
        if datetime.now(SHANGHAI) >= self._plan.expires_at:
            raise DeliveryError('fnos_qualification_expired')

    def save(self, snapshot):
        self._current()
        return self._core.save(snapshot)

    def get(self, receipt):
        self._current()
        return self._core.get(receipt)

    def reconcile(self, snapshot):
        self._current()
        return self._core.reconcile(snapshot)

    def ciphertext_digest(self, receipt):
        self._current()
        return self._core.ciphertext_digest(receipt)

    def status(self):
        result = self._core.status()
        current = datetime.now(SHANGHAI) < self._plan.expires_at
        result['qualified'] = current and result['state'] in {'ready', 'read_only'}
        result['qualification_policy'] = POLICY
        if not current:
            result.update(state='unqualified', code='fnos_qualification_expired')
        return result
