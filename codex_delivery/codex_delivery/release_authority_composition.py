"""Image-pinned release authority registration, loaded only by trusted boot."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import hmac
import os
from pathlib import Path
import re
import stat

from .release_authority import AuthorityPrincipal, CanonicalReleaseAuthority
from .store import DeliveryError
from .target_observation_client import ManagerTargetObservationClient, decode, origin_binding

REGISTRATION_PATH = Path('/etc/codex-delivery/release-authority/registration.json')
POLICY_PATH = Path('/opt/codex-delivery/release-authority-policy.json')
ROLES = frozenset({'broker', 'manager', 'backup'})
REFS = frozenset({'owner_refs', 'project_refs', 'target_refs', 'release_refs'})
SHA = re.compile(r'sha256:[a-f0-9]{64}\Z')
LIMIT = 32768


def fail():
    raise DeliveryError('release_authority_registration_invalid') from None


def _file(path, *, private=False, optional=False):
    """Root ownership/no-follow on each opened ancestor and the opened file."""
    directory = descriptor = None
    try:
        if not path.is_absolute() or any(part in {'.', '..'} for part in path.parts):
            fail()
        directory = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for part in path.parts[1:-1]:
            metadata = os.fstat(directory)
            if metadata.st_uid != 0 or metadata.st_mode & 0o022:
                fail()
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory); directory = child
        metadata = os.fstat(directory)
        if (metadata.st_uid != 0 or metadata.st_mode & 0o022
                or private and stat.S_IMODE(metadata.st_mode) != 0o700):
            fail()
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        before = os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != 0 or before.st_nlink != 1
                or before.st_mode & 0o022 or private and stat.S_IMODE(before.st_mode) != 0o600
                or not 1 <= before.st_size <= LIMIT):
            fail()
        raw = os.read(descriptor, LIMIT + 1)
        after = os.fstat(descriptor)
        if (len(raw) != before.st_size or (before.st_dev, before.st_ino, before.st_size,
                before.st_mtime_ns, before.st_ctime_ns) != (after.st_dev, after.st_ino, after.st_size,
                after.st_mtime_ns, after.st_ctime_ns)):
            fail()
        return raw
    except FileNotFoundError:
        if optional:
            return None
        fail()
    except Exception:
        fail()
    finally:
        for opened in (descriptor, directory):
            if opened is not None:
                os.close(opened)


def _closed(value, fields):
    if type(value) is not dict or set(value) != fields:
        fail()
    return value


def parse_policy(raw):
    """Closed nonsecret image policy parser; no credentials or filesystem access."""
    try:
        if type(raw) is not bytes or not 1 <= len(raw) <= LIMIT:
            fail()
        policy = _closed(decode(raw), {'version', 'manager_origin', 'manager_instance_ref',
            'roles', 'forbidden_token_sha256'})
        if type(policy['version']) is not int or policy['version'] != 1:
            fail()
        from .store import reference
        reference(policy['manager_instance_ref'])
        origin_binding(policy['manager_origin'])
        roles = _closed(policy['roles'], ROLES)
        for role in roles.values():
            for values in _closed(role, REFS).values():
                if (type(values) is not list or not 1 <= len(values) <= 128
                        or any(type(value) is not str for value in values) or len(set(values)) != len(values)):
                    fail()
                for value in values:
                    reference(value)
        prohibited = policy['forbidden_token_sha256']
        if (type(prohibited) is not list or len(prohibited) > 128
                or any(type(value) is not str or SHA.fullmatch(value) is None for value in prohibited)
                or len(set(prohibited)) != len(prohibited)):
            fail()
        return policy
    except Exception:
        fail()


@dataclass(frozen=True, repr=False)
class ReleaseAuthorityBootPlan:
    principals: tuple[AuthorityPrincipal, ...] = field(repr=False)
    manager_origin: str
    manager_instance_ref: str
    _target_token: str = field(repr=False)
    _forbidden: tuple = field(repr=False)

    @classmethod
    def load(cls, *, forbidden_credentials=()):
        try:
            try:
                REGISTRATION_PATH.lstat()
            except FileNotFoundError:
                return None
            raw = _file(REGISTRATION_PATH, private=True, optional=True)
            if raw is None:
                return None
            registration = decode(raw)
            if registration == {'version': 1, 'enabled': False} and type(registration['version']) is int:
                return None
            return cls.parse(raw, _file(POLICY_PATH), forbidden_credentials=forbidden_credentials)
        except Exception:
            fail()

    @classmethod
    def parse(cls, raw, policy_raw, *, forbidden_credentials=()):
        """Pure data validation; parse alone never establishes root provenance."""
        try:
            if (type(raw) is not bytes or type(policy_raw) is not bytes
                    or not 1 <= len(raw) <= LIMIT or not 1 <= len(policy_raw) <= LIMIT
                    or not isinstance(forbidden_credentials, (tuple, list))
                    or any(type(secret) not in {str, bytes} for secret in forbidden_credentials)):
                fail()
            registration = _closed(decode(raw), {'version', 'enabled', 'policy_sha256', 'manager_instance_ref',
                'role_tokens', 'target_observation_token'})
            policy = parse_policy(policy_raw)
            if (type(registration['version']) is not int or registration['version'] != 1
                    or registration['enabled'] is not True or registration['policy_sha256'] !=
                    'sha256:' + hashlib.sha256(policy_raw).hexdigest()
                    or registration['manager_instance_ref'] != policy['manager_instance_ref']):
                fail()
            roles = policy['roles']
            tokens = _closed(registration['role_tokens'], ROLES)
            prohibited = policy['forbidden_token_sha256']
            principals = []
            for role in sorted(ROLES):
                lists = roles[role]
                principals.append(AuthorityPrincipal(role, tokens[role], **{
                    name: frozenset(values) for name, values in lists.items()}))
            target_token = registration['target_observation_token']
            values = [principal.token for principal in principals] + [target_token]
            reserved = tuple(secret.encode() if type(secret) is str else secret for secret in forbidden_credentials)
            used = []
            for value in values:
                if (type(value) is not str or not 32 <= len(value) <= 4096
                        or any(not 33 <= ord(char) <= 126 for char in value)):
                    fail()
                encoded = value.encode('ascii')
                if (any(hmac.compare_digest(encoded, other) for other in (*reserved, *used))
                        or 'sha256:' + hashlib.sha256(encoded).hexdigest() in prohibited):
                    fail()
                used.append(encoded)
            # Canonical authority expects strings, while the plan also accepts
            # raw binary fnOS keys for its stronger pre-construction comparison.
            reserved_strings = tuple(secret for secret in forbidden_credentials if type(secret) is str)
            return cls(tuple(principals), policy['manager_origin'], policy['manager_instance_ref'],
                       target_token, reserved_strings + (target_token,))
        except Exception:
            fail()

    def create(self, store, *, release_reader=None, source_qualifier=None):
        if any(port is not None and not callable(port) for port in (release_reader, source_qualifier)):
            fail()
        targets = {principal.role: principal.target_refs for principal in self.principals}
        client = ManagerTargetObservationClient(self.manager_origin, self._target_token, target_allowlists=targets)
        return CanonicalReleaseAuthority(store, principals=self.principals, target_reader=client,
            release_reader=release_reader, source_qualifier=source_qualifier, reserved_secrets=self._forbidden)
